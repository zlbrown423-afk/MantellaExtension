"""Perception records sent by the game, and what they mean.

The Living World build of Mantella-Spell queues one text record per perceived event (a grab released,
something eaten or drunk) and sends the queue with the next Mantella request, under the context key
``mantella_lw_events``. Each record is ``key=value`` pairs joined by ``|``::

    v=1|key=812345-3|type=grab|gt=3.512|loc=Riverwood|actor=<actor>|target=<actor>
      |node=NPC L Forearm [LLar]|hand=L|dur=4.2|w=<witness>|w=<witness>

    v=1|key=812345-4|type=consume|gt=3.6|loc=Riverwood|actor=<actor>|item=Ale|item_form=<int>
      |food=1|via=higgs_mouth|qty=1|w=<witness>

``<actor>`` is ``ref^base^ref_plugin^base_plugin^is_player^name`` and ``<witness>`` is
``distance_m^had_los^rank_to_actor^rank_to_target^`` followed by an actor. Ranks are the game's own
``GetRelationshipRank`` (4 lover .. 0 acquaintance .. -4 archnemesis); ``trank`` is the target's rank
towards the actor. The name always comes last so it may hold any character
except ``|``. Nothing here is invented: a record exists only when a game event fired.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from livingworld.store.identity import ActorIdentity, parse_form_id

KEY_EVENTS = "mantella_lw_events"
RECORD_VERSION = "1"


class BadRecord(ValueError):
    pass


@dataclass(frozen=True)
class GameActor:
    ref_id: int
    base_id: int
    ref_plugin: str | None
    base_plugin: str | None
    is_player: bool
    name: str

    def identity(self) -> ActorIdentity:
        return ActorIdentity(name=self.name, base=parse_form_id(self.base_id, self.base_plugin),
                             ref=parse_form_id(self.ref_id, self.ref_plugin), is_player=self.is_player)


@dataclass(frozen=True)
class Witness:
    actor: GameActor
    distance_m: float
    had_los: bool
    rank_to_actor: int | None = None
    rank_to_target: int | None = None


@dataclass
class PerceptionRecord:
    key: str
    type: str                       # 'grab' | 'consume'
    game_time: float
    location: str | None
    actor: GameActor
    target: GameActor | None = None
    node: str | None = None         # skeleton node HIGGS reports as held
    hand: str | None = None         # 'L' | 'R'
    duration_s: float | None = None
    item: str | None = None
    item_form: int | None = None
    is_food: bool = False
    via: str | None = None          # 'higgs_mouth' | 'equip' | 'npc_action' (an NPC drinking by Drink action)
    quantity: int = 1
    target_rank: int | None = None  # the target's relationship rank towards the actor
    witnesses: list[Witness] = field(default_factory=list)
    raw: str = ""


def _actor(text: str) -> GameActor:
    parts = text.split("^", 5)
    if len(parts) != 6:
        raise BadRecord(f"actor needs 6 fields: {text!r}")
    ref, base, ref_plugin, base_plugin, is_player, name = parts
    try:
        return GameActor(int(ref), int(base), ref_plugin or None, base_plugin or None, is_player == "1", name)
    except ValueError as e:
        raise BadRecord(f"bad actor {text!r}") from e


def _rank(text: str) -> int | None:
    return int(text) if text not in ("", None) else None


def _witness(text: str) -> Witness:
    parts = text.split("^", 4)
    if len(parts) != 5:
        raise BadRecord(f"witness needs distance, line of sight, two ranks and an actor: {text!r}")
    return Witness(_actor(parts[4]), float(parts[0]), parts[1] == "1", _rank(parts[2]), _rank(parts[3]))


def parse_record(text: str) -> PerceptionRecord:
    fields: dict[str, str] = {}
    witnesses: list[Witness] = []
    for part in text.split("|"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip().lower()
        if name == "w":
            witnesses.append(_witness(value))
        else:
            fields[name] = value
    if fields.get("v") != RECORD_VERSION:
        raise BadRecord(f"unknown record version {fields.get('v')!r}")
    for required in ("key", "type", "gt", "actor"):
        if not fields.get(required):
            raise BadRecord(f"record has no {required}")
    # Papyrus keeps one case-insensitive table of every string and identifier, so a literal such as
    # "grab" can arrive as "Grab". Enumerated values are compared lowercased.
    kind = fields["type"].strip().lower()
    if fields.get("via"):
        fields["via"] = fields["via"].strip().lower()
    if fields.get("hand"):
        fields["hand"] = fields["hand"].strip().upper()
    if kind not in ("grab", "consume"):
        raise BadRecord(f"unknown event type {kind!r}")
    record = PerceptionRecord(
        key=fields["key"], type=kind, game_time=float(fields["gt"]), location=fields.get("loc") or None,
        actor=_actor(fields["actor"]), target=_actor(fields["target"]) if fields.get("target") else None,
        node=fields.get("node") or None, hand=fields.get("hand") or None,
        duration_s=float(fields["dur"]) if fields.get("dur") else None,
        item=fields.get("item") or None, item_form=int(fields["item_form"]) if fields.get("item_form") else None,
        is_food=fields.get("food") == "1", via=fields.get("via") or None,
        quantity=int(fields.get("qty") or 1), target_rank=_rank(fields.get("trank", "")), witnesses=witnesses, raw=text)
    if kind == "grab" and record.target is None:
        raise BadRecord("a grab needs a target")
    if kind == "consume" and not record.item:
        raise BadRecord("a consume event needs an item")
    return record


def records_from_context(context: dict[str, Any] | None) -> list[str]:
    if not context:
        return []
    value = context.get(KEY_EVENTS) or []
    return [v for v in value if isinstance(v, str) and v.strip()]


def status_for_rank(rank: int | None) -> str | None:
    """The game's relationship rank as a relationship.status."""
    if rank is None:
        return None
    return {4: "partner", 3: "close_friend", 2: "close_friend", 1: "friend", 0: "acquaintance", -1: "rival"}.get(
        rank, "enemy" if rank < -1 else "partner")


