"""G2: duties, shift reports, the service record, pay and probation.

A duty is an ordinary W1 task (``task`` / ``task_change`` / ``v_task``) whose template is ``duty:<key>`` and
whose community is the guard's key: the member's superior gives it, the member is the assignee from the
start, and it ends when the member reports back (completed, or failed for a false or early report) or the
report window passes (expired). So duties roll back with saves, count toward the worker's reliability and
reach gossip like any other task, with no tables of their own.

Rules only, no LLM:

- **Giving a duty**: when a serving member talks to their superior (the lowest-ranked leader above them),
  has no duty in hand and has rested since the last one, the superior gives one whose hours include now,
  whose rank range fits and which differs from the last one (picked with a seed, so it is repeatable).
- **At the post**: a conversation at a location whose name contains one of the duty's ``where`` words
  during the shift marks the member as seen at their post (with whoever was there as witnesses).
- **Reporting back** (``ReportDuty``): seen at the post and at least half the shift gone = done (merit,
  a little more respect and trust); seen but early = left the post early; never seen = a false report.
  The superior keeps the member's own words from the report as a memory either way.
- **Missing it**: no report before the shift's end plus ``report_grace_hours`` = missed (reprimand).
- **Commendation**: every ``commend_every`` honest duties in a row (no reprimand between) = commended.
- **Probation**: at the end of probation with no reprimand the member is confirmed (active); with one,
  probation is extended once; still reprimanded at the end of the extension = dismissed.
- **Pay**: every full week served (not suspended) earns the week's wage of the rank held then; each
  reprimand docks ``dock_per_reprimand``; what was paid out is a ``guard.paid`` event. The superior pays
  what is owed when asked (``PayWages``); the game hands over the gold.
"""
from __future__ import annotations

import functools
import hashlib
import json
from dataclasses import dataclass
from importlib import resources

from livingworld.guard import orgs as O, profiles, store as S
from livingworld.work import tasks as T
from livingworld.work.templates import Template

PREFIX = "duty:"


@dataclass(frozen=True)
class Duty:
    key: str
    title: str
    why: str
    instructions: str
    where: tuple[str, ...]
    hours: tuple[int, int]
    shift_hours: float
    min_rank: str | None
    max_rank: str | None
    merit: float


@dataclass(frozen=True)
class Rules:
    duties: tuple[Duty, ...]
    missed_merit: float = -2.0
    false_merit: float = -3.0
    early_merit: float = -1.0
    report_grace_hours: float = 12.0
    rest_hours: float = 8.0
    commend_every: int = 5
    commend_merit: float = 3.0

    def duty(self, key: str | None) -> Duty | None:
        k = (key or "").removeprefix(PREFIX)
        return next((d for d in self.duties if d.key == k), None)


@functools.lru_cache(maxsize=None)
def rules(org_key: str) -> Rules:
    org = O.by_key(org_key)
    if org is None or not org.duty_file:
        return Rules(())
    raw = json.loads(resources.files("livingworld.guard").joinpath("data", org.duty_file).read_text(encoding="utf-8"))
    duties = tuple(Duty(d["key"], d["title"], d.get("why", ""), d.get("instructions", ""), tuple(d.get("where") or ()),
                        (int(d["hours"][0]), int(d["hours"][1])), float(d.get("shift_hours", 4)),
                        d.get("min_rank"), d.get("max_rank"), float(d.get("merit", 1.0)))
                   for d in raw.get("duties") or ())
    keep = {k: raw[k] for k in ("missed_merit", "false_merit", "early_merit", "report_grace_hours", "rest_hours",
                                "commend_every", "commend_merit") if k in raw}
    return Rules(duties, **keep)


# ---- small helpers ----

def hour_of(now: float) -> int:
    return int((now % 1.0) * 24)


def in_hours(hours: tuple[int, int], hour: int) -> bool:
    start, end = hours
    return start <= hour < end if start < end else (hour >= start or hour < end)


def at_post(d: Duty, location: str | None) -> bool:
    loc = (location or "").lower()
    return bool(loc) and any(w.lower() in loc for w in d.where)


def superior_id(db, org: O.Org, svc: S.Service) -> int | None:
    person = org.superior(svc.rank_key)
    return profiles.leader_id(db, org, person.key) if person else None


def _duties(db, member_id: int, states: tuple[str, ...]) -> list[T.Task]:
    marks = ",".join("?" * len(states))
    return T.find(db, f"template LIKE 'duty:%' AND assignee_id = ? AND state IN ({marks})", (member_id, *states))


