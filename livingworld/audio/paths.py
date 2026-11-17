"""The two signal paths of the capture layer, as small stateful processors that work block by block.

    raw mic (native rate)
      -> DCBlocker -> FixedGain                      = the analysis signal (kept as is, nothing else touches it)
      -> StreamResampler to 16 kHz -> StreamFilters -> soft_limit   = the speech-to-text signal

Nothing here adapts to the signal: the gain is one constant from the mic profile, the filters are exactly the ones
the profile lists (none by default), and the limiter only acts above 0.9 of full scale."""
from __future__ import annotations

from math import ceil, gcd
from typing import Any, Sequence

import numpy as np

STT_RATE = 16000
LIMIT_KNEE = 0.9


class DCBlocker:
    """First-order high-pass near 10 Hz: removes DC offset without touching anything a voice does."""

    def __init__(self, sample_rate: int, cutoff_hz: float = 10.0) -> None:
        self.r = float(np.exp(-2.0 * np.pi * cutoff_hz / sample_rate))
        self._zi = np.zeros(1)
        self._first = True

    def process(self, x: np.ndarray) -> np.ndarray:
        from scipy.signal import lfilter
        x = np.asarray(x, dtype=np.float64)
        if self._first and len(x):
            self._zi = np.array([-x[0]])          # start settled, so the first block has no DC step
            self._first = False
        y, self._zi = lfilter([1.0, -1.0], [1.0, -self.r], x, zi=self._zi)
        return y.astype(np.float32)


def db_to_lin(db: float) -> float:
    return float(10.0 ** (db / 20.0))


class StreamResampler:
    """Polyphase resampler for a stream of blocks. The output equals one resample_poly over the whole stream
    (up to rounding): each call keeps a guard of input samples before and holds back a guard after, so block
    edges are never filtered against padding."""

    def __init__(self, in_rate: int, out_rate: int = STT_RATE) -> None:
        g = gcd(in_rate, out_rate)
        self.up, self.down = out_rate // g, in_rate // g
        self.in_rate, self.out_rate = in_rate, out_rate
        self.passthrough = self.up == self.down
        half_len = 10 * max(self.up, self.down)                # scipy's default filter half-length, upsampled rate
        guard = int(ceil(half_len / self.up)) + self.down
        self.guard = int(ceil(guard / self.down)) * self.down  # in input samples, a multiple of `down`
        self._buf = np.zeros(0, dtype=np.float64)
        self._base = 0          # absolute input index of _buf[0]; always a multiple of `down`
        self._done = 0          # absolute input index up to which output has been emitted (multiple of `down`)

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32)
        if self.passthrough:
            return x
        from scipy.signal import resample_poly
        self._buf = np.concatenate([self._buf, x.astype(np.float64)])
        total = self._base + len(self._buf)
        target = ((total - self.guard) // self.down) * self.down
        if target <= self._done:
            return np.zeros(0, dtype=np.float32)
        y = resample_poly(self._buf, self.up, self.down)
        j0 = (self._done - self._base) * self.up // self.down
        j1 = (target - self._base) * self.up // self.down
        out = y[j0:j1].astype(np.float32)
        self._done = target
        new_base = max(0, ((self._done - self.guard) // self.down) * self.down)
        if new_base > self._base:
            self._buf = self._buf[new_base - self._base:]
            self._base = new_base
        return out


def _peaking_sos(fs: int, f0: float, gain_db: float, q: float) -> np.ndarray:
    """RBJ cookbook peaking EQ as one second-order section."""
    a = 10 ** (gain_db / 40)
    w0 = 2 * np.pi * f0 / fs
    alpha = np.sin(w0) / (2 * q)
    b0, b1, b2 = 1 + alpha * a, -2 * np.cos(w0), 1 - alpha * a
    a0, a1, a2 = 1 + alpha / a, -2 * np.cos(w0), 1 - alpha / a
    return np.array([[b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]])


MAX_BAND_GAIN_DB = 6.0


class StreamFilters:
    """The speech-to-text filters a mic profile asks for. ``cfg`` keys (all optional):

        highpass_hz: float                 2nd-order Butterworth high-pass
        notch_hz: list[float]              narrow notches (mains hum and its harmonics), Q = 30
        bands: list[{f, gain_db, q}]       peaking bands, each limited to +-6 dB

    An empty or missing config leaves the signal untouched."""

    def __init__(self, fs: int, cfg: dict[str, Any] | None = None) -> None:
        from scipy.signal import butter, iirnotch, tf2sos
        cfg = cfg or {}
        sections: list[np.ndarray] = []
        hp = cfg.get("highpass_hz")
        if hp:
            sections.append(butter(2, float(hp), "highpass", fs=fs, output="sos"))
        for f in cfg.get("notch_hz") or []:
            if 0 < float(f) < fs / 2:
                b, a = iirnotch(float(f), 30.0, fs=fs)
                sections.append(tf2sos(b, a))
        for band in cfg.get("bands") or []:
            f, g, q = float(band["f"]), float(band["gain_db"]), float(band.get("q", 1.0))
            g = float(np.clip(g, -MAX_BAND_GAIN_DB, MAX_BAND_GAIN_DB))
            if 0 < f < fs / 2 and g != 0.0:
                sections.append(_peaking_sos(fs, f, g, q))
        self.sos = np.vstack(sections) if sections else None
        self._zi = None if self.sos is None else np.zeros((self.sos.shape[0], 2))

    @property
    def active(self) -> bool:
        return self.sos is not None

    def process(self, x: np.ndarray) -> np.ndarray:
        if self.sos is None:
            return np.asarray(x, dtype=np.float32)
        from scipy.signal import sosfilt
        y, self._zi = sosfilt(self.sos, np.asarray(x, dtype=np.float64), zi=self._zi)
        return y.astype(np.float32)


def soft_limit(x: np.ndarray, knee: float = LIMIT_KNEE) -> np.ndarray:
    """Leaves everything below ``knee`` exactly as it is and rolls larger peaks off smoothly toward 1.0, so a
    later int16 conversion can never wrap around."""
    x = np.asarray(x, dtype=np.float32)
    mag = np.abs(x)
    over = mag > knee
    if not np.any(over):
        return x
    out = x.copy()
    room = 1.0 - knee
    out[over] = np.sign(x[over]) * (knee + room * np.tanh((mag[over] - knee) / room))
    return out


class Rechunker:
    """Collects a stream of arbitrary-length pieces and hands back fixed-size blocks."""

    def __init__(self, size: int) -> None:
        self.size = size
        self._buf = np.zeros(0, dtype=np.float32)

    def push(self, x: np.ndarray) -> list[np.ndarray]:
        self._buf = np.concatenate([self._buf, np.asarray(x, dtype=np.float32)])
        n = len(self._buf) // self.size
        out = [self._buf[i * self.size:(i + 1) * self.size].copy() for i in range(n)]
        self._buf = self._buf[n * self.size:]
        return out


def native_block_samples(sample_rate: int, block_s: float = 0.032) -> int:
    """Capture block length: about 32 ms and a whole multiple of the resampler's decimation, so blocks line up."""
    down = sample_rate // gcd(sample_rate, STT_RATE)
    return down * max(1, round(block_s * sample_rate / down))
