"""Each person's own bathing habits: how clean they want to be, how often they bathe, how dirty their work makes them.

Defaults come from the person's work (guessed from their name and bio), spread per person by a random
draw seeded with their form id, so the same NPC keeps the same habits in every save and two people with
the same job still differ. Sex is never an input. Any value can be set by hand in the NPC's
``persona_version.traits_json`` under the same key, and that always wins.
"""
from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass

# (words in the name or bio, dirt per game day from work, hygiene standard, usual days between baths)
OCCUPATIONS: list[tuple[tuple[str, ...], float, float, float]] = [
    (("miner", "mine"), 0.35, 0.35, 4.0),
    (("blacksmith", "smith", "smelter", "tanner"), 0.30, 0.40, 3.5),
    (("farmer", "farmhand", "farm", "lumber", "woodcutter", "mill", "stable"), 0.25, 0.40, 3.5),
    (("hunter", "trapper", "fisher", "fisherman"), 0.25, 0.40, 4.0),
    (("beggar", "vagrant", "skooma"), 0.20, 0.15, 9.0),
    (("bandit", "outlaw", "forsworn", "necromancer"), 0.25, 0.15, 10.0),
    (("guard", "soldier", "legion", "stormcloak", "housecarl", "mercenary", "sellsword", "warrior"), 0.20, 0.50, 3.5),
    (("innkeeper", "barkeep", "bard", "cook", "server", "merchant", "trader", "shopkeeper", "fletcher"),
     0.12, 0.60, 2.5),
    (("jarl", "steward", "thane", "noble", "court", "priest", "priestess", "healer", "scholar", "mage",
      "wizard", "alchemist", "apothecary"), 0.08, 0.75, 2.0),
]
DEFAULT = (0.15, 0.50, 3.0)
PLAYER_DIRT_RATE = 0.10        # the player gets dirty more slowly; they bathe by swimming or a bathing mod

WINDOWS = {"dawn": 0.25, "evening": 0.80, "night": 0.92}   # time of day, as a fraction of a day


@dataclass(frozen=True)
class Habits:
    hygiene_standard: float     # 0..1: how clean they want to be
    bath_interval_days: float   # usual gap between baths
    work_dirt_rate: float       # dirt per game day from daily life and work
    bath_window: str            # 'dawn' | 'evening' | 'night'

    @property
    def threshold(self) -> float:
        """The dirt level that sends them to bathe early: 0.3 for the fussy, about 0.75 for those who don't mind."""
        return 0.75 - 0.45 * self.hygiene_standard

    def opinion(self) -> str:
        if self.hygiene_standard >= 0.65:
            return "particular about cleanliness"
        if self.hygiene_standard <= 0.35:
            return "not bothered by a bit of dirt"
        return ""


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def occupation_defaults(text: str) -> tuple[float, float, float]:
    words = set(re.findall(r"[a-z]+", (text or "").lower()))
    for keys, rate, standard, interval in OCCUPATIONS:
        if words & set(keys):
            return rate, standard, interval
    return DEFAULT


def habits_for(db, npc_id: int) -> Habits:
    row = db.execute("SELECT display_name, plugin, base_form_id, is_player FROM npc WHERE npc_id = ?",
                     (npc_id,)).fetchone()
    if row is None:
        return Habits(0.5, 3.0, DEFAULT[0], "evening")
    name, plugin, base, is_player = row[0], row[1], row[2], bool(row[3])
    persona = db.execute("SELECT bio, traits_json FROM persona_version WHERE npc_id = ? ORDER BY version DESC LIMIT 1",
                         (npc_id,)).fetchone()
    bio, traits = (persona[0], json.loads(persona[1] or "{}")) if persona else ("", {})
    rate, standard, interval = occupation_defaults(f"{name} {bio}")
    draw = random.Random(f"hygiene:{plugin}:{base}")
    standard = _clamp(standard + draw.gauss(0, 0.15), 0.05, 0.95)
    interval = max(1.0, interval * (1.35 - 0.7 * standard) * draw.uniform(0.85, 1.2))
    rate = rate * draw.uniform(0.8, 1.2)
    window = draw.choice(["evening", "evening", "dawn", "night"])
    if is_player:
        rate = PLAYER_DIRT_RATE
    return Habits(
        hygiene_standard=float(traits.get("hygiene_standard", standard)),
        bath_interval_days=float(traits.get("bath_interval_days", interval)),
        work_dirt_rate=float(traits.get("work_dirt_rate", rate)),
        bath_window=str(traits.get("bath_window", window)) if str(traits.get("bath_window", window)) in WINDOWS
        else window)