def in_hand(db, member_id: int) -> T.Task | None:
    rows = _duties(db, member_id, T.TAKEN)
    return rows[-1] if rows else None


def last(db, member_id: int) -> T.Task | None:
    rows = T.find(db, "template LIKE 'duty:%' AND assignee_id = ?", (member_id,))
    return rows[-1] if rows else None


def assigned_gt(db, task: T.Task) -> float:
    return T.last_change_gt(db, task.task_id, "assigned") or task.created_gt


def shift_end(db, task: T.Task, d: Duty) -> float:
    return assigned_gt(db, task) + d.shift_hours / 24.0


def seen_count(db, task: T.Task) -> int:
    return T.count_changes(db, task.task_id, "seen at post")


def _rank_ok(org: O.Org, d: Duty, rank_key: str | None) -> bool:
    mine = org.rank_index(rank_key)
    lo = org.rank_index(d.min_rank) if d.min_rank else 0
    hi = org.rank_index(d.max_rank) if d.max_rank else len(org.ranks)
    return lo <= mine <= hi


# ---- giving a duty ----

@dataclass
class Check:
    name: str
    ok: bool
    why: str


def checks(db, org: O.Org, svc: S.Service, now: float) -> list[Check]:
    """Whether the member's superior would give them a duty now (not which one)."""
    r = rules(org.key)
    out = [Check("duties", bool(r.duties), f"{len(r.duties)} duties configured" if r.duties else "no duties configured"),
           Check("status", svc.status in ("probation", "active"), f"status {svc.status}")]
    held = in_hand(db, svc.member_id)
    out.append(Check("free", held is None, "no duty in hand" if held is None else f"already on duty: {held.title}"))
    prev = last(db, svc.member_id)
    if prev is not None and prev.state in T.DONE:
        rested = (now - (prev.state_gt or prev.created_gt)) * 24
        out.append(Check("rested", rested >= r.rest_hours, f"last duty ended {rested:.1f} h ago (rest {r.rest_hours:g} h)"))
    return out


def choices(org: O.Org, svc: S.Service, prev_key: str | None, hour: int) -> list[Duty]:
    r = rules(org.key)
    fit = [d for d in r.duties if in_hours(d.hours, hour) and _rank_ok(org, d, svc.rank_key)]
    fresh = [d for d in fit if PREFIX + d.key != prev_key]
    return fresh or fit


def pick(org: O.Org, svc: S.Service, prev_key: str | None, now: float, key: str | None = None,
         any_hour: bool = False) -> Duty | None:
    if key:
        return rules(org.key).duty(key)
    pool = choices(org, svc, prev_key, hour_of(now))
    if not pool and any_hour:
        pool = [d for d in rules(org.key).duties if _rank_ok(org, d, svc.rank_key)]
    if not pool:
        return None
    seed = int(hashlib.sha256(f"{svc.service_id}|{int(now)}|{prev_key}".encode()).hexdigest(), 16)
    return pool[seed % len(pool)]


def assign(db, org: O.Org, svc: S.Service, giver_id: int, d: Duty, now: float, *, witnesses: list[int] | None = None,
           source: str = "sim") -> T.Task:
    r = rules(org.key)
    t = Template(key=PREFIX + d.key, giver=org.key, title=d.title, why=d.why, instructions=d.instructions,
                 need_item=None, need_count=1, units="shift", location=d.where[0] if d.where else org.hold,
                 community=org.key, size=0.25)
    task = T.create(db, t, giver_id, now, source=source)
    deadline = now + (d.shift_hours + r.report_grace_hours) / 24.0
    return T.change(db, task, "accepted", f"assigned by superior ({org.title(svc.rank_key)})", now,
                    assignee_id=svc.member_id, deadline_gt=deadline, witnesses=witnesses,
                    payload={"duty": d.key, "org": org.key, "shift_end": now + d.shift_hours / 24.0}, source=source)


# ---- at the post ----

def mark_seen(db, member_id: int, location: str | None, now: float, witnesses: list[int] | None = None,
              force: bool = False) -> T.Task | None:
    task = in_hand(db, member_id)
    if task is None:
        return None
    org = _org_of(db, task)
    d = rules(org.key).duty(task.template) if org else None
    if d is None:
        return None
    if not force and (not at_post(d, location) or now > shift_end(db, task, d) + 1e-9):
        return None
    state = "in_progress" if task.state in ("accepted", "paused") else None
    return T.change(db, task, state, f"seen at post: {location or 'developer'}", now, witnesses=witnesses)


def _org_of(db, task: T.Task) -> O.Org | None:
    row = db.execute("SELECT community_key FROM task WHERE task_id = ?", (task.task_id,)).fetchone()
    return O.by_key(row[0]) if row else None


