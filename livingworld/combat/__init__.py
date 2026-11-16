"""Shared fights: what each NPC remembers of a fight they were in beside the player.

The game sends one ``fight`` record per person who fought (the player and each adult ally) once the player's
fight ends, with the lowest share of health they fell to. ``fights.py`` groups the records of one fight into a
single ``combat.fight`` world event and gives each NPC their own first-person memory of it: who stood beside
them, and how badly they got hurt. Two people in the same fight remember it differently.

What the game does not send, and this module therefore never claims: who the enemies were, how many, who won,
or who died (a dead actor sends nothing). Memories say "I fought beside X", never "we beat the bandits".
See the README (Shared fights) for the record fields the game would need to send for more.

On with the Living World unless ``MANTELLA_LW_COMBAT=0``. It only runs for a fight in which the player took part.
"""
from __future__ import annotations

import os

ENV = "MANTELLA_LW_COMBAT"
_OFF = {"0", "false", "no", "off"}

# Fight records of one fight arrive together when it ends; records this close in game days (about 7 game
# minutes) and in the same place belong to the same fight.
WINDOW_DAYS = 0.005


def combat_enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in _OFF
