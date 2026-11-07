"""How much an NPC has had to drink, and what that does to them.

Drinks are the NPC's own ``consumption`` events on the active save timeline (an NPC drinks through
the Living World Drink action; see ``data/actions/livingworld_drink.json``). Alcohol wears off at a
steady rate, so the level rises with each drink and falls with game time.

Drink carries real weight: it changes speech, mood and social behaviour (how loud, open, bold and
emotional an NPC is, what they let slip, whether they sing or pick a fight) and how clearly they
remember what they saw while drunk (``hazy``). It never changes what they are willing to do with
their body: nothing here reads or writes relationships, and the prompt line it produces says so
explicitly (``CONSENT_UNCHANGED``). Decisions about undressing or intimacy must never depend on drink.
"""
from __future__ import annotations

from dataclasses import dataclass

from livingworld.perception.records import is_alcohol

GAME_HOUR = 1.0 / 24.0
WEAR_OFF_PER_HOUR = 0.35     # drinks the body clears per game hour (Skyrim's default: 1 game hour = 3 real minutes)
COUNT_WINDOW_DAYS = 0.5      # "drinks in the last few hours" looks back this far

# (minimum level, stage, how it shows)
STAGES = [
    (6.0, "very drunk", "words slur often (write some slurred or stretched words), sentences trail off or "
                        "repeat, and emotions swing hard: maudlin one moment, laughing the next. They overshare "
                        "badly, spilling secrets and gossip they would normally guard and forgetting who is "
                        "listening, may burst into song or stagger, and say they need to sit down. They "
                        "forget or muddle what was said a minute ago"),
    (4.0, "drunk", "loud, rambling, some words slur, and they lose the thread. Their mood is right on the surface: "
                   "sentimental and affectionate with people they like, boastful, or quarrelsome and quick to pick a "
                   "fight with people they dislike, whichever fits their temperament. Loose-lipped: they share "
                   "rumours, gossip and their own grievances or secrets far more readily than sober, and they are "
                   "much more likely to sing, dance, laugh loudly or make a scene"),
    (2.0, "tipsy", "louder and chattier than usual, quicker to laugh, joke, tease or take offence, bolder in what "
                   "they say, warmer and more open with company, more willing to pass on gossip and talk about "
                   "themselves, and in the mood for a song or a dance"),
    (0.75, "merry", "warmer and more relaxed than usual, talking more freely and laughing more easily"),
]

# A memory formed while drunk: confidence factor and the note added to it
HAZE = {"drunk": (0.7, "It's a bit hazy; I'd had a lot to drink."),
        "very drunk": (0.45, "It's all a blur; I was very drunk.")}

CONSENT_UNCHANGED = ("Drink changes how {name} talks, feels and behaves in company. It does not change {name}'s "
                     "consent: anything {name} would refuse sober, such as undressing or intimacy, {name} still "
                     "refuses, and drink is never a reason for {name} to agree to it.")


@dataclass
class DrinkState:
    level: float          # drinks still in their system
    recent_drinks: int    # drinks in the last COUNT_WINDOW_DAYS
    stage: str | None     # None when sober

    def prompt_line(self, name: str) -> str | None:
        if self.stage is None:
            return None
        description = next(text for minimum, stage, text in STAGES if stage == self.stage)
        drinks = f"{self.recent_drinks} drink{'s' if self.recent_drinks != 1 else ''}"
        return (f"{name} has had {drinks} in the last few hours and is {self.stage}: {description}. "
                + CONSENT_UNCHANGED.format(name=name))


def hazy(draft, stage: str | None):
    """A memory formed while drunk is less certain and says so. Returns the draft, changed in place."""
    if stage not in HAZE:
        return draft
    factor, note = HAZE[stage]
    draft.confidence = round(draft.confidence * factor, 2)
    draft.content = f"{draft.content} {note}"
    return draft


def stage_for(level: float) -> str | None:
    for minimum, stage, _ in STAGES:
        if level >= minimum:
            return stage
    return None


def level_from(drinks: list[tuple[float, int]], now_gt: float) -> float:
    """Drinks still in the system at ``now_gt`` from (game_time, quantity) pairs, cleared at a steady rate."""
    level, last = 0.0, None
    for game_time, quantity in sorted(drinks):
        if game_time > now_gt:
            break
        if last is not None:
            level = max(0.0, level - (game_time - last) / GAME_HOUR * WEAR_OFF_PER_HOUR)
        level += max(1, quantity)
        last = game_time
    if last is not None:
        level = max(0.0, level - (now_gt - last) / GAME_HOUR * WEAR_OFF_PER_HOUR)
    return round(level, 2)


def alcohol_events(db, npc_id: int, now_gt: float, since_gt: float) -> list[tuple[float, int]]:
    rows = db.execute(
        "SELECT game_time, json_extract(payload_json, '$.item'), COALESCE(json_extract(payload_json, '$.quantity'), 1)"
        " FROM v_visible_event WHERE type = 'consumption' AND json_extract(actors_json, '$[0]') = ?"
        " AND game_time > ? AND game_time <= ?", (npc_id, since_gt, now_gt)).fetchall()
    return [(gt, int(qty)) for gt, item, qty in rows if is_alcohol(item)]


def drink_state(db, npc_id: int, now_gt: float) -> DrinkState:
    # long enough back that anything older has worn off even at the highest stage
    lookback = max(COUNT_WINDOW_DAYS, 12.0 / WEAR_OFF_PER_HOUR * GAME_HOUR)
    drinks = alcohol_events(db, npc_id, now_gt, now_gt - lookback)
    level = level_from(drinks, now_gt)
    recent = sum(max(1, q) for gt, q in drinks if gt > now_gt - COUNT_WINDOW_DAYS)
    return DrinkState(level, recent, stage_for(level))


def drinks_seen(db, observer_id: int, drinker_id: int, now_gt: float) -> int:
    """How many alcoholic drinks ``observer_id`` saw ``drinker_id`` have in the last few hours."""
    rows = db.execute(
        "SELECT json_extract(e.payload_json, '$.item'), COALESCE(json_extract(e.payload_json, '$.quantity'), 1)"
        " FROM v_visible_event e JOIN event_witness w ON w.event_id = e.event_id AND w.npc_id = ?"
        " WHERE e.type = 'consumption' AND json_extract(e.actors_json, '$[0]') = ? AND e.game_time > ?"
        " AND e.game_time <= ?", (observer_id, drinker_id, now_gt - COUNT_WINDOW_DAYS, now_gt)).fetchall()
    return sum(max(1, int(q)) for item, q in rows if is_alcohol(item))
