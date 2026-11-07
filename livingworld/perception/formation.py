"""One event, many perspectives: who perceived it, what each of them remembers, and what they make of it.

All of this is local rules and templates, no LLM call. A witness never learns the actor's intent;
what they make of it comes from their own relationships (``v_relationship``), the duration, the body
part and how far away they stood. Memory text is first person, the way ``recall`` shows it in prompts.
"""
from __future__ import annotations

from dataclasses import dataclass

from livingworld.perception.records import (SENSITIVE_PARTS, PerceptionRecord, Witness, body_part,
                                            consume_verb, item_category)

# What a witness can perceive (metres). Configurable; see README.
SIGHT_RANGE_M = 20.0
CLEAR_VIEW_M = 8.0       # closer than this, a witness is sure of what they saw
BRIEF_S = 1.5            # a grab shorter than this reads as a touch
LONG_S = 5.0             # a grab longer than this reads as being held

CLOSE_STATUSES = {"friend", "close_friend", "courting", "partner", "spouse"}


@dataclass
class Bond:
    """How ``holder`` sees ``other`` right now (from v_relationship; neutral when there is no row)."""
    status: str = "stranger"
    kinship: str | None = None
    trust: float = 0.0

    @property
    def close(self) -> bool:
        return self.kinship is not None or self.status in CLOSE_STATUSES or self.trust >= 0.4


@dataclass
class Perception:
    """Whether one witness perceived the event, and how well."""
    how: str                 # event_witness.how: 'participated' or 'saw'
    source_kind: str         # memory.source_kind: 'experienced' or 'witnessed'
    confidence: float
    distance_m: float | None = None
    had_los: bool | None = None


def perceives(witness: Witness) -> Perception | None:
    """A witness sees the event only with line of sight and within sight range."""
    if not witness.had_los or witness.distance_m > SIGHT_RANGE_M:
        return None
    if witness.distance_m <= CLEAR_VIEW_M:
        confidence = 0.95
    else:
        confidence = round(0.95 - 0.45 * (witness.distance_m - CLEAR_VIEW_M) / (SIGHT_RANGE_M - CLEAR_VIEW_M), 2)
    return Perception("saw", "witnessed", confidence, witness.distance_m, True)


@dataclass
class MemoryDraft:
    content: str
    interpretation: str | None
    importance: float
    valence: float
    intensity: float
    confidence: float
    source_kind: str
    deltas_to_actor: dict[str, float]


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return round(max(low, min(high, value)), 2)


def _place(record: PerceptionRecord) -> str:
    return f" in {record.location}" if record.location else ""


def _grab_phrase(record: PerceptionRecord, seen_form: bool = False) -> tuple[str, str]:
    """('grabbed', ' by the left forearm') etc., from duration and body part.
    ``seen_form`` gives the verb as in "I saw him grab her"."""
    part = body_part(record.node)
    dur = record.duration_s or 0.0
    brief = dur < BRIEF_S
    if seen_form:
        verb = "touch" if brief else ("grab and hold" if dur >= LONG_S else "grab")
    else:
        verb = "touched" if brief else ("grabbed and held" if dur >= LONG_S else "grabbed")
    where = (f" on the {part}" if brief else f" by the {part}") if part else ""
    return verb, where


def _how_long(record: PerceptionRecord) -> str:
    dur = record.duration_s or 0.0
    if dur < BRIEF_S:
        return ""
    return f" for about {round(dur)} second{'s' if round(dur) != 1 else ''}"


def grab_for_target(record: PerceptionRecord, to_actor: Bond) -> MemoryDraft:
    """What the person who was grabbed experienced."""
    actor = record.actor.name
    verb, where = _grab_phrase(record)
    where_mine = where.replace(" the ", " my ")
    part = body_part(record.node) or ""
    dur = record.duration_s or 0.0
    sensitive = part.split(" ")[-1] in SENSITIVE_PARTS
    if to_actor.close and dur < LONG_S and not sensitive:
        interpretation = f"{actor} is close to me; it felt like a friendly touch, maybe to get my attention."
        valence, trust = 0.1, 0.0
    elif dur < BRIEF_S and not sensitive:
        interpretation = f"A stranger putting hands on me. Rude, at the least."
        valence, trust = -0.3, -0.05
    elif dur >= LONG_S or sensitive:
        interpretation = f"{actor} held on and would not let go. It felt threatening."
        valence, trust = -0.6, -0.15
    else:
        interpretation = f"{actor} grabbed me without asking. I did not like it."
        valence, trust = -0.4, -0.08
    importance = 0.5 + (0.2 if dur >= LONG_S else 0) + (0.1 if sensitive else 0)
    deltas = {"trust": trust}
    if valence <= -0.5:
        deltas["fear"] = 0.05
    return MemoryDraft(
        content=f"{actor} {verb} me{where_mine}{_how_long(record)}{_place(record)}.",
        interpretation=interpretation, importance=_clamp(importance), valence=valence,
        intensity=_clamp(abs(valence) + 0.1), confidence=1.0, source_kind="experienced", deltas_to_actor=deltas)


