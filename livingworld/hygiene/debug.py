"""Test commands for hygiene, said (or typed) to an NPC during a conversation.

* "debug max dirt"            - makes the player filthy and bloodied
* "debug max dirt on Hod"     - the same for an NPC in the conversation (named anywhere in the line)
* "debug clean" / "debug clean Hod" - a bath, right now

The words "debug" (or "test") and "max dirt" / "filthy" / "clean" must all be in the line, so normal speech
never triggers it. Anyone made filthy this way is noticed afresh, even by people who already noticed them.
Switch the commands off with MANTELLA_LW_HYGIENE_DEBUG=0.
"""
from __future__ import annotations

import re

from livingworld.hygiene import model

DEBUG_ENV = "MANTELLA_LW_HYGIENE_DEBUG"


def parse(text: str) -> str | None:
    """'dirty', 'clean' or None."""
    words = " ".join(re.findall(r"[a-z]+", (text or "").lower()))
    if not re.search(r"\b(debug|test)\b", words):
        return None
    if re.search(r"\bmax(imum)? dirt\b|\bfilthy\b|\bmax dirty\b", words):
        return "dirty"
    if re.search(r"\bclean\b", words):
        return "clean"
    return None


def target(text: str, present: dict[int, str], player_id: int) -> int:
    """The NPC named in the line, else the player."""
    lowered = (text or "").lower()
    for npc_id, name in present.items():
        if npc_id != player_id and name and re.search(rf"\b{re.escape(name.lower())}\b", lowered):
            return npc_id
    return player_id


def apply(db, command: str, npc_id: int, now: float) -> None:
    model.ensure_seeded(db, npc_id, now)
    if command == "dirty":
        model._insert(db, npc_id, now, "debug", 1.0, 1.0)
        db.execute("DELETE FROM hygiene_notice WHERE subject_id = ?", (npc_id,))
    else:
        model._insert(db, npc_id, now, "bath")
