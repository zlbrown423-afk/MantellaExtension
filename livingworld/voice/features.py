"""The seven Voice Lab measurements for one utterance.

1. Duration and pauses   - speech span after trimming leading/trailing silence, voiced share, pauses >= 250 ms
2. Loudness              - RMS of speech blocks in dBFS (median and 95th percentile). Relative only: the mic
                           path (Windows, Steam Link) may apply gain control, so only changes against the
                           player's own baseline mean anything.
3. Clipping              - share of samples at full scale, and the peak level
4. SNR                   - speech level minus the noise floor (from the silence step, or from the silence
                           around the utterance)
5. Pitch median and range - semitones re 100 Hz; range = 10th to 90th percentile
6. Spectral tilt         - energy above 1 kHz vs below, in dB (vocal effort; less affected by gain control)
7. Speaking rate         - syllable nuclei per second of phonation, found acoustically; plus a transcript
                           based rate when a transcript is given

Every value comes with a ``reliable`` flag and a reason when it is not. Nothing is stored and no audio leaves
the function.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from livingworld.voice import pitch as pitch_mod

VAD_BLOCK_S = 512 / 16000          # Mantella's Silero VAD block (32 ms)
VAD_THRESHOLD = 0.4                # Mantella's default Audio Threshold
MIN_PAUSE_S = 0.25
MIN_SPEECH_S = 0.5
CLIP_LEVEL = 0.999
EPS = 1e-12


@dataclass(frozen=True)
class MeasureInfo:
    key: str
    label: str
    unit: str
    group: str            # which of the seven measurements it belongs to


MEASURES: tuple[MeasureInfo, ...] = (
    MeasureInfo("duration_s", "Speech duration", "s", "duration_pauses"),
    MeasureInfo("voiced_ratio", "Voiced share", "", "duration_pauses"),
    MeasureInfo("pause_count", "Pauses", "", "duration_pauses"),
    MeasureInfo("longest_pause_s", "Longest pause", "s", "duration_pauses"),
    MeasureInfo("loudness_db", "Loudness (median)", "dBFS", "loudness"),
    MeasureInfo("loudness_p95_db", "Loudness (95th pct)", "dBFS", "loudness"),
    MeasureInfo("peak_db", "Peak", "dBFS", "clipping"),
    MeasureInfo("clipped_fraction", "Clipped samples", "", "clipping"),
    MeasureInfo("snr_db", "Signal to noise", "dB", "snr"),
    MeasureInfo("pitch_median_st", "Pitch median", "st", "pitch"),
    MeasureInfo("pitch_range_st", "Pitch range", "st", "pitch"),
    MeasureInfo("tilt_db", "Spectral tilt (effort)", "dB", "tilt"),
    MeasureInfo("rate_syl_s", "Speaking rate (acoustic)", "syl/s", "rate"),
    MeasureInfo("text_rate_syl_s", "Speaking rate (transcript)", "syl/s", "rate"),
)
MEASURE_KEYS = tuple(m.key for m in MEASURES)
MEASURE_BY_KEY = {m.key: m for m in MEASURES}


@dataclass
class Measure:
    value: float | None
    reliable: bool
    note: str = ""


@dataclass
class VocalFeatures:
    measures: dict[str, Measure] = field(default_factory=dict)
    pitch_backend: str = ""
    speech_start_s: float | None = None
    speech_end_s: float | None = None
    notes: list[str] = field(default_factory=list)

    def value(self, key: str) -> float | None:
        m = self.measures.get(key)
        return None if m is None else m.value

    def reliable_value(self, key: str) -> float | None:
        m = self.measures.get(key)
        return m.value if (m is not None and m.reliable) else None

    def to_row(self) -> dict[str, Any]:
        """Flat numbers for a CSV row: value and reliability per measurement (never audio)."""
        row: dict[str, Any] = {}
        for key in MEASURE_KEYS:
            m = self.measures.get(key, Measure(None, False, "missing"))
            row[key] = None if m.value is None else round(float(m.value), 4)
            row[key + "_ok"] = int(bool(m.reliable))
        row["pitch_backend"] = self.pitch_backend
        return row


def _db(power: np.ndarray | float) -> np.ndarray | float:
    return 10.0 * np.log10(np.asarray(power, dtype=np.float64) + EPS)


def _block_view(x: np.ndarray, n: int) -> np.ndarray:
    count = len(x) // n
    return x[: count * n].reshape(count, n)


def speech_mask(x: np.ndarray, sr: int, vad_probs: np.ndarray | None, block_s: float, threshold: float,
                noise_floor_db: float | None) -> tuple[np.ndarray, int]:
    """Speech/non-speech per block. Uses the VAD probabilities when given (aligned to the start of ``x``),
    otherwise an energy rule. Returns (mask, block length in samples)."""
    n = max(1, int(round(block_s * sr)))
    blocks = _block_view(x, n)
    if vad_probs is not None and len(vad_probs):
        probs = np.asarray(vad_probs, dtype=np.float64)[: len(blocks)]
        mask = np.zeros(len(blocks), dtype=bool)
        mask[: len(probs)] = probs > threshold
        if noise_floor_db is not None and len(blocks):
            # Silero holds its probability up for a moment after speech; a block at the noise floor is a pause
            mask &= _db(np.mean(blocks ** 2, axis=1)) > noise_floor_db + 6.0
        return mask, n
    db = _db(np.mean(blocks ** 2, axis=1)) if len(blocks) else np.zeros(0)
    if not len(db):
        return np.zeros(0, dtype=bool), n
    floor = noise_floor_db if noise_floor_db is not None else float(np.percentile(db, 10))
    cut = max(floor + 8.0, float(np.percentile(db, 95)) - 30.0)
    return db > cut, n


def _runs(mask: np.ndarray, value: bool) -> list[tuple[int, int]]:
    """(start, length) of runs where mask == value."""
    out, start = [], None
    for i, v in enumerate(mask):
        if v == value and start is None:
            start = i
        elif v != value and start is not None:
            out.append((start, i - start))
            start = None
    if start is not None:
        out.append((start, len(mask) - start))
    return out


def count_syllables(text: str) -> int:
    """Rough English syllable count (vowel groups, silent final e). Good enough for a rate."""
    total = 0
    for word in re.findall(r"[a-zA-Z']+", text.lower()):
        w = word.replace("'", "")
        groups = re.findall(r"[aeiouy]+", w)
        n = len(groups)
        if w.endswith("e") and not w.endswith(("le", "ee", "ye")) and n > 1:
            n -= 1
        total += max(1, n)
    return total


def syllable_nuclei(x: np.ndarray, sr: int, f0_times: np.ndarray | None = None, f0: np.ndarray | None = None,
                    min_dip_db: float = 2.0, min_gap_s: float = 0.08) -> int:
    """Count syllable nuclei: peaks of 300-3000 Hz band energy that are voiced and stand out from the dip
    before them (after de Jong & Wempe 2009, simplified)."""
    win, hop = int(0.025 * sr), int(0.01 * sr)
    if len(x) < win + hop:
        return 0
    idx = np.arange(win)[None, :] + hop * np.arange(1 + (len(x) - win) // hop)[:, None]
    frames = x[idx] * np.hanning(win)
    spec = np.abs(np.fft.rfft(frames, axis=1)) ** 2
    freqs = np.fft.rfftfreq(win, 1.0 / sr)
    band = (freqs >= 300) & (freqs <= min(3000, sr / 2 - 1))
    env = _db(spec[:, band].sum(axis=1))
    env = np.convolve(env, np.ones(3) / 3, mode="same")
    top = float(np.max(env))
    times = (np.arange(len(env)) * hop + win / 2) / sr
    count, last_t, low_since = 0, -1.0, env[0]
    for i in range(1, len(env) - 1):
        low_since = min(low_since, env[i])
        if not (env[i] >= env[i - 1] and env[i] > env[i + 1]):
            continue
        if env[i] < top - 25.0 or env[i] - low_since < min_dip_db or times[i] - last_t < min_gap_s:
            continue
        if f0 is not None and f0_times is not None and len(f0):
            near = np.abs(f0_times - times[i]) <= 0.03
            if not np.any(f0[near] > 0):
                continue
        count += 1
        last_t = times[i]
        low_since = env[i]
    return count


def analyze(audio: np.ndarray, sr: int, vad_probs: np.ndarray | None = None, vad_block_s: float = VAD_BLOCK_S,
            vad_threshold: float = VAD_THRESHOLD, noise_floor_db: float | None = None, transcript: str | None = None,
            prefer_praat: bool = True) -> VocalFeatures:
    """Measure one utterance. ``vad_probs`` (one per ``vad_block_s``, aligned to the start of ``audio``) are the
    same Silero probabilities Mantella uses; without them an energy rule finds the speech."""
    raw = np.asarray(audio, dtype=np.float64).ravel()
    out = VocalFeatures()
    m = out.measures
    if len(raw) == 0:
        out.notes.append("empty")
        return _fill_missing(out, "no audio")
    clipped = float(np.mean(np.abs(raw) >= CLIP_LEVEL))
    m["clipped_fraction"] = Measure(clipped, True)
    m["peak_db"] = Measure(float(20 * np.log10(np.max(np.abs(raw)) + EPS)), True)
    x = raw - np.mean(raw)

    mask, n = speech_mask(x, sr, vad_probs, vad_block_s, vad_threshold, noise_floor_db)
    if not mask.any():
        out.notes.append("no speech found")
        return _fill_missing(out, "no speech")
    first = int(np.argmax(mask))
    last = len(mask) - 1 - int(np.argmax(mask[::-1]))
    span = mask[first:last + 1]
    out.speech_start_s, out.speech_end_s = first * n / sr, (last + 1) * n / sr
    duration = len(span) * n / sr
    phonation = float(span.sum()) * n / sr
    gaps = [length * n / sr for start, length in _runs(span, False)]
    pauses = [g for g in gaps if g >= MIN_PAUSE_S]
    short = phonation < MIN_SPEECH_S
    why_short = "under 0.5 s of speech" if short else ""
    m["duration_s"] = Measure(duration, not short, why_short)
    m["voiced_ratio"] = Measure(phonation / duration if duration else None, not short, why_short)
    m["pause_count"] = Measure(float(len(pauses)), not short, why_short)
    m["longest_pause_s"] = Measure(max(pauses) if pauses else 0.0, not short, why_short)

    blocks = _block_view(x, n)
    block_db = _db(np.mean(blocks ** 2, axis=1))
    speech_db = block_db[first:last + 1][span]
    loud, loud95 = float(np.median(speech_db)), float(np.percentile(speech_db, 95))
    loud_ok = not short and clipped <= 0.001
    loud_note = why_short or ("clipped" if clipped > 0.001 else "relative to baseline only")
    m["loudness_db"] = Measure(loud, loud_ok, loud_note)
    m["loudness_p95_db"] = Measure(loud95, loud_ok, loud_note)

    noise = noise_floor_db
    if noise is None:
        quiet = block_db[: len(mask)][~mask]
        noise = float(np.median(quiet)) if len(quiet) >= 5 else None
    if noise is None:
        m["snr_db"] = Measure(None, False, "no silence to measure noise")
        snr = None
    else:
        snr = loud - noise
        m["snr_db"] = Measure(snr, True)
    noisy = snr is not None and snr < 10.0

    seg = x[first * n:(last + 1) * n]
    times, f0, backend = pitch_mod.track(seg, sr, prefer_praat)
    out.pitch_backend = backend
    voiced = pitch_mod.clean_track(f0)
    if len(voiced) >= 5:
        st = pitch_mod.semitones(voiced)
        med_ok = len(voiced) >= 50 and not noisy
        rng_ok = len(voiced) >= 100 and not noisy
        note = "noisy" if noisy else ("too little voiced speech" if not rng_ok else "")
        m["pitch_median_st"] = Measure(float(np.median(st)), med_ok, note)
        m["pitch_range_st"] = Measure(float(np.percentile(st, 90) - np.percentile(st, 10)), rng_ok, note)
    else:
        m["pitch_median_st"] = Measure(None, False, "no pitch found")
        m["pitch_range_st"] = Measure(None, False, "no pitch found")

    sb = blocks[first:last + 1][span] * np.hanning(n)
    spec = np.abs(np.fft.rfft(sb, axis=1)) ** 2
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    low = spec[:, (freqs >= 50) & (freqs < 1000)].sum(axis=1)
    high = spec[:, (freqs >= 1000) & (freqs < min(5000, sr / 2))].sum(axis=1)
    tilt = float(np.median(_db(high) - _db(low)))
    m["tilt_db"] = Measure(tilt, not short and not noisy, why_short or ("noisy" if noisy else ""))

    nuclei = syllable_nuclei(seg, sr, times, f0)
    rate_ok = phonation >= 1.5
    m["rate_syl_s"] = Measure(nuclei / phonation if phonation else None, rate_ok,
                              "" if rate_ok else "under 1.5 s of speech")
    if transcript and transcript.strip():
        syl = count_syllables(transcript)
        m["text_rate_syl_s"] = Measure(syl / phonation if phonation else None, rate_ok,
                                       "" if rate_ok else "under 1.5 s of speech")
    else:
        m["text_rate_syl_s"] = Measure(None, False, "no transcript")
    return out


def _fill_missing(out: VocalFeatures, note: str) -> VocalFeatures:
    for key in MEASURE_KEYS:
        out.measures.setdefault(key, Measure(None, False, note))
    return out