# ---- consequences shared by reports, misses and commendations ----

def _relate(db, from_id: int, to_id: int, deltas: dict[str, float], event_id: int | None, reason: str, gt: float) -> None:
    db.execute("INSERT OR IGNORE INTO relationship (from_id, to_id, status, updated_gt) VALUES (?,?,?,?)",
               (from_id, to_id, "stranger", gt))
    db.execute("INSERT INTO relationship_change (timeline_id, from_id, to_id, deltas_json, cause_event_id, reason,"
               " game_time) VALUES (?,?,?,?,?,?,?)",
               (S._timeline(db), from_id, to_id, json.dumps(deltas), event_id, reason, gt))


def _remember(db, owner_id: int, about_id: int, text: str, valence: float, importance: float, event_id: int | None,
              location: str | None, gt: float) -> None:
    mem_id = db.execute(
        "INSERT INTO memory (timeline_id, owner_id, kind, content, about_ids_json, location, occurred_gt, importance,"
        " valence, intensity, created_by, source_kind, confidence, event_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (S._timeline(db), owner_id, "episodic", text, json.dumps([about_id]), location, gt, importance, valence,
         min(1.0, abs(valence)), "perception", "experienced", 1.0, event_id)).lastrowid
    if event_id is not None:
        db.execute("INSERT INTO memory_source (mem_id, event_id) VALUES (?,?)", (mem_id, event_id))


def _name(db, npc_id: int | None) -> str:
    row = db.execute("SELECT display_name FROM npc WHERE npc_id = ?", (npc_id,)).fetchone() if npc_id else None
    return row[0] if row else "someone"


def reprimand(db, svc: S.Service, by: int | None, why: str, merit: float, now: float, *, task_id: int | None = None,
              witnesses: list[int] | None = None, source: str = "sim") -> S.Service:
    return S.change(db, svc, f"reprimanded: {why}", now, d_merit=merit, kind="reprimanded", actor_id=by,
                    payload={"why": why, "task_id": task_id}, witnesses=witnesses, source=source)


def _maybe_commend(db, org: O.Org, svc: S.Service, by: int | None, now: float, witnesses: list[int] | None,
                   source: str) -> bool:
    r = rules(org.key)
    if r.commend_every <= 0:
        return False
    since = max(S.last_event_gt(db, "commended", svc.member_id, org.key) or -1e9,
                S.last_event_gt(db, "reprimanded", svc.member_id, org.key) or -1e9, svc.joined_gt - 1e-9)
    done = db.execute("SELECT COUNT(*) FROM v_task WHERE template LIKE 'duty:%' AND assignee_id = ? AND state = 'completed'"
                      " AND is_reset = 0 AND state_gt > ?", (svc.member_id, since)).fetchone()[0]
    if done < r.commend_every:
        return False
    S.change(db, svc, f"commended: {done} duties done well in a row", now, d_merit=r.commend_merit, kind="commended",
             actor_id=by, payload={"duties": done}, witnesses=witnesses, source=source)
    return True


# ---- reporting back ----

@dataclass
class Report:
    outcome: str          # done | early | false | none
    task: T.Task | None
    commended: bool = False


