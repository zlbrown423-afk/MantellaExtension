"""Who may have a personality profile: adult women only, and only when being an adult can be established.

A person is eligible when all of these hold:
* not the player, and the game says female;
* not a child by any rule in ``store/minors.py`` (child race, child voice, Mantella's child list, the game's flag);
* her race is one of Skyrim's adult person races (vampire variants and the elderly included).

If her race is missing or unknown (a creature, a modded race, an NPC registered without a race), being an adult is
not established and she gets no profile, unless the settings file confirms it for her by hand
(``"adult_confirmed": true``). That confirmation can never switch a profile on for anyone ``minors.py`` marks a child.
"""
from __future__ import annotations

from dataclasses import dataclass

from livingworld.store.minors import is_child

ADULT_RACES = ("nord", "imperial", "breton", "redguard", "highelf", "altmer", "woodelf", "bosmer", "darkelf",
               "dunmer", "orc", "orsimer", "khajiit", "argonian", "elder", "snowelf")


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    why: str


def adult_race(race: str | None) -> bool:
    flat = (race or "").lower().replace(" ", "").replace("-", "").replace("_", "")
    if not flat or "child" in flat:
        return False
    return any(r in flat for r in ADULT_RACES)


def check(db, npc_id: int, adult_confirmed: bool = False) -> Eligibility:
    row = db.execute("SELECT is_player, sex, race FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()
    if row is None:
        return Eligibility(False, "unknown person")
    if row[0]:
        return Eligibility(False, "the player")
    if is_child(db, npc_id):
        return Eligibility(False, "a child")
    if row[1] != "female":
        return Eligibility(False, "not a woman" if row[1] else "sex unknown")
    if adult_race(row[2]):
        return Eligibility(True, "adult woman")
    if adult_confirmed:
        return Eligibility(True, "adult confirmed in the settings file")
    return Eligibility(False, f"age cannot be established (race {row[2]!r})")
