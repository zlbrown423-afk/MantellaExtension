"""Microphone profiles: one per physical mic / connection, kept on this PC in
Documents\\My Games\\Mantella\\data\\audio\\mic_profiles.json. Numbers only, never audio.

The profile is what the later capture layer reads: which device to open (by name, not index), the fixed
analysis-path gain, the noise floor and SNR, whether the analysis signal looks clean, and the STT-path filters
(empty until the EQ step proves a filter helps)."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

PROFILE_FILE = "mic_profiles.json"
VERSION = 1


@dataclass
class MicProfile:
    device_name: str
    hostapi: str
    sample_rate: int
    channels_available: int
    channel_used: int = 0
    created: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))
    analysis_gain_db: float | None = None        # fixed gain for the analysis path; never changes during play
    noise_floor_db: float | None = None
    speech_level_db: float | None = None
    snr_db: float | None = None
    snr_rating: str = "unknown"
    loud_peak_db: float | None = None
    clipped_samples: int = 0
    flat_top_runs: int = 0
    analysis_clean: bool = False                 # False also when it could not be judged
    clean_judged: bool = False
    clean_reasons: list[str] = field(default_factory=list)
    hum_hz: int | None = None
    quiet_detected_share: float | None = None    # share of the quiet read Mantella's VAD (threshold 0.4) hears
    echo_speech_s: float | None = None           # seconds the VAD heard as speech while only an NPC talked
    noise_bands_db: dict[str, float] = field(default_factory=dict)
    speech_bands_db: dict[str, float] = field(default_factory=dict)
    stt_filters: dict[str, Any] = field(default_factory=dict)
    advice: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "MicProfile":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def profile_key(device_name: str, hostapi: str) -> str:
    return f"{device_name} | {hostapi}"


class ProfileStore:
    def __init__(self, root: Path) -> None:
        self.path = Path(root) / PROFILE_FILE

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("profiles"), dict):
                return data
        except (OSError, ValueError):
            pass
        return {"version": VERSION, "active": None, "profiles": {}}

    def all(self) -> dict[str, MicProfile]:
        return {k: MicProfile.from_json(v) for k, v in self._read()["profiles"].items()}

    def active(self) -> MicProfile | None:
        data = self._read()
        key = data.get("active")
        return MicProfile.from_json(data["profiles"][key]) if key in data["profiles"] else None

    def save(self, profile: MicProfile, make_active: bool = False) -> str:
        data = self._read()
        key = profile_key(profile.device_name, profile.hostapi)
        data["profiles"][key] = profile.to_json()
        if make_active or not data.get("active"):
            data["active"] = key
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        return key

    def set_active(self, key: str) -> bool:
        data = self._read()
        if key not in data["profiles"]:
            return False
        data["active"] = key
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return True
