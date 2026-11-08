"""What finishing or letting down a task does to the giver's view of the worker, and what they remember.

Only through the shared spine: ``relationship_change`` rows (caused by the task's world event), one
first-person memory for the giver, an ``obligation`` row, and the ``task.completed`` / ``task.failed`` /
``task.expired`` events that ``social/claims.py`` turns into gossip. Dimensions touched: trust, affinity,
respect, admiration, familiarity. Never attraction, never anything romance owns.

How the worker behaved is read from what is already stored: whether the work was on time, the giver's
mood tags while it was on (``task_change.mood``), extra work beyond what was asked, and anything rough
the worker did to the giver meanwhile (``physical_interaction`` events). The summary goes into the
event payload, the memory and the prompt.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from livingworld.work import tasks as T

COMPLETED = {"trust": 0.06, "affinity": 0.05, "respect": 0.05, "familiarity": 0.04}
LET_DOWN = {"trust": -0.08, "respect": -0.06, "affinity": -0.03}
WITHDREW_SCALE = 0.5          # saying so before the deadline costs half of silently letting it lapse
REPEAT_STEP = 0.5             # each earlier let-down makes the next one weigh this much more...
REPEAT_CAP = 2.0              # ...up to this many times as much


@dataclass
class Conduct:
    on_time: bool | None = None
    extra_units: float = 0.0
    moods: list[int] = field(default_factory=list)
    rough: int = 0             # times the worker grabbed or held the giver while the work was on
    quality: float = 0.0       # -1..1

    def words(self) -> list[str]:
        out = []
        if self.on_time is True:
            out.append("on time")
        elif self.on_time is False:
            out.append("late")
        if self.extra_units >= 1:
            out.append("did more than asked")
        if self.moods:
            avg = sum(self.moods) / len(self.moods)
            out.append("pleasant company" if avg >= 6.5 else "trying company" if avg <= 3.5 else "fair company")
        if self.rough:
            out.append("got rough with me" if self.rough == 1 else f"got rough with me {self.rough} times")
        return out


def conduct(db, task: T.Task, worker_id: int, gt: float) -> Conduct:
    c = Conduct()
    if task.deadline_gt is not None:
        c.on_time = gt <= task.deadline_gt + 1e-9
    c.extra_units = max(0.0, task.progress - task.need_count)
    c.moods = T.moods(db, task.task_id)
    started = T.last_change_gt(db, task.task_id, "accepted") or task.created_gt
    c.rough = db.execute(
        "SELECT COUNT(*) FROM v_visible_event WHERE type = 'physical_interaction' AND game_time BETWEEN ? AND ?"
        " AND json_extract(actors_json, '$[0]') = ? AND json_extract(actors_json, '$[1]') = ?",
        (started, gt, worker_id, task.giver_id)).fetchone()[0]
    q = 0.0
    if c.on_time is True:
        q += 0.3
    elif c.on_time is False:
        q -= 0.3
    if c.extra_units >= 1:
        q += 0.15
    if c.moods:
        q += 0.4 * (sum(c.moods) / len(c.moods) - 5) / 5
    q -= 0.5 * c.rough
    c.quality = max(-1.0, min(1.0, q))
    return c


def _scale(task: T.Task) -> float:
    return 0.5 + task.size


def _change(db, task: T.Task, worker_id: int, deltas: dict[str, float], event_id: int, reason: str, gt: float) -> None:
    deltas = {k: round(v, 4) for k, v in deltas.items() if abs(v) >= 0.001}
    if not deltas:
        return
    db.execute("INSERT OR IGNORE INTO relationship (from_id, to_id, status, updated_gt) VALUES (?,?,?,?)",
               (task.giver_id, worker_id, "stranger", gt))
    db.execute("INSERT INTO relationship_change (timeline_id, from_id, to_id, deltas_json, cause_event_id, reason,"
               " game_time) VALUES (?,?,?,?,?,?,?)",
               (T._timeline(db), task.giver_id, worker_id, json.dumps(deltas), event_id, reason, gt))


def _name(db, npc_id: int) -> str:
    return db.execute("SELECT display_name FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()[0]


def _remember(db, task: T.Task, worker_id: int, text: str, valence: float, importance: float, event_id: int,
              gt: float) -> None:
    # created_by 'perception': the giver went through it themselves (the CHECK has no 'work' value)
    mem_id = db.execute(
        "INSERT INTO memory (timeline_id, owner_id, kind, content, about_ids_json, location, occurred_gt, importance,"
        " valence, intensity, created_by, source_kind, confidence, event_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (T._timeline(db), task.giver_id, "episodic", text, json.dumps([worker_id]), task.location, gt, importance,
         valence, min(1.0, abs(valence)), "perception", "experienced", 1.0, event_id)).lastrowid
    db.execute("INSERT INTO memory_source (mem_id, event_id) VALUES (?,?)", (mem_id, event_id))


def completed(db, task: T.Task, gt: float, reason: str, source: str = "sim") -> T.Task:
    worker = task.assignee_id
    c = conduct(db, task, worker, gt)
    done = T.change(db, task, "completed", reason, gt,
                    payload={"conduct": c.words(), "quality": round(c.quality, 2), "units": task.progress},
                    source=source)
    event_id = db.execute("SELECT MAX(cause_event_id) FROM task_change WHERE task_id = ?", (task.task_id,)).fetchone()[0]
    s = _scale(task)
    gain = 1.0 + 0.6 * c.quality if c.quality < 0 else 1.0
    deltas = {k: v * s * max(0.2, gain) for k, v in COMPLETED.items()}
    if c.quality > 0.3:
        deltas["admiration"] = 0.03 * c.quality * s
        deltas["respect"] += 0.03 * c.quality * s
    _change(db, task, worker, deltas, event_id, f"task {task.task_id} done: {task.title}", gt)
    how = c.words()
    _remember(db, task, worker,
              f"{_name(db, worker)} helped me {task.title}" + (f" ({', '.join(how)})" if how else "") + ".",
              round(0.3 + 0.4 * max(0.0, c.quality), 2), round(min(1.0, 0.35 + task.size), 2), event_id, gt)
    db.execute("INSERT INTO obligation (timeline_id, kind, debtor_id, creditor_id, description, status, created_gt)"
               " VALUES (?, 'favor', ?, ?, ?, 'open', ?)",
               (T._timeline(db), task.giver_id, worker, f"owes thanks for help to {task.title} (task {task.task_id})", gt))
    return done


def let_down(db, task: T.Task, state: str, gt: float, reason: str, withdrew: bool = False,
             source: str = "sim") -> T.Task:
    """Failed (gave up, or the giver called it off) or expired (deadline passed) after someone agreed."""
    worker = task.assignee_id
    _, _, earlier = _reliability(db, worker)
    ended = T.change(db, task, state, reason, gt, payload={"withdrew": withdrew, "units": task.progress},
                     source=source)
    if worker is None:
        return ended
    event_id = db.execute("SELECT MAX(cause_event_id) FROM task_change WHERE task_id = ?", (task.task_id,)).fetchone()[0]
    s = _scale(task) * min(REPEAT_CAP, 1 + REPEAT_STEP * earlier) * (WITHDREW_SCALE if withdrew else 1.0)
    _change(db, task, worker, {k: v * s for k, v in LET_DOWN.items()}, event_id,
            f"task {task.task_id} {'given up' if withdrew else state}: {task.title}", gt)
    what = ("said they could not finish" if withdrew else
            "never finished it before the deadline" if state == "expired" else "did not finish it")
    _remember(db, task, worker, f"{_name(db, worker)} agreed to help me {task.title} but {what}.",
              -0.3 if withdrew else -0.5, round(min(1.0, 0.35 + task.size), 2), event_id, gt)
    return ended


def _reliability(db, worker_id):
    from livingworld.work.conditions import reliability
    return reliability(db, worker_id)
