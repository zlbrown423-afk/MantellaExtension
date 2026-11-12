"""This conversation's touch agreements, on the server: what each NPC agreed to, sent to the game, logged.

``Desk.handle`` reads an NPC line's actions (``AllowTouch`` / ``StopTouch``, from the dialogue model or
from ``words.py``) and returns the keys for the game. Each agreement and each withdrawal is written to
``consent_decision`` (kind ``touch``) with what was agreed and how, so it can be checked afterwards.

The game keeps the agreement itself (for the conversation and a few minutes after, or the minutes the NPC
named) and clears it on every load: it is never carried over into another save.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from livingworld import touch as T
from livingworld.perception import intoxication
from livingworld.social.consent import DRUNK_STAGES
from livingworld.store.minors import is_child

logger = logging.getLogger("livingworld.touch")


@dataclass
class Grant:
    name: str
    kinds: list[str]
    minutes: int = 0


@dataclass
class Desk:
    grants: dict[int, Grant] = field(default_factory=dict)   # npc_id -> what they agreed to, this conversation
    _handled: set[int] = field(default_factory=set)          # id(sentence) already read
    notes: list[str] = field(default_factory=list)           # for the dialogue model with the player's next line

    def handle(self, db, sentence_id: int, actions: list[dict], npc_id: int, name: str, other_id: int | None,
               player_said: str | None, now: float) -> dict[str, Any]:
        """Keys for the game from this NPC line's touch actions (each line read once)."""
        if sentence_id in self._handled:
            return {}
        mine = [a for a in actions if a.get("identifier") in (T.ALLOW, T.WITHDRAW)]
        if not mine:
            return {}
        self._handled.add(sentence_id)
        if is_child(db, npc_id):
            return {}
        if any(a.get("identifier") == T.WITHDRAW for a in mine):
            return self._withdraw(db, npc_id, name, other_id, now)
        allow = next(a for a in mine if a.get("identifier") == T.ALLOW)
        return self._allow(db, npc_id, name, other_id, allow.get("arguments") or {}, player_said, now)

    def _allow(self, db, npc_id: int, name: str, other_id: int | None, args: dict, player_said: str | None,
               now: float) -> dict[str, Any]:
        from livingworld.touch.words import asked_kinds
        kinds = T.normalize_kinds(args.get("touch"))
        via = str(args.get("via") or "action")
        if not kinds:
            kinds = asked_kinds(player_said) or ["hug"]   # "AllowTouch:" with no kinds answers what was asked
        dropped = []
        stage = intoxication.drink_state(db, npc_id, now).stage
        if stage in DRUNK_STAGES:
            dropped = [k for k in kinds if k in T.INTIMATE]   # no clear yes to those while drunk
            kinds = [k for k in kinds if k not in T.INTIMATE]
        minutes = _minutes(args.get("minutes"))
        reasons = {"kinds": kinds, "via": via, "minutes": minutes, "drink_stage": stage}
        if dropped:
            reasons["not_while_drunk"] = dropped
        if player_said:
            reasons["asked"] = player_said[:200]
        _log(db, npc_id, other_id, "invite", "yes" if kinds else "no", reasons, now)
        if not kinds:
            logger.log(23, f"Living World touch: {name} is too drunk to agree to {', '.join(dropped)}; not sent")
            return {}
        old = self.grants.get(npc_id)
        if old is not None:
            kinds = T.normalize_kinds(old.kinds + kinds)
            minutes = max(minutes, old.minutes)
        self.grants[npc_id] = Grant(name, kinds, minutes)
        self.notes.append(f"{name} has agreed to be touched in these ways: {', '.join(kinds)}"
                          f"{f' (for {minutes} minutes)' if minutes else ''}. Anything further is not agreed, and "
                          f"{name} can take it back at any moment.")
        logger.log(23, f"Living World touch: {name} agreed to {', '.join(kinds)}"
                       f" ({f'{minutes} min' if minutes else 'this conversation'}, via {via})")
        return {T.KEY_ALLOW: [f"{name}|{','.join(kinds)}|{minutes}"]}

    def _withdraw(self, db, npc_id: int, name: str, other_id: int | None, now: float) -> dict[str, Any]:
        had = self.grants.pop(npc_id, None)
        self.notes.append(f"{name} has taken back any agreement to be touched; hands on {name} now are unwelcome.")
        _log(db, npc_id, other_id, "during", "withdrew", {"kinds": had.kinds if had else []}, now)
        logger.log(23, f"Living World touch: {name} took back their agreement")
        return {T.KEY_WITHDRAW: [name]}

    def breach(self, npc_id: int) -> None:
        """The game reported a hand where they had not agreed: the agreement is over (the game ended it too)."""
        grant = self.grants.pop(npc_id, None)
        if grant is not None:
            self.notes.append(f"{grant.name}'s agreement to be touched is over: the hands went further than "
                              f"{grant.name} agreed to.")

    def take_notes(self) -> list[str]:
        notes, self.notes = self.notes, []
        return notes

    def prompt_lines(self, npc_id: int, name: str, player_name: str) -> list[str]:
        grant = self.grants.get(npc_id)
        if grant is None:
            return []
        how_long = f"for the next {grant.minutes} minutes" if grant.minutes else "for now"
        return [f"{name} has agreed to let {player_name} {T.describe(grant.kinds)}, {how_long}. It is {name}'s "
                f"own choice: {name} can take it back at any moment (StopTouch), and anything further is not "
                f"agreed."]


def _minutes(value) -> int:
    try:
        return max(0, min(T.MAX_MINUTES, int(float(value))))
    except (TypeError, ValueError):
        return 0


def _log(db, npc_id: int, other_id: int | None, phase: str, answer: str, reasons: dict, now: float) -> None:
    if other_id is None:
        return
    db.execute("INSERT INTO consent_decision (timeline_id, encounter_id, npc_id, other_id, kind, phase, answer,"
               " reasons_json, game_time) VALUES ((SELECT timeline_id FROM timeline WHERE is_active = 1),"
               " NULL,?,?,?,?,?,?,?)", (npc_id, other_id, "touch", phase, answer, json.dumps(reasons), now))
