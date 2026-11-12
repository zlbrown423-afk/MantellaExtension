"""G2g: recruit training, the steps a new Recruit goes through before routine duties.

The member's superior hands the steps out one at a time, in order, as ordinary duties (template
``duty:training:<key>``), ahead of routine duties and incidents and with no rest needed between them. Each step
says how it is done:

- ``seen``: be at the place for the drill (a conversation there), then report back after half of it;
- ``fight``: a sparring bout, fists only (Mantella's Brawl action with a fellow guard); the game's fight record
  counts, as for bandits;
- ``escort``: a patrol beside a senior guard: seen at the place with a fellow guard in the conversation;
- ``quiz``: the superior asks questions on the hold's law and the guard's orders, there and then. Each answer
  is checked against the expected points by rules (key words); ``pass`` right answers pass, ``fail`` wrong ones
  end the attempt (no reprimand, try again after a rest).

Each step done adds its merit; all of them done is a ``guard.trained`` event, more merit and, for a Recruit
still on probation with no reprimand, probation passed early. Configured under ``training`` in the duties file.
"""
from __future__ import annotations

import functools
import json
import re
from dataclasses import dataclass
from importlib import resources

from livingworld.guard import orgs as O, store as S
from livingworld.work import tasks as T

PREFIX = "duty:training:"


@dataclass(frozen=True)
class Question:
    key: str
    ask: str
    answer: str                    # the gist of a right answer, for the superior's prompt
    words: tuple[str, ...]         # any of these in the answer counts it right


@dataclass(frozen=True)
class Step:
    key: str
    title: str
    why: str
    instructions: str
    where: tuple[str, ...]
    need: str                      # seen | fight | escort | quiz
    shift_hours: float
    merit: float


@dataclass(frozen=True)
class Course:
    steps: tuple[Step, ...] = ()
    questions: tuple[Question, ...] = ()
    pass_right: int = 2
    fail_wrong: int = 3
    complete_merit: float = 2.0
    early_probation: bool = True
    for_ranks: tuple[str, ...] = ()       # ranks that train (default: the first)
    retired: tuple[Step, ...] = ()        # old steps: no longer given, but one already in hand still resolves

    def step(self, key: str | None) -> Step | None:
        k = (key or "").removeprefix(PREFIX)
        return next((s for s in self.steps + self.retired if s.key == k), None)


@functools.lru_cache(maxsize=None)
def course(org_key: str) -> Course:
    org = O.by_key(org_key)
    if org is None or not org.duty_file:
        return Course()
    raw = json.loads(resources.files("livingworld.guard").joinpath("data", org.duty_file).read_text(encoding="utf-8"))
    t = raw.get("training") or {}
    def steps_of(name: str) -> tuple[Step, ...]:
        return tuple(Step(s["key"], s["title"], s.get("why", ""), s.get("instructions", ""),
                          tuple(s.get("where") or ()), s.get("need", "seen"), float(s.get("shift_hours", 2)),
                          float(s.get("merit", 0.5))) for s in t.get(name) or ())
    steps = steps_of("steps")
    qs = tuple(Question(q["key"], q["ask"], q.get("answer", ""), tuple(w.lower() for w in q.get("words") or ()))
               for q in t.get("questions") or ())
    return Course(steps, qs, int(t.get("pass_right", 2)), int(t.get("fail_wrong", 3)),
                  float(t.get("complete_merit", 2.0)), bool(t.get("early_probation", True)),
                  tuple(t.get("for_ranks") or ([org.ranks[0].key] if org.ranks else [])), steps_of("retired_steps"))


def is_training(template: str | None) -> bool:
    return bool(template) and template.startswith(PREFIX)


def _done(db, member_id: int, since: float) -> set[str]:
    rows = db.execute("SELECT template FROM v_task WHERE template LIKE ? AND assignee_id = ? AND is_reset = 0 AND"
                      " state = 'completed' AND created_gt >= ?", (PREFIX + "%", member_id, since - 1e-9)).fetchall()
    return {r[0].removeprefix(PREFIX) for r in rows}