def report(db, org: O.Org, svc: S.Service, superior: int, now: float, said: str | None = None, *,
           location: str | None = None, witnesses: list[int] | None = None, source: str = "sim") -> Report:
    task = in_hand(db, svc.member_id)
    if task is None:
        return Report("none", None)
    r = rules(org.key)
    d = r.duty(task.template)
    member, boss = svc.member_id, superior
    words = (said or "").strip()
    quoted = f" They said: \"{words[:300]}\"" if words else ""
    seen = seen_count(db, task) > 0
    half = assigned_gt(db, task) + (d.shift_hours if d else 4) / 48.0
    who = _name(db, member)
    if not seen:
        task = T.change(db, task, "failed", "false report: nobody saw them at their post", now,
                        payload={"duty_report": "false"}, witnesses=witnesses, source=source)
        event_id = _last_event(db, task)
        reprimand(db, svc, boss, f"false report about the duty to {task.title}", r.false_merit, now,
                  task_id=task.task_id, witnesses=witnesses, source=source)
        _relate(db, boss, member, {"trust": -0.08, "respect": -0.05}, event_id, f"false duty report (task {task.task_id})", now)
        _remember(db, boss, member, f"{who} reported that they had done their duty to {task.title}, but nobody saw "
                                    f"them at their post.{quoted}", -0.5, 0.6, event_id, location, now)
        return Report("false", task)
    if now < half:
        task = T.change(db, task, "failed", "left the post early", now, payload={"duty_report": "early"},
                        witnesses=witnesses, source=source)
        event_id = _last_event(db, task)
        reprimand(db, svc, boss, f"left the post early ({task.title})", r.early_merit, now, task_id=task.task_id,
                  witnesses=witnesses, source=source)
        _relate(db, boss, member, {"respect": -0.04}, event_id, f"left duty early (task {task.task_id})", now)
        _remember(db, boss, member, f"{who} came back from their duty to {task.title} long before the shift was out."
                                    f"{quoted}", -0.3, 0.45, event_id, location, now)
        return Report("early", task)
    task = T.change(db, task, "completed", "reported back after the shift", now,
                    payload={"duty_report": "done", "seen": seen_count(db, task)}, witnesses=witnesses, source=source)
    event_id = _last_event(db, task)
    S.change(db, svc, f"duty done: {task.title}", now, d_merit=d.merit if d else 1.0, source=source)
    _relate(db, boss, member, {"respect": 0.02, "trust": 0.02, "familiarity": 0.02}, event_id,
            f"duty done (task {task.task_id})", now)
    _remember(db, boss, member, f"{who} reported back from their duty to {task.title}.{quoted}", 0.2, 0.35, event_id,
              location, now)
    fresh = S.get(db, svc.service_id)
    return Report("done", task, _maybe_commend(db, org, fresh, boss, now, witnesses, source))


def _last_event(db, task: T.Task) -> int | None:
    return db.execute("SELECT MAX(cause_event_id) FROM task_change WHERE task_id = ?", (task.task_id,)).fetchone()[0]


# ---- missed duties and probation (upkeep) ----

def _reprimanded_for(db, task_id: int) -> bool:
    return db.execute("SELECT 1 FROM v_visible_event WHERE type = 'guard.reprimanded'"
                      " AND json_extract(payload_json, '$.task_id') = ? LIMIT 1", (task_id,)).fetchone() is not None


def upkeep(db, now: float) -> dict[str, list[int]]:
    """Expire unreported duties (with a reprimand) and settle probations. Call inside a transaction."""
    out: dict[str, list[int]] = {"missed": [], "confirmed": [], "extended": [], "dismissed": []}
    for task in T.find(db, f"template LIKE 'duty:%' AND state IN ({','.join('?' * len(T.TAKEN))})", T.TAKEN):
        if task.deadline_gt is not None and now > task.deadline_gt:
            T.change(db, task, "expired", "missed: never reported back", task.deadline_gt)
            out["missed"].append(task.task_id)
    for task in T.find(db, "template LIKE 'duty:%' AND state = 'expired'"):
        if task.assignee_id is None or _reprimanded_for(db, task.task_id):
            continue
        org = _org_of(db, task)
        svc = S.current(db, task.assignee_id, org.key) if org else None
        if svc is None:
            continue
        gt = task.state_gt or now
        reprimand(db, svc, task.giver_id, f"missed the duty to {task.title}", rules(org.key).missed_merit, gt,
                  task_id=task.task_id)
        _relate(db, task.giver_id, svc.member_id, {"trust": -0.05, "respect": -0.05}, _last_event(db, task),
                f"missed duty (task {task.task_id})", gt)
        _remember(db, task.giver_id, svc.member_id, f"{_name(db, svc.member_id)} never reported back from their duty "
                                                    f"to {task.title}.", -0.4, 0.5, _last_event(db, task), task.location, gt)
        if task.task_id not in out["missed"]:
            out["missed"].append(task.task_id)
    for svc in S.find(db, "status = 'probation'"):
        org = O.by_key(svc.org_key)
        if org is None:
            continue
        result = settle_probation(db, org, svc, now)
        if result:
            out[result].append(svc.service_id)
    return out


def _reprimands_since(db, svc: S.Service, since: float) -> int:
    return db.execute("SELECT COUNT(*) FROM v_visible_event WHERE type = 'guard.reprimanded'"
                      " AND json_extract(actors_json, '$[0]') = ? AND json_extract(payload_json, '$.org') = ?"
                      " AND game_time >= ?", (svc.member_id, svc.org_key, since)).fetchone()[0]


