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
import logging
import os
import re
from typing import Any, Iterable

from livingworld import mood as moods

ENV = "MANTELLA_LW_SURRENDER"
KEY_SURRENDER_ON = "mantella_lw_surrender_on"
ROLE = "surrender"
FRESH_DAYS = 0.05        # about an hour of game time: the conversation this record started
AFTER_DAYS = 30.0        # a background line for this long afterwards (the memory lasts on its own)
TERRIFIED = moods.Mood("terrified", 1)

logger = logging.getLogger("livingworld.surrender")


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
    home = (f" If {player_name} asks {name} to come along and {name} agrees, use Follow."
            f" If {player_name} tells {name} to stop following, use Unfollow: {name} then goes to wait at"
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


# ---- her "yes, I'll come along" really makes her follow ----------------------------------------------
# 2026-11-11 test: Salonia begged, the player took her along, she said "I'll come along", and the model never
# called Follow, so she was not a follower (Unfollow and going home then did nothing). While a surrender is
# fresh, a line of hers that goes along with the player gets Follow added, unless the player's last line
# turned her away. Begging to be taken ("Please, let me come with you?") is a question or a plea, not this.
# Later (2026-11-11 14:22, the plugin's persistent copy in Whiterun: "Can you follow me?" / "I will follow
# you", no Follow, then no way to send her off): a survivor's yes also counts when the player's own line
# just asked her to come along, and her yes to "stop following me" / "go home" gets Unfollow the same way.

FOLLOW = "mantella_npc_follow"
UNFOLLOW = "mantella_npc_unfollow"

_ACCEPT = re.compile(
    r"\b(?:i\s*'?\s*ll|i\s+will|i\s*'?\s*m\s+(?:coming|going\s+to\s+(?:come|follow|go))|i\s+shall)\s+(?:\w+\s+){0,2}?"
    r"(?:come\s+(?:with\s+you|along)|follow\s+you|go\s+with\s+you|stay\s+(?:with|close\s+to|by)\s+you|join\s+you"
    r"|accompany\s+you|be\s+at\s+your\s+side|keep\s+up)\b"
    r"|\bi\s*'?\s*m\s+(?:right\s+)?behind\s+you\b|\blead\s+the\s+way\b|\bwherever\s+you\s+(?:go|lead)\b",
    re.IGNORECASE)
_NOT_NOW = re.compile(r"\b(?:not|never|if|unless|later|once|when)\b|n\s*'?\s*t\b", re.IGNORECASE)
_PLAYER_NO = re.compile(r"^\W*(?:no|nope|nah|never)\b|\b(?:go\s+away|leave\s+me|get\s+lost|begone|be\s+gone|on\s+your\s+own"
                        r"|you\s+can\s*'?\s*t\s+come|you\s+cannot\s+come|not\s+coming|stay\s+here|don\s*'?\s*t\s+follow"
                        r"|do\s+not\s+follow|run\s+along|off\s+with\s+you|i\s+don\s*'?\s*t\s+need\s+you)\b", re.IGNORECASE)
_PLAYER_ASKS = re.compile(
    r"\b(?:follow\s+me|come\s+(?:with\s+me|along|on)|join\s+me|travel\s+with\s+me|tag\s+along|stay\s+(?:with|close\s+to)\s+me"
    r"|let\s*'?\s*s\s+go|you\s*'?\s*re\s+coming|keep\s+up|with\s+me)\b", re.IGNORECASE)
_PLAYER_SENDS = re.compile(
    r"\b(?:stop\s+following|quit\s+following|don\s*'?\s*t\s+follow|do\s+not\s+follow|go\s+home|head\s+home"
    r"|wait\s+(?:for\s+me\s+)?(?:at|in)\s+(?:my|the|our)\b|go\s+(?:to|back\s+to)\s+(?:my|the|our)\s+(?:house|home|inn)"
    r"|go\s+wait|stay\s+(?:here|behind|at\s+(?:my|the|our))|part\s+ways|on\s+your\s+own\s+now|you\s+can\s+go)\b",
    re.IGNORECASE)
_GOES = re.compile(
    r"\b(?:i\s*'?\s*ll|i\s+will|i\s+shall)\s+(?:\w+\s+){0,2}?(?:go|head|wait|leave|stay|be\s+(?:at|waiting))\b"
    r"|\bas\s+you\s+(?:wish|say|command)\b|\bvery\s+well\b|\bif\s+you\s+say\s+so\b|\bfarewell\b|\bgoodbye\b"
    r"|\bi\s*'?\s*m\s+(?:going|off|heading)\b", re.IGNORECASE)
_REFUSES = re.compile(r"\b(?:no|not|never|won\s*'?\s*t|refuse|can\s*'?\s*t)\b|n\s*'?\s*t\b", re.IGNORECASE)


def asked_to_follow(player_text: str | None) -> bool:
    text = (player_text or "").replace("’", "'")
    return bool(_PLAYER_ASKS.search(text)) and not turned_away(text) and not sent_away(text)


def sent_away(player_text: str | None) -> bool:
    return bool(_PLAYER_SENDS.search((player_text or "").replace("’", "'")))


def goes(text: str) -> bool:
    """A line that goes along with being sent off: "As you wish, I'll wait at the house"."""
    text = (text or "").replace("’", "'")
    if not text.strip() or text.rstrip().endswith("?"):
        return False
    m = _GOES.search(text)
    return bool(m) and not _REFUSES.search(_sentence_at(text, m.start()))


def _sentence_at(text: str, at: int) -> str:
    start = max([text.rfind(c, 0, at) for c in ".!?;"]) + 1
    ends = [i for i in (text.find(c, at) for c in ".!?;") if i >= 0]
    return text[start:min(ends) if ends else len(text)]


def is_survivor(db, survivor: int, player: int, now: float) -> bool:
    return _latest(db, survivor, player, now) is not None


def is_fresh(db, survivor: int, player: int, now: float) -> bool:
    """Whether ``survivor`` surrendered to the player within this conversation's span (``FRESH_DAYS``)."""
    found = _latest(db, survivor, player, now)
    return found is not None and now - found[1] <= FRESH_DAYS


def accepts(text: str) -> bool:
    """A line that goes along with being taken: "Thank you, I'll come along", "I'll follow you anywhere"."""
    text = (text or "").replace("’", "'")
    if not text.strip() or text.rstrip().endswith("?"):
        return False
    m = _ACCEPT.search(text)
    if not m:
        return False
    clause = re.split(r"[.!?;]", text[m.start():], maxsplit=1)[0]
    before = re.split(r"[.!?;]", text[:m.start()])[-1]
    return not _NOT_NOW.search(clause) and not re.search(r"\b(?:let\s+me|please|could\s+i|may\s+i|can\s+i)\b", before,
                                                         re.IGNORECASE)


def turned_away(player_text: str | None) -> bool:
    return bool(player_text) and bool(_PLAYER_NO.search(player_text.replace("’", "'")))


class FollowWatcher:
    """In the reply parser chain (``promises.make_parser`` ``also``): adds Follow to a survivor's line that
    accepts coming along, and Unfollow to one that accepts being sent off.

    ``pending``: survivors whose surrender is fresh and who are not following yet (their yes counts on its own);
    ``survivors``: every survivor in the conversation (their yes counts when the player's last line asked);
    ``done`` / ``sent``: who got Follow / Unfollow in this conversation. All shared and updated in place."""

    def __init__(self, pending: set[str], player_said: str | None, offered: Iterable[str], done: set[str] | None = None,
                 survivors: set[str] | None = None, sent: set[str] | None = None):
        offered = set(offered)
        self.pending = pending
        self.done = done if done is not None else set()
        self.survivors = survivors if survivors is not None else set(pending)
        self.sent = sent if sent is not None else set()
        self.player_said = player_said
        self.offered = FOLLOW in offered
        self.offered_unfollow = UNFOLLOW in offered

    def note_actions(self, actions: Iterable[Any]) -> None:
        for action in actions:
            if not isinstance(action, dict):
                continue
            if action.get("identifier") == FOLLOW:
                for name in _sources(action):
                    self.pending.discard(name)
                    self.done.add(name)
                    self.sent.discard(name)
            elif action.get("identifier") == UNFOLLOW:
                for name in _sources(action):
                    self.sent.add(name)
                    self.done.discard(name)

    def keep(self, speaker_name: str, text: str, sentence_actions: list) -> list[str]:
        self.note_actions(sentence_actions)
        if speaker_name not in self.survivors and speaker_name not in self.pending:
            return []
        if sent_away(self.player_said):
            if not self.offered_unfollow or speaker_name in self.sent or not goes(text):
                return []
            sentence_actions.append({"identifier": UNFOLLOW, "arguments": {"source": [speaker_name]}})
            self.note_actions(sentence_actions[-1:])
            self.pending.discard(speaker_name)
            logger.log(23, f"Living World surrender: {speaker_name} agreed to stop following; Unfollow")
            return [UNFOLLOW]
        if not self.offered or speaker_name in self.done or turned_away(self.player_said) or not accepts(text):
            return []
        if speaker_name not in self.pending and not asked_to_follow(self.player_said):
            return []
        sentence_actions.append({"identifier": FOLLOW, "arguments": {"source": [speaker_name]}})
        self.note_actions(sentence_actions[-1:])
        logger.log(23, f"Living World surrender: {speaker_name} agreed to come along; Follow")
        return [FOLLOW]


def _sources(action: dict) -> list[str]:
    src = (action.get("arguments") or {}).get("source")
    if isinstance(src, str):
        return [src]
    return list(src or [])
