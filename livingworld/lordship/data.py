"""Hand-edited data: districts, charter terms per level, officials (``data/*.json``)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

DATA = Path(__file__).parent / "data"


def _load(name: str) -> dict:
    with open(DATA / name, encoding="utf-8") as f:
        return json.load(f)


@dataclass(frozen=True)
class District:
    key: str
    name: str
    hold: str
    places: tuple[str, ...]
    routes: dict[str, tuple[str, ...]]
    communities: tuple[str, ...]
    about: str


@dataclass(frozen=True)
class Official:
    key: str
    name: str
    role: str
    plugin: str
    base_id: str
    weights: dict[str, float]
    cares: str
    signs: bool = False


@dataclass(frozen=True)
class Terms:
    period_days: int
    levels: dict[int, dict[str, Any]]
    wage_per_rider_month: int
    feed_per_horse_month: int
    default_riders: int
    default_horses: int


@lru_cache(maxsize=1)
def districts() -> dict[str, District]:
    out = {}
    for d in _load("districts.json")["districts"]:
        out[d["key"]] = District(d["key"], d["name"], d["hold"], tuple(d["places"]),
                                 {k: tuple(v) for k, v in d.get("routes", {}).items()},
                                 tuple(d.get("communities", [])), d.get("about", ""))
    return out


def district(key: str | None) -> District | None:
    return districts().get(key or "")


@lru_cache(maxsize=1)
def terms() -> Terms:
    raw = _load("charter_terms.json")
    up = raw.get("upkeep", {})
    rep = raw.get("defaults_reported", {})
    return Terms(int(raw.get("period_days", 30)), {int(l["level"]): l for l in raw["levels"]},
                 int(up.get("wage_per_rider_month", 10)), int(up.get("feed_per_horse_month", 5)),
                 int(rep.get("riders", 1)), int(rep.get("horses", 4)))


def level_terms(level: int) -> dict[str, Any]:
    levels = terms().levels
    if level not in levels:
        raise ValueError(f"no charter level {level}")
    return dict(levels[level])


def norm_form(raw: str | int | None) -> str:
    """'0x00013BBD', '13BBD', 0x13BBD -> '00013BBD' (compare form IDs whatever their spelling)."""
    if raw is None:
        return ""
    if isinstance(raw, int):
        return f"{raw:08X}"
    s = str(raw).strip().upper()
    if s.startswith("0X"):
        s = s[2:]
    return s.rjust(8, "0")[-8:]


@lru_cache(maxsize=1)
def officials() -> tuple[Official, ...]:
    return tuple(Official(o["key"], o["name"], o["role"], o.get("plugin", "Skyrim.esm"), o["base_id"],
                          dict(o.get("weights", {})), o.get("cares", ""), bool(o.get("signs", False)))
                 for o in _load("court.json")["officials"])


def official(key: str) -> Official | None:
    return next((o for o in officials() if o.key == key), None)


def official_for(plugin: str | None, base_id: str | None) -> Official | None:
    want = norm_form(base_id)
    return next((o for o in officials() if norm_form(o.base_id) == want
                 and (plugin is None or (o.plugin or "").lower() == plugin.lower())), None)
