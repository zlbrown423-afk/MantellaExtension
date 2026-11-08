"""Work templates: what each person may need done, read from ``data/*.json`` (see the file's ``_about``)."""
from __future__ import annotations

import functools
import json
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

from livingworld.social import seed as social_seed
from livingworld.store.minors import looks_like_child

WORK_FILES = ("riverwood_work.json",)
PLAYER = "player"


@dataclass(frozen=True)
class Template:
    key: str
    giver: str                      # person key in the social seed
    title: str
    why: str
    instructions: str
    need_item: str | None
    need_count: int
    units: str
    location: str | None = None
    community: str | None = None
    evidence: tuple[str, ...] = ("given",)
    helpers: tuple[str, ...] = (PLAYER,)
    months: tuple[str, ...] = ()           # empty = any month
    hours: tuple[int, int] = (0, 24)       # offered and worked between these hours
    outdoors: bool = False
    bad_weather: tuple[str, ...] = ()
    ask_min_trust: float = 0.0
    reward_gold: int = 0
    size: float = 0.3
    deadline_days: float | None = None
    open_days: float = 5.0
    cooldown_days: float = 2.0
    extra: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    @property
    def asks_player(self) -> bool:
        return PLAYER in self.helpers


def _template(raw: dict[str, Any]) -> Template:
    need = raw.get("need") or {}
    hours = raw.get("hours") or [0, 24]
    return Template(
        key=raw["key"], giver=raw["giver"], title=raw["title"], why=raw.get("why", ""),
        instructions=raw.get("instructions", ""), need_item=need.get("item"), need_count=int(need.get("count", 1)),
        units=need.get("units", "loads"), location=raw.get("location"), community=raw.get("community"),
        evidence=tuple(raw.get("evidence") or ("given",)), helpers=tuple(raw.get("helpers") or (PLAYER,)),
        months=tuple(raw.get("months") or ()), hours=(int(hours[0]), int(hours[1])),
        outdoors=bool(raw.get("outdoors", False)), bad_weather=tuple(w.lower() for w in raw.get("bad_weather") or ()),
        ask_min_trust=float(raw.get("ask_min_trust", 0.0)), reward_gold=int(raw.get("reward_gold", 0)),
        size=float(raw.get("size", 0.3)),
        deadline_days=float(raw["deadline_days"]) if raw.get("deadline_days") is not None else None,
        open_days=float(raw.get("open_days", 5.0)), cooldown_days=float(raw.get("cooldown_days", 2.0)),
        extra={k: v for k, v in raw.items() if k.startswith("_")})


@functools.lru_cache(maxsize=1)
def templates() -> tuple[Template, ...]:
    data = resources.files("livingworld.work").joinpath("data")
    out = []
    for name in WORK_FILES:
        for raw in json.loads(data.joinpath(name).read_text(encoding="utf-8"))["templates"]:
            out.append(_template(raw))
    return tuple(out)


def by_key(key: str) -> Template | None:
    return next((t for t in templates() if t.key == key), None)


@functools.lru_cache(maxsize=1)
def _people() -> dict[str, tuple[str, str]]:
    """person key -> (plugin, base form id) from the social seeds."""
    out = {}
    for s in social_seed.seeds():
        for p in s["people"]:
            out[p["key"]] = (p["plugin"], "0x" + p["base_id"].upper().rjust(8, "0"))
    return out


def person_id(db, key: str) -> int | None:
    """The npc_id of a seeded person, once they have been seen in play; never a child."""
    if key == PLAYER:
        row = db.execute("SELECT npc_id FROM npc WHERE is_player = 1").fetchone()
        return row[0] if row else None
    ref = _people().get(key)
    if ref is None or looks_like_child(ref[1]):
        return None
    row = db.execute("SELECT npc_id FROM npc WHERE plugin = ? AND base_form_id = ? AND is_child = 0"
                     " ORDER BY npc_id LIMIT 1", ref).fetchone()
    return row[0] if row else None


def person_key(db, npc_id: int) -> str | None:
    row = db.execute("SELECT plugin, base_form_id, is_player FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()
    if not row:
        return None
    if row[2]:
        return PLAYER
    for key, ref in _people().items():
        if ref == (row[0], row[1]):
            return key
    return None


def for_giver(db, npc_id: int) -> list[Template]:
    key = person_key(db, npc_id)
    return [t for t in templates() if t.giver == key] if key else []
