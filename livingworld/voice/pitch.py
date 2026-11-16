"""Fundamental frequency (F0) tracking for one utterance.

Uses Praat (through the ``praat-parselmouth`` package, GPLv3+) when it is installed, because its pitch
tracker is the research standard. Without it, a numpy YIN tracker is used: close on clean speech, a little
more prone to octave slips. Both return one value per 10 ms frame, 0.0 where the frame is unvoiced.
"""
from __future__ import annotations

import numpy as np

HOP_S = 0.01
FLOOR_HZ = 65.0
CEILING_HZ = 500.0

try:  # optional: GPLv3+, see livingworld/voice/README.md
    import parselmouth  # type: ignore
    HAS_PARSELMOUTH = True
except Exception:  # pragma: no cover - depends on the environment
    parselmouth = None
    HAS_PARSELMOUTH = False


def backend_name(prefer_praat: bool = True) -> str:
    return "praat" if (prefer_praat and HAS_PARSELMOUTH) else "yin"


def track(audio: np.ndarray, sr: int, prefer_praat: bool = True) -> tuple[np.ndarray, np.ndarray, str]:
    """F0 track. Returns (times in seconds, f0 in Hz with 0.0 = unvoiced, backend name)."""
    audio = np.asarray(audio, dtype=np.float64).ravel()
    if len(audio) < int(0.05 * sr):
        return np.zeros(0), np.zeros(0), backend_name(prefer_praat)
    if prefer_praat and HAS_PARSELMOUTH:
        snd = parselmouth.Sound(audio, sampling_frequency=float(sr))
        p = snd.to_pitch(time_step=HOP_S, pitch_floor=FLOOR_HZ, pitch_ceiling=CEILING_HZ)
        f0 = np.nan_to_num(np.asarray(p.selected_array["frequency"], dtype=np.float64))
        return np.asarray(p.xs(), dtype=np.float64), f0, "praat"
    times, f0 = yin(audio, sr)
    return times, f0, "yin"


def yin(audio: np.ndarray, sr: int, threshold: float = 0.15) -> tuple[np.ndarray, np.ndarray]:
    """YIN (de Cheveigne & Kawahara 2002), vectorised over frames. 40 ms windows, 10 ms hop."""
    win = int(round(0.04 * sr))
    hop = int(round(HOP_S * sr))
    min_lag = max(2, int(sr / CEILING_HZ))
    max_lag = int(sr / FLOOR_HZ) + 1
    span = win + max_lag
    if len(audio) < span:
        audio = np.pad(audio, (0, span - len(audio)))
    n_frames = 1 + (len(audio) - span) // hop
    idx = np.arange(span)[None, :] + hop * np.arange(n_frames)[:, None]
    frames = audio[idx]                                   # (n, win + max_lag)
    head = frames[:, :win]
    nfft = 1 << int(np.ceil(np.log2(span + win)))
    r = np.fft.irfft(np.conj(np.fft.rfft(head, nfft)) * np.fft.rfft(frames, nfft), nfft)[:, :max_lag]
    sq = np.concatenate([np.zeros((n_frames, 1)), np.cumsum(frames ** 2, axis=1)], axis=1)
    lags = np.arange(max_lag)
    e0 = sq[:, win][:, None]
    e_tau = sq[:, lags + win] - sq[:, lags]
    d = np.maximum(e0 + e_tau - 2.0 * r, 0.0)
    d[:, 0] = 0.0
    cum = np.cumsum(d[:, 1:], axis=1)
    cmnd = np.ones_like(d)
    with np.errstate(divide="ignore", invalid="ignore"):
        cmnd[:, 1:] = np.where(cum > 0, d[:, 1:] * lags[1:] / cum, 1.0)
    f0 = np.zeros(n_frames)
    energy = e0.ravel() / win
    quiet = energy < 1e-8
    for i in range(n_frames):
        if quiet[i]:
            continue
        c = cmnd[i]
        below = np.nonzero(c[min_lag:] < threshold)[0]
        if len(below) == 0:
            continue
        tau = below[0] + min_lag
        while tau + 1 < max_lag and c[tau + 1] < c[tau]:   # walk down to the local minimum
            tau += 1
        if 1 <= tau < max_lag - 1:                         # parabolic interpolation
            a, b, cc = c[tau - 1], c[tau], c[tau + 1]
            den = a - 2 * b + cc
            shift = 0.5 * (a - cc) / den if den != 0 else 0.0
        else:
            shift = 0.0
        f0[i] = sr / (tau + shift)
    times = (np.arange(n_frames) * hop + win / 2) / sr
    return times, f0


def clean_track(f0: np.ndarray) -> np.ndarray:
    """Drop likely octave slips: voiced values more than ~an octave from the utterance median."""
    voiced = f0[f0 > 0]
    if len(voiced) < 3:
        return voiced
    med = np.median(voiced)
    return voiced[(voiced > med * 0.55) & (voiced < med * 1.8)]


def semitones(hz: np.ndarray | float, ref_hz: float = 100.0) -> np.ndarray | float:
    """Semitones relative to 100 Hz, so a baseline compares on a musical (log) scale."""
    return 12.0 * np.log2(np.asarray(hz, dtype=np.float64) / ref_hz)
