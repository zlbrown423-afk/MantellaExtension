"""How clean someone is right now, worked out from their change log; and the baths they took off screen.

State is never stored. It is replayed from ``hygiene_change`` rows on the active timeline: dirt builds
up at the person's daily rate, fights add dirt and blood, swimming washes some off, a bath removes it
all. Blood fades slowly by itself (brushed off, clothes changed).

Nobody bathes in front of the player yet (that is milestone H2), so every NPC's bath happens off screen:
when someone is asked about and their usual bath time has passed, a bath is written in at the time
they would have taken it, in their preferred part of the day. The player never gets off-screen baths.
No LLM anywhere in here.
"""
from __future__ import annotations

import bisect
import math
import random
from dataclasses import dataclass

from livingworld.hygiene.profile import WINDOWS, Habits, habits_for
from livingworld.store.minors import is_child

BLOOD_FADE_PER_DAY = 0.15
BLOODIED = 0.3
MAX_CATCH_UP_BATHS = 100      # a safety stop; at most about one bath a day fits in the window below
CATCH_UP_WINDOW_DAYS = 60.0   # off-screen baths are written one by one for at most this far back...
CATCH_UP_WINDOW_INTERVALS = 4  # ...or this many of the person's bath intervals, whichever is longer
RESETS = ("seed", "bath", "offscreen_bath")

# (lowest dirt, label)
LABELS = [(0.80, "filthy"), (0.55, "grimy"), (0.30, "travel-worn"), (0.10, "clean"), (0.0, "fresh")]


@dataclass(frozen=True)
class HygieneState:
    dirt: float
    blood: float
    last_bath_gt: float | None

    @property
    def label(self) -> str:
        for lowest, label in LABELS:
            if self.dirt >= lowest:
                return label
        return "fresh"

    @property
    def bloodied(self) -> bool:
        return self.blood >= BLOODIED

    def days_since_bath(self, now: float) -> float | None:
        return None if self.last_bath_gt is None else max(0.0, now - self.last_bath_gt)


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def _changes(db, npc_id: int, until: float) -> list[tuple]:
    return [tuple(r) for r in db.execute("SELECT game_time, cause, dirt, blood FROM v_hygiene_change WHERE npc_id = ? AND game_time <= ?"
                      " ORDER BY game_time, change_id", (npc_id, until))]


class _Replay:
    """Replays a person's sorted change log forward in time, so the state at a series of rising times
    costs one pass over the log instead of one full re-read per time asked. Gives exactly the numbers
    ``state`` gives (same arithmetic, same order)."""

    def __init__(self, changes: list[tuple], habits: Habits):
        self.changes, self.habits = changes, habits
        self.i = 0
        self.dirt = self.blood = 0.0
        self.last_bath: float | None = None
        self.t: float | None = None

    def at(self, now: float) -> HygieneState:
        rate = self.habits.work_dirt_rate
        while self.i < len(self.changes) and self.changes[self.i][0] <= now:
            gt, cause, d_dirt, d_blood = self.changes[self.i][:4]
            if self.t is not None:
                self.dirt = _clamp(self.dirt + rate * (gt - self.t))
                self.blood = _clamp(self.blood - BLOOD_FADE_PER_DAY * (gt - self.t))
            self.t = gt
            if cause in RESETS:
                self.dirt, self.blood, self.last_bath = _clamp(d_dirt), _clamp(d_blood), gt
            else:
                self.dirt, self.blood = _clamp(self.dirt + d_dirt), _clamp(self.blood + d_blood)
            self.i += 1
        dirt, blood = self.dirt, self.blood
        if self.t is not None:
            dirt = _clamp(dirt + rate * (now - self.t))
            blood = _clamp(blood - BLOOD_FADE_PER_DAY * (now - self.t))
        return HygieneState(dirt, blood, self.last_bath)


def state(db, npc_id: int, now: float, habits: Habits | None = None) -> HygieneState:
    habits = habits or habits_for(db, npc_id)
    return _Replay(_changes(db, npc_id, now), habits).at(now)


def _insert(db, npc_id: int, gt: float, cause: str, dirt: float = 0.0, blood: float = 0.0,
            game_key: str | None = None) -> bool:
    timeline = db.execute("SELECT timeline_id FROM timeline WHERE is_active = 1").fetchone()[0]
    if game_key is not None and db.execute("SELECT 1 FROM v_hygiene_change WHERE game_key = ? LIMIT 1",
                                           (game_key,)).fetchone():
        return False   # the game sent it again after an older save was loaded; it already counts
    cur = db.execute("INSERT OR IGNORE INTO hygiene_change (timeline_id, npc_id, game_time, cause, dirt, blood, game_key)"
                     " VALUES (?,?,?,?,?,?,?)", (timeline, npc_id, gt, cause, dirt, blood, game_key))
    return cur.rowcount > 0


