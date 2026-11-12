"""One woman's profile as it stands now: the precedence rules live here and only here.

Precedence, strongest first:
1. Off switches: the module off, her entry ``"enabled": false``, or not eligible (``eligibility.py``) = no profile.
2. Values set by hand in the settings file (``config.py``). In MANUAL mode they are the whole profile; in HYBRID
   (and AUTOMATIC, if any are set) they replace the generated value and are locked: change over time never
   touches them, and nothing the AI says changes them.
3. Change over time (``personality_change`` on the save timeline), on top of
4. the generated starting point (``personality_profile``), shifted by the file's global baseline.

Boundaries: a boundary set by hand or generated is never removed by change over time (``evolve.py`` can only add).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from livingworld.personality import eligibility, generate, schema
from livingworld.personality.config import NpcSettings, Settings


@dataclass
class Profile:
    npc_id: int
    name: str
    eligible: bool
    why: str
    enabled: bool = False
    mode: str = "hybrid"
    values: dict[str, Any] = field(default_factory=dict)       # effective values (MANUAL: only what was set)
    generated: dict[str, Any] = field(default_factory=dict)
    manual: dict[str, Any] = field(default_factory=dict)       # set by hand, locked
    drift: dict[str, float] = field(default_factory=dict)      # total change over time per number
    added: dict[str, list[str]] = field(default_factory=dict)  # list items added over time
    private: set[str] = field(default_factory=set)             # never shared (from the settings file)
    sources: dict[str, str] = field(default_factory=dict)      # per attribute: manual / generated / changed / ...

    @property
    def active(self) -> bool:
        return self.eligible and self.enabled

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def locked(self, key: str) -> bool:
        return self.mode == "manual" or key in self.manual

    def is_private(self, item: str) -> bool:
        return item in self.private or item.split(":")[0] in self.private


def active_timeline(db) -> int:
    return db.execute("SELECT timeline_id FROM timeline WHERE is_active = 1").fetchone()[0]


def add_change(db, npc_id: int, attribute: str, kind: str, reason: str, gt: float, delta: float | None = None,
               value: Any = None, cause: str | None = None) -> int:
    return db.execute(
        "INSERT INTO personality_change (timeline_id, npc_id, attribute, kind, delta, value_json, reason, cause,"
        " game_time) VALUES (?,?,?,?,?,?,?,?,?)",
        (active_timeline(db), npc_id, attribute, kind, delta, None if value is None else json.dumps(value), reason,
         cause, gt)).lastrowid


def changes_since_reset(db, npc_id: int) -> list[dict]:
    rows = [dict(r) for r in db.execute(
        "SELECT * FROM v_personality_change WHERE npc_id = ? ORDER BY game_time, change_id", (npc_id,))]
    last_reset = max((i for i, r in enumerate(rows) if r["kind"] == "reset"), default=-1)
    return rows[last_reset + 1:]


def entry_for(db, settings: Settings, npc_id: int) -> NpcSettings | None:
    row = db.execute("SELECT display_name, plugin, base_form_id FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()
    return settings.for_npc(row[0], row[1], row[2]) if row else None


def resolve(db, npc_id: int, settings: Settings, gt: float, create: bool = True) -> Profile:
    """Her profile now. ``create`` generates and stores her starting point the first time (call in a transaction)."""
    name_row = db.execute("SELECT display_name FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()
    name = name_row[0] if name_row else "?"
    entry = entry_for(db, settings, npc_id)
    elig = eligibility.check(db, npc_id, entry.adult_confirmed if entry else False)
    p = Profile(npc_id, name, elig.eligible, elig.why)
    if not elig.eligible:
        return p
    p.enabled = settings.enabled and (entry.enabled if entry else True)
    p.mode = settings.mode_of(entry)
    p.manual = dict(entry.values) if entry else {}
    p.private = set(entry.private) if entry else set()
    if not p.enabled:
        return p
    if p.mode == "manual":
        p.values = dict(p.manual)
        p.sources = {k: "manual" for k in p.manual}
        return p
    if create:
        gen = generate.store_generated(db, npc_id, gt)
    else:
        row = db.execute("SELECT generated_json, sources_json FROM personality_profile WHERE npc_id = ?",
                         (npc_id,)).fetchone()
        gen = generate.Generated(json.loads(row[0]), json.loads(row[1])) if row else generate.generate(db, npc_id)
    p.generated = dict(gen.values)
    values = dict(gen.values)
    sources = {k: "generated" for k in values}
    for key, shift in settings.baseline.items():
        if key in values:
            values[key] = round(max(0.0, min(100.0, values[key] + shift)), 1)
            sources[key] = "generated + baseline"
    base = dict(values)
    for c in changes_since_reset(db, npc_id):
        key = c["attribute"]
        attr = schema.BY_KEY.get(key)
        if attr is None:
            continue
        if c["kind"] == "drift" and attr.kind == "number" and c["delta"] is not None:
            p.drift[key] = p.drift.get(key, 0.0) + c["delta"]
        elif c["kind"] in ("list_add", "list_remove") and attr.kind == "list":
            item = json.loads(c["value_json"]) if c["value_json"] else None
            if item not in attr.choices:
                continue
            current = values[key]
            if c["kind"] == "list_add" and item not in current:
                values[key] = current + [item]
                p.added.setdefault(key, []).append(item)
                sources[key] = "changed over time"
            elif c["kind"] == "list_remove" and item in current and key != "boundaries":
                values[key] = [x for x in current if x != item]     # boundaries are never removed this way
                sources[key] = "changed over time"
    for key, total in p.drift.items():
        limit = schema.BY_KEY[key].drift
        total = max(-limit, min(limit, total))
        p.drift[key] = round(total, 2)
        values[key] = round(max(0.0, min(100.0, base[key] + total)), 1)
        if abs(total) >= 0.05:
            sources[key] = "changed over time"
    for key, value in p.manual.items():
        values[key] = value
        sources[key] = "manual"
    p.values, p.sources = values, sources
    return p
