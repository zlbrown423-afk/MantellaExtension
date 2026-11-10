"""Public decency: a guard who sees the player walking about naked comes over and tells them to cover up.

The game decides on the spot (Mantella-Spell ``LwDecencyTick``): the player has had nothing on the body slot
for a few seconds, is not swimming, bathing, in an intimate scene or at home, and a guard who can see them is
close by. The guard comes over at a run, a ``react`` record (``via=decency``) is stored as a ``reaction``
world event, and a conversation starts. This module tells that guard what they saw and what they may do:

* The first time in a couple of days: a warning.
* After that: a fine of ``FINE`` gold. The guard asks for it on the spot, and the player can pay it there
  and then (the guard's ``PayFine`` action: the game moves the gold from the player to the guard, as much
  of it as the player has). Only if the player refuses does the guard put it on their bounty (ReportCrime).
* A thane of the guard's hold is never fined: the guard addresses them as their Thane, asks them to
  cover up with deference, and after repeated times says the Jarl may come to hear of it.

Rules only; the guard says it in their own words and their own manner.

The game's start reply carries ``mantella_lw_decency_on``; ``MANTELLA_LW_DECENCY=0`` switches it off.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

ENV = "MANTELLA_LW_DECENCY"
KEY_DECENCY_ON = "mantella_lw_decency_on"
FINE_ACTION = "mantella_lw_decency_fine"   # livingworld/actions/decency_fine.json
KEY_FINE_GOLD = "mantella_lw_fine_gold"    # with the guard's line: gold the game takes from the player
KEY_FINE_GUARD = "mantella_lw_fine_guard"  # and the guard it goes to (their name in the conversation)
FRESH_DAYS = 0.05        # about an hour of game time: the stepping-in this conversation is about
MEMORY_DAYS = 2.0        # earlier warnings that still count
FINE = 20                # gold, paid on the spot or put on the bounty with ReportCrime
EVENT_FINE = "decency.fine"


def enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in {"0", "false", "no", "off"}


def _warnings(db, player_id: int, since: float, until: float):
    return [r for r in db.execute(
        "SELECT event_id, actors_json, game_time FROM v_visible_event WHERE type = 'reaction'"
        " AND game_time > ? AND game_time <= ? AND json_extract(payload_json, '$.role') = 'decency'"
        " ORDER BY game_time DESC, event_id DESC", (since, until)).fetchall()
            if json.loads(r[1])[1:2] == [player_id]]


def _fresh(db, guard_id: int, player_id: int, now: float):
    recent = [r for r in _warnings(db, player_id, now - FRESH_DAYS, now + 1e-9) if json.loads(r[1])[0] == guard_id]
    return recent[0] if recent else None


def _prior(db, player_id: int, event_id: int, at: float) -> list:
    return [r for r in _warnings(db, player_id, at - MEMORY_DAYS, at) if r[0] != event_id]


def _paid(db, reaction_id: int) -> bool:
    return db.execute("SELECT 1 FROM v_visible_event WHERE type = ? AND json_extract(payload_json, '$.reaction') = ?",
                      (EVENT_FINE, reaction_id)).fetchone() is not None


def fine_due(db, guard_id: int, player_id: int, now: float, thane: bool = False) -> int:
    """The fine ``guard_id`` may ask of the player right now (0: a warning, a thane, or already paid)."""
    fresh = _fresh(db, guard_id, player_id, now)
    if fresh is None or thane:
        return 0
    event_id, _actors, at = fresh
    if not _prior(db, player_id, event_id, at) or _paid(db, event_id):
        return 0
    return FINE


def pay(db, guard_id: int, player_id: int, now: float, location: str | None, thane: bool = False) -> int:
    """The player agreed to pay: stores it and returns the gold the game should take (0: nothing owed)."""
    amount = fine_due(db, guard_id, player_id, now, thane)
    if not amount:
        return 0
    event_id = _fresh(db, guard_id, player_id, now)[0]
    timeline = db.execute("SELECT timeline_id FROM timeline WHERE is_active = 1").fetchone()[0]
    db.execute(
        "INSERT INTO world_event (timeline_id, type, location, actors_json, payload_json, source, game_time,"
        " real_time, confidence) VALUES (?,?,?,?,?,?,?,?,?)",
        (timeline, EVENT_FINE, location, json.dumps([guard_id, player_id]),
         json.dumps({"reaction": event_id, "gold": amount}), "game", now,
         datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), 1.0))
    return amount


def line(db, guard_id: int, name: str, player_id: int, player: str, now: float, thane_of: str | None = None) -> str | None:
    """What ``name`` (a guard who just came over) does about the naked player, or None when they did not.
    ``thane_of``: the hold, when the player is a thane of the hold this guard serves."""
    fresh = _fresh(db, guard_id, player_id, now)
    if fresh is None:
        return None
    event_id, _actors, at = fresh
    prior = _prior(db, player_id, event_id, at)
    times = "once" if len(prior) == 1 else f"{len(prior)} times"
    if thane_of:
        text = (f"{name}, a guard, saw {player} walking about naked in public and has come over about it. {player} is"
                f" Thane of {thane_of}: {name} addresses {player} as 'my Thane', with deference, and asks them to cover"
                f" up, for their own dignity and the hold's. A thane is never fined or arrested for this: do not use"
                f" ReportCrime or PayFine.")
        if prior:
            text += (f" Guards have had to ask {player} this {times} in the last two days; {name} may say, politely,"
                     f" that the Jarl could come to hear of it.")
        return text
    text = (f"{name}, a guard, saw {player} walking about naked in public and has come over about it. {name} tells"
            f" {player} to cover up, in {name}'s own words and manner (stern, embarrassed, amused or disgusted, from"
            f" who {name} is). If {player} has a good reason (bathing, robbed, a spell gone wrong) {name} may listen.")
    if not prior:
        return text + " This is the first time: a warning, no fine. Do not use ReportCrime or PayFine."
    if _paid(db, event_id):
        return text + f" {player} has already paid the fine for this; {name} only wants them covered up now."
    return text + (f" Guards have already had to tell {player} this {times} in the last two days, so {name} fines them"
                   f" {FINE} gold and asks for it now. If {player} agrees to pay on the spot, use PayFine. Only if"
                   f" {player} refuses to pay, use ReportCrime with a bounty of {FINE} gold instead, and say so.")
