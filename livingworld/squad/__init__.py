"""Temporary squads: the player's walking party, asked in conversation and tracked in the Living World database.

What this is. The player says "Brand, come with me" to a guard. Mantella already holds the NPCs of an open
conversation near the player (its participant AI package); this module adds the social layer on top:

- who was asked, who agreed (by what the NPC says, never assumed), who declined,
- one squad per player at a time, one squad per NPC, each NPC a person of their own (npc_id),
- a formation setting (loose, close, autonomous, patrol) the player can change by saying so,
- dismissal by word ("stand down", "you're dismissed"),
- prompt lines so each member knows they are walking with the player and with whom.

What it is not. Nobody is put in a follower faction, and no AI package or combat behaviour is changed, so
vanilla Skyrim combat and routines stay as they are. Members are held only while the conversation that holds
them stays open; when it ends the squad ends with it (status 'dropped'). Formation setting is recorded and
told to the NPCs in their prompts; moving them differently needs game-side work (see README, Squads).

On with the Living World unless ``MANTELLA_LW_SQUAD=0``.
"""
from __future__ import annotations

import os

ENV = "MANTELLA_LW_SQUAD"
_OFF = {"0", "false", "no", "off"}

FORMATIONS = ("loose", "close", "autonomous", "patrol")
DEFAULT_FORMATION = "loose"
MAX_SQUAD = 6   # the player plus this many: more than a patrol the conversation hold can keep up with


def squad_enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in _OFF
