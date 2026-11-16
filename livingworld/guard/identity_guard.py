"""Who someone is comes from the game's actor, never from what the dialogue model has them say.

Two guards can stand in one Mantella conversation and both show up to the model as "Whiterun Guard". The
second one may then say "I'm Kolbeinn" because the first just did. The database never mixes them up (each
guard is its own ``npc`` row keyed by reference ID, and ``profiles.py`` hands out distinct names), but a
false claim spoken aloud would still teach the player, and the fog, the wrong name.

This module catches that: a line in which the speaker introduces themselves by the name of another actor in
the conversation is a *false claim*. The caller then

- does not let the line teach anybody a name (``fog`` and ``guard.names`` are skipped for it),
- writes a row to ``identity_conflict`` and logs a warning, and
- sends the dialogue model a one-line correction with the player's next line.

Only self-introductions count ("I'm Kolbeinn", "my name is Kolbeinn", "call me Kolbeinn"). Talking *about*
Kolbeinn, or introducing him ("this is Kolbeinn"), is never a claim. Rules only, no model call, no
polling: it runs once per NPC line.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable

from livingworld.fog import names as N

logger = logging.getLogger("livingworld.identity")

# Phrases by which a speaker names *themselves*. Case-insensitive; the name after it is matched exactly,
# since the model spells names right. "this is X" is left out on purpose: it introduces someone else.
_SELF_INTRO = (r"(?i:my\s+name\s+is|my\s+name's|the\s+name's|the\s+name\s+is|name's|i\s+am\s+called|"
               r"i'm\s+called|they\s+call\s+me|people\s+call\s+me|you\s+can\s+call\s+me|just\s+call\s+me|"
               r"call\s+me|i\s+go\s+by|i\s+am|i['’]m)")
LINE_EXCERPT = 200


@dataclass(frozen=True)
class Claim:
    claimant_id: int
    claimant_name: str          # what the game calls the speaker ("Whiterun Guard")
    claimant_own: str | None    # the speaker's own personal name, if they have one ("Brand")
    owner_id: int
    owner_name: str             # the name that was claimed, as the owner is known ("Kolbeinn")
    claimed: str                # the words the speaker used


def _names_of(db, npc_id: int) -> list[str]:
    """Names this actor answers to: their guard name, and their real name unless it is a generic label."""
    row = db.execute("SELECT display_name, is_generic, is_player FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()
    if row is None:
        return []
    out: list[str] = []
    given = db.execute("SELECT given_name FROM guard_profile WHERE npc_id = ?", (npc_id,)).fetchone()
    if given is not None and given[0]:
        out.append(given[0])
    if not row["is_generic"] and not row["is_player"] and "guard" not in row["display_name"].lower():
        out.append(row["display_name"])
    return out


def guard_name(db, npc_id: int) -> str | None:
    """The personal name ``profiles.py`` gave this generic guard, or None."""
    row = db.execute("SELECT given_name FROM guard_profile WHERE npc_id = ?", (npc_id,)).fetchone()
    return row[0] if row and row[0] else None


def own_name(db, npc_id: int) -> str | None:
    names = _names_of(db, npc_id)
    return names[0] if names else None


def _claims(text: str, name: str) -> bool:
    """Whether ``text`` has the speaker introduce themselves as ``name`` (or the short name they go by)."""
    for call in N.call_names(name):
        pattern = _SELF_INTRO + r"\s+" + re.escape(call) + r"(?![A-Za-z\-]|['’]s\b)"
        if re.search(pattern, text):
            return True
    return False


def find_claim(db, speaker_id: int, text: str, present_ids: Iterable[int]) -> Claim | None:
    """The false claim in an NPC line, or None. ``present_ids`` are everyone in the conversation."""
    if not text:
        return None
    mine = {call.lower() for name in _names_of(db, speaker_id) for call in N.call_names(name)}
    for other in present_ids:
        if other == speaker_id:
            continue
        for name in _names_of(db, other):
            if any(call.lower() in mine for call in N.call_names(name)):
                continue   # a name the speaker really answers to too
            if _claims(text, name):
                row = db.execute("SELECT display_name FROM npc WHERE npc_id = ?", (speaker_id,)).fetchone()
                return Claim(speaker_id, row[0] if row else "someone", own_name(db, speaker_id),
                             other, name, name)
    return None


def record(db, claim: Claim, text: str, game_time: float, conv_id: int | None) -> bool:
    """Keep the attempt for debugging and log it. The caller owns the transaction. False when this very line
    was already recorded (the game side may ask about one line more than once)."""
    if db.execute("SELECT 1 FROM identity_conflict WHERE claimant_id = ? AND owner_id = ? AND game_time = ?"
                  " AND line = ? AND conv_id IS ?",
                  (claim.claimant_id, claim.owner_id, game_time, text[:LINE_EXCERPT], conv_id)).fetchone():
        return False
    db.execute("INSERT INTO identity_conflict (conv_id, game_time, claimant_id, owner_id, claimed_name, line)"
               " VALUES (?,?,?,?,?,?)",
               (conv_id, game_time, claim.claimant_id, claim.owner_id, claim.owner_name, text[:LINE_EXCERPT]))
    logger.warning("Living World identity: npc %s (%s%s) claimed the name %s, which belongs to npc %s; "
                   "the line teaches nobody a name",
                   claim.claimant_id, claim.claimant_name,
                   f", really {claim.claimant_own}" if claim.claimant_own else "", claim.owner_name, claim.owner_id)
    return True


def correction(claim: Claim) -> str:
    """One line for the dialogue model, sent with the player's next line."""
    if claim.claimant_own:
        return (f"Correction: {claim.owner_name} is a different person standing here. The one who just spoke is "
                f"{claim.claimant_own} (people only call them '{claim.claimant_name}'), not {claim.owner_name}. "
                f"{claim.claimant_own} speaks as themselves and never gives {claim.owner_name}'s name as their own.")
    return (f"Correction: {claim.owner_name} is a different person standing here, not the one who just spoke "
            f"as {claim.claimant_name}. They must not give {claim.owner_name}'s name as their own.")


def conflicts(db, limit: int = 50) -> list[dict]:
    """Recent conflicts, newest first, for the developer tools."""
    rows = db.execute(
        "SELECT c.*, a.display_name AS claimant_display, o.display_name AS owner_display"
        " FROM identity_conflict c JOIN npc a ON a.npc_id = c.claimant_id JOIN npc o ON o.npc_id = c.owner_id"
        " ORDER BY c.conflict_id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
