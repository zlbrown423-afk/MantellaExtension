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

import math
import random
from dataclasses import dataclass

from livingworld.hygiene.profile import WINDOWS, Habits, habits_for
from livingworld.store.minors import is_child

BLOOD_FADE_PER_DAY = 0.15
BLOODIED = 0.3
MAX_CATCH_UP_BATHS = 40
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
    return db.execute("SELECT game_time, cause, dirt, blood FROM v_hygiene_change WHERE npc_id = ? AND game_time <= ?"
                      " ORDER BY game_time, change_id", (npc_id, until)).fetchall()


def state(db, npc_id: int, now: float, habits: Habits | None = None) -> HygieneState:
    habits = habits or habits_for(db, npc_id)
    dirt = blood = 0.0
    last_bath = None
    t = None
    for gt, cause, d_dirt, d_blood in _changes(db, npc_id, now):
        if t is not None:
            dirt = _clamp(dirt + habits.work_dirt_rate * (gt - t))
            blood = _clamp(blood - BLOOD_FADE_PER_DAY * (gt - t))
        t = gt
        if cause in RESETS:
            dirt, blood, last_bath = _clamp(d_dirt), _clamp(d_blood), gt
        else:
            dirt, blood = _clamp(dirt + d_dirt), _clamp(blood + d_blood)
    if t is not None:
        dirt = _clamp(dirt + habits.work_dirt_rate * (now - t))
        blood = _clamp(blood - BLOOD_FADE_PER_DAY * (now - t))
    return HygieneState(dirt, blood, last_bath)


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


def next_bath_due(db, npc_id: int, habits: Habits, after: float, now: float) -> float:
    """When they would bathe next after their last bath at ``after``: at their usual interval, or sooner
    once dirt passes their own threshold, or within half a day of getting bloodied if they mind it."""
    latest = after + habits.bath_interval_days
    step = 0.25
    t = after + step
    while t < latest and t <= now:
        s = state(db, npc_id, t, habits)
        if s.dirt >= habits.threshold or (s.bloodied and habits.hygiene_standard >= 0.4):
            return _at_window(t + (0.5 if s.bloodied else 0.0), habits.bath_window)
        t += step
    return _at_window(latest, habits.bath_window)


def catch_up(db, npc_id: int, now: float, habits: Habits | None = None) -> int:
    """Write in the baths this NPC took off screen up to ``now``. Returns how many were added."""
    if _is_player(db, npc_id) or is_child(db, npc_id):
        return 0
    habits = habits or habits_for(db, npc_id)
    ensure_seeded(db, npc_id, now, habits)
    added = 0
    while added < MAX_CATCH_UP_BATHS:
        last = state(db, npc_id, now, habits).last_bath_gt
        if last is None:
            return added
        due = next_bath_due(db, npc_id, habits, last, now)
        if due > now:
            return added
        _insert(db, npc_id, due, "offscreen_bath")
        added += 1
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
