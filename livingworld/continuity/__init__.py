"""Conversational continuity: the few extra facts an NPC needs so that earlier shared experience shows up in
what they say, without putting the world database in the prompt.

Two additions to the recall that already exists (``memory/recall.py`` picks the NPC's memories by importance and
recency and keeps what was said apart from what was seen):

- ``shared_history``: for each person actually in the conversation, one line when the NPC has fought beside them
  before (a patrol companion, a shared fight). Only the people present, only a line each.
- ``topic_lines``: when the player's line names a place, that NPC's own memories of that place that are not
  already in the prompt, for that turn only, at most two.

Both read only what the NPC took part in or remembers; nothing else can reach their prompt, and the lines say
what they do not know (the enemies, how it ended) so the NPC does not make it up.

On with the Living World unless ``MANTELLA_LW_CONTINUITY=0``.
"""
from __future__ import annotations

import os

ENV = "MANTELLA_LW_CONTINUITY"
_OFF = {"0", "false", "no", "off"}

MAX_PEOPLE = 4     # shared-history lines per NPC
MAX_TOPIC = 2      # topic memories per player line


def continuity_enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in _OFF
