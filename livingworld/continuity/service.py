"""The two continuity lookups. Plain SQL over the timeline-aware views; no model call."""
from __future__ import annotations

from typing import Any

from livingworld.continuity import MAX_PEOPLE, MAX_TOPIC

SHARED = "combat.fight"


def _ago(days: float) -> str:
    from livingworld.memory.recall import time_ago
    return time_ago(days)


def shared_history(db, npc_id: int, others: dict[int, str], now: float) -> list[str]:
    """One line per person present that this NPC has fought beside, most recent first. ``others``: id -> name."""
    out: list[tuple[float, str]] = []
    for other_id, name in others.items():
        if other_id == npc_id:
            continue
        rows = db.execute(
            "SELECT e.game_time, e.location FROM world_event e"
            " JOIN v_timeline_scope s ON s.timeline_id = e.timeline_id AND e.game_time <= s.cutoff_gt"
            " JOIN event_witness a ON a.event_id = e.event_id AND a.npc_id = ? AND a.how = 'participated'"
            " JOIN event_witness b ON b.event_id = e.event_id AND b.npc_id = ? AND b.how = 'participated'"
            " WHERE e.type = ? AND e.game_time <= ? ORDER BY e.game_time DESC, e.event_id DESC",
            (npc_id, other_id, SHARED, now + 1e-9)).fetchall()
        if not rows:
            continue
        n = len(rows)
        last_gt, last_loc = rows[0][0], rows[0][1]
        times = "once" if n == 1 else f"{n} times"
        where = f", near {last_loc}" if last_loc else ""
        out.append((last_gt, f"I have fought beside {name} {times}; the last time was {_ago(now - last_gt)}{where}. "
                             f"I know only that we fought, not who the enemies were or how it ended, unless a "
                             f"memory above says so."))
    out.sort(key=lambda t: -t[0])
    return [line for _, line in out[:MAX_PEOPLE]]


def topic_lines(db, npc_id: int, places: list[str], skip_mem_ids: set[int], now: float) -> tuple[list[str], list[int]]:
    """This NPC's own memories of the places the player just named, that the prompt does not already hold."""
    found: list[tuple[float, int, str]] = []
    seen: set[int] = set()
    for place in places:
        rows = db.execute(
            "SELECT mem_id, content, occurred_gt FROM v_visible_memory WHERE owner_id = ? AND occurred_gt <= ?"
            " AND (lower(COALESCE(location, '')) = lower(?) OR instr(lower(content), lower(?)) > 0)"
            " ORDER BY importance DESC, occurred_gt DESC LIMIT 4", (npc_id, now + 1e-9, place, place)).fetchall()
        for mem_id, content, occurred in rows:
            if mem_id in skip_mem_ids or mem_id in seen:
                continue
            seen.add(mem_id)
            found.append((occurred, mem_id, f"({_ago(now - occurred)}) {content}"))
    found.sort(key=lambda t: -t[0])
    top = found[:MAX_TOPIC]
    return [t for _, _, t in top], [m for _, m, _ in top]
