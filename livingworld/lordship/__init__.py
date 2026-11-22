"""Territorial lordship and delegated military governance (design doc 2026-11-20, milestone L1 "The Charter").

The player, as Thane and founder of the Plains Wardens (the cavalry training grounds mod), holds a charter from
Jarl Balgruuf: terms (stipend, allowances, personnel cap, patrols, mobilisation, reports, review date), a
district (the Western Plains) and powers. The server owns all of it; Papyrus will only report facts (L2 bridge).

Every 30 game days a rules-only reckoning (``reckoning.py``, no LLM) judges the terms, pays or withholds the
stipend on the paper ledger (``livingworld/civic``), recomputes the seven influence measures, power, legitimacy
and the gap between them, updates how each official in ``data/court.json`` sees the Wardens, and lets at most one
official make a political move (withhold money, urge a review, suspend or restore the charter, commend). Nothing
here forces an outcome: the charter's status and the officials' stances are where the numbers and the player's
choices put them. Reckonings are caught up lazily at conversation start, never on a timer.

Officials in the conversation get their position and reasons in their prompt; the LLM only voices them.

OFF unless ``MANTELLA_LW_LORDSHIP=1``. Test commands ("debug charter ...") follow ``MANTELLA_LW_LORDSHIP_DEBUG``
(on with lordship unless set to 0). Children never take part.
"""
from __future__ import annotations

import os

ENV = "MANTELLA_LW_LORDSHIP"
DEBUG_ENV = "MANTELLA_LW_LORDSHIP_DEBUG"
_ON = {"1", "true", "yes", "on"}
_OFF = {"0", "false", "no", "off"}

COMPANY = "plains_wardens"
COURT = "whiterun_court"


def lordship_enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() in _ON


def lordship_debug_enabled() -> bool:
    return lordship_enabled() and os.environ.get(DEBUG_ENV, "").strip().lower() not in _OFF
