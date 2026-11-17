"""Input device listing and lookup by name (shared by Voice Lab and the capture layer). Needs sounddevice only
when a function is called, so importing this module never touches audio hardware."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Device:
    index: int
    name: str
    hostapi: str
    sample_rate: int
    channels: int
    is_default: bool

    def to_json(self) -> dict:
        return {"index": self.index, "name": self.name, "hostapi": self.hostapi, "sample_rate": self.sample_rate,
                "channels": self.channels, "is_default": self.is_default}


def list_devices() -> list[Device]:
    import sounddevice as sd
    apis = sd.query_hostapis()
    try:
        default_in = sd.default.device[0]
    except Exception:
        default_in = -1
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) < 1:
            continue
        out.append(Device(i, d["name"], apis[d["hostapi"]]["name"], int(d["default_samplerate"]),
                          int(d["max_input_channels"]), i == default_in))
    return out


def find_device(devices: list[Device], name: str, hostapi: str | None = None) -> Device | None:
    """Exact name (and host API when given) first, then a case-insensitive part of the name. Names are stable;
    indexes change when a headset reconnects."""
    exact = [d for d in devices if d.name == name and (hostapi is None or d.hostapi == hostapi)]
    if exact:
        return exact[0]
    loose = [d for d in devices if name.lower() in d.name.lower() and (hostapi is None or d.hostapi == hostapi)]
    if loose:
        return loose[0]
    loose = [d for d in devices if name.lower() in d.name.lower()]
    return loose[0] if loose else None


def default_device(devices: list[Device]) -> Device | None:
    for d in devices:
        if d.is_default:
            return d
    return devices[0] if devices else None
