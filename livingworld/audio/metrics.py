"""Microphone measurements for calibration: levels, clipping, noise floor, SNR, spectra, mains hum and signs of
processing upstream of Windows (a headset noise gate or automatic gain). Numpy only, no audio device needed.

Levels are dBFS: 0 dBFS is a full-scale sample, so a full-scale sine reads -3 dBFS RMS."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

FLOOR_DB = -120.0
CLIP_LEVEL = 0.999          # a sample this close to full scale counts as clipped
FLAT_TOP_LEVEL = 0.9        # flat tops: runs of equal samples this loud are clipping inside the mic or its converter
FLAT_TOP_RUN = 3


def db(power: float) -> float:
    """Power (mean square) to dB, floored so silence never gives -inf."""
    return float(max(FLOOR_DB, 10.0 * np.log10(max(float(power), 1e-12))))


def rms_db(x: np.ndarray) -> float:
    x = np.asarray(x, dtype=np.float64)
    return db(float(np.mean(x * x))) if len(x) else FLOOR_DB


def peak_db(x: np.ndarray) -> float:
    x = np.asarray(x, dtype=np.float64)
    return float(max(FLOOR_DB, 20.0 * np.log10(max(float(np.max(np.abs(x))) if len(x) else 0.0, 1e-6))))


def window_db(x: np.ndarray, sr: int, win_s: float = 0.1) -> np.ndarray:
    """RMS level of consecutive windows (default 100 ms), in dBFS."""
    n = max(1, int(round(win_s * sr)))
    x = np.asarray(x, dtype=np.float64)
    usable = (len(x) // n) * n
    if usable == 0:
        return np.array([rms_db(x)])
    p = np.mean(x[:usable].reshape(-1, n) ** 2, axis=1)
    return np.maximum(FLOOR_DB, 10.0 * np.log10(np.maximum(p, 1e-12)))


@dataclass
class Clipping:
    full_scale: int            # samples at or above CLIP_LEVEL
    flat_top_runs: int         # runs of FLAT_TOP_RUN+ identical loud samples
    share: float               # clipped samples / all samples

    @property
    def any(self) -> bool:
        return self.full_scale > 0 or self.flat_top_runs > 0


def clipping(x: np.ndarray) -> Clipping:
    x = np.asarray(x, dtype=np.float32)
    a = np.abs(x)
    full = int(np.sum(a >= CLIP_LEVEL))
    runs = 0
    loud = a >= FLAT_TOP_LEVEL
    if np.any(loud):
        same = np.concatenate(([False], (x[1:] == x[:-1]) & loud[1:] & loud[:-1]))
        # count runs of FLAT_TOP_RUN or more equal samples (FLAT_TOP_RUN - 1 consecutive "same" flags)
        edges = np.diff(np.concatenate(([0], same.astype(np.int8), [0])))
        starts, ends = np.where(edges == 1)[0], np.where(edges == -1)[0]
        runs = int(np.sum((ends - starts) >= FLAT_TOP_RUN - 1))
    return Clipping(full, runs, full / len(x) if len(x) else 0.0)


def speech_mask(vad_probs: Sequence[float], block_samples: int, n_samples: int, threshold: float = 0.5) -> np.ndarray:
    """Per-sample speech mask from per-block VAD probabilities."""
    probs = np.asarray(vad_probs, dtype=np.float32)
    mask = np.repeat(probs > threshold, block_samples)[:n_samples]
    if len(mask) < n_samples:
        mask = np.pad(mask, (0, n_samples - len(mask)))
    return mask


def noise_floor_db(x: np.ndarray, sr: int) -> float:
    """Median 100 ms level of a silent recording: robust to a cough or a click."""
    return float(np.median(window_db(x, sr)))


def speech_level_db(x: np.ndarray, mask: np.ndarray) -> float | None:
    """Mean power over the samples the VAD calls speech; None when there is too little speech (< 0.5 s at 16 kHz)."""
    x = np.asarray(x, dtype=np.float64)
    sel = x[np.asarray(mask, dtype=bool)[:len(x)]]
    if len(sel) < 8000:
        return None
    return db(float(np.mean(sel * sel)))


def snr_rating(snr: float | None) -> str:
    if snr is None:
        return "unknown"
    return "good" if snr >= 25 else ("usable" if snr >= 15 else "poor")


# spectra ---------------------------------------------------------------------------------------------------
def welch(x: np.ndarray, sr: int, nperseg: int = 4096, mask: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Average power spectrum (Hann window, 50% overlap). With ``mask``, only segments that are mostly speech count."""
    x = np.asarray(x, dtype=np.float64)
    nperseg = int(min(nperseg, len(x))) if len(x) else 1
    hop = max(1, nperseg // 2)
    win = np.hanning(nperseg)
    scale = 1.0 / (sr * np.sum(win ** 2))
    acc, count = np.zeros(nperseg // 2 + 1), 0
    for start in range(0, max(1, len(x) - nperseg + 1), hop):
        seg = x[start:start + nperseg]
        if len(seg) < nperseg:
            break
        if mask is not None and np.mean(mask[start:start + nperseg]) < 0.8:
            continue
        spec = np.abs(np.fft.rfft((seg - seg.mean()) * win)) ** 2 * scale
        acc += spec
        count += 1
    freqs = np.fft.rfftfreq(nperseg, 1.0 / sr)
    if count:
        acc /= count
        acc[1:-1] *= 2.0
    return freqs, acc


THIRD_OCTAVE_CENTRES = [round(1000 * 2 ** (k / 3), 1) for k in range(-12, 10)]   # 63 Hz to 8 kHz


def third_octave_db(x: np.ndarray, sr: int, mask: np.ndarray | None = None) -> dict[str, float]:
    """Level per 1/3-octave band (63 Hz to 8 kHz, bands above Nyquist left out), in dB re full scale."""
    freqs, psd = welch(x, sr, mask=mask)
    df = freqs[1] - freqs[0] if len(freqs) > 1 else 1.0
    out = {}
    for fc in THIRD_OCTAVE_CENTRES:
        lo, hi = fc / 2 ** (1 / 6), fc * 2 ** (1 / 6)
        if hi > sr / 2:
            break
        sel = (freqs >= lo) & (freqs < hi)
        out[f"{fc:g}"] = round(db(float(np.sum(psd[sel]) * df)), 1) if np.any(sel) else FLOOR_DB
    return out


@dataclass
class Hum:
    base_hz: int | None                         # 50 or 60 when a harmonic series stands out
    excess_db: dict[int, float] = field(default_factory=dict)   # harmonic number -> dB above its neighbours


def detect_hum(x: np.ndarray, sr: int, min_excess_db: float = 10.0) -> Hum:
    """Mains hum: at least two of the first five harmonics of 50 or 60 Hz stand 10 dB above the nearby spectrum."""
    freqs, psd = welch(x, sr, nperseg=int(sr))          # 1 Hz resolution
    best = Hum(None)
    best_count = 0
    for base in (50, 60):
        excess = {}
        for h in range(1, 6):
            f = base * h
            if f + 20 > sr / 2:
                break
            peak = np.max(psd[(freqs >= f - 1.5) & (freqs <= f + 1.5)])
            around = psd[((freqs >= f - 15) & (freqs <= f - 4)) | ((freqs >= f + 4) & (freqs <= f + 15))]
            excess[h] = round(db(peak) - db(float(np.median(around))), 1)
        count = sum(1 for v in excess.values() if v >= min_excess_db)
        if count >= 2 and count > best_count:
            best, best_count = Hum(base, excess), count
    return best


# upstream processing ---------------------------------------------------------------------------------------
@dataclass
class UpstreamCheck:
    clean: bool                  # True only when there were enough pauses to judge and nothing suspicious showed
    judged: bool                 # False when the take had too few pauses to tell
    reasons: list[str] = field(default_factory=list)
    gap_floor_db: float | None = None
    zero_share: float = 0.0


def upstream_processing(x: np.ndarray, vad_probs: Sequence[float], block_samples: int,
                        room_floor_db: float | None, block_s: float = 0.032) -> UpstreamCheck:
    """Look for a noise gate / suppressor or automatic gain before the audio reaches Windows.

    Signs used (heuristics, see the design doc): the pauses between words are much quieter than the room
    (a gate or suppressor), stretches of exact digital zero (a gate), and noise that climbs back up during a pause
    (automatic gain recovering after speech)."""
    x = np.asarray(x, dtype=np.float64)
    probs = np.asarray(vad_probs, dtype=np.float32)
    n_blocks = min(len(probs), len(x) // block_samples)
    if n_blocks == 0:
        return UpstreamCheck(False, False, ["no audio"])
    blocks = x[:n_blocks * block_samples].reshape(n_blocks, block_samples)
    levels = np.maximum(FLOOR_DB, 10 * np.log10(np.maximum(np.mean(blocks ** 2, axis=1), 1e-12)))
    speech = probs[:n_blocks] > 0.5
    quiet = probs[:n_blocks] < 0.2
    reasons: list[str] = []
    zero_share = float(np.mean(x == 0.0))
    if zero_share > 0.01:
        reasons.append(f"{zero_share:.0%} of samples are exact digital zero: a noise gate is probably active")

    # pauses inside the speech: quiet blocks with speech both before and after
    idx = np.where(speech)[0]
    if len(idx) < 2:
        return UpstreamCheck(False, False, reasons + ["too little speech to judge"], None, zero_share)
    inside = np.zeros(n_blocks, dtype=bool)
    inside[idx[0]:idx[-1] + 1] = True
    gaps = quiet & inside
    if np.sum(gaps) * block_s < 0.25:
        return UpstreamCheck(False, False, reasons + ["too few pauses between words to judge"], None, zero_share)
    gap_floor = float(np.median(levels[gaps]))
    if room_floor_db is not None and gap_floor < room_floor_db - 10:
        reasons.append(f"pauses between words ({gap_floor:.0f} dBFS) are {room_floor_db - gap_floor:.0f} dB quieter "
                       "than the room: a noise gate or suppressor is probably active")

    # automatic gain: within pauses of 0.6 s or more, the later part is clearly louder than the start
    rises = []
    run: list[int] = []
    for i in range(n_blocks):
        if gaps[i]:
            run.append(i)
            continue
        if len(run) * block_s >= 0.6:
            k = max(2, len(run) // 4)
            rises.append(float(np.median(levels[run[-k:]]) - np.median(levels[run[:k]])))
        run = []
    if len(rises) >= 2 and np.median(rises) > 6:
        reasons.append(f"background noise rises {np.median(rises):.0f} dB during pauses: automatic gain is probably active")
    return UpstreamCheck(not reasons, True, reasons, round(gap_floor, 1), zero_share)


def recommended_gain_db(speech_db: float | None, target_db: float = -23.0) -> float | None:
    """Fixed digital gain that brings normal speech to ``target_db``, limited to -12..+18 dB."""
    if speech_db is None:
        return None
    return round(float(np.clip(target_db - speech_db, -12.0, 18.0)), 1)