# ---- body parts ----------------------------------------------------------------------------------
# Skyrim skeleton node names look like "NPC L Forearm [LLar]". HIGGS names the node the hand holds.
_PARTS = [
    (r"head|face|jaw", "head"), (r"neck", "neck"), (r"clavicle", "shoulder"), (r"upperarm", "upper arm"),
    (r"forearm", "forearm"), (r"hand|finger|thumb", "hand"), (r"spine2|chest|breast", "chest"),
    (r"spine|back", "back"), (r"pelvis", "hips"), (r"thigh", "thigh"), (r"calf", "leg"), (r"foot|toe", "foot"),
]


def body_part(node: str | None) -> str | None:
    """'NPC L Forearm [LLar]' -> 'left forearm'. None when the node is unknown or not a body part."""
    if not node:
        return None
    lowered = node.lower()
    for pattern, part in _PARTS:
        if re.search(pattern, lowered):
            side = ""
            if re.search(r"\bl\b|\[l", lowered):
                side = "left "
            elif re.search(r"\br\b|\[r", lowered):
                side = "right "
            return side + part
    return None


SENSITIVE_PARTS = {"head", "neck", "chest", "hips"}


# ---- consumables ---------------------------------------------------------------------------------
# Vanilla Skyrim has no alcohol keyword that I could confirm, so drinks are recognised by name.
ALCOHOL_NAMES = (
    "ale", "mead", "wine", "sujamma", "flin", "brandy", "shein", "matze", "skooma",
)
DRINK_WORDS = ("milk", "water", "tea", "juice", "drink")


def item_category(item: str, is_food: bool) -> str:
    """'alcohol', 'food', 'drink' or 'potion'."""
    words = re.findall(r"[a-z]+", item.lower())
    if any(w in ALCOHOL_NAMES for w in words):
        return "alcohol"
    if is_food:
        return "drink" if any(w in DRINK_WORDS for w in words) else "food"
    return "potion"


def consume_verb(category: str) -> str:
    return "ate" if category == "food" else "drank"
