"""What one NPC notices right now: their own state and that of the people around them.

Added to that NPC's own prompt only. Covers:

* clothing: who, including the NPC, has no clothes or armor on their body (from the game's latest
  attire snapshot; others count only if they are in the conversation or the NPC can see them);
* drink: how much the NPC has had (``intoxication``), and how many drinks they saw others present have;
* cleanliness: how clean the NPC is, and what they notice about the people they are talking to
  (``hygiene/noticing.py``), when hygiene is on;
* privacy: which other adults are nearby but not in the conversation (from the same snapshot), so the
  NPC can judge whether a place is private, for instance before choosing to undress.

Noticing something never changes what an NPC is willing to do; the block says so whenever it has
anything to say about clothing or drink.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from livingworld.perception import formation
from livingworld.perception.attire import bare_words, is_nude, sex_of
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
    sex: str | None = None                            # as the game reported it, if it did
    size: object | None = None                        # intimacy.size.Size: a man with The New Gentleman (the player)
    is_player: bool = False
    erect: bool = False                               # the player: visibly aroused (intimacy/arousal.py)
    eyes_covered: bool = False                        # the player: eyes covered (LWCoverEyes)


def snapshot(records: list[PerceptionRecord], ids: dict[int, int], witness_ids: dict[int, int]) -> dict[int, AttireNow]:
    """The latest attire snapshot by npc_id. ``ids`` and ``witness_ids`` map game ref ids to npc_ids."""
    result: dict[int, AttireNow] = {}
    for record in records:
        npc_id = ids[record.actor.ref_id]
        seen_by = {witness_ids[w.actor.ref_id] for w in record.witnesses
                   if w.actor.ref_id in witness_ids and formation.perceives(w) is not None}
        from livingworld.intimacy import size as sizes
        size = sizes.from_scale(record.player_size) if record.actor.is_player else None
        result[npc_id] = AttireNow(npc_id, record.actor.name, record.cover != "none", seen_by, record.sex, size,
                                   record.actor.is_player, record.erect, record.eyes_covered)
    return result


def _bare(db, person: AttireNow) -> str:
    return bare_words(person.size is not None or is_nude(sex_of(db, person.npc_id, person.sex)))


def lines_for(db, npc_id: int, name: str, now_gt: float, present: dict[int, str],
              attire: dict[int, AttireNow], hygiene: bool = False) -> list[str]:
    """Prompt lines for ``npc_id``. ``present`` maps everyone in the conversation (player included) to names."""
    lines: list[str] = []
    if is_child(db, npc_id):
        return lines        # children are left out of clothing and drink awareness entirely
    mine = attire.get(npc_id)
    if mine and not mine.covered:
        lines.append(f"{name} has no clothes or armor on right now, {_bare(db, mine)}, and is aware of it.")
    for other_id, other in attire.items():
        if other_id == npc_id or other.covered or is_child(db, other_id):
            continue
        if other_id in present or npc_id in other.seen_by:
            lines.append(f"{name} can see that {other.name} has no clothes or armor on, {_bare(db, other)}.")
            if other.size is not None and sex_of(db, npc_id) == "female":
                from livingworld.intimacy import size as sizes
                lines.append(sizes.seen_line(name, other.name, other.size))
    noticed_clothing = bool(lines)
    if noticed_clothing:
        lines.append(NOTICING_UNCHANGED.format(name=name))

    from livingworld.intimacy import size as sizes
    if sizes.size_enabled():
        for other_id, other_name in present.items():
            if other_id != npc_id:
                remembered = sizes.remembered_line(db, npc_id, name, other_id, other_name)
                if remembered:
                    lines.append(remembered)

    own = drink_state(db, npc_id, now_gt).prompt_line(name)
    if own:
        lines.append(own)
    for other_id, other_name in present.items():
        if other_id == npc_id or is_child(db, other_id):
            continue
        seen = drinks_seen(db, npc_id, other_id, now_gt)
        if seen >= SEEN_DRINKS_WORTH_MENTIONING:
            lines.append(f"{name} has seen {other_name} have {seen} drinks in the last few hours.")

    if hygiene:
        from livingworld.hygiene.noticing import lines_for as hygiene_lines
        lines.extend(hygiene_lines(db, npc_id, name, now_gt, present))

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
        # the game counts people within 25 m; Mantella's own "nearby" list reaches further, so say "close by"
        return "No one else is close by; anyone further off cannot see or overhear this conversation."
    return "Also nearby, not part of this conversation: " + ", ".join(others) + "."

