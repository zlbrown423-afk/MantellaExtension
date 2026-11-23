"""The relationship dimensions ``v_relationship`` reads, and a check for change rows that use any other.

``relationship`` stores a durable base (trust, affinity, respect, attraction, fear, familiarity, plus status and
kinship). Every later change is a ``relationship_change`` row whose ``deltas_json`` the view sums, with fading,
on the active timeline: that view *is* the materialised state, so a change is never "applied" to the base row.
Dimensions with no base column (admiration, suspicion, intimacy, commitment, satisfaction, jealousy) exist only
as those sums. A key the view does not read would be silently ignored, so ``unknown_dimensions`` reports it.
"""
from __future__ import annotations

import json

DIMENSIONS = ("trust", "affinity", "respect", "attraction", "fear", "familiarity",
              "admiration", "suspicion", "intimacy", "commitment", "satisfaction", "jealousy")


def unknown_keys(deltas: dict) -> list[str]:
    return sorted(k for k in deltas if k not in DIMENSIONS)


def unknown_dimensions(db) -> dict[str, int]:
    """Every key in any change row that ``v_relationship`` does not read, with how many rows use it."""
    out: dict[str, int] = {}
    for (raw,) in db.execute("SELECT deltas_json FROM relationship_change"):
        try:
            keys = unknown_keys(json.loads(raw) or {})
        except (TypeError, ValueError, AttributeError):
            keys = ["<not a JSON object>"]
        for k in keys:
            out[k] = out.get(k, 0) + 1
    return out
