"""The task store and lifecycle.

States: available -> offered -> accepted -> in_progress <-> paused -> completed | failed | expired.
A declined offer goes back to available. Every change is a ``task_change`` row plus a ``task.<state>``
world event, both on the active save timeline; ``v_task`` gives the current view. All functions take an
open connection inside a transaction (``WorldStore.transaction()``).
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from livingworld.store.minors import is_child
from livingworld.work.templates import Template

STATES = ("available", "offered", "accepted", "in_progress", "paused", "completed", "failed", "expired")
OPEN = ("available", "offered", "accepted", "in_progress", "paused")
TAKEN = ("accepted", "in_progress", "paused")
DONE = ("completed", "failed", "expired")

ALLOWED: dict[str, set[str]] = {
    "available": {"offered", "accepted", "expired"},
    "offered": {"available", "accepted", "expired"},
    "accepted": {"in_progress", "paused", "completed", "failed", "expired"},
    "in_progress": {"paused", "completed", "failed", "expired"},
    "paused": {"in_progress", "completed", "failed", "expired"},
    "completed": set(),
    "failed": set(),
    "expired": set(),
}


class BadTransition(ValueError):
    pass


@dataclass
class Task:
    task_id: int
    template: str
    giver_id: int
    title: str
    need_item: str | None
    need_count: int
    units: str
    reward_gold: int
    size: float
    deadline_days: float | None
    location: str | None
    state: str
    state_gt: float | None
    assignee_id: int | None
    deadline_gt: float | None
    progress: float
    created_gt: float
    source: str

    @property
    def remaining(self) -> float:
        return max(0.0, self.need_count - self.progress)

    @property
    def done_enough(self) -> bool:
        return self.progress >= self.need_count - 1e-9


def _timeline(db) -> int:
    return db.execute("SELECT timeline_id FROM timeline WHERE is_active = 1").fetchone()[0]


def _row(row) -> Task:
    keys = row.keys()
    return Task(**{f: row[f] for f in Task.__dataclass_fields__ if f in keys})


def get(db, task_id: int) -> Task | None:
    row = db.execute("SELECT * FROM v_task WHERE task_id = ?", (task_id,)).fetchone()
    return _row(row) if row else None


def find(db, where: str = "1", params: tuple = (), include_reset: bool = False) -> list[Task]:
    """Tasks on the active timeline; tasks the developer tool reset are left out unless asked for."""
    keep = "" if include_reset else " AND is_reset = 0"
    return [_row(r) for r in db.execute(f"SELECT * FROM v_task WHERE ({where}){keep} ORDER BY task_id", params)]


def open_for_giver(db, giver_id: int) -> list[Task]:
    return find(db, f"giver_id = ? AND state IN ({','.join('?' * len(OPEN))})", (giver_id, *OPEN))


def taken_by(db, worker_id: int) -> list[Task]:
    return find(db, f"assignee_id = ? AND state IN ({','.join('?' * len(TAKEN))})", (worker_id, *TAKEN))


def last_of_template(db, giver_id: int, template: str) -> Task | None:
    rows = find(db, "giver_id = ? AND template = ?", (giver_id, template))
    return rows[-1] if rows else None


def event(db, task: Task | int, kind: str, actors: list[int], payload: dict | None = None,
          gt: float = 0.0, witnesses: list[int] | None = None, source: str = "sim",
          location: str | None = None) -> int:
    """A ``task.<kind>`` world event; actors are [worker, giver] when there is a worker, else [giver]."""
    task_id = task if isinstance(task, int) else task.task_id
    loc = location if location is not None else (None if isinstance(task, int) else task.location)
    event_id = db.execute(
        "INSERT INTO world_event (timeline_id, type, location, actors_json, payload_json, source, game_time)"
        " VALUES (?,?,?,?,?,?,?)",
        (_timeline(db), f"task.{kind}", loc, json.dumps(actors), json.dumps({"task_id": task_id, **(payload or {})}),
         source, gt)).lastrowid
    for npc_id in dict.fromkeys(witnesses if witnesses is not None else actors):
        if npc_id is not None and not is_child(db, npc_id):
            db.execute("INSERT OR IGNORE INTO event_witness (event_id, npc_id, how) VALUES (?,?,?)",
                       (event_id, npc_id, "participated" if npc_id in actors else "saw"))
    return event_id


def create(db, t: Template, giver_id: int, gt: float, source: str = "sim") -> Task:
    if is_child(db, giver_id):
        raise ValueError("children never give tasks")
    task_id = db.execute(
        "INSERT INTO task (timeline_id, template, giver_id, location, community_key, title, need_item, need_count,"
        " units, reward_gold, size, deadline_days, source, created_gt) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (_timeline(db), t.key, giver_id, t.location, t.community, t.title, t.need_item, t.need_count, t.units,
         t.reward_gold, t.size, t.deadline_days, source, gt)).lastrowid
    event_id = event(db, task_id, "available", [giver_id], {"template": t.key}, gt, source=source, location=t.location)
    db.execute("INSERT INTO task_change (timeline_id, task_id, state, reason, cause_event_id, game_time)"
               " VALUES (?,?,?,?,?,?)", (_timeline(db), task_id, "available", "need opened", event_id, gt))
    return get(db, task_id)


def change(db, task: Task, state: str | None, reason: str, gt: float, *, assignee_id: int | None = None,
           d_progress: float = 0.0, deadline_gt: float | None = None, mood: int | None = None,
           cause_event_id: int | None = None, payload: dict | None = None, witnesses: list[int] | None = None,
           source: str = "sim") -> Task:
    """Record a change. A state change is checked against ``ALLOWED`` and gets its own world event."""
    if state is not None and state != task.state:
        if state not in ALLOWED[task.state]:
            raise BadTransition(f"task {task.task_id}: {task.state} -> {state} is not allowed")
        if assignee_id is not None and is_child(db, assignee_id):
            raise ValueError("children never take tasks")
        worker = assignee_id if assignee_id is not None else task.assignee_id
        actors = [worker, task.giver_id] if worker is not None and worker != task.giver_id else [task.giver_id]
        cause_event_id = event(db, task, state, actors, {"reason": reason, **(payload or {})}, gt,
                               witnesses=witnesses, source=source)
    elif state == task.state:
        state = None
    db.execute("INSERT INTO task_change (timeline_id, task_id, state, assignee_id, d_progress, deadline_gt, mood,"
               " reason, cause_event_id, game_time) VALUES (?,?,?,?,?,?,?,?,?,?)",
               (_timeline(db), task.task_id, state, assignee_id, d_progress, deadline_gt, mood, reason,
                cause_event_id, gt))
    return get(db, task.task_id)


def history(db, task_id: int) -> list[dict]:
    return [dict(r) for r in db.execute(
        "SELECT change_id, state, assignee_id, d_progress, deadline_gt, mood, reason, game_time FROM v_task_change"
        " WHERE task_id = ? ORDER BY game_time, change_id", (task_id,))]


def moods(db, task_id: int) -> list[int]:
    return [r[0] for r in db.execute("SELECT mood FROM v_task_change WHERE task_id = ? AND mood IS NOT NULL"
                                     " ORDER BY game_time, change_id", (task_id,))]


def last_change_gt(db, task_id: int, reason_prefix: str) -> float | None:
    return db.execute("SELECT MAX(game_time) FROM v_task_change WHERE task_id = ? AND reason LIKE ?",
                      (task_id, reason_prefix + "%")).fetchone()[0]


def count_changes(db, task_id: int, reason_prefix: str) -> int:
    return db.execute("SELECT COUNT(*) FROM v_task_change WHERE task_id = ? AND reason LIKE ?",
                      (task_id, reason_prefix + "%")).fetchone()[0]
