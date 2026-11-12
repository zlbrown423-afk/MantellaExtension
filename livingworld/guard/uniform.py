"""Guard attire: people who see the player in a guard's (or a soldier's) uniform take them for one.

The game already sends what the player wears in the body slot with every request (``attire`` records). When it
is a uniform listed in ``data/uniforms.json``, each adult NPC in the conversation gets prompt lines:

* what they take the player for ("one of the Whiterun Guard"), and whether that is true. Someone who does not
  know otherwise simply assumes it and may ask about it (their post, their sergeant); a real guard of that force
  who has never seen the player among them may wonder out loud who they are;
* how that person feels about the force, from Skyrim lore only (a curated table keyed on the uniform and the
  person's name, race or a word in their name, or the hold's side in the civil war). Nobody is a hater by
  chance: someone not in the table treats a guard as people ordinarily do;
* their own regard for the player, which can win a dislike over (or harden it): the relationship row the rest
  of Living World already keeps (affinity, trust, respect), so helping them, fair dealing and good conversations
  move it, and bullying moves it the other way.

Rules only, no extra LLM call and no new table. Follows ``MANTELLA_LW_GUARD``; off on its own with
``MANTELLA_LW_UNIFORM=0``.
"""
from __future__ import annotations

import functools
import json
import os
from dataclasses import dataclass
from importlib import resources

ENV = "MANTELLA_LW_UNIFORM"
STANCES = ("despise", "distrust", "wary", "neutral", "respect", "approve")
WON_OVER = 0.25          # the person's regard for the player (mean of affinity, trust, respect) at which a dislike
WARMING = 0.1            # ... gives way to an exception for them, or starts to soften
SOURED = -0.25           # ... and at which any stance sours for this guard in particular


def enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class Uniform:
    key: str
    label: str                    # 'the Whiterun Guard'
    hold: str | None = None       # the hold whose guard wears it
    side: str | None = None       # 'imperial' | 'stormcloak' for soldiers
    org: str | None = None        # a guard career in orgs.json, if there is one
    item: str = ""                # the item as the game named it

    @property
    def city_guard(self) -> bool:
        return self.hold is not None


@dataclass(frozen=True)
class Attitude:
    stance: str
    why: str


@functools.lru_cache(maxsize=1)
def _data() -> dict:
    return json.loads(resources.files("livingworld.guard").joinpath("data", "uniforms.json").read_text(encoding="utf-8"))


def _norm(text: str | None) -> str:
    return " ".join((text or "").replace("’", "'").lower().split())


def _race(race: str | None) -> str:
    """'[Race <DarkElfRace (00013746)>]', 'DarkElfRace', 'Dark Elf' -> 'darkelf'."""
    text = _norm(race)
    if "<" in text:
        text = text.split("<", 1)[1].split("(", 1)[0]
    text = text.replace(" ", "").replace("vampire", "")
    return text.removesuffix("race")


def hold_of(location: str | None) -> str | None:
    loc = _norm(location)
    if not loc:
        return None
    for hold, info in _data()["holds"].items():
        if any(_norm(p) in loc for p in info.get("places", ())) or _norm(hold) in loc:
            return hold
    return None


def uniform_for(body: str | None, location: str | None) -> Uniform | None:
    """The uniform the body-slot item ``body`` is, or None. A guard's armour not listed by name counts as the
    guard of the hold the player is in."""
    name = _norm(body)
    if not name:
        return None
    for u in _data()["uniforms"]:
        if any(_norm(n) == name for n in u.get("names", ())):
            return Uniform(u["key"], u["label"], u.get("hold"), u.get("side"), u.get("org"), body or "")
    if "guard's armor" in name or "guard armor" in name or "guard's armour" in name:
        hold = hold_of(location)
        if hold is not None:
            u = next((x for x in _data()["uniforms"] if x.get("hold") == hold), None)
            if u is not None:
                return Uniform(u["key"], u["label"], hold, None, u.get("org"), body or "")
    return None


