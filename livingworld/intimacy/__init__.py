"""Intimate scenes played by OStim: NPCs know when one is happening, what is happening in it, and remember it.

The game (Mantella-Spell) listens to OStim's own thread events. While a scene runs, the player's hands on a
partner (HIGGS grabs) and slaps or spanks are counted into the scene instead of being sent as grabs and hits,
so nobody in it takes them as an assault, nobody pulls away or calls a guard, and none of it becomes gossip
about the player's rough hands. When the scene ends the game sends one ``scene`` record (see
``perception/records.py``) and ``scenes.py`` turns it into memories. While it runs, the game sends a
snapshot with every request so a conversation knows what is going on.

Only an active scene the NPC is part of changes anything: grabs and hits outside a scene, or on someone who
is not in the player's scene, are handled exactly as before. A scene OStim plays as aggressive is
remembered as being forced, never as something wanted.

``MANTELLA_LW_INTIMACY=0`` turns it off (the game then sends grabs and hits as before).
"""
from __future__ import annotations

import os

ENV = "MANTELLA_LW_INTIMACY"
_OFF = {"0", "false", "no", "off"}


def intimacy_enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in _OFF