def ensure_seeded(db, npc_id: int, now: float, habits: Habits | None = None) -> None:
    """The first time anyone looks at this person: they last bathed some time within their usual interval."""
    if db.execute("SELECT 1 FROM v_hygiene_change WHERE npc_id = ? LIMIT 1", (npc_id,)).fetchone():
        return
    habits = habits or habits_for(db, npc_id)
    plugin, base = db.execute("SELECT plugin, base_form_id FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()
    ago = random.Random(f"hygiene-seed:{plugin}:{base}").uniform(0.1, 0.9) * habits.bath_interval_days
    _insert(db, npc_id, now - ago, "seed")


def _is_player(db, npc_id: int) -> bool:
    row = db.execute("SELECT is_player FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()
    return bool(row and row[0])


def _at_window(t: float, window: str) -> float:
    """The first time at or after ``t`` that falls in the person's preferred part of the day."""
    hour = WINDOWS.get(window, 0.8)
    day = math.floor(t)
    candidate = day + hour
    return candidate if candidate >= t else candidate + 1.0


def next_bath_due(db, npc_id: int, habits: Habits, after: float, now: float,
                  changes: list[tuple] | None = None) -> float:
    """When they would bathe next after their last bath at ``after``: at their usual interval, or sooner
    once dirt passes their own threshold, or within half a day of getting bloodied if they mind it.
    ``changes`` is their change log up to ``now`` if the caller already has it (saves re-reading it)."""
    latest = after + habits.bath_interval_days
    step = 0.25
    t = after + step
    replay = _Replay(changes if changes is not None else _changes(db, npc_id, now), habits)
    while t < latest and t <= now:
        s = replay.at(t)
        if s.dirt >= habits.threshold or (s.bloodied and habits.hygiene_standard >= 0.4):
            return _at_window(t + (0.5 if s.bloodied else 0.0), habits.bath_window)
        t += step
    return _at_window(latest, habits.bath_window)


def catch_up(db, npc_id: int, now: float, habits: Habits | None = None) -> int:
    """Write in the baths this NPC took off screen up to ``now``. Returns how many were added.

    The change log is read once and kept in step with the baths written, so a long gap costs one pass
    per bath rather than a full re-read every quarter day. When someone has not been seen for longer than
    ``CATCH_UP_WINDOW_DAYS`` (or a few of their bath intervals), the baths of the distant past are not
    written one by one: nothing from before the latest bath ever shows, so one bath is written at the start
    of the window and the usual catch-up runs from there."""
    if _is_player(db, npc_id) or is_child(db, npc_id):
        return 0
    habits = habits or habits_for(db, npc_id)
    ensure_seeded(db, npc_id, now, habits)
    changes = _changes(db, npc_id, now)
    added = 0

    def write(gt: float) -> None:
        nonlocal added
        _insert(db, npc_id, gt, "offscreen_bath")
        # sorted like _changes (game_time, then change_id): a new row sorts after rows at the same time
        bisect.insort_right(changes, (gt, "offscreen_bath", 0.0, 0.0), key=lambda c: c[0])
        added += 1

    last = _Replay(changes, habits).at(now).last_bath_gt
    if last is None:
        return 0
    window = max(CATCH_UP_WINDOW_DAYS, CATCH_UP_WINDOW_INTERVALS * habits.bath_interval_days)
    jump = _at_window(now - window, habits.bath_window)
    if jump - last > habits.bath_interval_days and jump <= now:
        write(jump)
    while added < MAX_CATCH_UP_BATHS:
        last = _Replay(changes, habits).at(now).last_bath_gt
        due = next_bath_due(db, npc_id, habits, last, now, changes)
        if due > now:
            return added
        write(due)
    return added


def current(db, npc_id: int, now: float) -> tuple[HygieneState, Habits]:
    """Catch the person up and return their state now, with their habits."""
    habits = habits_for(db, npc_id)
    if _is_player(db, npc_id):
        ensure_seeded(db, npc_id, now, habits)
    else:
        catch_up(db, npc_id, now, habits)
    return state(db, npc_id, now, habits), habits


# ---- game records ----
FIGHT_DIRT = 0.15
SWIM_MIN_SECONDS = 10.0


def fight_blood(hp: float | None) -> float:
    """Blood on someone after a fight, from the lowest health they fell to (1.0 = never hurt)."""
    if hp is None:
        return 0.15
    if hp < 0.5:
        return 0.6
    if hp < 0.85:
        return 0.35
    return 0.15            # other people's blood


def record_fight(db, npc_id: int, gt: float, hp: float | None, key: str) -> bool:
    ensure_seeded(db, npc_id, gt)
    return _insert(db, npc_id, gt, "fight", FIGHT_DIRT, fight_blood(hp), key)


def record_swim(db, npc_id: int, gt: float, seconds: float | None, key: str) -> bool:
    ensure_seeded(db, npc_id, gt)
    full = seconds is None or seconds >= SWIM_MIN_SECONDS
    return _insert(db, npc_id, gt, "swim", -0.5 if full else -0.2, -0.8 if full else -0.3, key)
