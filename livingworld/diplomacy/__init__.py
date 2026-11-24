"""Diplomacy: a memory of politics, not a script (design doc 2026-11-24, "Living World diplomacy: audit and design").

The player is trying to talk the Empire and the Stormcloaks into working together against the Thalmor. This
package remembers what was actually said about that, by whom, to whom, in front of whom; what each person
believes (kept apart from what they say in public and from what their faction officially holds); what was
promised, and whether it was kept; and the meetings people propose, who was invited and how they answered.

It never decides an outcome. Nothing here moves anyone toward agreement: the dialogue model voices each NPC
from their own convictions, history and interests, and the rules only write down what happened and work out
who could know it.

- No new AI calls: statements are read by the memory extraction that already runs after each conversation
  (``extract.py``), and only for conversations that touch the war or its leaders.
- Everything is on the save timeline (migration 030), so loading an older save rolls the politics back.
- Each NPC's prompt block holds only rows that NPC holds (``prompts.py``); private statements never reach anyone
  who was not there.

OFF unless ``MANTELLA_LW_DIPLOMACY=1``. Test commands ("debug diplomacy ...") follow
``MANTELLA_LW_DIPLOMACY_DEBUG`` (on with diplomacy unless set to 0). Children never take part.
"""
from __future__ import annotations

import os

ENV = "MANTELLA_LW_DIPLOMACY"
DEBUG_ENV = "MANTELLA_LW_DIPLOMACY_DEBUG"
_ON = {"1", "true", "yes", "on"}
_OFF = {"0", "false", "no", "off"}

DIMENSIONS = ("empire", "stormcloaks", "thalmor", "negotiation", "concordat", "ceasefire", "alliance")
KINDS = ("proposal", "position", "support", "opposition", "condition", "commitment", "refusal", "request",
         "invitation", "invitation_response", "meeting_location", "goodwill", "retraction", "fulfilment",
         "disagreement", "agreement", "report")
ISSUES = ("ceasefire", "alliance", "concordat", "meeting", "negotiation", "empire", "stormcloaks", "thalmor",
          "prisoners", "territory", "other")
MODALITIES = ("asserted", "conditional", "hypothetical", "question", "reported", "rumor")
RESPONSES = ("accepted", "conditional", "declined", "countered")
FACTIONS = ("empire", "stormcloaks", "thalmor", "whiterun", "player", "other")

# an issue that is also a dimension: a position, support or opposition on it moves that dimension
ISSUE_DIMENSION = {i: i for i in ISSUES if i in DIMENSIONS}

MAX_ITEMS = 8              # per conversation, after validation


def diplomacy_enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() in _ON


def diplomacy_debug_enabled() -> bool:
    return diplomacy_enabled() and os.environ.get(DEBUG_ENV, "").strip().lower() not in _OFF
