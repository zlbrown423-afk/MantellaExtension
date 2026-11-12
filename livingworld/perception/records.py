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

Touch consent (``livingworld/touch``) adds ``consent`` to a grab: the kind of touch the person grabbed had
agreed to (``hug``, ``hands``, ``back``, ``neck``, ``face``, ``butt``, ``chest``, ``anywhere``) when the hand was
inside it, or ``breach`` when they had agreed to something (``allowed=hug,hands``) and the hand went to a
sensitive place outside it. No ``consent`` means nothing was agreed.

A ``react`` record is someone reacting on the spot to the player's hands on them, outside any conversation:
``actor`` is who reacts, ``target`` the player, ``via`` is ``victim`` (the person grabbed speaks up) or ``guard``
(a guard who saw it steps in, and ``party`` is the person grabbed); ``area`` and ``dur`` are the grab's. The game
starts a conversation with them right after sending it. ``via=decency`` is a guard who saw the player walking
about naked in public coming over about it (``dur`` is how long the player had been naked; see
``guard/decency.py``). ``via=surrender`` is a survivor whose party was just killed coming to the player to beg
for their life: ``party`` is the one they lost who mattered most, each ``d`` another dead companion, and
``killer`` is ``player`` or ``other`` (see ``social/surrender.py``).

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

Guard careers (G2e) add a ``watch`` record when one of the player's watch windows on a guard shift comes
due: ``via=approach`` when the game had a nearby NPC (``target``) come over, ``via=observe`` when nobody was
near and the player noticed something instead; ``win`` is the window and ``task`` the duty (see
``guard/encounter.py``)::

    v=1|key=812345-10|type=watch|gt=4.2|loc=Whiterun|actor=<player>|target=<actor>|via=approach|win=1|task=17

A ``gift`` is items the player handed to an NPC outside trading (``gold`` is their total value; the game
folds several items given to one person within a few seconds into one record). A ``theft`` is sent when
the game counts a theft or a picked pocket (``via=take`` or ``pickpocket``); ``owner`` is the owner's name
when the game knows it, and on a pickpocket the victim is the ``target`` and never notices. A ``hit`` is the
player striking an NPC who was not an enemy (``item`` is the weapon or spell, empty for a punch;
``via`` is ``assault`` when the game counted it as a crime, ``conversation`` when the NPC was talking
with the player).

Intimate scenes (OStim, see ``intimacy/``) add a ``scene`` record. ``via=end`` is queued once when a scene
ends; ``via=now`` is sent with every request, under the context key ``mantella_lw_scene``, for each scene
running near the player. ``actor``, ``target`` and ``party`` are the people in it (the player first when they
are in it), ``acts`` the OStim action types seen so far, ``touch`` and ``slap`` the player's hands and slaps
on a partner that the game folded into the scene instead of sending them as grabs and hits, ``forced=1``
an aggressive OStim scene, ``climax`` how often someone climaxed, ``dur`` seconds, ``psize`` the scale The New
Gentleman gives the player's genitals when the player is a man in the scene (``intimacy/size.py``)::

    v=1|key=812345-11|type=scene|gt=4.3|loc=Whiterun|via=end|thread=0|scene=<OStim scene id>|actor=<player>
      |target=<actor>|acts=kissing,vaginalsex,spanking|touch=14|slap=3|forced=0|climax=1|dur=420.5|w=<witness>

The same scenes add a ``climax`` record each time someone in one climaxes (OStim's ``ostim_actor_orgasm``;
OCum draws it): ``actor`` climaxed, ``target`` is the partner, ``acts`` the OStim action types of the scene
at that moment (they tell where it landed, ``intimacy/climax.py``), ``sex`` the actor's sex::

    v=1|key=812345-12|type=climax|gt=4.31|loc=Whiterun|thread=0|scene=<id>|actor=<actor>|target=<actor>
      |acts=blowjob,kissing|sex=m|w=<witness>

