"""Body language, step 1 (BL1): proximity and gaze.

The game watches where the player stands relative to nearby NPCs and reacts on its own, at once, from a
small per-NPC "expression profile" (comfort distance, how readily they hold the player's gaze, how
guarded they get, whether they step back). It reports what the player did as ``type=presence`` records.
The server turns those into short-lived feelings (``state.py``), works out the next profile
(``presence.py``) and tells the dialogue model, in neutral words, what the NPC noticed.

Loop guards (no reaction can feed itself):
- Only the player's behaviour is ever a percept. An NPC's own reactions (stepping back, looking away,
  folding their arms) are never sent back as presence records, and NPCs do not react to each other.
- The game uses hysteresis (tense inside the comfort distance, relax only 30% beyond it), cooldowns per
  reaction, and a cap on reactions per minute; NPCs never step toward the player on their own.
- Each record is stored by key once, and each observation reaches the dialogue model once.

Step 2 (BL2) adds the player's hand and head gestures, read in VR through VRIK: wave, nod, head shake,
point, beckon, hands up, thumbs up and thumbs down. They are presence records like the rest, so the same
guards hold (the game adds a cooldown per gesture).

``MANTELLA_LW_BODY_LANGUAGE=0`` turns all of it off, the mood clamp included. ``MANTELLA_LW_GESTURES=0``
turns off only the gestures, and ``MANTELLA_LW_GESTURE_NOTICES=0`` stops the game showing each gesture it
recognised (on while gestures are being tuned).
"""
from __future__ import annotations

import os

ENV = "MANTELLA_LW_BODY_LANGUAGE"
ENV_GESTURES = "MANTELLA_LW_GESTURES"
ENV_GESTURE_NOTICES = "MANTELLA_LW_GESTURE_NOTICES"
_OFF = {"0", "false", "no", "off"}


def body_language_enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in _OFF


def gestures_enabled() -> bool:
    return body_language_enabled() and os.environ.get(ENV_GESTURES, "").strip().lower() not in _OFF


def gesture_notices() -> bool:
    return os.environ.get(ENV_GESTURE_NOTICES, "").strip().lower() not in _OFF
