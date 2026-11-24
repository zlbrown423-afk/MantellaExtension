"""Small shared helpers for the diplomacy package (names, presence, memories, standing, events)."""
from __future__ import annotations

import json

from livingworld.store.minors import is_child


def name_of(db, npc_id: int | None) -> str:
    if npc_id is None:
        return "someone"
    row = db.execute("SELECT display_name FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()
    return row[0] if row else "someone"


def is_player(db, npc_id: int | None) -> bool:
    if npc_id is None:
        return False
    row = db.execute("SELECT is_player FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()
    return bool(row and row[0])


def conversation_timeline(db, conv_id: int) -> int:
    return db.execute("SELECT timeline_id FROM conversation WHERE conv_id = ?", (conv_id,)).fetchone()[0]


def active_timeline(db) -> int:
    return db.execute("SELECT timeline_id FROM timeline WHERE is_active = 1").fetchone()[0]


def present_at(db, conv_id: int, seq: int) -> list[int]:
    """Everyone in the conversation when line ``seq`` was spoken (children left out), player included."""
    out = []
    for npc_id, joined, left in db.execute("SELECT npc_id, joined_seq, left_seq FROM participant WHERE conv_id = ?"
                                           " ORDER BY npc_id", (conv_id,)):
        if seq > joined and (left is None or seq <= left) and not is_child(db, npc_id):
            out.append(npc_id)
    return out


def remember(db, timeline: int, owner: int, content: str, about: list[int], gt: float, importance: float,
             valence: float, source_kind: str, event_id: int | None = None, location: str | None = None) -> int | None:
    """A first-person memory for ``owner`` that a rule wrote (never for the player or a child)."""
    if is_player(db, owner) or is_child(db, owner):
        return None
    return db.execute(
        "INSERT INTO memory (timeline_id, owner_id, kind, content, about_ids_json, location, occurred_gt, importance,"
        " valence, intensity, created_by, source_kind, confidence, event_id)"
        " VALUES (?, ?, 'episodic', ?, ?, ?, ?, ?, ?, ?, 'consolidation', ?, 1.0, ?)",
        (timeline, owner, content, json.dumps(sorted(set(about))), location, gt, importance, valence, abs(valence),
         source_kind, event_id)).lastrowid


def standing(db, timeline: int, holder: int, subject: int, domain: str, delta: float, reason: str, gt: float,
             firsthand: bool = True, event_id: int | None = None) -> bool:
    """One change in how ``holder`` sees ``subject`` in ``domain`` (reputation layers). Never for the player's own
    view, a child, or someone about themselves. Returns True when written."""
    if holder == subject or is_player(db, holder) or is_child(db, holder) or is_child(db, subject):
        return False
    db.execute("INSERT INTO standing_change (timeline_id, holder_id, subject_id, domain, delta, firsthand,"
               " cause_event_id, reason, game_time) VALUES (?,?,?,?,?,?,?,?,?)",
               (timeline, holder, subject, domain, max(-1.0, min(1.0, delta)), int(firsthand), event_id, reason, gt))
    return True


def event(db, timeline: int, etype: str, payload: dict, gt: float, location: str | None = None) -> int:
    """A diplomacy world event. actors are left empty on purpose, so the gossip appraisal never turns it into a
    second claim (public statements get their claim from ``record.py``)."""
    return db.execute("INSERT INTO world_event (timeline_id, type, location, actors_json, payload_json, source,"
                      " game_time) VALUES (?, ?, ?, '[]', ?, 'sim', ?)",
                      (timeline, etype, location, json.dumps(payload, sort_keys=True), gt)).lastrowid


def event_exists(db, etype: str, key: str) -> bool:
    return db.execute("SELECT 1 FROM v_visible_event WHERE type = ? AND json_extract(payload_json, '$.key') = ?"
                      " LIMIT 1", (etype, key)).fetchone() is not None


def days_ago(now: float, then: float) -> str:
    d = max(0.0, now - then)
    if d < 0.5:
        return "earlier today"
    if d < 1.5:
        return "yesterday"
    if d < 13.5:
        return f"{int(round(d))} days ago"
    if d < 60:
        return f"{int(round(d / 7))} weeks ago"
    return f"{int(round(d / 30))} months ago"