An ``arousal`` record is the player's erection (The New Gentleman) crossing "visibly aroused": ``via=erect``
or ``soft``, ``cover`` as on an attire record, ``level`` TNG's bend (-10 soft .. 9), ``psize`` as above;
witnesses only when the player has nothing on the body. An ``eyes`` record is the player covering
(``via=covered``) or uncovering their eyes (the LWCoverEyes power). The player's own attire record also
carries ``erect=1`` while visibly aroused and ``eyes=1`` while their eyes are covered.

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
    win: int | None = None          # watch: which watch window of the duty
    task: int | None = None         # watch: the duty (task id) the window belongs to
    thread: int | None = None       # scene: the OStim thread id (0 = the player's own)
    acts: list[str] = field(default_factory=list)   # scene: OStim action types seen ('kissing', 'spanking')
    touches: int = 0                # scene: the player's hands on a partner during it (HIGGS grabs, folded in)
    slaps: int = 0                  # scene: slaps and spanks during it (hits on a partner, OStim spank events)
    forced: bool = False            # scene: OStim played it as an aggressive scene
    climax: int = 0                 # scene: how many times someone in it climaxed
    scene_id: str | None = None     # scene: OStim scene id (the last one, on an end record)
    player_size: float | None = None  # scene: TNG's scale of the player's "NPC GenitalsBase" bone (intimacy/size.py)
    erect: bool = False             # attire (the player): visibly aroused right now
    eyes_covered: bool = False      # attire (the player): their eyes are covered right now
    level: int | None = None        # arousal: TNG's bend, -10 (soft) .. 9
    dead: list[GameActor] = field(default_factory=list)   # react via=surrender: other dead companions (d=)
    killer: str | None = None       # react via=surrender: 'player' | 'other'
    consent: str | None = None      # grab: the kind of touch the target had agreed to ('hug'), or 'breach' (livingworld/touch)
    allowed: list[str] = field(default_factory=list)   # grab with consent=breach: what they had agreed to
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
    dead: list[GameActor] = []
    for part in text.split("|"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip().lower()
        if name == "w":
            witnesses.append(_witness(value))
        elif name == "d":
            dead.append(_actor(value))
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
    if kind not in ("grab", "consume", "attire", "purchase", "fight", "swim", "gift", "theft", "hit", "watch", "react", "jail",
                    "scene", "climax", "arousal", "eyes", "rebind"):
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
        win=int(fields["win"]) if fields.get("win") else None, task=int(fields["task"]) if fields.get("task") else None,
        thread=int(fields["thread"]) if fields.get("thread") else None, acts=_acts(fields.get("acts")),
        touches=_count(fields.get("touch")), slaps=_count(fields.get("slap")), forced=fields.get("forced") == "1",
        climax=_count(fields.get("climax")), scene_id=(fields.get("scene") or "").strip() or None,
        player_size=_scale(fields.get("psize")), erect=fields.get("erect") == "1", eyes_covered=fields.get("eyes") == "1",
        level=_level(fields.get("level")),
        dead=dead, killer=(fields.get("killer") or "").strip().lower() or None,
        consent=_consent(fields.get("consent")), allowed=_kinds(fields.get("allowed")),
        witnesses=witnesses, raw=text)
    if kind == "scene" and (record.via not in ("end", "now") or record.target is None):
        raise BadRecord("a scene record needs via=end or now and at least two people (actor and target)")
    if kind == "climax" and record.target is None:
        raise BadRecord("a climax record needs the partner as target")
    if kind == "arousal" and record.via not in ("erect", "soft"):
        raise BadRecord("an arousal record needs via=erect or soft")
    if kind == "eyes" and record.via not in ("covered", "uncovered"):
        raise BadRecord("an eyes record needs via=covered or uncovered")
    if kind == "watch" and (record.via not in ("approach", "observe") or record.win is None or not record.task):
        raise BadRecord("a watch record needs via=approach or observe, win and task")
    if kind == "jail" and (record.via not in ("jailed", "released") or record.target is None or not record.task):
        raise BadRecord("a jail record needs via=jailed or released, the prisoner as target and the arrest as task")
    if kind == "react" and (record.via not in ("victim", "guard", "decency", "surrender") or record.target is None
                            or (record.via in ("guard", "surrender") and record.party is None)):
        raise BadRecord("a react record needs via=victim, guard, decency or surrender, the player as target, and party"
                        " for a guard or a surrender")
    if kind == "rebind" and record.target is None:
        raise BadRecord("a rebind record needs the old reference as target")
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


def _acts(text: str | None) -> list[str]:
    """OStim action types, lowercased, each once, in the order first seen. Anything that is not a plain id is
    dropped, so a garbled record can never put odd words into a memory."""
    acts: list[str] = []
    for part in (text or "").split(","):
        word = part.strip().lower()
        if word and re.fullmatch(r"[a-z0-9_]{1,40}", word) and word not in acts:
            acts.append(word)
    return acts


def _consent(text: str | None) -> str | None:
    """A touch kind the target agreed to, 'breach', or None (nothing agreed, or a value we do not know)."""
    from livingworld.touch import KINDS
    word = (text or "").strip().lower()
    return word if word in KINDS or word == "breach" else None


def _kinds(text: str | None) -> list[str]:
    from livingworld.touch import normalize_kinds
    return normalize_kinds(text) if text else []


def _level(text: str | None) -> int | None:
    try:
        return max(-10, min(9, int(float(text)))) if text else None
    except ValueError:
        return None


def _scale(text: str | None) -> float | None:
    try:
        value = float(text) if text else None
    except ValueError:
        return None
    return value if value is not None and 0.05 < value <= 5.0 else None


def _count(text: str | None) -> int:
    try:
        return max(0, int(float(text))) if text else 0
    except ValueError:
        return 0


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