def trained(db, svc: S.Service) -> bool:
    return db.execute("SELECT 1 FROM v_visible_event WHERE type = 'guard.trained' AND"
                      " json_extract(payload_json, '$.service_id') = ? LIMIT 1", (svc.service_id,)).fetchone() is not None


def next_step(db, org: O.Org, svc: S.Service) -> Step | None:
    """The first training step this member still has to do, or None (trained, or not a rank that trains)."""
    c = course(org.key)
    if not c.steps or svc.rank_key not in c.for_ranks or trained(db, svc):
        return None
    done = _done(db, svc.member_id, svc.joined_gt)
    return next((s for s in c.steps if s.key not in done), None)


def progress(db, org: O.Org, svc: S.Service) -> tuple[int, int]:
    c = course(org.key)
    return len(_done(db, svc.member_id, svc.joined_gt) & {s.key for s in c.steps}), len(c.steps)


# ---- the quiz ----

def _norm(text: str) -> str:
    return " " + " ".join(re.findall(r"[a-z']+", (text or "").lower())) + " "


def answers(db, task: T.Task) -> tuple[list[str], int]:
    """(keys of questions answered right, number of wrong answers) so far on this quiz."""
    rows = db.execute("SELECT reason FROM v_task_change WHERE task_id = ? AND reason LIKE 'quiz answer:%'",
                      (task.task_id,)).fetchall()
    right = [r[0].split(":", 2)[2].strip() for r in rows if r[0].startswith("quiz answer: right:")]
    wrong = sum(1 for r in rows if r[0].startswith("quiz answer: wrong"))
    return right, wrong


def judge(org: O.Org, said: str, already: list[str], superior: str) -> Question | None:
    """The unanswered question this answer gets right, if any (key words, whole words or word starts)."""
    text = _norm(said)
    for q in course(org.key).questions:
        if q.key in already:
            continue
        for w in q.words:
            w = w.replace("{superior}", superior.lower())
            if f" {w}" in text:
                return q
    return None


def answer(db, org: O.Org, task: T.Task, said: str, superior: str, gt: float) -> str:
    """Score one answer. Returns 'right', 'wrong', 'passed' or 'failed'."""
    right, wrong = answers(db, task)
    c = course(org.key)
    q = judge(org, said, right, superior)
    state = "in_progress" if task.state in ("accepted", "paused") else None
    if q is not None:
        T.change(db, task, state, f"quiz answer: right: {q.key}", gt)
        return "passed" if len(right) + 1 >= c.pass_right else "right"
    T.change(db, task, state, "quiz answer: wrong", gt)
    return "failed" if wrong + 1 >= c.fail_wrong else "wrong"


def complete(db, org: O.Org, svc: S.Service, by: int | None, now: float, witnesses: list[int] | None = None,
             source: str = "sim") -> bool:
    """After a step is done: if it was the last, the member is trained (merit, and probation passed early)."""
    if next_step(db, org, svc) is not None or trained(db, svc):
        return False
    c = course(org.key)
    svc = S.change(db, svc, "recruit training completed", now, d_merit=c.complete_merit, kind="trained", actor_id=by,
                   payload={"steps": len(c.steps)}, witnesses=witnesses, source=source)
    reprimanded = db.execute("SELECT COUNT(*) FROM v_visible_event WHERE type = 'guard.reprimanded' AND"
                             " json_extract(actors_json, '$[0]') = ? AND json_extract(payload_json, '$.org') = ? AND"
                             " game_time >= ?", (svc.member_id, svc.org_key, svc.joined_gt)).fetchone()[0]
    if c.early_probation and svc.status == "probation" and reprimanded == 0:
        S.change(db, svc, "probation passed early: training completed", now, status="active", kind="confirmed",
                 actor_id=by, source=source)
    return True
