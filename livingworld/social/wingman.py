"""Wingman: a companion who has made it their business to help the player win women over.

Salonia Carvain's idea of being useful once she has begged for her life and come along (``surrender.py``): she
insists on being the player's wingman. In any conversation the player holds with her and a woman, she talks
the player up, nudges the woman, sets up openings and covers awkward silences. How far she goes depends on
reading the room, from what the Living World already knows:

* the woman's mood right now (the mood meter: what she was left in, if anything) and how attracted she is to
  the player (``relationship.attraction``);
* whether the woman is spoken for (a spouse or a partner, from ``intimacy.common.partner_in``), and whether
  that partner is standing right there;
* who else is listening: a guard in the conversation, how many other people, any child (then nothing
  suggestive at all).

Bold, lewd banter is only for a woman who is clearly receptive, with no guard and at most one other
person listening. Otherwise she stays charming and suggestive at most; with a cold, upset or taken woman she
backs off or turns it into a joke. Rules only; she says it in her own words.

Who counts as a wingman: anyone who surrendered to the player (a ``reaction`` event, role ``surrender``).
``MANTELLA_LW_WINGMAN=0`` switches it off.
"""
from __future__ import annotations

import json
import os

from livingworld import mood as moods
from livingworld.store.minors import is_child

ENV = "MANTELLA_LW_WINGMAN"
RECEPTIVE_MOOD = 7       # mood meter score (0..10) from which a woman is warm enough for bold talk
COLD_MOOD = 3            # at or below: she is upset or hostile, back off
ATTRACTED = 0.4          # relationship.attraction (0..1) that counts as interested in the player
MAX_LISTENERS = 1        # other people (besides the player, the wingman and the woman) for lewd talk


def enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in {"0", "false", "no", "off"}


def is_wingman(db, npc_id: int, player_id: int) -> bool:
    from livingworld.social.surrender import ROLE
    for (actors,) in db.execute(
            "SELECT actors_json FROM v_visible_event WHERE type = 'reaction' AND json_extract(payload_json, '$.role') = ?",
            (ROLE,)).fetchall():
        if json.loads(actors)[:2] == [npc_id, player_id]:
            return True
    return False


def _sex(db, npc_id: int) -> str | None:
    row = db.execute("SELECT sex FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()
    return row[0] if row else None


def _attraction(db, woman: int, player_id: int) -> float:
    row = db.execute("SELECT attraction FROM v_relationship WHERE from_id = ? AND to_id = ?",
                     (woman, player_id)).fetchone()
    return float(row[0]) if row and row[0] is not None else 0.0


def _partner(db, woman: int, others: list[int]) -> tuple[int | None, bool]:
    """(the woman's partner, whether they are here): from the people here first, then anyone at all."""
    from livingworld.intimacy.common import partner_in
    here = partner_in(db, woman, others)
    if here is not None:
        return here, True
    row = db.execute("SELECT to_id FROM v_relationship WHERE from_id = ? AND (kinship = 'spouse'"
                     " OR status IN ('spouse', 'partner', 'courting')) LIMIT 1", (woman,)).fetchone()
    return (row[0], False) if row else (None, False)


def line(db, me: int, name: str, player_id: int, player_name: str, present: dict[int, str], guards: set[int],
         now: float) -> str | None:
    """How ``name`` plays wingman in this conversation, or None when they are not the player's wingman."""
    if not is_wingman(db, me, player_id):
        return None
    others = [i for i in present if i not in (me, player_id)]
    if any(is_child(db, i) for i in others):
        return (f"{name} sees to it that {player_name} makes a good impression, but there is a child here:"
                f" nothing suggestive, not a word of it.")
    women = [i for i in others if _sex(db, i) == "female"]
    base = (f"{name} has made it {name}'s business to be {player_name}'s wingman and will not hear otherwise: {name}"
            f" talks {player_name} up, sets up openings and covers awkward moments, in {name}'s own way.")
    if not women:
        return base + f" There is no woman here to win over right now, so {name} keeps it to a remark or two at most."
    guard_here = bool(guards & set(others))
    parts = [base]
    for w in women:
        wname = present[w]
        carried = moods.carried_over(db, w, now)
        score = carried.score if carried else moods.NEUTRAL
        attraction = _attraction(db, w, player_id)
        partner, partner_here = _partner(db, w, [i for i in others if i != w] + [player_id])
        listeners = len([i for i in others if i != w])
        if partner == player_id:
            parts.append(f"{wname} is already with {player_name}; {name} teases them both about it instead.")
            continue
        if score <= COLD_MOOD:
            parts.append(f"{wname} is in no mood ({carried.label if carried else 'cold'}): {name} reads that and"
                         f" backs off, or makes light of it, rather than pushing.")
            continue
        if partner is not None and partner_here:
            parts.append(f"{wname}'s partner, {present.get(partner, 'her partner')}, is standing right here: {name}"
                         f" keeps it to harmless compliments, nothing that could start a fight.")
            continue
        receptive = score >= RECEPTIVE_MOOD or attraction >= ATTRACTED
        taken = " (she is spoken for, so lightly)" if partner is not None else ""
        if receptive and not guard_here and listeners <= MAX_LISTENERS and partner is None:
            parts.append(f"{wname} seems receptive and it is private enough: {name} can be bold and openly flirtatious"
                         f" for {player_name}, even lewd if {wname} plays along, and gauges {wname}'s reaction as"
                         f" {name} goes; if {wname} bristles, {name} pulls back at once.")
        elif guard_here:
            parts.append(f"A guard is listening: {name} works on {wname}{taken} with charm and a wink, nothing crude.")
        elif receptive:
            parts.append(f"{wname} seems warm to {player_name}{taken}, but there are others about: {name} keeps it"
                         f" suggestive and playful, not crude.")
        else:
            parts.append(f"{name} cannot tell yet how {wname} feels about {player_name}{taken}: {name} tests the"
                         f" water with a compliment or a well-placed story about {player_name} first.")
    return " ".join(parts)
