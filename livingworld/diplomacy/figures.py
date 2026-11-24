"""Political figures (``data/figures.json``): who they are, their seeded convictions, and their court circles.

A figure is matched to an ``npc`` row by plugin + base form ID, the same way lordship matches officials. Seeds
are written lazily, the first time Living World sees the figure on the active timeline, at game time 0 so every
later save on that branch sees them. Nothing here moves anyone's view afterwards; ``record.py`` does that, and
only from the person's own words.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache

from livingworld.diplomacy import DIMENSIONS
from livingworld.lordship.data import norm_form

DATA = os.path.join(os.path.dirname(__file__), "data", "figures.json")


@dataclass(frozen=True)
class Figure:
    key: str
    name: str
    aliases: tuple[str, ...]
    plugin: str
    base_id: str
    faction: str
    role: str
    circle: str | None
    cares: str
    personal: dict = field(default_factory=dict)    # dimension -> (value, confidence)
    official: dict = field(default_factory=dict)
    speaks_for: str | None = None


def _figure(raw: dict) -> Figure:
    def dims(d: dict | None) -> dict:
        out = {}
        for dim, pair in (d or {}).items():
            if dim not in DIMENSIONS:
                raise ValueError(f"figures.json: unknown dimension {dim!r} for {raw.get('key')}")
            value, confidence = float(pair[0]), float(pair[1])
            if not (-1 <= value <= 1 and 0 <= confidence <= 1):
                raise ValueError(f"figures.json: {raw.get('key')}.{dim} out of range")
            out[dim] = (value, confidence)
        return out
    return Figure(raw["key"], raw["name"], tuple(raw.get("aliases") or ()), raw.get("plugin", "Skyrim.esm"),
                  raw["base_id"], raw.get("faction", "other"), raw.get("role", ""), raw.get("circle"),
                  raw.get("cares", ""), dims(raw.get("personal")), dims(raw.get("official")), raw.get("speaks_for"))


@lru_cache(maxsize=1)
def _load() -> tuple[tuple[Figure, ...], dict]:
    with open(DATA, encoding="utf-8") as f:
        raw = json.load(f)
    return tuple(_figure(r) for r in raw["figures"]), dict(raw.get("circles") or {})


def figures() -> tuple[Figure, ...]:
    return _load()[0]


def circles() -> dict:
    return _load()[1]


def figure(key: str) -> Figure | None:
    return next((f for f in figures() if f.key == key), None)


def figure_for(plugin: str | None, base_form_id: str | None) -> Figure | None:
    want = norm_form(base_form_id)
    for f in figures():
        if norm_form(f.base_id) == want and (plugin or "").lower() == f.plugin.lower():
            return f
    return None


def figure_of(db, npc_id: int | None) -> Figure | None:
    if npc_id is None:
        return None
    r = db.execute("SELECT plugin, base_form_id FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()
    return figure_for(r[0], r[1]) if r else None


def npc_for(db, fig: Figure) -> int | None:
    want = norm_form(fig.base_id)
    for npc_id, plugin, base in db.execute("SELECT npc_id, plugin, base_form_id FROM npc WHERE is_player = 0"
                                           " ORDER BY npc_id"):
        if norm_form(base) == want and (plugin or "").lower() == fig.plugin.lower():
            return npc_id
    return None


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9' -]+", "", (name or "").lower()).strip()


def by_name(name: str) -> Figure | None:
    n = _norm(name)
    if not n:
        return None
    for f in figures():
        if n == _norm(f.name) or n in {_norm(a) for a in f.aliases}:
            return f
    return None


def resolve(db, name: str, present: dict[int, str]) -> int | None:
    """The npc_id a name in the transcript means: someone present, a known figure Living World has met, or one
    unambiguous NPC of that name. None when it is nobody Living World knows (never guessed)."""
    n = _norm(name)
    if not n:
        return None
    for npc_id, shown in present.items():
        if _norm(shown) == n:
            return npc_id
    fig = by_name(name)
    if fig is not None:
        found = npc_for(db, fig)
        if found is not None:
            return found
    for npc_id, shown in present.items():   # "Ulfric" for "Ulfric Stormcloak" when he is here
        first = _norm(shown).split(" ")[0] if shown else ""
        if first and first == n:
            return npc_id
    rows = db.execute("SELECT npc_id FROM npc WHERE lower(display_name) = ? AND is_player = 0", (n,)).fetchall()
    return rows[0][0] if len(rows) == 1 else None


def is_seeded(db, npc_id: int) -> bool:
    """Seeds sit at game time 0, so any seed row on a timeline in the active scope is visible."""
    return db.execute(
        "SELECT 1 FROM political_position p JOIN v_timeline_scope s ON s.timeline_id = p.timeline_id"
        " WHERE p.npc_id = ? AND p.source = 'seed' LIMIT 1", (npc_id,)).fetchone() is not None


def seed(db, npc_id: int, timeline: int) -> int:
    """Write a figure's starting convictions once per timeline branch. Returns rows written."""
    fig = figure_of(db, npc_id)
    if fig is None or is_seeded(db, npc_id):
        return 0
    rows = 0
    for layer, dims in (("personal", fig.personal), ("official", fig.official)):
        for dim, (value, confidence) in dims.items():
            db.execute("INSERT INTO political_position (timeline_id, npc_id, dimension, layer, value, confidence,"
                       " source, reason, game_time) VALUES (?,?,?,?,?,?,'seed',?,0)",
                       (timeline, npc_id, dim, layer, value, confidence, f"figures.json {fig.key}"))
            rows += 1
    ensure_circle(db, fig, npc_id)
    return rows


def ensure_circle(db, fig: Figure, npc_id: int) -> str | None:
    """The figure's court as a community (kind 'faction') so the existing gossip can carry public statements
    between its members. Membership is not on the timeline (like every community); it only says who talks."""
    if not fig.circle:
        return None
    info = circles().get(fig.circle, {})
    db.execute("INSERT OR IGNORE INTO community (key, name, kind, location) VALUES (?,?, 'faction', ?)",
               (fig.circle, info.get("name", fig.circle), info.get("location")))
    cid = db.execute("SELECT community_id FROM community WHERE key = ?", (fig.circle,)).fetchone()[0]
    db.execute("INSERT OR IGNORE INTO membership (npc_id, community_id, role, influence) VALUES (?,?,?,?)",
               (npc_id, cid, fig.role[:80], 0.8 if fig.speaks_for else 0.5))
    return fig.circle


def circle_keys(db, npc_ids: list[int]) -> list[str]:
    """Court circles the given people belong to (figures only)."""
    keys = []
    for npc_id in npc_ids:
        fig = figure_of(db, npc_id)
        if fig is not None and fig.circle and fig.circle not in keys:
            keys.append(fig.circle)
    return keys
