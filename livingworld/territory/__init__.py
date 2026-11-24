"""Territory (design doc 2026-11-24): cleared bandit camps held by the Thalmor or by the Plains Wardens.

Split of work, on purpose:

* the GAME (LWTerritory.esp + Papyrus, in the save) owns who holds each site, every timer, every soldier and all
  spawning. Living World cannot run any of that: it only hears from the game during a conversation.
* LIVING WORLD (this package) keeps a read-only mirror the game feeds with ``type=territory`` perception
  records, and what each person has seen, been reported or heard about it, with source and confidence. It never
  moves a soldier or changes an owner. Nobody is omniscient: a person only knows what reached them.

World events are ``territory.<kind>`` (occupied, reinforced, cleared, withdrawn, established, contested,
abandoned, sighted) with payload ``location_key, location_name, owner_before, owner_after, strength, contested,
source, confidence`` (agreed with the diplomacy thread, which may cite them later).

OFF unless ``MANTELLA_LW_TERRITORY=1``. Children never take part.
"""
from __future__ import annotations

import os

ENV = "MANTELLA_LW_TERRITORY"
DEBUG_ENV = "MANTELLA_LW_TERRITORY_DEBUG"
_ON = {"1", "true", "yes", "on"}
_OFF = {"0", "false", "no", "off"}

OWNERS = ("unoccupied", "bandit", "thalmor", "wardens", "contested")
FACTIONS = ("thalmor", "wardens")
EVENT_PREFIX = "territory."


def territory_enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() in _ON


def territory_debug_enabled() -> bool:
    return territory_enabled() and os.environ.get(DEBUG_ENV, "").strip().lower() not in _OFF
