"""Which measurements are real on this mic (Voice Lab acceptance tests V3, V4 and V6).

- Natural spread (V3): the standard deviation of each measurement over the same neutral sentence said
  several times.
- Separation (V4): for each performed delivery (loud, quiet, fast, slow, hesitant, high, low), how far the
  measurement moved from neutral, in units of natural spread. A measurement is kept only if a delivery that
  should move it moves it the right way by at least ``KEEP_FACTOR`` spreads.
- Day to day (V6): a second day's baseline medians should move less than the first day's spread.

Only measurements kept here may be used in game (Stage 2).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import numpy as np

from livingworld.voice.baseline import SPREAD_FLOOR, Baseline
from livingworld.voice.features import MEASURE_KEYS, VocalFeatures

KEEP_FACTOR = 3.0   # starting threshold from the plan; tune from real data

PERFORMED = ("loud", "quiet", "fast", "slow", "hesitant", "high", "low")

# delivery -> {measurement: expected direction (+1 up, -1 down)}
EXPECTED: dict[str, dict[str, int]] = {
    "loud": {"loudness_db": 1, "loudness_p95_db": 1, "tilt_db": 1},
    "quiet": {"loudness_db": -1, "loudness_p95_db": -1, "tilt_db": -1},
    "fast": {"rate_syl_s": 1, "text_rate_syl_s": 1, "duration_s": -1},
    "slow": {"rate_syl_s": -1, "text_rate_syl_s": -1, "duration_s": 1},
    "hesitant": {"pause_count": 1, "longest_pause_s": 1, "voiced_ratio": -1, "duration_s": 1},
    "high": {"pitch_median_st": 1},
    "low": {"pitch_median_st": -1},
}
# Recording-quality numbers: reported, never kept or dropped.
QUALITY_KEYS = ("peak_db", "clipped_fraction", "snr_db")


def _values(takes: Iterable[VocalFeatures], key: str) -> np.ndarray:
    vals = [t.reliable_value(key) for t in takes]
    return np.array([v for v in vals if v is not None], dtype=np.float64)


def natural_spread(neutral: list[VocalFeatures]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for key in MEASURE_KEYS:
        v = _values(neutral, key)
        out[key] = max(float(np.std(v, ddof=1)), SPREAD_FLOOR.get(key, 0.0)) if len(v) >= 3 else None
    return out


@dataclass
class Row:
    key: str
    neutral_median: float | None
    spread: float | None
    separation: dict[str, float | None] = field(default_factory=dict)   # delivery -> signed shift / spread
    verdict: str = ""        # kept | dropped | not tested | quality | not enough data
    because: str = ""


def separation_table(neutral: list[VocalFeatures], performed: dict[str, list[VocalFeatures]],
                     keep_factor: float = KEEP_FACTOR) -> list[Row]:
    spreads = natural_spread(neutral)
    rows = []
    for key in MEASURE_KEYS:
        nv = _values(neutral, key)
        row = Row(key, float(np.median(nv)) if len(nv) else None, spreads[key])
        for label in PERFORMED:
            pv = _values(performed.get(label, []), key)
            if row.spread is None or row.neutral_median is None or not len(pv):
                row.separation[label] = None
            else:
                row.separation[label] = (float(np.median(pv)) - row.neutral_median) / row.spread
        if key in QUALITY_KEYS:
            row.verdict = "quality"
        elif row.spread is None:
            row.verdict, row.because = "not enough data", "fewer than 3 reliable neutral takes"
        else:
            tests = [(label, EXPECTED[label][key]) for label in PERFORMED if key in EXPECTED[label]]
            done = [(label, sign, row.separation[label]) for label, sign in tests if row.separation[label] is not None]
            if not tests:
                row.verdict, row.because = "not tested", "no performed delivery targets it"
            elif not done:
                row.verdict, row.because = "not enough data", "no reliable performed takes"
            else:
                passed = [label for label, sign, sep in done if sep * sign >= keep_factor]
                if passed:
                    row.verdict, row.because = "kept", "moved by " + ", ".join(passed)
                else:
                    best = max(done, key=lambda d: d[2] * d[1])
                    row.verdict = "dropped"
                    row.because = f"best was {best[0]} at {best[2] * best[1]:.1f}x spread (needs {keep_factor:g}x)"
        rows.append(row)
    return rows


@dataclass
class DayCompare:
    key: str
    first: float
    second: float
    spread: float
    held: bool


def compare_baselines(first: Baseline, second: Baseline) -> list[DayCompare]:
    """V6: did the medians stay within the first day's spread?"""
    out = []
    for key in MEASURE_KEYS:
        a, b = first.stats.get(key), second.stats.get(key)
        if a is None or b is None:
            continue
        out.append(DayCompare(key, a.median, b.median, a.spread, abs(b.median - a.median) < a.spread))
    return out
