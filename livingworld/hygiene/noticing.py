"""Prompt lines about cleanliness: the NPC's own state, and what they notice about the people they are talking to.

Most dirt is not visible on the game's models, so noticing comes from the stored state. Each observer
gets a line about someone only the first time a state is worth noticing (grimy, filthy, bloodied, or
clean again after being seen grimy), for the conversation in which they notice it; after that it is
theirs to remember or forget. What they make of it depends on their own standards.
"""
from __future__ import annotations

from livingworld.hygiene.model import HygieneState, current
from livingworld.hygiene.profile import Habits
from livingworld.store.minors import is_child

NOTICE_HOLD_DAYS = 0.1     # a notice stays in the prompt for the rest of that conversation (about 2.4 game hours)
RECENT_BATH_DAYS = 0.5


def _days(days: float) -> str:
    if days < 1.0:
        return "today"
    n = round(days)
    return "yesterday" if n <= 1 else f"{n} days ago"


def own_line(name: str, s: HygieneState, now: float) -> str | None:
    since = s.days_since_bath(now)
    parts = []
    if s.label in ("grimy", "filthy"):
        when = f"; they last bathed {_days(since)}" if since is not None else ""
        parts.append(f"{name} is {s.label} and could do with a bath{when}")
    elif since is not None and since <= RECENT_BATH_DAYS:
        parts.append(f"{name} bathed recently and feels clean")
    if s.bloodied:
        parts.append(f"{name} has blood on their clothes and skin from a recent fight")
    return ". ".join(parts) + "." if parts else None


def _reaction(observer: str, habits: Habits) -> str:
    if habits.hygiene_standard >= 0.65:
        return f", and {observer} finds it off-putting"
    if habits.hygiene_standard <= 0.35:
        return f", though it hardly bothers {observer}"
    return ""


def _noticed(db, observer: int, subject: int, what: str, since: float | None, now: float) -> str:
    """'new' (notice it now), 'holding' (noticed earlier in this conversation), or 'old' (already noticed)."""
    row = db.execute("SELECT MIN(game_time) FROM v_hygiene_notice WHERE observer_id = ? AND subject_id = ? AND what = ?"
                     " AND game_time >= ?", (observer, subject, what, since if since is not None else -1e18)).fetchone()
    first = row[0] if row else None
    if first is None:
        return "new"
    return "holding" if now - first <= NOTICE_HOLD_DAYS else "old"


def _remember(db, observer: int, subject: int, what: str, now: float) -> None:
    timeline = db.execute("SELECT timeline_id FROM timeline WHERE is_active = 1").fetchone()[0]
    db.execute("INSERT INTO hygiene_notice (timeline_id, observer_id, subject_id, what, game_time) VALUES (?,?,?,?,?)",
               (timeline, observer, subject, what, now))


def _saw_dirty_before(db, observer: int, subject: int, before: float) -> bool:
    return db.execute("SELECT 1 FROM v_hygiene_notice WHERE observer_id = ? AND subject_id = ? AND game_time < ?"
                      " AND what IN ('grimy','filthy') LIMIT 1", (observer, subject, before)).fetchone() is not None


def about_other(db, observer: int, observer_name: str, observer_habits: Habits, subject: int, subject_name: str,
                s: HygieneState, now: float) -> list[str]:
    lines = []
    candidates = []
    if s.label in ("grimy", "filthy"):
        candidates.append((s.label, f"{observer_name} notices that {subject_name} is {s.label} and smells of "
                                    f"it{_reaction(observer_name, observer_habits)}."))
    if s.bloodied:
        candidates.append(("bloodied", f"{observer_name} notices blood on {subject_name}'s clothes, as if from a "
                                       f"recent fight."))
    since = s.days_since_bath(now)
    if since is not None and since <= 1.0 and s.last_bath_gt is not None \
            and _saw_dirty_before(db, observer, subject, s.last_bath_gt):
        candidates.append(("cleaner", f"{observer_name} notices that {subject_name} has washed and looks much "
                                      f"cleaner than when {observer_name} last saw them."))
    for what, line in candidates:
        seen = _noticed(db, observer, subject, what, s.last_bath_gt, now)
        if seen == "new":
            _remember(db, observer, subject, what, now)
        if seen in ("new", "holding"):
            lines.append(line)
    return lines


def lines_for(db, npc_id: int, name: str, now: float, present: dict[int, str]) -> list[str]:
    """Hygiene lines for ``npc_id``'s prompt. ``present`` maps everyone in the conversation to names."""
    if is_child(db, npc_id):
        return []
    mine, habits = current(db, npc_id, now)
    lines = []
    own = own_line(name, mine, now)
    if own:
        lines.append(own)
    for other_id, other_name in present.items():
        if other_id == npc_id or is_child(db, other_id):
            continue
        theirs, _ = current(db, other_id, now)
        lines.extend(about_other(db, npc_id, name, habits, other_id, other_name, theirs, now))
    return lines
