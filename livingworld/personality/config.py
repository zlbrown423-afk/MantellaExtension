"""The administrator's settings file: global baseline, the mode, and each NPC's hand-set values.

One JSON file for every world, by default ``<Mantella save folder>/data/livingworld/personality.json`` (next to the
world databases); ``MANTELLA_LW_PERSONALITY_FILE`` points elsewhere. Missing file = everyone automatic, nothing set
by hand. The file is read again whenever it changes, so edits take effect at the next conversation without a restart.

A hand-set value is authoritative: generation, change over time and the AI never overwrite it (``profile.py``).
A broken file is never fatal: its problems are logged, the bad parts are skipped and the rest is used.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any

from livingworld.personality import schema

logger = logging.getLogger(__name__)

FILE_ENV = "MANTELLA_LW_PERSONALITY_FILE"
FILE_NAME = "personality.json"


@dataclass
class NpcSettings:
    key: str                                  # as written in the file
    enabled: bool = True
    mode: str | None = None                   # None = the file's default_mode
    values: dict[str, Any] = field(default_factory=dict)
    private: list[str] = field(default_factory=list)
    adult_confirmed: bool = False
    note: str = ""


@dataclass
class Settings:
    enabled: bool = True
    default_mode: str = "hybrid"
    evolution_enabled: bool = True
    evolution_rate: float = 1.0
    baseline: dict[str, float] = field(default_factory=dict)
    npcs: dict[str, NpcSettings] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    path: str | None = None

    def for_npc(self, name: str, plugin: str | None, base_form_id: str | None) -> NpcSettings | None:
        """This NPC's entry: by 'Plugin|0xID' first (exact person), then by name (case ignored)."""
        if plugin and base_form_id:
            wanted = f"{plugin}|{normal_id(base_form_id)}".lower()
            for key, entry in self.npcs.items():
                if "|" in key and _norm_key(key) == wanted:
                    return entry
        for key, entry in self.npcs.items():
            if "|" not in key and key.strip().lower() == (name or "").strip().lower():
                return entry
        return None

    def mode_of(self, entry: NpcSettings | None) -> str:
        return (entry.mode if entry and entry.mode else self.default_mode)


def normal_id(form_id: str) -> str:
    raw = str(form_id).strip().lower().replace("0x", "")
    return "0x" + raw.upper().rjust(8, "0")


def _norm_key(key: str) -> str:
    plugin, _, fid = key.partition("|")
    return f"{plugin.strip()}|{normal_id(fid)}".lower()


def default_path(save_folder: str) -> str:
    return os.environ.get(FILE_ENV) or os.path.join(save_folder, "data", "livingworld", FILE_NAME)


def parse(data: Any, path: str | None = None) -> Settings:
    """Settings from the file's JSON. Anything invalid is skipped and listed in ``problems``."""
    s = Settings(path=path)
    if not isinstance(data, dict):
        s.problems.append("the file is not a JSON object; using defaults")
        return s
    version = data.get("version", schema.PROFILE_VERSION)
    if version != schema.PROFILE_VERSION:
        s.problems.append(f"version {version} is not {schema.PROFILE_VERSION}; reading it anyway")
    if isinstance(data.get("enabled"), bool):
        s.enabled = data["enabled"]
    if data.get("default_mode") in schema.MODES:
        s.default_mode = data["default_mode"]
    elif "default_mode" in data:
        s.problems.append(f"default_mode must be one of {', '.join(schema.MODES)}")
    evo = data.get("evolution") or {}
    if isinstance(evo, dict):
        if isinstance(evo.get("enabled"), bool):
            s.evolution_enabled = evo["enabled"]
        if isinstance(evo.get("rate"), (int, float)) and 0 <= evo["rate"] <= 3:
            s.evolution_rate = float(evo["rate"])
    for key, value in (data.get("baseline") or {}).items():
        if key in schema.NUMBERS and isinstance(value, (int, float)) and -50 <= value <= 50:
            s.baseline[key] = float(value)
        else:
            s.problems.append(f"baseline.{key}: needs a known number attribute and a shift of -50..50")
    for key, raw in (data.get("npcs") or {}).items():
        if not isinstance(raw, dict):
            s.problems.append(f"npcs.{key}: must be an object")
            continue
        entry = NpcSettings(key=key)
        if isinstance(raw.get("enabled"), bool):
            entry.enabled = raw["enabled"]
        if raw.get("mode") in schema.MODES:
            entry.mode = raw["mode"]
        elif "mode" in raw:
            s.problems.append(f"npcs.{key}.mode must be one of {', '.join(schema.MODES)}")
        for attr, value in (raw.get("values") or {}).items():
            try:
                entry.values[attr] = schema.validate_value(attr, value)
            except ValueError as e:
                s.problems.append(f"npcs.{key}.values: {e}")
        for item in raw.get("private") or []:
            if str(item).split(":")[0] in schema.BY_KEY:
                entry.private.append(str(item))
            else:
                s.problems.append(f"npcs.{key}.private: unknown attribute {item!r}")
        entry.adult_confirmed = raw.get("adult_confirmed") is True
        entry.note = str(raw.get("note") or "")
        if raw.get("plugin") and raw.get("base_id") and "|" not in key:
            # 'Gerdur': {"plugin": ..., "base_id": ...} is the same as the key 'Skyrim.esm|0x...'
            key = f"{raw['plugin']}|{normal_id(raw['base_id'])}"
        s.npcs[key] = entry
    return s


def to_json(s: Settings) -> dict[str, Any]:
    out: dict[str, Any] = {"version": schema.PROFILE_VERSION, "enabled": s.enabled, "default_mode": s.default_mode,
                           "evolution": {"enabled": s.evolution_enabled, "rate": s.evolution_rate},
                           "baseline": dict(s.baseline), "npcs": {}}
    for key, e in s.npcs.items():
        entry: dict[str, Any] = {}
        if not e.enabled:
            entry["enabled"] = False
        if e.mode:
            entry["mode"] = e.mode
        if e.values:
            entry["values"] = dict(e.values)
        if e.private:
            entry["private"] = list(e.private)
        if e.adult_confirmed:
            entry["adult_confirmed"] = True
        if e.note:
            entry["note"] = e.note
        out["npcs"][key] = entry
    return out


class SettingsFile:
    """The settings file, read again when it changes on disk."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._stamp: tuple[float, int] | None = None
        self._settings = Settings(path=path)

    def get(self) -> Settings:
        with self._lock:
            try:
                st = os.stat(self.path)
                stamp = (st.st_mtime, st.st_size)
            except OSError:
                if self._stamp is not None:
                    self._settings, self._stamp = Settings(path=self.path), None
                return self._settings
            if stamp != self._stamp:
                try:
                    with open(self.path, encoding="utf-8-sig") as f:
                        self._settings = parse(json.load(f), self.path)
                except (OSError, ValueError) as e:
                    self._settings = Settings(path=self.path, problems=[f"could not read: {e}"])
                for p in self._settings.problems:
                    logger.warning(f"Living World personality settings ({self.path}): {p}")
                self._stamp = stamp
            return self._settings

    def save(self, s: Settings) -> None:
        with self._lock:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(to_json(s), f, indent=2, ensure_ascii=False)
                f.write("\n")
            os.replace(tmp, self.path)
            self._stamp = None
