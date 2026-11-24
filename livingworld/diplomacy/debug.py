"""Test commands said or typed in a conversation: "debug diplomacy <verb>". The result goes to the server log.

    debug diplomacy status      counts of statements, promises, meetings
    debug diplomacy journal     the player's diplomatic journal (what the player has learned)
    debug diplomacy catch up    run the catch-up now (lapses, couriers, meetings)
"""
from __future__ import annotations

import re

from livingworld.diplomacy import catchup, journal

COMMANDS = ("status", "journal", "catch", "catchup")
OPEN_PROMISES = "SELECT COUNT(*) FROM v_dip_commitment WHERE status = 'open'"
_LINE = re.compile(r"^\W*debug\s+diplomacy\b\s*(\w+)?", re.I)


def parse(text: str | None) -> str | None:
    if not text:
        return None
    m = _LINE.match(text)
    if not m:
        return None
    verb = (m.group(1) or "status").lower()
    if verb not in COMMANDS:
        return None
    return "catchup" if verb == "catch" else verb


def status_text(db) -> str:
    def n(sql):
        return db.execute(sql).fetchone()[0]
    return (f"{n('SELECT COUNT(*) FROM v_dip_statement')} statements, "
            f"{n('SELECT COUNT(DISTINCT npc_id) FROM v_political_position')} people with positions, "
            f"{n(OPEN_PROMISES)} open promises, "
            f"{n('SELECT COUNT(*) FROM v_dip_meeting')} meetings")


def run(db, text: str, player_id: int | None, now: float, present: list[int], location: str | None,
        conv_id: int | None) -> str | None:
    verb = parse(text)
    if verb is None:
        return None
    if verb == "status":
        return status_text(db)
    if verb == "journal":
        return "\n" + journal.render(journal.player_view(db, player_id, now))
    if verb == "catchup":
        return str(catchup.run(db, now, present, location, conv_id))
    return None
