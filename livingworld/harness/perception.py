"""Builds perception records exactly as the Living World build of MantellaListenerScript.psc writes them.

Used by tests and for simulating game events without Skyrim::

    rec = grab(actors.player("Torvar"), actors.gerdur(), node="NPC L Forearm [LLar]", seconds=4.2,
               game_days=3.51, location="Riverwood", witnesses=[seen(actors.hod(), 4.0, rank_to_target=4)])
    session.start([...], context_extra={KEY_EVENTS: [rec]})
"""
from __future__ import annotations

from typing import Any

from src.http.communication_constants import communication_constants as comm_consts

from livingworld.perception.records import KEY_ATTIRE, KEY_EVENTS  # noqa: F401  (re-exported for tests)
from livingworld.store.identity import KEY_BASE_PLUGIN, KEY_REF_PLUGIN

_counter = 0


def _key(prefix: str = "sim") -> str:
    global _counter
    _counter += 1
    return f"{prefix}-{_counter}"


def actor_field(actor_json: dict[str, Any]) -> str:
    return "^".join([
        str(actor_json[comm_consts.KEY_ACTOR_REFID]), str(actor_json[comm_consts.KEY_ACTOR_BASEID]),
        actor_json.get(KEY_REF_PLUGIN, ""), actor_json.get(KEY_BASE_PLUGIN, ""),
        "1" if actor_json.get(comm_consts.KEY_ACTOR_ISPLAYER) else "0", str(actor_json[comm_consts.KEY_ACTOR_NAME])])


def seen(actor_json: dict[str, Any], distance_m: float, los: bool = True, rank_to_actor: int | None = 0,
         rank_to_target: int | None = 0) -> str:
    rank = lambda r: "" if r is None else str(r)  # noqa: E731
    return f"{distance_m:.1f}^{1 if los else 0}^{rank(rank_to_actor)}^{rank(rank_to_target)}^{actor_field(actor_json)}"


def grab(actor_json: dict[str, Any], target_json: dict[str, Any], node: str | None, seconds: float,
         game_days: float, location: str = "Riverwood", hand: str = "L", witnesses: list[str] | None = None,
         target_rank: int | None = 0, key: str | None = None, area: str | None = None) -> str:
    parts = [f"v=1", f"key={key or _key()}", "type=grab", f"gt={game_days}", f"loc={location}",
             f"actor={actor_field(actor_json)}", f"target={actor_field(target_json)}", f"node={node or ''}",
             f"area={area or ''}",
             f"hand={hand}", f"dur={seconds}", f"trank={'' if target_rank is None else target_rank}"]
    parts += [f"w={w}" for w in witnesses or []]
    return "|".join(parts)


def consume(actor_json: dict[str, Any], item: str, game_days: float, is_food: bool = True, via: str = "higgs_mouth",
            location: str = "Riverwood", quantity: int = 1, witnesses: list[str] | None = None,
            key: str | None = None) -> str:
    parts = ["v=1", f"key={key or _key()}", "type=consume", f"gt={game_days}", f"loc={location}",
             f"actor={actor_field(actor_json)}", f"item={item}", "item_form=0", f"food={1 if is_food else 0}",
             f"via={via}", f"qty={quantity}"]
    parts += [f"w={w}" for w in witnesses or []]
    return "|".join(parts)


def attire(actor_json: dict[str, Any], cover: str, game_days: float, body: str = "", location: str = "Riverwood",
           witnesses: list[str] | None = None, key: str | None = None, via: str | None = None,
           sex: str | None = None) -> str:
    """What ``actor_json`` wears in the body slot: cover is 'clothing', 'armor' or 'none'.
    The game lists witnesses only when cover is 'none'. ``via='action'``: the NPC chose it (Undress / Dress)."""
    parts = ["v=1", f"key={key or _key('attire')}", "type=attire", f"gt={game_days}", f"loc={location}",
             f"actor={actor_field(actor_json)}", f"body={body}", f"cover={cover}"]
    if via:
        parts.append(f"via={via}")
    if sex:
        parts.append(f"sex={sex}")
    parts += [f"w={w}" for w in witnesses or []]
    return "|".join(parts)


def fight(actor_json: dict[str, Any], hp: float, game_days: float, location: str = "Riverwood",
          key: str | None = None) -> str:
    """Someone who fought beside the player, sent when the player's fight ends (``hp``: lowest health share)."""
    return "|".join(["v=1", f"key={key or _key('fight')}", "type=fight", f"gt={game_days}", f"loc={location}",
                     f"actor={actor_field(actor_json)}", f"hp={hp}"])


def swim(actor_json: dict[str, Any], seconds: float, game_days: float, location: str = "Riverwood",
         key: str | None = None) -> str:
    """The player stopped swimming after ``seconds``."""
    return "|".join(["v=1", f"key={key or _key('swim')}", "type=swim", f"gt={game_days}", f"loc={location}",
                     f"actor={actor_field(actor_json)}", f"dur={seconds}"])


def purchase(buyer_json: dict[str, Any], seller_json: dict[str, Any], item: str, quantity: int, gold: int,
             game_days: float, payer: str = "player", party_json: dict[str, Any] | None = None,
             location: str = "Sleeping Giant Inn", witnesses: list[str] | None = None, key: str | None = None) -> str:
    """A round bought by the OrderDrinks action, as MantellaRepository.LwRecordPurchase writes it."""
    parts = ["v=1", f"key={key or _key()}", "type=purchase", f"gt={game_days}", f"loc={location}",
             f"actor={actor_field(buyer_json)}", f"target={actor_field(seller_json)}", f"item={item}", "item_form=0",
             f"qty={quantity}", f"gold={gold}", f"payer={payer}"]
    if party_json:
        parts.append(f"party={actor_field(party_json)}")
    parts += [f"w={w}" for w in witnesses or []]
    return "|".join(parts)
