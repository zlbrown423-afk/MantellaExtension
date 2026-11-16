"""What fighting side by side does to how the people in the fight see each other.

Small, slow, and the same for everyone present: each participant's view of each other participant moves a
little towards familiarity, trust, respect and liking. Someone who stayed in the fight while badly hurt earns a
little more respect from the others. Each further fight between the same two people counts for less, so a long
campaign builds a bond without any single fight making best friends. Only ever positive: the game does not say
who ran, who was struck by whom or who died, so nothing here can claim a betrayal or a grudge.

The change is written as a ``relationship_change`` row caused by the fight's event and memory, the same way every
other event moves relationships, so it rolls back with saves. ``MANTELLA_LW_BONDS=0`` turns it off.
"""
from __future__ import annotations

import json
import os

ENV = "MANTELLA_LW_BONDS"
_OFF = {"0", "false", "no", "off"}
REASON = "fought side by side"

BASE = {"familiarity": 0.04, "trust": 0.03, "respect": 0.02, "affinity": 0.02}
HURT_RESPECT = 0.02        # extra respect for someone who stayed in it down to under half health
DECAY = 0.5                # each earlier fight together shrinks the next one by 1 / (1 + DECAY * n)


def bonds_enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in _OFF


def _earlier(db, from_id: int, to_id: int) -> int:
    return db.execute("SELECT COUNT(*) FROM relationship_change WHERE from_id = ? AND to_id = ? AND reason = ?",
                      (from_id, to_id, REASON)).fetchone()[0]


def deltas(earlier: int, other_hp: float | None) -> dict[str, float]:
    scale = 1.0 / (1.0 + DECAY * earlier)
    d = dict(BASE)
    if other_hp is not None and other_hp < 0.5:
        d["respect"] += HURT_RESPECT
    return {k: round(v * scale, 4) for k, v in d.items()}


def apply(db, timeline: int, event_id: int, gt: float, hp: dict[int, float | None], mem_of: dict[int, int],
          players: list[int]) -> int:
    """Move every pair in the fight, both directions, except that the player's own side is not stored as an NPC's
    view of themselves. Returns the number of changes written."""
    n = 0
    for a in hp:
        for b in hp:
            if a == b:
                continue
            db.execute("INSERT OR IGNORE INTO relationship (from_id, to_id, status, updated_gt) VALUES (?,?,?,?)",
                       (a, b, "stranger", gt))
            d = deltas(_earlier(db, a, b), hp[b])
            db.execute("INSERT INTO relationship_change (timeline_id, from_id, to_id, deltas_json, cause_mem_id,"
                       " cause_event_id, reason, game_time) VALUES (?,?,?,?,?,?,?,?)",
                       (timeline, a, b, json.dumps(d), mem_of.get(a), event_id, REASON, gt))
            n += 1
    return n
