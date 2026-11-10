"""Fog: names and places stay unknown until someone tells you.

The game shows a named NPC the player has not met as a description ("Nord woman") instead of their name,
and the NPCs do not know the player's name until they are told it. Places the player hears about from an
NPC get their map marker added (undiscovered).

- ``names``: plain text rules (who said whose name in a line, the player introducing themselves).
- ``service``: the store side: who knows whose name (``name_known``), what each NPC's prompt says about it,
  and the names the game should show.
- ``places``: map markers for places an NPC mentions (``place_known``).

Learned names are on the save timeline. The game keeps its own copy in the save (the display names it
set), so loading an older save rolls both back together.

On with the Living World unless ``MANTELLA_LW_FOG=0``. Switched off, the game shows every name again.
"""
from __future__ import annotations

import os

ENV = "MANTELLA_LW_FOG"
_OFF = {"0", "false", "no", "off"}

# Sent with the conversation start reply
KEY_FOG_ON = "mantella_lw_fog_on"          # bool: hide the names of people the player has not met
KEY_FOG_KNOWN = "mantella_lw_fog_known"    # string[]: real names of the people the player knows
# Sent with an NPC line
KEY_FOG_REVEAL = "mantella_lw_fog_reveal"  # string[]: real names the player just learned
KEY_FOG_MARKER_IDS = "mantella_lw_fog_marker_ids"          # int[]: map markers to add (plugin-relative REFR ids)
KEY_FOG_MARKER_PLUGINS = "mantella_lw_fog_marker_plugins"  # string[]: the plugin of each
KEY_FOG_MARKER_NAMES = "mantella_lw_fog_marker_names"      # string[]: the place's name (for the notice)
# Sent by the game, per actor
KEY_FOG_LABEL = "mantella_lw_fog_label"    # 'Nord woman': what the game shows for this person while unknown


def fog_enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in _OFF
