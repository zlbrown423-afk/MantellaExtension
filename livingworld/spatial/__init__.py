"""Spatial and assignment context: what the party met, and whether it is what someone sent them to deal with.

A duty names the places it points at (``where``). A fight the squad has somewhere else is not that job, and
the NPC who gave the job should not treat it as done, nor as nothing: they can say "that is not where our
scouts reported trouble". This package records each fight against the member's assignment and gives the giver
one note about it, the next time the player talks to them.

It only claims what place names show: at an assigned place, on the open road (for road work), in the wilds with
no place name, or at some other named place. There is no map geometry, so nothing says "near" or "far".
Quest objectives, task state and vanilla quests are never changed.

On with the Living World unless ``MANTELLA_LW_SPATIAL=0``.
"""
from __future__ import annotations

import os

ENV = "MANTELLA_LW_SPATIAL"
_OFF = {"0", "false", "no", "off"}

MAX_TOLD = 3   # reports in one note: more than that is a list, not a remark


def spatial_enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in _OFF
