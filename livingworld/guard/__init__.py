"""Living World guard careers (G1: joining a hold's guard).

The player can ask a hold's guard leader to take them on. Whether the leader says yes is worked out by
rules (``recruit.py``) from what is already stored: the leader's own view of the player, what they have
heard about them, any bounty in that hold, and whether the player already serves elsewhere. The LLM only
voices the answer. A yes starts a ``service`` (Recruit, on probation), hands out the hold's uniform and
writes a ``guard.enlisted`` event that gossip turns into news.

Membership lives in the Living World database only. The player is never added to a vanilla guard or
crime faction, so arrests, bounties and guard AI behave exactly as before.

Each hold's guard is configured in ``data/orgs.json``. Generic guards the player meets get a stable
personal name (``profiles.py``). Rank, status and history ride the save timeline like tasks do.

On with Living World unless ``MANTELLA_LW_GUARD=0``. Children never recruit, serve or witness here.
"""
from __future__ import annotations

import os

GUARD_ENV = "MANTELLA_LW_GUARD"
GUARD_DEBUG_ENV = "MANTELLA_LW_GUARD_DEBUG"
_OFF = {"0", "false", "no", "off"}


def guard_enabled() -> bool:
    return os.environ.get(GUARD_ENV, "").strip().lower() not in _OFF


def guard_debug_enabled() -> bool:
    return guard_enabled() and os.environ.get(GUARD_DEBUG_ENV, "").strip().lower() not in _OFF
