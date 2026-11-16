"""The player's own vocal baseline: what "normal" sounds like for this player on this mic.

Per measurement it keeps a robust median and spread (1.4826 x the median absolute deviation, with a floor so a
very steady measurement cannot produce huge z-scores). Deviations are reported as z-scores against it.

Refinement is slow and conservative: only utterances that already look ordinary (every reliable z within
+-1.5) move the baseline, by 5 % each, so a shouting match never teaches the baseline that shouting is normal.

Stored as a small JSON file per mic. It holds numbers only.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import numpy as np

from livingworld.voice.features import MEASURE_KEYS, VocalFeatures

VERSION = 1
MIN_SAMPLES = 3

# The smallest spread taken seriously, per measurement (in its own unit).
SPREAD_FLOOR: dict[str, float] = {
    "duration_s": 0.1, "voiced_ratio": 0.03, "pause_count": 0.5, "longest_pause_s": 0.08,
    "loudness_db": 1.0, "loudness_p95_db": 1.0, "peak_db": 1.0, "clipped_fraction": 1e-4, "snr_db": 1.5,
    "pitch_median_st": 0.3, "pitch_range_st": 0.4, "tilt_db": 1.0, "rate_syl_s": 0.2, "text_rate_syl_s": 0.2,
}
# Measurements that describe the voice (the rest describe the recording: clipping, SNR, peak).
VOICE_KEYS = ("duration_s", "voiced_ratio", "pause_count", "longest_pause_s", "loudness_db", "loudness_p95_db",
              "pitch_median_st", "pitch_range_st", "tilt_db", "rate_syl_s", "text_rate_syl_s")


@dataclass
class Stat:
    median: float
    spread: float
    n: int


@dataclass
class Baseline:
    mic: str
    sample_rate: int = 0
    pitch_backend: str = ""
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    stats: dict[str, Stat] = field(default_factory=dict)
    refined: int = 0
    version: int = VERSION

    @classmethod
    def from_features(cls, takes: Iterable[VocalFeatures], mic: str, sample_rate: int = 0) -> "Baseline":
        takes = list(takes)
        base = cls(mic=mic, sample_rate=sample_rate,
                   pitch_backend=next((t.pitch_backend for t in takes if t.pitch_backend), ""))
        for key in MEASURE_KEYS:
            values = [t.reliable_value(key) for t in takes]
            values = np.array([v for v in values if v is not None], dtype=np.float64)
            if len(values) < MIN_SAMPLES:
                continue
            med = float(np.median(values))
            mad = float(np.median(np.abs(values - med))) * 1.4826
            base.stats[key] = Stat(med, max(mad, SPREAD_FLOOR.get(key, 0.0)), int(len(values)))
        return base

    def z(self, key: str, value: float | None) -> float | None:
        stat = self.stats.get(key)
        if stat is None or value is None or stat.spread <= 0:
            return None
        return (float(value) - stat.median) / stat.spread

    def zscores(self, feats: VocalFeatures) -> dict[str, float | None]:
        """z per measurement; None where either side is missing or the value is unreliable."""
        return {key: self.z(key, feats.reliable_value(key)) for key in MEASURE_KEYS}

    def refine(self, feats: VocalFeatures, weight: float = 0.05, gate: float = 1.5) -> bool:
        """Nudge the baseline towards an ordinary-looking utterance. Returns whether it was used."""
        zs = {k: v for k, v in self.zscores(feats).items() if v is not None and k in VOICE_KEYS}
        if len(zs) < 3 or any(abs(v) > gate for v in zs.values()):
            return False
        for key in zs:
            stat, value = self.stats[key], float(feats.reliable_value(key))
            dev = value - stat.median
            stat.median += weight * dev
            stat.spread = max(math.sqrt((1 - weight) * stat.spread ** 2 + weight * dev ** 2),
                              SPREAD_FLOOR.get(key, 0.0))
            stat.n += 1
        self.refined += 1
        self.updated = time.time()
        return True

    def to_json(self) -> dict:
        return {"version": self.version, "mic": self.mic, "sample_rate": self.sample_rate,
                "pitch_backend": self.pitch_backend, "created": self.created, "updated": self.updated,
                "refined": self.refined,
                "stats": {k: {"median": s.median, "spread": s.spread, "n": s.n} for k, s in self.stats.items()}}

    @classmethod
    def from_json(cls, data: dict) -> "Baseline":
        base = cls(mic=data.get("mic", ""), sample_rate=int(data.get("sample_rate", 0)),
                   pitch_backend=data.get("pitch_backend", ""), created=float(data.get("created", 0)),
                   updated=float(data.get("updated", 0)), refined=int(data.get("refined", 0)),
                   version=int(data.get("version", VERSION)))
        for k, s in (data.get("stats") or {}).items():
            base.stats[k] = Stat(float(s["median"]), float(s["spread"]), int(s.get("n", 0)))
        return base

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_json(), indent=2), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "Baseline | None":
        try:
            return cls.from_json(json.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError):
            return None
