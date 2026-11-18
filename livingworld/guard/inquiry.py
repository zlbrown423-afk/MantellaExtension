"""Inquiry duties: a theft the member has to actually look into (the cutpurse in the Plains District market).

Before this, a theft was "dealt with" by any conversation in Whiterun, so reporting back caught a thief nobody
had looked for. Now an incident whose ``need`` is ``inquiry`` is solved in steps, all from conversations, rules
only:

1. **Ask around**: talk with the complainant or one of the two market witnesses picked when it opened (each
   remembers what they saw and tells the guard sent to it). Each one talked to is a task change ``asked: <name>``.
2. **Catch the thief**, one of two ways:
   - an off-screen culprit went to ground at a ``hideout`` (a shop or inn the game names, e.g. Warmaiden's). After
     asking at least one person, a conversation there is ``tracked: <place>``: the people there see the guard
     corner the thief (no NPC is spawned; the culprit stays a description and a name, as before).
   - a named suspect (a real townsperson) has to be arrested (G2f): the arrest for this incident, submitted to,
     is what counts. Asking around first makes the evidence stronger.
3. **Report back**: as before, the superior closes the incident as ``arrested``.

Incidents opened before this keep their stored ``need`` ("seen") and work as they did.
"""
from __future__ import annotations

from dataclasses import dataclass

from livingworld.work import tasks as T

NEED = "inquiry"
ASKED, TRACKED = "asked: ", "tracked: "


@dataclass
class Step:
    """What happened in this conversation, for notices and prompt lines."""
    asked: list[str]           # names asked for the first time now
    tracked: str | None        # the hideout, if the thief was cornered now


def _incident(db, d):
    from livingworld.guard import incident as INC
    return INC.get(db, d.incident_id) if d is not None and d.incident_id is not None else None


def asked_names(db, task: T.Task) -> list[str]:
    rows = db.execute("SELECT reason FROM v_task_change WHERE task_id = ? AND reason LIKE ? ORDER BY game_time",
                      (task.task_id, ASKED + "%")).fetchall()
    return [r[0][len(ASKED):] for r in rows]


def tracked(db, task: T.Task) -> bool:
    return T.count_changes(db, task.task_id, TRACKED) > 0


def suspect_arrested(db, task: T.Task, inc) -> bool:
    """The named suspect of this incident was arrested by the member and went quietly (G2f)."""
    from livingworld.guard import arrest as AR
    if inc is None or not inc.suspect or task.assignee_id is None:
        return False
    return any(a.incident == inc.event_id and a.response == AR.SUBMIT for a in AR.by_member(db, task.assignee_id))


def done(db, task: T.Task, d) -> int:
    inc = _incident(db, d)
    if inc is None:
        return 0
    if tracked(db, task):            # the thief cornered (or the developer command)
        return 1
    return 1 if inc.suspect and suspect_arrested(db, task, inc) else 0


def _people(inc) -> list[str]:
    return [n for n in ([inc.complainant] if inc.complainant else []) + [w for w, _ in inc.witnesses] if n]


def progress(db, task: T.Task, d, present_names: dict[int, str], location: str | None, now: float,
             witnesses: list[int] | None = None) -> Step:
    """Record what this conversation adds to the inquiry: people asked, the thief cornered."""
    step = Step([], None)
    inc = _incident(db, d)
    if inc is None or (task.deadline_gt and now > task.deadline_gt):
        return step
    already = {n.casefold() for n in asked_names(db, task)}
    for name in _people(inc):
        if name.casefold() in already:
            continue
        if any((n or "").casefold() == name.casefold() for n in present_names.values()):
            state = "in_progress" if task.state in ("accepted", "paused") else None
            task = T.change(db, task, state, ASKED + name, now, witnesses=witnesses)
            step.asked.append(name)
            already.add(name.casefold())
    if (inc.hideout and not inc.suspect and already and not tracked(db, task) and location
            and _same_place(inc.hideout, location)):
        state = "in_progress" if task.state in ("accepted", "paused") else None
        T.change(db, task, state, TRACKED + location, now, witnesses=witnesses)
        step.tracked = location
    return step


def _same_place(hideout: str, location: str) -> bool:
    def norm(x: str) -> str:
        x = x.casefold().strip()
        return x[4:] if x.startswith("the ") else x
    return norm(hideout) in norm(location) or norm(location) in norm(hideout)


def missing(db, task: T.Task, d, player: str) -> str:
    """What is still to do, in words for the superior."""
    inc = _incident(db, d)
    if inc is None:
        return "nothing on record yet"
    asked = asked_names(db, task)
    people = ", ".join(_people(inc)) or "the stallholders"
    if inc.suspect:
        if not asked:
            return f"{player} has not asked around yet ({people}), and {inc.suspect} has not been brought in"
        return f"{player} has asked around, but {inc.suspect}, the one the trail points at, has not been arrested"
    if not asked:
        return f"{player} has not asked anyone yet: {people} saw something"
    return f"{player} has asked around ({', '.join(asked)}), but has not followed the trail to where the thief went"


def witness_line(name: str, player: str, inc, clue: str, *, player_on_it: bool, guard: str) -> str:
    out = (f"{name} was in the Plains District market when {inc.complainant or 'someone'} was robbed, and saw "
           f"something. In {name}'s own words: \"{clue}.\" {name} does not know the thief's name and makes up nothing "
           f"more.")
    if player_on_it:
        out += (f" {player} is the guard from {guard} looking into it: if {player} asks about the theft or what "
                f"{name} saw, {name} tells them this readily.")
    return out


def caught_line(name: str, player: str, inc, place: str) -> str:
    look = inc.look or "the cutpurse"
    return (f"Just now {player}, a guard, followed the trail of the cutpurse who robbed {inc.complainant or 'a woman'} "
            f"in the market to {place}, and cornered them here: {look}. The thief gave up without a fight and "
            f"handed over what was left of the stolen coin. {name} saw it happen and may remark on it or point the way "
            f"the thief tried to run; {name} does not name the thief and invents nothing beyond this.")
