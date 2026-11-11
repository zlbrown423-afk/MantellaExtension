"""Surrender: a survivor whose party has just been cut down comes to the player and begs to be spared.

Built for Salonia Carvain (the Dark Brotherhood wedding-guest encounter: Plautis and Salonia Carvain on the road
with a hired bodyguard), but nothing here is about her by name. A small game plugin watches the party; once
the husband and the bodyguard are dead and the survivor is alive and close, it sends ``LivingWorld_Surrender``.
Mantella-Spell (``LwSurrender``) stops the survivor fighting or fleeing, walks them to the player, queues a
``react`` record (``via=surrender``) and starts a conversation with them::

    v=1|key=812345-12|type=react|gt=4.4|loc=Haafingar|actor=<survivor>|target=<player>|via=surrender
      |party=<husband>|d=<bodyguard>|killer=player

``party`` is the person the survivor lost who mattered most (the husband), each ``d`` another dead
companion, ``killer`` is ``player`` when the player (or their followers) killed them, ``other`` when
something else did (bandits, a dragon) and the survivor is simply alone and helpless.

The record is stored as a ``reaction`` world event (role ``surrender``). Here the survivor gets a lasting
memory of it and starts the conversation terrified (the mood meter), and their prompt says what they do:
submit, beg for their life and offer to come along, because they have nobody and nowhere left. If the
player accepts, they use Follow and stay with the player as an ordinary follower (Mantella's Follow:
Unfollow later sends them away). If the player refuses or threatens them, they may Flee. Later
conversations still know how they came to be with the player; the memory does the rest.

``MANTELLA_LW_SURRENDER=0`` switches it off (the start reply's ``mantella_lw_surrender_on`` tells the game).
"""
from __future__ import annotations

import json
import os

from livingworld import mood as moods

ENV = "MANTELLA_LW_SURRENDER"
KEY_SURRENDER_ON = "mantella_lw_surrender_on"
ROLE = "surrender"
FRESH_DAYS = 0.05        # about an hour of game time: the conversation this record started
AFTER_DAYS = 30.0        # a background line for this long afterwards (the memory lasts on its own)
TERRIFIED = moods.Mood("terrified", 1)


def enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in {"0", "false", "no", "off"}


def _names(db, ids: list[int]) -> list[str]:
    out = []
    for i in ids:
        row = db.execute("SELECT display_name FROM npc WHERE npc_id = ?", (i,)).fetchone()
        if row and row[0]:
            out.append(row[0])
    return out


def _join(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def remember(db, timeline: int, event_id: int, survivor: int, player: int, dead: list[int], killer: str,
             location: str | None, gt: float) -> None:
    """The survivor's memory of it and their mood going into the conversation."""
    names = _names(db, dead)
    player_name = (_names(db, [player]) or ["the stranger"])[0]
    lost = _join(names) or "everyone travelling with them"
    if killer == "player":
        content = (f"{player_name} killed {lost} in front of me{(' at ' + location) if location else ''}. I was left"
                   f" alone, and I begged {player_name} to spare me.")
        valence = -1.0
    else:
        content = (f"{lost} died{(' at ' + location) if location else ''}, and I was left alone on the road. I"
                   f" turned to {player_name} and begged for help.")
        valence = -0.8
    mem_id = db.execute(
        "INSERT INTO memory (timeline_id, owner_id, kind, content, about_ids_json, location, occurred_gt, importance,"
        " valence, intensity, created_by, source_kind, interpretation, confidence, event_id)"
        " VALUES (?, ?, 'episodic', ?, ?, ?, ?, 1.0, ?, 1.0, 'perception', 'experienced', ?, 1.0, ?)",
        (timeline, survivor, content, json.dumps([player, *dead]), location, gt, valence,
         "the worst day of my life" if killer == "player" else "grief and fear", event_id)).lastrowid
    db.execute("INSERT INTO memory_source (mem_id, event_id) VALUES (?,?)", (mem_id, event_id))
    moods.save(db, survivor, TERRIFIED, gt)


def _latest(db, survivor: int, player: int, until: float):
    """The survivor's most recent surrender to the player before ``until``: (payload, game time) or None."""
    for actors_json, payload_json, at in db.execute(
            "SELECT actors_json, payload_json, game_time FROM v_visible_event WHERE type = 'reaction'"
            " AND json_extract(payload_json, '$.role') = ? AND game_time <= ? ORDER BY game_time DESC, event_id DESC",
            (ROLE, until + 1e-9)).fetchall():
        actors = json.loads(actors_json)
        if actors[:2] == [survivor, player]:
            return json.loads(payload_json or "{}"), at
    return None


def line(db, survivor: int, name: str, player: int, player_name: str, now: float) -> str | None:
    """What ``name`` is going through with the player, or None when they never surrendered to them."""
    found = _latest(db, survivor, player, now)
    if found is None:
        return None
    payload, at = found
    if now - at > AFTER_DAYS:
        return None
    dead = _names(db, payload.get("dead") or [])
    lost = _join(dead) or "everyone travelling with them"
    by_player = payload.get("killer") == "player"
    home = (f" If {player_name} tells {name} to stop following, use Unfollow: {name} then goes to wait at"
            f" {player_name}'s own house, or takes a room at the Bannered Mare in Whiterun if {player_name} has none.")
    if now - at > FRESH_DAYS:
        if by_player:
            return (f"{name} has not forgotten that {player_name} killed {lost}. If {name} is travelling with"
                    f" {player_name} now, it began as surrender and fear: {name} stays wary and careful around"
                    f" {player_name}, and any warmth has to be earned, slowly." + home)
        return (f"{name} lost {lost} and turned to {player_name}, alone and with nowhere to go. If {name} is"
                f" travelling with {player_name} now, it began out of need, and {name} is still grieving." + home)
    if by_player:
        text = (f"{player_name} has just killed {lost}, right in front of {name}. {name} is alone, unarmed in all"
                f" that matters, and terrified for {name}'s own life. {name} does not attack and does not run: {name}"
                f" submits, drops to {name}'s knees if need be, and begs {player_name} to spare {name}. {name} is"
                f" grieving and in shock, and may cry, plead or bargain, in {name}'s own words and manner. {name} has"
                f" nobody and nowhere left, so {name} offers to come with {player_name}, to be useful, to do"
                f" whatever is asked, as long as {name} is allowed to live.")
    else:
        text = (f"{lost} {'is' if len(dead) == 1 else 'are'} dead, and {name} is left alone and helpless on the road."
                f" {name} comes to {player_name}, frightened and grieving, begs for protection and offers to come"
                f" along, because {name} has nobody and nowhere left.")
    return text + (f" If {player_name} agrees to take {name} along, use Follow. If {player_name} refuses, sends"
                   f" {name} away or threatens {name}, {name} may use Flee. Never use Attack.")
