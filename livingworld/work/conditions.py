"""When a need makes sense: season, hour, weather, and whether the giver would ask this person.

Every check returns a ``Check`` with a plain reason, so the developer tool can explain exactly why a
need is open, waiting or blocked.
"""
from __future__ import annotations

from dataclasses import dataclass

from livingworld.social import claims
from livingworld.work.templates import Template

MONTHS = ("Morning Star", "Sun's Dawn", "First Seed", "Rain's Hand", "Second Seed", "Midyear", "Sun's Height",
          "Last Seed", "Hearthfire", "Frostfall", "Sun's Dusk", "Evening Star")
MONTH_DAYS = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
# A new game starts on 17 Last Seed 4E 201; GameDaysPassed counts from there.
START_DAY_OF_YEAR = sum(MONTH_DAYS[:7]) + 16

# Below this reliability (0..1, see ``reliability``), after at least this many let-downs, nobody asks again.
UNRELIABLE = 0.35
UNRELIABLE_AFTER = 2


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    why: str


def month(game_days: float) -> str:
    day = (START_DAY_OF_YEAR + int(game_days)) % 365
    for name, length in zip(MONTHS, MONTH_DAYS):
        if day < length:
            return name
        day -= length
    return MONTHS[-1]


def hour_of(game_days: float) -> int:
    return int((game_days % 1.0) * 24)


def season_check(t: Template, game_days: float) -> Check:
    m = month(game_days)
    if not t.months or m in t.months:
        return Check("season", True, f"{m} is a month for it" if t.months else "any month")
    return Check("season", False, f"not needed in {m}")


def hour_check(t: Template, hour: int) -> Check:
    start, end = t.hours
    ok = start <= hour < end
    return Check("hour", ok, f"{hour}:00 is within {start}:00 to {end}:00" if ok
                 else f"{hour}:00 is outside working hours ({start}:00 to {end}:00)")


def weather_check(t: Template, weather: str | None) -> Check:
    text = (weather or "").lower()
    if not t.outdoors:
        return Check("weather", True, "indoor work")
    bad = next((w for w in t.bad_weather if w in text), None)
    if bad:
        return Check("weather", False, f"too much {bad} to work outdoors")
    return Check("weather", True, f"weather fits ({weather or 'unknown'})")


def working_checks(t: Template, hour: int, weather: str | None) -> list[Check]:
    return [hour_check(t, hour), weather_check(t, weather)]


def pause_reason(t: Template, hour: int, weather: str | None) -> str | None:
    failed = [c for c in working_checks(t, hour, weather) if not c.ok]
    if not failed:
        return None
    if failed[0].name == "hour":
        return "it is too late in the day" if hour >= t.hours[1] else "it is too early in the day"
    return failed[0].why


def reliability(db, worker_id: int) -> tuple[float, int, int]:
    """(reliability 0..1, tasks finished, tasks let down) for work this person agreed to, on this timeline."""
    rows = db.execute("SELECT state, COUNT(*) FROM v_task WHERE assignee_id = ? AND is_reset = 0 AND state IN"
                      " ('completed','failed','expired') GROUP BY state", (worker_id,)).fetchall()
    done = sum(n for s, n in rows if s == "completed")
    let_down = sum(n for s, n in rows if s in ("failed", "expired"))
    return (done + 1) / (done + let_down + 2), done, let_down


def ask_checks(db, t: Template, giver_id: int, worker_id: int) -> list[Check]:
    """Would the giver ask this person? Trust in them, and whether they keep their word."""
    b = claims.bond(db, giver_id, worker_id)
    out = [Check("trust", b["trust"] >= t.ask_min_trust,
                 f"trust {b['trust']:+.2f} (needs {t.ask_min_trust:+.2f})")]
    if b["status"] in ("enemy",):
        out.append(Check("relationship", False, "they are enemies"))
    rel, done, let_down = reliability(db, worker_id)
    out.append(Check("reliability", not (let_down >= UNRELIABLE_AFTER and rel < UNRELIABLE),
                     f"finished {done}, let down {let_down} (reliability {rel:.2f})"))
    return out
