"""Public decency: a guard who sees the player walking about naked comes over and tells them to cover up.

The game decides on the spot (Mantella-Spell ``LwDecencyTick``): the player has had nothing on the body slot
for a while, is not swimming, bathing, in an intimate scene or at home, and a guard close by can see them.
The guard walks up, a ``react`` record (``via=decency``) is stored as a ``reaction`` world event, and a
conversation starts. This module tells that guard what they saw and what they may do: the first time in a
couple of days, a warning; after that, a fine with Mantella's ReportCrime action. Rules only; the guard says
it in their own words and their own manner.

The game's start reply carries ``mantella_lw_decency_on``; ``MANTELLA_LW_DECENCY=0`` switches it off.
"""
from __future__ import annotations

import json
import os

ENV = "MANTELLA_LW_DECENCY"
KEY_DECENCY_ON = "mantella_lw_decency_on"
FRESH_DAYS = 0.05        # about an hour of game time: the stepping-in this conversation is about
MEMORY_DAYS = 2.0        # earlier warnings that still count
BOUNTY = 20              # gold, for ReportCrime


def enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in {"0", "false", "no", "off"}


def _warnings(db, player_id: int, since: float, until: float):
    return [r for r in db.execute(
        "SELECT event_id, actors_json, game_time FROM v_visible_event WHERE type = 'reaction'"
        " AND game_time > ? AND game_time <= ? AND json_extract(payload_json, '$.role') = 'decency'"
        " ORDER BY game_time DESC, event_id DESC", (since, until)).fetchall()
            if json.loads(r[1])[1:2] == [player_id]]


def line(db, guard_id: int, name: str, player_id: int, player: str, now: float) -> str | None:
    """What ``name`` (a guard who just came over) does about the naked player, or None when they did not."""
    recent = [r for r in _warnings(db, player_id, now - FRESH_DAYS, now + 1e-9) if json.loads(r[1])[0] == guard_id]
    if not recent:
        return None
    event_id, _actors, at = recent[0]
    prior = [r for r in _warnings(db, player_id, at - MEMORY_DAYS, at) if r[0] != event_id]
    text = (f"{name}, a guard, saw {player} walking about naked in public and has come over about it. {name} tells"
            f" {player} to cover up, in {name}'s own words and manner (stern, embarrassed, amused or disgusted, from"
            f" who {name} is). If {player} has a good reason (bathing, robbed, a spell gone wrong) {name} may listen.")
    if not prior:
        text += " This is the first time: a warning, no fine. Do not use ReportCrime."
    else:
        times = "once" if len(prior) == 1 else f"{len(prior)} times"
        text += (f" Guards have already had to tell {player} this {times} in the last two days, so {name} may fine"
                 f" them for it now: use ReportCrime with a bounty of {BOUNTY} gold, and say so.")
    return text
