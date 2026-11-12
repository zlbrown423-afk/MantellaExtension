"""One event, many perspectives: who perceived it, what each of them remembers, and what they make of it.

All of this is local rules and templates, no LLM call. A witness never learns the actor's intent;
what they make of it comes from their own relationships (``v_relationship``), the duration, the body
part and how far away they stood. Memory text is first person, the way ``recall`` shows it in prompts.
"""
from __future__ import annotations

from dataclasses import dataclass

from livingworld.perception.records import (SENSITIVE_PARTS, PerceptionRecord, Witness, grab_area,
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
    part = grab_area(record)
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


# ---- touch consent (livingworld/touch): a touch the person had agreed to, or one past what they agreed ----

PARTNER_STATUSES = {"courting", "partner", "spouse"}
_AFFECTION = {"hug", "hands", "back"}                    # friendly; the rest is intimate


_PART = {"neck": "neck", "face": "face", "butt": "backside", "chest": "chest"}


def _agreed_phrase(record: PerceptionRecord, me: bool) -> str:
    """As told by the person touched ('hugged me', 'put a hand on my backside') or by a witness, after
    'I saw Zach' ('hug Gerdur', 'rub Gerdur's back')."""
    kind = record.consent
    if me:
        if kind in _PART:
            return f"{'put a hand on' if kind == 'butt' else 'touched'} my {_PART[kind]}"
        return {"hug": "hugged me", "hands": "held my hand", "back": "rubbed my back"}.get(kind, "touched me")
    target = record.target.name if record.target else "someone"
    if kind in _PART:
        return f"{'put a hand on' if kind == 'butt' else 'touch'} {target}'s {_PART[kind]}"
    return {"hug": f"hug {target}", "hands": f"hold hands with {target}",
            "back": f"rub {target}'s back"}.get(kind, f"touch {target}")


def agreed_for_target(record: PerceptionRecord, to_actor: Bond) -> MemoryDraft:
    """A touch the person had said yes to: affection, not an assault."""
    actor = record.actor.name
    friendly = record.consent in _AFFECTION
    if friendly:
        interpretation = f"I had said yes. It felt warm{', from someone close to me' if to_actor.close else ''}."
        valence, deltas = (0.35 if to_actor.close else 0.25), {"trust": 0.03, "affinity": 0.04}
    else:
        interpretation = f"I had said yes to it. It was close and personal, and I wanted it."
        valence, deltas = 0.3, {"trust": 0.03, "affinity": 0.05, "intimacy": 0.03}
    return MemoryDraft(
        content=f"{actor} {_agreed_phrase(record, me=True)}{_how_long(record)}{_place(record)}, as I had agreed.",
        interpretation=interpretation, importance=0.45 if friendly else 0.55, valence=valence,
        intensity=_clamp(abs(valence) + 0.1), confidence=1.0, source_kind="experienced", deltas_to_actor=deltas)


def breach_for_target(record: PerceptionRecord, to_actor: Bond) -> MemoryDraft:
    """A hand on a sensitive place they had not agreed to, while trusting the player with something less."""
    from livingworld.touch import describe
    actor = record.actor.name
    verb, where = _grab_phrase(record)
    allowed = describe(record.allowed) or "less"
    return MemoryDraft(
        content=f"{actor} {verb} me{where.replace(' the ', ' my ')}{_how_long(record)}{_place(record)}, when I had "
                f"only agreed to let them {allowed}.",
        interpretation=f"I trusted {actor} and {actor} went further than I said. I will not forget it.",
        importance=0.8, valence=-0.7, intensity=0.8, confidence=1.0, source_kind="experienced",
        deltas_to_actor={"trust": -0.25, "fear": 0.06, "affinity": -0.1})


def agreed_for_witness(record: PerceptionRecord, seen: Perception, to_actor: Bond, to_target: Bond) -> MemoryDraft:
    """A bystander who saw an agreed touch: it looked friendly and welcome. Their partner may not like it."""
    actor, target = record.actor.name, record.target.name if record.target else "someone"
    distance = seen.distance_m or 0.0
    seen_from = "" if distance <= CLEAR_VIEW_M else f", from about {round(distance)} metres away"
    hedge = "I saw" if seen.confidence >= 0.8 else "I think I saw"
    friendly = record.consent in _AFFECTION
    jealous = to_target.status in PARTNER_STATUSES and not to_actor.close
    if jealous:
        interpretation = (f"{target} seemed glad of it, and that is what stings. {target} is mine; "
                          f"{actor} has no business there.")
        valence, deltas, importance = -0.4, {"trust": -0.08, "affinity": -0.06}, 0.6
    elif friendly:
        interpretation = f"It looked friendly, and {target} clearly welcomed it."
        valence, deltas, importance = 0.1, {}, 0.25
    else:
        interpretation = f"{target} did not seem to mind at all. Those two must be close."
        valence, deltas, importance = 0.0, {}, 0.4
    return MemoryDraft(
        content=f"{hedge} {actor} {_agreed_phrase(record, me=False)}{_how_long(record)}{_place(record)}{seen_from};"
                f" {target} was willing.",
        interpretation=interpretation, importance=_clamp(importance * (0.7 + 0.3 * seen.confidence)),
        valence=valence, intensity=_clamp(abs(valence) + 0.1), confidence=seen.confidence,
        source_kind="witnessed", deltas_to_actor=deltas)


def grab_for_target(record: PerceptionRecord, to_actor: Bond) -> MemoryDraft:
    """What the person who was grabbed experienced."""
    if record.consent == "breach":
        return breach_for_target(record, to_actor)
    if record.consent:
        return agreed_for_target(record, to_actor)
    actor = record.actor.name
    verb, where = _grab_phrase(record)
    where_mine = where.replace(" the ", " my ")
    part = grab_area(record) or ""
    dur = record.duration_s or 0.0
    sensitive = part.split(" ")[-1] in SENSITIVE_PARTS
    if to_actor.close and dur < LONG_S and not sensitive:
        interpretation = f"{actor} is close to me; it felt like a friendly touch, maybe to get my attention."
        valence, trust = 0.1, 0.0
    elif dur < BRIEF_S and not sensitive:
        interpretation = f"A stranger putting hands on me. Rude, at the least."
        valence, trust = -0.3, -0.05
    elif dur >= LONG_S:
        interpretation = f"{actor} held on and would not let go. It felt threatening."
        valence, trust = -0.6, -0.15
    elif sensitive:
        # a quick hand on the face or chest is not "holding on", but it is no less unwelcome
        interpretation = f"{actor} put a hand on my {part.split(' ')[-1]} without asking. It felt threatening."
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
    if record.consent and record.consent != "breach":
        return agreed_for_witness(record, seen, to_actor, to_target)
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


# ---- buying a round (OrderDrinks action) ----------------------------------------------------------

def _round(record: PerceptionRecord) -> str:
    return f"{record.quantity} {record.item}" if record.quantity > 1 else f"a {record.item}"


def _price(record: PerceptionRecord) -> str:
    return f" for {record.gold} gold" if record.gold else ""


def purchase_for_actor(record: PerceptionRecord) -> MemoryDraft:
    """The NPC who went to the bar."""
    party = record.party.name if record.party else None
    if record.payer == "player" and party:
        paid, interpretation = f" {party} paid.", None
    else:
        paid = " I paid."
        interpretation = f"My treat for {party}." if party else None
    share = f", a round for {party} and me" if party else ""
    return MemoryDraft(content=f"I bought {_round(record)} from {record.target.name}{_price(record)}"
                               f"{_place(record)}{share}.{paid}",
                       interpretation=interpretation, importance=0.25, valence=0.2, intensity=0.15, confidence=1.0,
                       source_kind="experienced", deltas_to_actor={})


def purchase_for_seller(record: PerceptionRecord) -> MemoryDraft:
    """The bartender: a sale, nothing more."""
    return MemoryDraft(content=f"{record.actor.name} bought {_round(record)} from me{_price(record)}{_place(record)}.",
                       interpretation=None, importance=0.1, valence=0.1, intensity=0.05, confidence=1.0,
                       source_kind="experienced", deltas_to_actor={})


def purchase_for_witness(record: PerceptionRecord, seen: Perception) -> MemoryDraft:
    hedge = "I saw" if seen.confidence >= 0.8 else "I think I saw"
    share = f" for {record.actor.name} and {record.party.name}" if record.party else ""
    return MemoryDraft(content=f"{hedge} {record.actor.name} buy {_round(record)} from {record.target.name}{share}"
                               f"{_place(record)}.",
                       interpretation=None, importance=0.15, valence=0.0, intensity=0.05, confidence=seen.confidence,
                       source_kind="witnessed", deltas_to_actor={})


# ---- reputation (S2): gifts, thefts and blows from the player ------------------------------------

def _qty_item(record: PerceptionRecord) -> str:
    return f"{record.quantity} {record.item}" if record.quantity > 1 else (record.item or "something")


def _hedge(seen: Perception) -> str:
    return "I saw" if seen.confidence >= 0.8 else "I think I saw"


def gift_for_target(record: PerceptionRecord) -> MemoryDraft:
    """The person the player gave something to."""
    generous = (record.gold or 0) >= 100
    interpretation = "A generous gift. That means something." if generous else "A kind gesture."
    deltas = {"affinity": 0.1 if generous else 0.06, "trust": 0.03}
    return MemoryDraft(content=f"{record.actor.name} gave me {_qty_item(record)}{_place(record)}.",
                       interpretation=interpretation, importance=0.5 if generous else 0.35, valence=0.4,
                       intensity=0.3, confidence=1.0, source_kind="experienced", deltas_to_actor=deltas)


def gift_for_witness(record: PerceptionRecord, seen: Perception) -> MemoryDraft:
    target = record.target.name if record.target else "someone"
    return MemoryDraft(content=f"{_hedge(seen)} {record.actor.name} give {target} {_qty_item(record)}{_place(record)}.",
                       interpretation=None, importance=0.25, valence=0.1, intensity=0.1, confidence=seen.confidence,
                       source_kind="witnessed", deltas_to_actor={"affinity": 0.02})


def theft_for_witness(record: PerceptionRecord, seen: Perception, is_owner: bool, to_owner: Bond) -> MemoryDraft:
    """Someone who saw the player take what was not theirs. The owner, if they saw it, takes it worst."""
    actor = record.actor.name
    how = "pick the pocket of" if record.via == "pickpocket" else "steal"
    if is_owner:
        content = f"{_hedge(seen)} {actor} steal my {_qty_item(record)}{_place(record)}."
        interpretation = f"{actor} is a thief, and it was my things they took."
        valence, deltas = -0.7, {"trust": -0.3, "affinity": -0.15, "suspicion": 0.3}
    else:
        owner = record.target.name if record.target else record.owner
        if record.via == "pickpocket" and owner:
            content = f"{_hedge(seen)} {actor} {how} {owner}{_place(record)}."
        else:
            whose = f" from {owner}" if owner else ""
            content = f"{_hedge(seen)} {actor} {how} {_qty_item(record)}{whose}{_place(record)}."
        if to_owner.close:
            interpretation = f"{actor} stole from someone I care about. I won't forget it."
            valence, deltas = -0.6, {"trust": -0.2, "suspicion": 0.25, "affinity": -0.08}
        else:
            interpretation = f"{actor} has light fingers. I'll keep an eye on my things."
            valence, deltas = -0.4, {"trust": -0.12, "suspicion": 0.2}
    return MemoryDraft(content=content, interpretation=interpretation, importance=_clamp(0.55 + (0.15 if is_owner else 0)),
                       valence=valence, intensity=_clamp(abs(valence)), confidence=seen.confidence,
                       source_kind="witnessed", deltas_to_actor=deltas)


def _with(record: PerceptionRecord) -> str:
    return f" with {record.item}" if record.item and record.item.lower() not in ("none", "unarmed") else ""


def hit_for_target(record: PerceptionRecord, to_actor: Bond) -> MemoryDraft:
    actor = record.actor.name
    verb = "hit me" if _with(record) else "struck me"
    interpretation = (f"{actor} turned on me. I thought better of them." if to_actor.close
                      else f"{actor} attacked me. They are dangerous.")
    return MemoryDraft(content=f"{actor} {verb}{_with(record)}{_place(record)}.", interpretation=interpretation,
                       importance=0.75, valence=-0.7, intensity=0.8, confidence=1.0, source_kind="experienced",
                       deltas_to_actor={"trust": -0.2, "affinity": -0.15, "fear": 0.15})


def hit_for_witness(record: PerceptionRecord, seen: Perception, to_actor: Bond, to_target: Bond) -> MemoryDraft:
    actor, target = record.actor.name, record.target.name if record.target else "someone"
    if to_target.close:
        interpretation = f"{actor} attacked {target}. I won't stand for that."
        valence, deltas = -0.7, {"trust": -0.15, "affinity": -0.1, "fear": 0.06}
    else:
        interpretation = f"{actor} is quick to violence."
        valence, deltas = -0.45, {"trust": -0.08, "fear": 0.05}
    return MemoryDraft(content=f"{_hedge(seen)} {actor} hit {target}{_with(record)}{_place(record)}.",
                       interpretation=interpretation, importance=_clamp(0.55 + (0.15 if to_target.close else 0)),
                       valence=valence, intensity=_clamp(abs(valence)), confidence=seen.confidence,
                       source_kind="witnessed", deltas_to_actor=deltas)
