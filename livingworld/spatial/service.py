"""Record what a party met against the assignment each member had, and tell the giver once."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from livingworld.spatial import MAX_TOLD
from livingworld.spatial import geo


@dataclass(frozen=True)
class Assignment:
    task_id: int | None
    giver_id: int
    title: str
    where: tuple[str, ...]
    need: str = "fight"            # 'fight' work expects fighting at the place; anything else is not about fights
    open_country: bool = False


Lookup = Callable[[Any, int], "Assignment | None"]


def guard_assignment(db, npc_id: int) -> Assignment | None:
    """The duty a guard has in hand, from the guard module. None for anyone without one, or if it is off."""
    from livingworld.guard import guard_enabled
    if not guard_enabled():
        return None
    from livingworld.guard import duty as DU
    task = DU.in_hand(db, npc_id)
    d = DU.duty_for(db, task) if task else None
    if task is None or d is None:
        return None
    return Assignment(task.task_id, task.giver_id, d.title, tuple(d.where), d.need, DU.open_country_ok(db, d))


def record_fight(db, timeline: int, event_id: int | None, member_ids: list[int], location: str | None, gt: float,
                 lookup: Lookup | None = None) -> list[int]:
    """One field report for each member with an assignment, for a fight at ``location``. Returns report ids."""
    out: list[int] = []
    lookup = lookup or guard_assignment
    for member in member_ids:
        a = lookup(db, member)
        if a is None or a.giver_id == member:
            continue
        rel = geo.relation(a.where, location, a.open_country)
        cur = db.execute(
            "INSERT OR IGNORE INTO field_report (timeline_id, event_id, member_id, giver_id, task_id, duty_title,"
            " assigned, location, relation, kind, game_time) VALUES (?,?,?,?,?,?,?,?,?,'fight',?)",
            (timeline, event_id, member, a.giver_id, a.task_id, a.title, "; ".join(a.where), location, rel, gt))
        if cur.rowcount:
            out.append(cur.lastrowid)
    return out


def _name(db, npc_id: int) -> str:
    from livingworld.guard import identity_guard
    row = db.execute("SELECT display_name FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()
    return identity_guard.guard_name(db, npc_id) or (row[0] if row else "someone")


def pending(db, giver_id: int) -> list[Any]:
    """Untold reports for this giver on the visible timeline, oldest first."""
    return db.execute(
        "SELECT r.* FROM field_report r JOIN v_timeline_scope s ON s.timeline_id = r.timeline_id"
        " AND r.game_time <= s.cutoff_gt WHERE r.giver_id = ? AND r.told_gt IS NULL ORDER BY r.game_time, r.report_id",
        (giver_id,)).fetchall()


def notes_for(db, giver_id: int, now: float) -> list[str]:
    """What the giver knows the party ran into, as notes for the AI; marks those reports told. Empty if none."""
    rows = pending(db, giver_id)
    if not rows:
        return []
    by_place: dict[tuple, list[Any]] = {}
    for r in rows:
        by_place.setdefault((r["task_id"], r["duty_title"], r["assigned"], r["location"] or "", r["relation"]), []).append(r)
    notes: list[str] = []
    for (_, title, assigned, loc, rel), group in list(by_place.items())[:MAX_TOLD]:
        who = ", ".join(sorted({_name(db, r["member_id"]) for r in group}))
        spot = loc or "open country with no place name"
        if rel == geo.ASSIGNED:
            notes.append(f"({who} fought at {spot}, which is where you sent them to {title}. That is the place you "
                         f"meant. Whether the trouble is dealt with is for them to say.)")
        elif rel == geo.OPEN_ROAD:
            notes.append(f"({who} fought out on the open road, and you sent them to {title}, which is road work. It "
                         f"may well be the trouble you meant, but nobody said so.)")
        else:
            notes.append(f"({who} fought at {spot}. You sent them to {title}, which points at {assigned}. This was "
                         f"not that place, so do not treat it as the job done or as the trouble you heard about. "
                         f"It may be something else; ask what they found.)")
    with_ids = [r["report_id"] for g in by_place.values() for r in g]
    db.execute(f"UPDATE field_report SET told_gt = ? WHERE report_id IN ({','.join('?' * len(with_ids))})",
               (now, *with_ids))
    return notes
