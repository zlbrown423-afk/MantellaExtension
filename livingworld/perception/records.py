"""Perception records sent by the game, and what they mean.

The Living World build of Mantella-Spell queues one text record per perceived event (a grab released,
something eaten or drunk) and sends the queue with the next Mantella request, under the context key
``mantella_lw_events``. Each record is ``key=value`` pairs joined by ``|``::

    v=1|key=812345-3|type=grab|gt=3.512|loc=Riverwood|actor=<actor>|target=<actor>
      |node=NPC L Forearm [LLar]|area=left forearm|hand=L|dur=4.2|w=<witness>|w=<witness>

    v=1|key=812345-4|type=consume|gt=3.6|loc=Riverwood|actor=<actor>|item=Ale|item_form=<int>
      |food=1|via=higgs_mouth|qty=1|w=<witness>

    v=1|key=812345-5|type=attire|gt=3.7|loc=Riverwood|actor=<actor>|body=Farm Clothes|cover=clothing|w=<witness>

    v=1|key=812345-6|type=purchase|gt=3.8|loc=Sleeping Giant Inn|actor=<actor>|target=<actor>|item=Nord Mead
      |item_form=<int>|qty=2|gold=10|payer=player|party=<actor>|w=<witness>

On a grab, ``node`` is the ragdoll body HIGGS holds and ``area`` is where the player's hand actually is,
worked out in game from the hand's position against the NPC's skeleton (empty when the game could not
tell). Skyrim's ragdoll has only a few large spine bodies for the whole torso, so ``node`` alone cannot
tell a shoulder from the waist or the chest from the back.

An ``attire`` record is a snapshot of what one actor wears in the body slot (32): ``cover`` is
``clothing``, ``armor`` or ``none`` (nothing in the body slot), and witnesses are listed only when
``cover`` is ``none``. The game sends one per nearby person with every request, under the context key
``mantella_lw_attire``, and queues one with the events when the player takes off or puts on body
clothing. ``via=action`` on a consume record means an NPC chose to drink (the Living World Drink action);
on an attire record it means an NPC chose to undress or dress (the Undress and Dress actions), and the game
queues it the moment they do, with witnesses.

A ``fight`` record is sent for the player and each adult ally who fought beside them, once the player's
fight ends: ``hp`` is the lowest share of health they fell to (0..1). A ``swim`` record is sent when the player
stops swimming: ``dur`` is how long, in seconds. Both only change how clean someone is (see ``hygiene/``).
An ``attire`` record also carries ``sex`` (``m`` or ``f``), the actor's sex in game.

A ``purchase`` record is an NPC (``actor``) buying a round from a bartender (``target``) with the
Living World OrderDrinks action: ``gold`` paid in total, ``payer`` is ``player`` or ``npc`` (the buyer),
and ``party`` is who the round is shared with (the player). Buying is not drinking: it never counts
towards being tipsy.

Reputation (S2) adds three records, each about something the player did, with witnesses:

    v=1|key=812345-7|type=gift|gt=3.9|loc=Riverwood|actor=<player>|target=<actor>|item=Sweetroll|qty=2|gold=16
      |via=gift_menu|w=<witness>

    v=1|key=812345-8|type=theft|gt=4.0|loc=Riverwood Trader|actor=<player>|item=Silver Goblet|qty=1|gold=30
      |via=take|owner=Lucan Valerius|w=<witness>

    v=1|key=812345-9|type=hit|gt=4.1|loc=Riverwood|actor=<player>|target=<actor>|item=Iron Sword|via=assault
      |w=<witness>

A ``gift`` is items the player handed to an NPC outside trading (``gold`` is their total value; the game
folds several items given to one person within a few seconds into one record). A ``theft`` is sent when
the game counts a theft or a picked pocket (``via=take`` or ``pickpocket``); ``owner`` is the owner's name
when the game knows it, and on a pickpocket the victim is the ``target`` and never notices. A ``hit`` is the
player striking an NPC who was not an enemy (``item`` is the weapon or spell, empty for a punch;
``via`` is ``assault`` when the game counted it as a crime, ``conversation`` when the NPC was talking
with the player).

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
KEY_ATTIRE = "mantella_lw_attire"
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
    type: str                       # 'grab' | 'consume' | 'attire' | 'purchase'
    game_time: float
    location: str | None
    actor: GameActor
    target: GameActor | None = None
    node: str | None = None         # skeleton node HIGGS reports as held
    area: str | None = None         # grab: where the hand was, from its position ('left shoulder', 'lower back')
    hand: str | None = None         # 'L' | 'R'
    duration_s: float | None = None
    item: str | None = None
    item_form: int | None = None
    is_food: bool = False
    via: str | None = None          # consume: 'higgs_mouth' | 'equip' | 'action'; attire: 'action' (own choice)
    quantity: int = 1
    target_rank: int | None = None  # the target's relationship rank towards the actor
    body: str | None = None         # attire: the item worn in the body slot
    cover: str | None = None        # attire: 'clothing' | 'armor' | 'none'
    gold: int | None = None         # purchase: total paid
    payer: str | None = None        # purchase: 'player' | 'npc' (the buyer)
    party: GameActor | None = None  # purchase: who the round is shared with
    hp: float | None = None         # fight: the lowest share of health the actor fell to
    sex: str | None = None          # attire: 'male' | 'female', the actor's sex in game
    owner: str | None = None        # theft: the owner's name, when the game knows it
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
    if fields.get("cover"):
        fields["cover"] = fields["cover"].strip().lower()
    if fields.get("payer"):
        fields["payer"] = fields["payer"].strip().lower()
    if kind not in ("grab", "consume", "attire", "purchase", "fight", "swim", "gift", "theft", "hit"):
        raise BadRecord(f"unknown event type {kind!r}")
    record = PerceptionRecord(
        key=fields["key"], type=kind, game_time=float(fields["gt"]), location=fields.get("loc") or None,
        actor=_actor(fields["actor"]), target=_actor(fields["target"]) if fields.get("target") else None,
        node=fields.get("node") or None, area=_area(fields.get("area")), hand=fields.get("hand") or None,
        duration_s=float(fields["dur"]) if fields.get("dur") else None,
        item=fields.get("item") or None, item_form=int(fields["item_form"]) if fields.get("item_form") else None,
        is_food=fields.get("food") == "1", via=fields.get("via") or None,
        quantity=int(fields.get("qty") or 1), target_rank=_rank(fields.get("trank", "")),
        body=fields.get("body") or None, cover=fields.get("cover") or None,
        gold=int(fields["gold"]) if fields.get("gold") else None, payer=fields.get("payer") or None,
        party=_actor(fields["party"]) if fields.get("party") else None,
        hp=_fraction(fields.get("hp")), sex=_sex(fields.get("sex")), owner=(fields.get("owner") or "").strip() or None,
        witnesses=witnesses, raw=text)
    if kind == "grab" and record.target is None:
        raise BadRecord("a grab needs a target")
    if kind == "consume" and not record.item:
        raise BadRecord("a consume event needs an item")
    if kind == "attire" and record.cover not in ("clothing", "armor", "none"):
        raise BadRecord(f"an attire record needs cover=clothing, armor or none, not {record.cover!r}")
    if kind == "purchase":
        if record.target is None or not record.item:
            raise BadRecord("a purchase needs a seller (target) and an item")
        if record.payer not in ("player", "npc"):
            raise BadRecord(f"a purchase needs payer=player or npc, not {record.payer!r}")
    if kind == "gift" and (record.target is None or not record.item):
        raise BadRecord("a gift needs a receiver (target) and an item")
    if kind == "theft" and not record.item:
        raise BadRecord("a theft needs an item")
    if kind == "hit" and record.target is None:
        raise BadRecord("a hit needs a target")
    return record


def _fraction(text: str | None) -> float | None:
    if not text:
        return None
    return max(0.0, min(1.0, float(text)))


def _sex(text: str | None) -> str | None:
    return {"f": "female", "m": "male", "female": "female", "male": "male"}.get((text or "").strip().lower())


def records_from_context(context: dict[str, Any] | None, key: str = KEY_EVENTS) -> list[str]:
    if not context:
        return []
    value = context.get(key) or []
    return [v for v in value if isinstance(v, str) and v.strip()]


def status_for_rank(rank: int | None) -> str | None:
    """The game's relationship rank as a relationship.status."""
    if rank is None:
        return None
    return {4: "partner", 3: "close_friend", 2: "close_friend", 1: "friend", 0: "acquaintance", -1: "rival"}.get(
        rank, "enemy" if rank < -1 else "partner")