def settle_probation(db, org: O.Org, svc: S.Service, now: float) -> str | None:
    days = org.probation_days
    extended = db.execute("SELECT MAX(game_time) FROM v_service_change WHERE service_id = ? AND reason LIKE"
                          " 'probation extended%'", (svc.service_id,)).fetchone()[0]
    boss = superior_id(db, org, svc)
    if extended is None:
        end = svc.joined_gt + days
        if now < end:
            return None
        if _reprimands_since(db, svc, svc.joined_gt) == 0:
            S.change(db, svc, "probation passed", end, status="active", kind="confirmed", actor_id=boss)
            return "confirmed"
        S.change(db, svc, f"probation extended by {days:g} days after a reprimand", end)
        return "extended"
    end = extended + days
    if now < end:
        return None
    if _reprimands_since(db, svc, extended) == 0:
        S.change(db, svc, "probation passed after extension", end, status="active", kind="confirmed", actor_id=boss)
        return "confirmed"
    S.change(db, svc, "dismissed at the end of an extended probation", end, status="dismissed", kind="dismissed",
             actor_id=boss)
    return "dismissed"


# ---- pay ----

@dataclass
class Pay:
    weeks: int
    earned: int
    docked: int
    paid: int

    @property
    def owed(self) -> int:
        return max(0, self.earned - self.docked - self.paid)


def pay(db, org: O.Org, svc: S.Service, now: float) -> Pay:
    rows = S.changes(db, svc.service_id)
    weeks = int(max(0.0, now - svc.joined_gt) // 7)
    earned = 0
    for k in range(1, weeks + 1):
        at = svc.joined_gt + 7 * k
        rank = status = None
        for r in rows:
            if r["game_time"] > at + 1e-9:
                break
            rank = r["rank_key"] or rank
            status = r["status"] or status
        if status in ("probation", "active"):
            earned += org.wage(rank)
    reprimands = _reprimands_since(db, svc, svc.joined_gt)
    paid = db.execute("SELECT COALESCE(SUM(json_extract(payload_json, '$.amount')), 0) FROM v_visible_event"
                      " WHERE type = 'guard.paid' AND json_extract(payload_json, '$.service_id') = ?",
                      (svc.service_id,)).fetchone()[0]
    return Pay(weeks, earned, reprimands * org.dock, int(paid))


def pay_out(db, org: O.Org, svc: S.Service, by: int | None, now: float, *, witnesses: list[int] | None = None,
            source: str = "sim") -> int:
    owed = pay(db, org, svc, now).owed
    if owed <= 0:
        return 0
    S.event(db, "paid", [svc.member_id, by], {"service_id": svc.service_id, "org": org.key, "amount": owed}, now,
            svc.location, witnesses, source)
    return owed


# ---- the record ----

@dataclass
class Record:
    done: int
    missed: int
    false_reports: int
    early: int
    commended: int
    reprimanded: int
    merit: float

    def words(self) -> str:
        parts = [f"{self.done} dut{'y' if self.done == 1 else 'ies'} done"]
        if self.missed:
            parts.append(f"{self.missed} missed")
        if self.false_reports:
            parts.append(f"{self.false_reports} false report{'s' if self.false_reports != 1 else ''}")
        if self.early:
            parts.append(f"{self.early} left early")
        if self.commended:
            parts.append(f"{self.commended} commendation{'s' if self.commended != 1 else ''}")
        if self.reprimanded:
            parts.append(f"{self.reprimanded} reprimand{'s' if self.reprimanded != 1 else ''}")
        return ", ".join(parts)


def record(db, svc: S.Service) -> Record:
    def tasks(where: str) -> int:
        return db.execute("SELECT COUNT(*) FROM v_task WHERE template LIKE 'duty:%' AND assignee_id = ? AND is_reset = 0"
                          f" AND created_gt >= ? AND {where}", (svc.member_id, svc.joined_gt - 1e-9)).fetchone()[0]

    def events(kind: str) -> int:
        return db.execute("SELECT COUNT(*) FROM v_visible_event WHERE type = ? AND json_extract(actors_json, '$[0]') = ?"
                          " AND json_extract(payload_json, '$.service_id') = ?",
                          (f"guard.{kind}", svc.member_id, svc.service_id)).fetchone()[0]

    false_n = db.execute("SELECT COUNT(*) FROM v_task_change c JOIN task t ON t.task_id = c.task_id WHERE"
                         " t.template LIKE 'duty:%' AND c.reason LIKE 'false report%' AND c.state = 'failed'"
                         " AND t.created_gt >= ? AND EXISTS (SELECT 1 FROM v_task v WHERE v.task_id = t.task_id"
                         " AND v.assignee_id = ?)", (svc.joined_gt - 1e-9, svc.member_id)).fetchone()[0]
    failed = tasks("state = 'failed'")
    return Record(tasks("state = 'completed'"), tasks("state = 'expired'"), false_n, failed - false_n,
                  events("commended"), events("reprimanded"), svc.merit)