def grab_for_witness(record: PerceptionRecord, seen: Perception, to_actor: Bond, to_target: Bond) -> MemoryDraft:
    """What a bystander saw. They never learn why it happened."""
    actor, target = record.actor.name, record.target.name if record.target else "someone"
    verb, where = _grab_phrase(record, seen_form=True)
    distance = seen.distance_m or 0.0
    seen_from = "" if distance <= CLEAR_VIEW_M else f", from about {round(distance)} metres away"
    hedge = "I saw" if seen.confidence >= 0.8 else "I think I saw"
    dur = record.duration_s or 0.0
    if to_target.close and not to_actor.close:
        interpretation = (f"I don't know why {actor} did it, but I didn't like seeing {target} handled like that. "
                          f"I'll be keeping an eye on {actor}.")
        valence, trust = -0.5, -0.08 if dur >= BRIEF_S else -0.04
    elif to_actor.close:
        interpretation = f"I don't know what that was about. {actor} probably meant no harm."
        valence, trust = -0.05, 0.0
    else:
        interpretation = f"I don't know what was going on there. Strange, maybe rough."
        valence, trust = -0.2, -0.03 if dur >= BRIEF_S else 0.0
    importance = (0.45 + (0.15 if to_target.close else 0) + (0.1 if dur >= LONG_S else 0)) * (0.7 + 0.3 * seen.confidence)
    deltas = {"trust": trust} if trust else {}
    return MemoryDraft(
        content=f"{hedge} {actor} {verb} {target}{where}{_how_long(record)}{_place(record)}{seen_from}.",
        interpretation=interpretation, importance=_clamp(importance), valence=valence,
        intensity=_clamp(abs(valence)), confidence=seen.confidence, source_kind="witnessed", deltas_to_actor=deltas)


def grab_for_actor(record: PerceptionRecord) -> MemoryDraft:
    """An NPC who did the grabbing (rare: HIGGS only reports the player's hands)."""
    target = record.target.name if record.target else "someone"
    verb, where = _grab_phrase(record)
    return MemoryDraft(content=f"I {verb} {target}{where}{_how_long(record)}{_place(record)}.", interpretation=None,
                       importance=0.4, valence=0.0, intensity=0.2, confidence=1.0, source_kind="experienced",
                       deltas_to_actor={})


def _item_phrase(record: PerceptionRecord, seen_form: bool = False) -> tuple[str, str]:
    category = item_category(record.item or "", record.is_food)
    qty = f"{record.quantity} " if record.quantity > 1 else "some "
    verb = consume_verb(category)
    if seen_form:
        verb = {"ate": "eat", "drank": "drink"}[verb]
    return category, f"{verb} {qty}{record.item}"


def consume_for_witness(record: PerceptionRecord, seen: Perception, to_actor: Bond) -> MemoryDraft:
    category, phrase = _item_phrase(record, seen_form=True)
    hedge = "I saw" if seen.confidence >= 0.8 else "I think I saw"
    interpretation = None
    importance = 0.2
    if category == "alcohol":
        importance = 0.35
        interpretation = f"{record.actor.name} was drinking. Worth remembering if it becomes a habit."
    return MemoryDraft(content=f"{hedge} {record.actor.name} {phrase}{_place(record)}.", interpretation=interpretation,
                       importance=importance, valence=0.0, intensity=0.1, confidence=seen.confidence,
                       source_kind="witnessed", deltas_to_actor={})


def consume_for_actor(record: PerceptionRecord) -> MemoryDraft:
    _, phrase = _item_phrase(record)
    return MemoryDraft(content=f"I {phrase}{_place(record)}.", interpretation=None, importance=0.15, valence=0.1,
                       intensity=0.1, confidence=1.0, source_kind="experienced", deltas_to_actor={})
