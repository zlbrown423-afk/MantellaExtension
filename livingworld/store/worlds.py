"""Which world (database file and Mantella summary folder) a conversation belongs to.

Mantella names a world after the player's display name plus the MCM World ID ("ZacharyLex1"). That
breaks in two ways:

* Renaming the character (or a new game that has not set the name yet, so it runs as "Adventurer"
  or "Prisoner" for the first stretch) starts a fresh, empty world: NPCs forget everything said
  under the old name.
* Every new game starts with the same default name, so two playthroughs share the default world.

The fix is a world key that the game keeps in the save (Papyrus stores it in MantellaRepository, like
the timeline id) and sends with each conversation start. The key is the world's name itself, so no
separate index file can get lost or out of step:

* a request that carries a key uses that world, whatever the player is called now;
* a request from a game that knows about keys but has none yet:
  - a fresh New Game (no save has been loaded in this session, so no load nonce) gets a world of its
    own: the name-based world, or "<name>-2", "-3"... when that one already exists;
  - a loaded save from before keys existed keeps the name-based world, as before;
* a request from an older game script that never sends the key field keeps the old behaviour.

The game is told the world in every start reply and keeps it from then on. Changing the World ID
slider in the MCM clears the key on the game side, so that still starts a separate world.
"""
from __future__ import annotations

import glob
import os
import re

KEY_WORLD_KEY = "mantella_lw_world_key"   # world kept in the save; "" until the server names one

_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,79}$")
_CLEAN = re.compile(r"[^A-Za-z0-9-]+")


def clean_key(value: object) -> str:
    """The key as sent by the game, or "" when it is missing or not a safe file name."""
    text = _CLEAN.sub("", str(value or ""))
    return text if _SAFE.match(text) else ""


def world_exists(folder: str, world: str) -> bool:
    """A world is taken once it has a database file or a Mantella summary folder."""
    if os.path.exists(os.path.join(folder, "data", "livingworld", f"{world}.sqlite")):
        return True
    pattern = os.path.join(glob.escape(folder), "data", "*", "conversations", glob.escape(world))
    return any(os.path.isdir(p) for p in glob.glob(pattern))


def fresh_world(folder: str, name_world: str) -> str:
    if not world_exists(folder, name_world):
        return name_world
    n = 2
    while world_exists(folder, f"{name_world}-{n}"):
        n += 1
    return f"{name_world}-{n}"


def resolve_world(folder: str, name_world: str, request: dict) -> str:
    """The world this start request belongs to (see the module notes)."""
    if KEY_WORLD_KEY not in request:
        return name_world                       # older game scripts: names only
    key = clean_key(request.get(KEY_WORLD_KEY))
    if key:
        return key
    new_game = not request.get("mantella_lw_load_nonce")
    return fresh_world(folder, name_world) if new_game else name_world
