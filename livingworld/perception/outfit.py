"""Outfit manager: followers keep their clothes on when the player changes area or loads a save.

Skyrim re-applies a follower's default outfit whenever her 3D loads again (a door, fast travel, a load).
Anything the player gave her is unequipped, and if her own outfit pieces were taken off her (traded away,
or never put back after a scene) she is left naked. Lydia walking out of Breezehome with nothing on is
the usual case.

The work is all game side (Mantella-Spell ``MantellaRepository`` ``LwOutfit*``): for each current follower
(an adult the game calls a player teammate) it keeps a snapshot of the playable armor she wears, taken
only while she is dressed and settled. Right after she or the player changes area, after a save load and
after a dismissal, any snapshot piece still in her inventory but not worn goes back on. A deliberate
undress (the Living World Undress action, the undress yes/no prompt, an OStim scene) is left alone while
she stays where it happened; once she follows the player somewhere else she is dressed again. Each
re-dress queues an attire record with ``via=outfit``, so the server closes her "seen naked" state without
new memories, and is traced in Papyrus.0.log as ``Mantella Living World outfit:``.

The game's start reply carries ``mantella_lw_outfit_on`` and ``mantella_lw_outfit_notices``;
``MANTELLA_LW_OUTFIT_MANAGER=0`` switches the manager off, ``MANTELLA_LW_OUTFIT_NOTICES=0`` keeps it on
without the "Lydia put her gear back on" notification. The game remembers the last values it was sent
(on by default), so a change takes effect from the next conversation.
"""
from __future__ import annotations

import os

ENV = "MANTELLA_LW_OUTFIT_MANAGER"
NOTICES_ENV = "MANTELLA_LW_OUTFIT_NOTICES"
KEY_OUTFIT_ON = "mantella_lw_outfit_on"
KEY_OUTFIT_NOTICES = "mantella_lw_outfit_notices"
VIA = "outfit"   # attire record sent after the game dressed a follower again

_OFF = {"0", "false", "no", "off"}


def enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in _OFF


def notices() -> bool:
    return os.environ.get(NOTICES_ENV, "").strip().lower() not in _OFF


def start_keys(living_world_on: bool) -> dict[str, bool]:
    """The two switches for the conversation start reply."""
    on = bool(living_world_on and enabled())
    return {KEY_OUTFIT_ON: on, KEY_OUTFIT_NOTICES: bool(on and notices())}