def attitude(u: Uniform, name: str, race: str | None, location: str | None) -> Attitude:
    """How this person feels about the force, by the lore table: the first rule that names them (by name, race
    or a word in their name), else the first default for the place or the hold's side, else neutral."""
    n, r, here = _norm(name), _race(race), hold_of(location)
    side = _data()["holds"].get(here, {}).get("side") if here else None
    loc = _norm(location)
    fallback: Attitude | None = None
    for rule in _data()["attitudes"]:
        wanted = rule.get("uniform", ())
        if u.key not in wanted and not (u.city_guard and "*city_guard" in wanted):
            continue
        if rule.get("places") and not any(_norm(p) in loc for p in rule["places"]):
            continue
        if rule.get("hold_side") and rule["hold_side"] != side:
            continue
        who = [k for k in ("names", "races", "name_words") if rule.get(k)]
        if not who:
            if rule.get("default") and fallback is None:
                fallback = Attitude(rule["stance"], rule.get("why", ""))
            continue
        if (any(_norm(x) == n for x in rule.get("names", ()))
                or (r and any(_race(x) == r for x in rule.get("races", ())))
                or any(_norm(x) in n.split() for x in rule.get("name_words", ()))):
            return Attitude(rule["stance"], rule.get("why", ""))
    return fallback or Attitude("neutral", "")


def regard(db, npc_id: int, player_id: int) -> float:
    """This person's own regard for the player: the mean of affinity, trust and respect (-1..1), 0 if none."""
    row = db.execute("SELECT affinity, trust, respect FROM relationship WHERE from_id = ? AND to_id = ?",
                     (npc_id, player_id)).fetchone()
    return (row[0] + row[1] + row[2]) / 3.0 if row else 0.0


_FEEL = {
    "despise": "despises {label}",
    "distrust": "distrusts {label}",
    "wary": "is wary around {label}",
    "respect": "respects {label}",
    "approve": "approves of {label} and is glad to see one of them",
}


def lines(name: str, player: str, u: Uniform, *, serves: bool, is_member: bool, knows_player: bool,
          att: Attitude, reg: float) -> list[str]:
    """Prompt lines for ``name`` about the player in uniform ``u``."""
    who = "one of " + u.label if u.city_guard or u.key != "thalmor" else "a Thalmor agent"
    out = [f"{player} is wearing {u.item or 'the uniform'}, the uniform of {u.label}."]
    if serves:
        out.append(f"{player} really is {who}." + ("" if knows_player else f" {name} takes them for exactly that."))
    elif is_member:
        out.append(f"{name} serves in {u.label} and does not know {player} as one of their own. {name} may ask who "
                   f"they are, where they are posted and who their superior is.")
    else:
        out.append(f"{name} takes {player} for {who} and treats them as one. {name} may ask about it (how long, "
                   f"where they are posted). In truth {player} does not serve in it; {name} does not know that.")
    if att.stance == "neutral" or att.stance not in _FEEL:
        out.append(f"{name} treats a guard the way most people do: polite, and may bring them a worry or a complaint.")
    else:
        feel = _FEEL[att.stance].format(label=u.label)
        out.append(f"{name} {feel}" + (f", because {att.why}." if att.why else "."))
    bad = att.stance in ("despise", "distrust", "wary")
    if bad and reg >= WON_OVER:
        out.append(f"But {player} has earned {name}'s personal regard: {name} still thinks no better of "
                   f"{u.label} as a whole, but treats {player} as the exception.")
    elif bad and reg >= WARMING:
        out.append(f"{name} is starting to think {player} may be different from the rest of them, and is a little "
                   f"less guarded with them.")
    elif bad:
        out.append(f"{name} can be won over by what {player} actually does and says: fair dealing, help, honesty. "
                   f"Bullying or threats only confirm what they think.")
    elif reg <= SOURED:
        out.append(f"Whatever {name} thinks of {u.label}, they think poorly of {player} in particular.")
    return out
