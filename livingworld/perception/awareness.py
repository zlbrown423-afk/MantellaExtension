"""What one NPC notices right now: their own state and that of the people around them.

Added to that NPC's own prompt only. Covers:

* clothing: who, including the NPC, has no clothes or armor on their body (from the game's latest
  attire snapshot; others count only if they are in the conversation or the NPC can see them);
* drink: how much the NPC has had (``intoxication``), and how many drinks they saw others present have;
* privacy: which other adults are nearby but not in the conversation (from the same snapshot), so the
  NPC can judge whether a place is private, for instance before choosing to undress.

Noticing something never changes what an NPC is willing to do; the block says so whenever it has
anything to say about clothing or drink.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from livingworld.perception import formation
from livingworld.perception.intoxication import drink_state, drinks_seen
from livingworld.perception.records import PerceptionRecord
from livingworld.store.minors import is_child

SEEN_DRINKS_WORTH_MENTIONING = 2
NOTICING_UNCHANGED = "Noticing this does not change what {name} wants or is willing to do."


@dataclass
class AttireNow:
    npc_id: int
    name: str
    covered: bool
    seen_by: set[int] = field(default_factory=set)   # npc_ids who can see this person right now


def snapshot(records: list[PerceptionRecord], ids: dict[int, int], witness_ids: dict[int, int]) -> dict[int, AttireNow]:
    """The latest attire snapshot by npc_id. ``ids`` and ``witness_ids`` map game ref ids to npc_ids."""
    result: dict[int, AttireNow] = {}
    for record in records:
        npc_id = ids[record.actor.ref_id]
        seen_by = {witness_ids[w.actor.ref_id] for w in record.witnesses
                   if w.actor.ref_id in witness_ids and formation.perceives(w) is not None}
        result[npc_id] = AttireNow(npc_id, record.actor.name, record.cover != "none", seen_by)
    return result


def lines_for(db, npc_id: int, name: str, now_gt: float, present: dict[int, str],
              attire: dict[int, AttireNow]) -> list[str]:
    """Prompt lines for ``npc_id``. ``present`` maps everyone in the conversation (player included) to names."""
    lines: list[str] = []
    if is_child(db, npc_id):
        return lines        # children are left out of clothing and drink awareness entirely
    mine = attire.get(npc_id)
    if mine and not mine.covered:
        lines.append(f"{name} has no clothes or armor on right now, only underclothes, and is aware of it.")
    for other_id, other in attire.items():
        if other_id == npc_id or other.covered or is_child(db, other_id):
            continue
        if other_id in present or npc_id in other.seen_by:
            lines.append(f"{name} can see that {other.name} has no clothes or armor on, only underclothes.")
    noticed_clothing = bool(lines)
    if noticed_clothing:
        lines.append(NOTICING_UNCHANGED.format(name=name))

    own = drink_state(db, npc_id, now_gt).prompt_line(name)
    if own:
        lines.append(own)
    for other_id, other_name in present.items():
        if other_id == npc_id or is_child(db, other_id):
            continue
        seen = drinks_seen(db, npc_id, other_id, now_gt)
        if seen >= SEEN_DRINKS_WORTH_MENTIONING:
            lines.append(f"{name} has seen {other_name} have {seen} drinks in the last few hours.")

    privacy = privacy_line(db, present, attire)
    if privacy:
        lines.append(privacy)
    return lines


def privacy_line(db, present: dict[int, str], attire: dict[int, AttireNow]) -> str | None:
    """Who else is close by. The game sends a snapshot for every adult nearby, so it doubles as a head count."""
    if not attire:
        return None         # no snapshot from the game: say nothing rather than guess
    others = sorted({a.name for i, a in attire.items() if i not in present and not is_child(db, i)})
    if not others:
        return "No one else is nearby; only the people in this conversation are here."
    return "Also nearby, not part of this conversation: " + ", ".join(others) + "."

