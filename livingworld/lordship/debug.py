"""Test commands said or typed in a conversation: "debug charter <verb> [number]".

    debug charter status            what the charter, purse and last reckoning say (logged)
    debug charter grant [level N]   sign a charter now (L2 will do this from the cavalry quest, stage 20)
    debug charter watch             from now on, missing riders/patrol/report facts count as missed terms
    debug charter riders N          the game reports N riders (horses N likewise)
    debug charter patrol [N]        N patrols ridden (default 1)
    debug charter report            a written report filed with the steward
    debug charter month             run the next monthly reckoning now, without waiting 30 days

All of it is written on the save timeline with ``source = 'admin'`` so an older save undoes it.
"""
from __future__ import annotations

import re

from livingworld.lordship import charter as C
from livingworld.lordship import data as D
from livingworld.lordship import reckoning as R

COMMANDS = ("status", "grant", "watch", "riders", "horses", "patrol", "patrols", "report", "month", "tick")
_LINE = re.compile(r"^\W*debug\s+charter\b\s*(\w+)?(?:\D*(\d+))?", re.I)
_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
            "ten": 10, "twelve": 12, "sixteen": 16, "twenty": 20}


def parse(text: str | None) -> tuple[str, int | None] | None:
    if not text:
        return None
    m = _LINE.match(text)
    if not m:
        return None
    verb = (m.group(1) or "status").lower()
    if verb not in COMMANDS:
        return None
    number = int(m.group(2)) if m.group(2) else None
    if number is None:
        for word in re.findall(r"[a-z]+", text.lower()):
            if word in _NUMBERS:
                number = _NUMBERS[word]
    return {"patrols": "patrol", "tick": "month"}.get(verb, verb), number


def status_text(db, now: float) -> str:
    charter = C.current(db) or C.latest(db)
    if charter is None:
        return "no charter yet"
    riders, horses = C.strength(db, now)
    snap = R.latest_snapshot(db, charter)
    tail = (f"; month {snap[0]}: power {snap[2]:.2f}, legitimacy {snap[3]:.2f}, gap {snap[4]:+.2f} ({snap[5]})"
            if snap else "; no reckoning yet")
    stances = ", ".join(f"{k} {p} {s:+.2f}" for k, p, s in db.execute(
        "SELECT official_key, position, score FROM v_official_stance_now WHERE subject = ?", (charter.holder,)))
    return (f"charter v{charter.version} {charter.title} ({charter.status}); riders {riders}/"
            f"{charter.terms.get('personnel_cap')}, horses {horses}; purse {C.treasury(db)} gold{tail}"
            + (f"; {stances}" if stances else ""))


def _fact_time(db, now: float) -> float:
    """After 'debug charter month' ran a reckoning ahead of the clock, new test facts belong to the next month."""
    charter = C.current(db)
    if charter is None:
        return now
    end = charter.signed_gt + R.periods_done(db, charter) * D.terms().period_days
    return max(now, end + 0.001)


def run(db, text: str, player_id: int | None, now: float) -> str | None:
    """Apply a test command. Returns what happened (for the log), or None when ``text`` is not one."""
    parsed = parse(text)
    if parsed is None or player_id is None:
        return None
    verb, n = parsed
    if verb == "status":
        return status_text(db, now)
    if verb == "grant":
        level = n if n in D.terms().levels else 1
        c = C.grant(db, player_id, now, level=level, source="admin", reason="developer test command")
        return f"granted charter v{c.version} ({c.title})"
    if C.current(db) is None:
        return "no charter yet: say 'debug charter grant' first"
    at = _fact_time(db, now)
    if verb == "watch":
        C.declare_watching(db, C.FACT_KINDS, at, source="admin")
        return "strength, patrols and reports are now judged even when nothing is reported"
    if verb in ("riders", "horses"):
        riders, horses = C.strength(db, at)
        if n is None:
            return f"say how many {verb}"
        riders, horses = (n, horses) if verb == "riders" else (riders, n)
        C.report_fact(db, "strength", at, player_id, {"riders": riders, "horses": horses}, source="admin")
        return f"strength reported: {riders} riders, {horses} horses"
    if verb == "patrol":
        C.report_fact(db, "patrol", at, player_id, {"route": "test", "count": n or 1}, source="admin")
        return f"{n or 1} patrol(s) recorded"
    if verb == "report":
        C.report_fact(db, "report", at, player_id, {}, source="admin")
        return "report filed with the steward"
    if verb == "month":
        charter = C.current(db)
        done = R.periods_done(db, charter)
        end = charter.signed_gt + (done + 1) * D.terms().period_days
        result = R.catch_up(db, max(now, end + 0.0001), limit=1)
        return f"reckoned month {done + 1}: paid {result.paid} gold; {'; '.join(result.moves) or 'no moves'}; " \
               f"charter {result.status}. " + status_text(db, max(now, end))
    return None
