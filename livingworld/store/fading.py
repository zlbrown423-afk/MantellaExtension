"""Relationships and reputations fade (architecture audit M4); the switch and the clock behind it.

The fading itself is in the views ``v_relationship`` and ``v_standing`` (migration 026, where every rate is
listed): each change counts less as it ages, down to a small lasting mark, so a feeling left without contact
drifts back toward where the relationship started. Kin, spouses and partners do not fade. ``fade_clock`` holds
"now" (kept by triggers: the start of the newest conversation, or the game time of the save just loaded) and
whether fading is on.

``MANTELLA_LW_FADE=0`` turns fading off: the views then show the plain sums exactly as before. The setting is
written to the world database each time a world opens, so it takes effect on the next server start. Nothing is
deleted either way; ``v_relationship_raw`` and ``v_standing_raw`` always show the unfaded sums.
"""
from __future__ import annotations

import os
import sqlite3

ENV_VAR = "MANTELLA_LW_FADE"


def enabled() -> bool:
    return os.environ.get(ENV_VAR, "").strip().lower() not in ("0", "false", "no", "off")


def apply_setting(conn: sqlite3.Connection) -> None:
    """Write the env setting into ``fade_clock`` when it differs (no write when nothing changed)."""
    want = 1 if enabled() else 0
    try:
        row = conn.execute("SELECT enabled FROM fade_clock WHERE id = 1").fetchone()
    except sqlite3.OperationalError:   # a database from before migration 026 (never the case after migrate())
        return
    if row is None:
        conn.execute("INSERT INTO fade_clock (id, enabled, now_gt) VALUES (1, ?, NULL)", (want,))
    elif row[0] != want:
        conn.execute("UPDATE fade_clock SET enabled = ? WHERE id = 1", (want,))
