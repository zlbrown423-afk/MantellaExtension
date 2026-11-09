"""Keeping the peace: a guard who saw the player put hands on someone steps in.

The game decides on the spot (Mantella-Spell, ``LivingWorldReactToGrab``): a guard who saw an unwelcome grab
comes over, and a ``react`` record (``via=guard``) is stored as a ``reaction`` world event. This module tells that
guard what they saw and what they may do: the first time guards have to step in, a firm warning; once they have
already had to (within ``MEMORY_DAYS``), they may arrest the player with Mantella's ReportCrime action.
Rules only; the guard says it in their own words.
"""
from __future__ import annotations

import json

FRESH_DAYS = 0.05        # about an hour of game time: the stepping-in this conversation is about
MEMORY_DAYS = 2.0        # earlier times guards had to step in that still count
BOUNTY = 40              # gold, for ReportCrime


def _name(db, npc_id: int) -> str:
    return db.execute("SELECT display_name FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()[0]


def _guard_reactions(db, player_id: int, since: float, until: float):
    return [r for r in db.execute(
        "SELECT event_id, actors_json, payload_json, game_time FROM v_visible_event WHERE type = 'reaction'"
        " AND game_time > ? AND game_time <= ? ORDER BY game_time DESC, event_id DESC", (since, until)).fetchall()
            if json.loads(r[2] or "{}").get("role") == "guard" and json.loads(r[1])[1:2] == [player_id]]


def line(db, guard_id: int, name: str, player_id: int, player: str, now: float,
         fellow: str | None = None) -> str | None:
    """What ``name`` (a guard) has to do about the player's hands on someone, or None when they did not step in.
    ``fellow`` is the guard's organisation when the player serves in it too."""
    recent = [r for r in _guard_reactions(db, player_id, now - FRESH_DAYS, now + 1e-9)
              if json.loads(r[1])[0] == guard_id]
    if not recent:
        return None
    event_id, actors, payload, at = recent[0]
    victim = json.loads(actors)[2]
    area = json.loads(payload or "{}").get("area")
    by = f" by the {area}" if area else ""
    prior = [r for r in _guard_reactions(db, player_id, at - MEMORY_DAYS, at) if r[0] != event_id]
    text = (f"{name} saw {player} put hands on {_name(db, victim)}{by} against their will just now and has stepped "
            f"in. {name} tells {player} to stop, plainly and in their own words, and checks that "
            f"{_name(db, victim)} is all right.")
    if not prior:
        text += (f" This is the first time a guard has had to stop {player}: a firm warning, no arrest. "
                 f"Do not use ReportCrime.")
    else:
        times = "once" if len(prior) == 1 else f"{len(prior)} times"
        text += (f" Guards have already had to stop {player} doing this {times} in the last two days, so {name} "
                 f"may arrest them for it now: use ReportCrime with a bounty of {BOUNTY} gold, and say so.")
    if fellow:
        text += f" {player} serves in {fellow} too; {name} tells them their superior will hear of it."
    return text
