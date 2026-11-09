"""Written reports: after a duty report is filed, the member gives their report in their own words (spoken or
typed) and it goes into the guard's ledger as a ``guard.written_report`` event.

Rules check it against what the record shows for that duty (``encounter.summary`` and the incident, if any):

- **covered**: the share of the shift's matters the report mentions (by the name of who came over, or a telling
  word from what was noticed or the incident);
- **made up**: claims of things the record has no sign of (an arrest, a killing, bandits, a fire, a dragon...).

A thorough, honest report (most matters covered, nothing made up) adds merit; a report with made-up claims takes
some away and the superior remembers it. The superior keeps every report as a memory and can quote it back, and
the record counts them. A readable in-game note would need a plugin (.esp), so the ledger lives here.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from livingworld.guard import orgs as O, store as S
from livingworld.work import tasks as T

THOROUGH = 0.6
MERIT_GOOD = 0.5
MERIT_MADE_UP = -0.75
MIN_WORDS = 6
STOP = {"there", "their", "about", "which", "would", "could", "after", "before", "where", "while", "these", "those",
        "with", "from", "into", "near", "over", "some", "have", "been", "were", "they", "them", "this", "that", "your",
        "again", "along", "seems", "looks", "asked", "thing", "other", "under", "since"}
# claims a report can make that need something in the record to back them: (words, what would back it)
CLAIMS = (
    (("arrested", "arrest", "locked up", "in chains", "jailed"), ("guard.arrested",)),
    (("killed", "slew", "slain", "dead body", "murdered"), ("guard.cleared",)),
    (("bandit", "bandits", "raiders"), ("guard.cleared",)),
    (("fire", "burning", "blaze"), ()),
    (("dragon",), ()),
    (("giant", "giants"), ()),
)


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z']+", (text or "").lower())


def _telling(text: str) -> set[str]:
    return {w for w in _words(text) if len(w) >= 5 and w not in STOP}


@dataclass
class Judged:
    items: int
    covered: int
    made_up: list[str]

    @property
    def share(self) -> float:
        return self.covered / self.items if self.items else 1.0

    @property
    def verdict(self) -> str:
        if self.made_up:
            return "made up"
        return "thorough" if self.share >= THOROUGH else "thin"


def judge(db, org: O.Org, task: T.Task, text: str, now: float) -> Judged:
    from livingworld.guard import duty as DU, encounter as ENC
    said = " " + " ".join(_words(text)) + " "
    said_words = set(_words(text))
    s = ENC.summary(db, org, task, now)
    items = covered = 0
    for who, what, _, _ in s.encounters:
        items += 1
        first = (who or "").split()[0].lower() if who else ""
        if (first and f" {first} " in said) or (_telling(what) & said_words):
            covered += 1
    for obs, _ in s.observations:
        items += 1
        if len(_telling(obs) & said_words) >= 1:
            covered += 1
    d = DU.duty_for(db, task)
    if d is not None and d.incident_id is not None:
        items += 1
        if _telling(d.title) & said_words or _telling(d.why) & said_words:
            covered += 1
    backed = {r[0] for r in db.execute(
        "SELECT type FROM v_visible_event WHERE type IN ('guard.arrested', 'guard.cleared') AND"
        " json_extract(payload_json, '$.incident') = ?", (d.incident_id if d else -1,))}
    record_text = " ".join([s.text(), d.title if d else "", d.why if d else ""]).lower()
    made_up = []
    for words, needs in CLAIMS:
        hit = next((w for w in words if f" {w} " in said), None)
        if hit is None:
            continue
        if any(n in backed for n in needs) or any(w in record_text for w in words):
            continue
        made_up.append(hit)
    return Judged(items, covered, made_up)


def file(db, org: O.Org, svc: S.Service, task: T.Task, boss: int | None, text: str, now: float,
         location: str | None = None, witnesses: list[int] | None = None) -> Judged | None:
    """Keep the member's own words as their written report on ``task`` (once per duty)."""
    from livingworld.guard import duty as DU
    text = " ".join((text or "").split())
    if len(_words(text)) < MIN_WORDS:
        return None
    if db.execute("SELECT 1 FROM v_visible_event WHERE type = 'guard.written_report' AND"
                  " json_extract(payload_json, '$.task') = ? LIMIT 1", (task.task_id,)).fetchone():
        return None
    j = judge(db, org, task, text, now)
    S.event(db, "written_report", [svc.member_id, boss],
            {"org": org.key, "service_id": svc.service_id, "task": task.task_id, "title": task.title,
             "text": text[:2000], "items": j.items, "covered": j.covered, "made_up": j.made_up,
             "verdict": j.verdict}, now, location, witnesses)
    who = DU._name(db, svc.member_id)
    if boss is not None:
        tail = {"made up": f" Parts of it had no sign in the record ({', '.join(j.made_up)}).",
                "thin": " It left things out.", "thorough": " It was thorough."}[j.verdict]
        DU._remember(db, boss, svc.member_id, f"{who}'s written report on their duty to {task.title}: \"{text[:400]}\"."
                     + tail, -0.3 if j.made_up else 0.15, 0.4, None, location, now)
    if j.verdict == "thorough":
        S.change(db, svc, f"a thorough written report ({task.title})", now, d_merit=MERIT_GOOD)
    elif j.verdict == "made up":
        S.change(db, svc, f"a written report with claims the record does not bear out ({task.title})", now,
                 d_merit=MERIT_MADE_UP)
    return j


def recent(db, svc: S.Service, n: int = 2) -> list[tuple[str, str]]:
    rows = db.execute("SELECT payload_json FROM v_visible_event WHERE type = 'guard.written_report' AND"
                      " json_extract(payload_json, '$.service_id') = ? ORDER BY game_time DESC, event_id DESC LIMIT ?",
                      (svc.service_id, n)).fetchall()
    return [(json.loads(r[0]).get("title", ""), json.loads(r[0]).get("text", "")) for r in rows]


def count(db, svc: S.Service) -> int:
    return db.execute("SELECT COUNT(*) FROM v_visible_event WHERE type = 'guard.written_report' AND"
                      " json_extract(payload_json, '$.service_id') = ?", (svc.service_id,)).fetchone()[0]
