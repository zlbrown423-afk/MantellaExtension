"""The shared event processor: each consumer reads ``world_event`` from where it last stopped
(``event_cursor``), so every event is handled once, even across crashes and restarts.

Only events visible on the active save timeline are handled; events on a timeline an older save left
behind are skipped for good. Consumers so far: ``appraisal`` (claims from perceived events) and ``work`` (task progress). Goals,
player knowledge and body language will register here too.
"""
from __future__ import annotations

from typing import Callable

from livingworld.social import claims
from livingworld.work import progress as work_progress

Consumer = Callable[[object, object], object]

CONSUMERS: dict[str, Consumer] = {
    "appraisal": claims.appraise,
    "work": work_progress.consume,   # task progress from items handed over and work done (livingworld/work)
}


def process(db, consumers: dict[str, Consumer] | None = None) -> dict[str, int]:
    """Run every consumer over the events it has not seen yet. Call inside a transaction."""
    handled = {}
    newest = db.execute("SELECT COALESCE(MAX(event_id), 0) FROM world_event").fetchone()[0]
    for name, consume in (consumers or CONSUMERS).items():
        row = db.execute("SELECT last_event_id FROM event_cursor WHERE consumer = ?", (name,)).fetchone()
        last = row[0] if row else 0
        if last >= newest:
            continue
        events = db.execute("SELECT * FROM v_visible_event WHERE event_id > ? AND event_id <= ? ORDER BY event_id",
                            (last, newest)).fetchall()
        for event in events:
            consume(db, event)
        db.execute("INSERT INTO event_cursor (consumer, last_event_id) VALUES (?, ?)"
                   " ON CONFLICT(consumer) DO UPDATE SET last_event_id = excluded.last_event_id", (name, newest))
        handled[name] = len(events)
    return handled