# ---- body parts ----------------------------------------------------------------------------------
# Skyrim skeleton node names look like "NPC L Forearm [LLar]". HIGGS names the ragdoll body the hand
# holds. The torso has only a few large spine bodies, so a spine node says little more than "the torso":
# grabbing the shoulder, the waist or the back all report a spine. The game therefore also sends ``area``,
# worked out from the hand's position; the node is only the fallback (older saves, or no hand position).
_PARTS = [
    (r"head|face|jaw", "head"), (r"neck", "neck"), (r"clavicle", "shoulder"), (r"upperarm", "upper arm"),
    (r"forearm", "forearm"), (r"hand|finger|thumb", "hand"), (r"spine2", "upper body"),
    (r"spine", "waist"), (r"pelvis", "hips"), (r"thigh", "thigh"), (r"calf", "leg"), (r"foot|toe", "foot"),
]

# Every area the game can send, without its side. Anything else is ignored, so a garbled record can
# never put odd words into a memory.
AREAS = {"head", "face", "neck", "shoulder", "upper arm", "forearm", "hand", "chest", "upper back", "stomach",
         "lower back", "side", "hips", "hip", "backside", "thigh", "leg", "foot"}


def _area(text: str | None) -> str | None:
    if not text:
        return None
    words = " ".join(text.lower().split())
    side, _, rest = words.partition(" ")
    if side in ("left", "right") and rest in AREAS:
        return words
    return words if words in AREAS else None


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


def grab_area(record: "PerceptionRecord") -> str | None:
    """Where the grab landed: the area the game worked out from the hand's position, else from the node."""
    return record.area or body_part(record.node)


SENSITIVE_PARTS = {"head", "face", "neck", "chest", "hips", "backside"}


# ---- consumables ---------------------------------------------------------------------------------
# Vanilla Skyrim has no alcohol keyword that I could confirm, so drinks are recognised by name.
ALCOHOL_NAMES = (
    "ale", "mead", "wine", "sujamma", "flin", "brandy", "shein", "matze", "skooma",
)
DRINK_WORDS = ("milk", "water", "tea", "juice", "drink")


def is_alcohol(item: str | None) -> bool:
    """An alcoholic drink, for counting drinks. Skooma is a drug, not a drink, and is not counted."""
    words = re.findall(r"[a-z]+", (item or "").lower())
    return any(w in ALCOHOL_NAMES for w in words) and "skooma" not in words


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
