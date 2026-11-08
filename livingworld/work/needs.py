"""Upkeep run when the world is brought up to date (conversation start): open needs that have a real
reason, lapse needs nobody took, expire missed deadlines, and pause or resume outdoor work.

Rules only, no LLM. ``explain`` lists every check for one giver, for the developer tool.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from livingworld.store.minors import is_child
from livingworld.work import conditions as C, consequences, tasks as T
from livingworld.work.templates import Template, for_giver, person_id, templates

DECLINE_COOLDOWN_DAYS = 1.0   # after a "no", the giver waits this long before asking anyone again


@dataclass
class Upkeep:
    opened: list[int] = field(default_factory=list)
    lapsed: list[int] = field(default_factory=list)
    expired: list[int] = field(default_factory=list)
    paused: list[int] = field(default_factory=list)
    resumed: list[int] = field(default_factory=list)


def open_checks(db, t: Template, giver_id: int, now: float) -> list[C.Check]:
    """Should this need open now? (Not whether it can be offered right now: see ``offer_checks``.)"""
    out = [C.season_check(t, now)]
    busy = [x for x in T.open_for_giver(db, giver_id)]
    out.append(C.Check("free", not busy, "no other open task" if not busy
                       else f"already has open task {busy[0].task_id} ({busy[0].title}, {busy[0].state})"))
    last = T.last_of_template(db, giver_id, t.key)
    if last is not None and last.state in T.DONE:
        since = now - (last.state_gt or last.created_gt)
        out.append(C.Check("cooldown", since >= t.cooldown_days,
                           f"last one ended {since:.1f} days ago (cooldown {t.cooldown_days:g})"))
    return out


def offer_checks(db, task: T.Task, t: Template, worker_id: int, now: float, hour: int,
                 weather: str | None) -> list[C.Check]:
    if T.count_changes(db, task.task_id, "forced offer"):
        return [C.Check("forced", True, "the developer tool forced this offer")]
    out = C.working_checks(t, hour, weather)
    worker_key_ok = worker_id is not None and (t.asks_player or not _is_player(db, worker_id))
    out.append(C.Check("helper", worker_key_ok, "they are someone the giver would ask" if worker_key_ok
                       else "the giver does not ask them for this"))
    declined = T.last_change_gt(db, task.task_id, "declined")
    if declined is not None:
        out.append(C.Check("asked recently", now - declined >= DECLINE_COOLDOWN_DAYS,
                           f"last turned down {now - declined:.2f} days ago"))
    if worker_id is not None:
        out += C.ask_checks(db, t, task.giver_id, worker_id)
    return out


def _is_player(db, npc_id: int) -> bool:
    row = db.execute("SELECT is_player FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()
    return bool(row and row[0])


def upkeep(db, now: float, hour: int | None = None, weather: str | None = None) -> Upkeep:
    """Bring every task up to ``now``. Call inside a transaction."""
    result = Upkeep()
    hour = C.hour_of(now) if hour is None else hour
    for task in T.find(db, f"state IN ({','.join('?' * len(T.OPEN))})", T.OPEN):
        t = _template(task)
        if task.state in T.TAKEN and task.deadline_gt is not None and now > task.deadline_gt:
            consequences.let_down(db, task, "expired", max(task.deadline_gt, task.state_gt or 0.0), "deadline passed")
            result.expired.append(task.task_id)
        elif task.state in ("available", "offered") and t is not None and now - task.created_gt > t.open_days:
            T.change(db, task, "expired", "nobody took it on", max(task.created_gt + t.open_days, task.state_gt or 0.0))
            result.lapsed.append(task.task_id)
        elif t is not None and task.state == "in_progress" and C.pause_reason(t, hour, weather):
            T.change(db, task, "paused", C.pause_reason(t, hour, weather), now)
            result.paused.append(task.task_id)
        elif t is not None and task.state == "paused" and not C.pause_reason(t, hour, weather):
            T.change(db, task, "in_progress", "work can go on again", now)
            result.resumed.append(task.task_id)
    for t in templates():
        giver = person_id(db, t.giver)
        if giver is None or is_child(db, giver):
            continue
        if all(c.ok for c in open_checks(db, t, giver, now)):
            result.opened.append(T.create(db, t, giver, now).task_id)
    return result


def _template(task: T.Task) -> Template | None:
    from livingworld.work.templates import by_key
    return by_key(task.template)


def explain(db, giver_id: int, now: float, hour: int | None, weather: str | None,
            worker_id: int | None) -> list[str]:
    """Every template this person gives, and which checks pass or block it."""
    hour = C.hour_of(now) if hour is None else hour
    lines = []
    for t in for_giver(db, giver_id):
        lines.append(f"{t.key}: {t.title}")
        for c in open_checks(db, t, giver_id, now):
            lines.append(f"  open  {'ok ' if c.ok else 'NO '} {c.name}: {c.why}")
        task = next((x for x in T.open_for_giver(db, giver_id) if x.template == t.key), None)
        if task is not None and worker_id is not None:
            for c in offer_checks(db, task, t, worker_id, now, hour, weather):
                lines.append(f"  offer {'ok ' if c.ok else 'NO '} {c.name}: {c.why}")
    return lines or ["(no work templates for this person)"]
