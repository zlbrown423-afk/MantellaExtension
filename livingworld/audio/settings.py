"""Switches and per-mic settings for the capture layer.

    MANTELLA_LW_AUDIO_V2=1       use AudioCapture instead of Mantella's own microphone stream (off by default)
    MANTELLA_LW_AUDIO_DEVICE     optional: capture this input by (part of) its name instead of the active profile's

Everything else comes from the active mic profile written by the calibration page
(Documents\\My Games\\Mantella\\data\\audio\\mic_profiles.json)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from livingworld.audio.profile import MicProfile, ProfileStore

ENV_FLAG = "MANTELLA_LW_AUDIO_V2"
ENV_DEVICE = "MANTELLA_LW_AUDIO_DEVICE"


def enabled() -> bool:
    return os.environ.get(ENV_FLAG, "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class CaptureSettings:
    device_name: str | None
    hostapi: str | None
    gain_db: float
    stt_filters: dict[str, Any]
    profile: MicProfile | None


def audio_root(save_folder: str | os.PathLike) -> Path:
    return Path(save_folder) / "data" / "audio"


def load(save_folder: str | os.PathLike) -> CaptureSettings:
    """The device, fixed gain and STT filters to capture with. No profile: the Windows default mic, 0 dB, no filters."""
    override = os.environ.get(ENV_DEVICE, "").strip()
    profile = None
    try:
        profile = ProfileStore(audio_root(save_folder)).active()
    except Exception:
        profile = None
    if override:
        match = profile if profile and override.lower() in profile.device_name.lower() else None
        return CaptureSettings(override, match.hostapi if match else None,
                               (match.analysis_gain_db or 0.0) if match else 0.0,
                               match.stt_filters if match else {}, match)
    if profile is None:
        return CaptureSettings(None, None, 0.0, {}, None)
    return CaptureSettings(profile.device_name, profile.hostapi, profile.analysis_gain_db or 0.0,
                           profile.stt_filters or {}, profile)
