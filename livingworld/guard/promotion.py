"""G3: the promotion board. Rules only, no LLM, every check explained.

Once a member could move up, the board (``board``) checks them for the next rank of their guard. The numbers
live in ``orgs.json`` under ``promotion`` (per rank, with a default) and ``ranks[].min_days``:

- **Status**: serving and past probation (``active``).
- **Time**: at least ``min_days`` in the current rank.
- **Training** (when the rank asks for it): recruit training finished.
- **Duties**: at least ``duties`` duties completed in the current rank.
- **Merit** earned in the current rank, with diminishing returns: each kind of duty pays its full merit for its
  first ``halves_after`` completions in a rank, half for the next ``halves_after``, a quarter after that, and so
  on, so walking the same patrol forty times does not promote anyone. Merit from anything else (commendations,
  written reports, people dealt with on shift, arrests, reprimands) counts as recorded.
- **Written reports**: at least ``reports`` written reports in the current rank.
- **Deeds**: at least ``deeds`` commendations, incidents dealt with or arrests booked in the current rank.
- **Clean record**: no reprimand in the last ``clean_days`` days.
- **The superior's view**: their standing of the member (trust, respect and what they have heard, as for
  recruiting) at least ``view``.
- **The Jarl's favour** (when the rank asks for it): the same, from the Jarl.
- **An open seat**: ranks with a seat count are full when leaders from ``orgs.json`` or other members hold
  every seat. Captain and Commander are held by Caius and Irileth, so they open only if a seat is freed.

A passed board is not instant. It writes ``guard.promotion_due``; the superior wants a word with the member,
and the next time the member talks to them the promotion takes effect (``take``) and the superior announces
it. The member can turn it down in that conversation (``declines``); the board then waits ``decline_days``.
The promotion becomes news (``guard.promoted``) when that conversation ends without a refusal.

Switch off with MANTELLA_LW_GUARD_PROMOTIONS=0 (``debug guard rank`` still works).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from livingworld.guard import duty as DU, orgs as O, profiles, recruit as R, store as S
from livingworld.work.conditions import Check

ENV = "MANTELLA_LW_GUARD_PROMOTIONS"
DUE = "promotion_due"
DECLINED = "promotion_declined"
DECLINE_WORDS = re.compile(
    r"\b(i\s+(must\s+|have\s+to\s+|'ll\s+|will\s+|would\s+)?(decline|refuse|turn\s+(it|this|that|the\s+promotion)\s+down)"
    r"|i\s+(don't|do\s+not)\s+want\s+(the|this|that|a)\s+promotion|i'?m\s+not\s+ready\s+(for\s+(it|this|that|the\s+promotion))?"
    r"|no,?\s+thank\s+you,?\s+(sir|commander|captain|ma'am)?\.?\s*$|keep\s+me\s+(as|at)\s+(a\s+)?\w+)",
    re.I)


def enabled() -> bool:
    return os.environ.get(ENV, "1").strip().lower() not in ("0", "false", "off", "no")


@dataclass(frozen=True)
class Need:
    """What the board asks of a member for one rank (from ``orgs.json`` ``promotion``)."""
    merit: float = 0.0
    duties: int = 0
    reports: int = 0
    deeds: int = 0
    view: float = -1.0
    training: bool = False
    jarl: float | None = None          # the Jarl's standing of the member needed, or None


@dataclass(frozen=True)
class Config:
    halves_after: int = 3
    clean_days: float = 7.0
    decline_days: float = 7.0
    needs: dict = field(default_factory=dict, hash=False, compare=False)

    def need(self, rank_key: str) -> Need:
        raw = {**(self.needs.get("default") or {}), **(self.needs.get(rank_key) or {})}
        known = {k: raw[k] for k in Need.__dataclass_fields__ if k in raw}
        return Need(**known)


def config(org: O.Org) -> Config:
    raw = org.promotion or {}
    keep = {k: raw[k] for k in ("halves_after", "clean_days", "decline_days") if k in raw}
    return Config(needs=raw.get("ranks") or {}, **keep)


@dataclass
class Board:
    org: O.Org
    svc: S.Service
    target: O.Rank | None
    checks: list[Check] = field(default_factory=list)
    merit: float = 0.0          # merit counted for this rank (after diminishing returns)

    @property
    def ok(self) -> bool:
        return self.target is not None and all(c.ok for c in self.checks)

    @property
    def missing(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]


# ---- what the member has done in their current rank ----

def rank_start(db, svc: S.Service) -> float:
    """When the member took up their current rank. Turning a promotion down puts the old rank back without
    starting its clock again."""
    rows = db.execute("SELECT game_time, reason FROM v_service_change WHERE service_id = ? AND rank_key IS NOT NULL"
                      " ORDER BY game_time DESC, change_id DESC", (svc.service_id,)).fetchall()
    skip = 0
    for gt, reason in rows:
        if (reason or "").startswith("turned down promotion"):
            skip += 1                    # this and the promotion it undid
            continue
        if skip:
            skip -= 1
            continue
        return gt
    return svc.joined_gt


def _since(svc: S.Service, db=None) -> float:
    start = rank_start(db, svc) if db is not None else (svc.rank_gt if svc.rank_gt is not None else svc.joined_gt)
    return start - 1e-9


def _kind(template: str) -> str:
    """The duty kind for diminishing returns: 'gate_watch', 'incident:theft' (not each incident on its own)."""
    key = template.removeprefix(DU.PREFIX)
    if key.startswith("incident:"):
        return ":".join(key.split(":")[:2])
    return key


def duty_merit(db, org: O.Org, svc: S.Service, halves_after: int) -> tuple[float, int]:
    """(merit from duties done in the current rank after diminishing returns, how many duties were done)."""
    rows = db.execute("SELECT task_id FROM v_task WHERE template LIKE 'duty:%' AND assignee_id = ? AND is_reset = 0"
                      " AND state = 'completed' AND state_gt >= ? ORDER BY state_gt, task_id",
                      (svc.member_id, _since(svc, db))).fetchall()
    from livingworld.work import tasks as T
    seen: dict[str, int] = {}
    total = 0.0
    n = 0
    for (task_id,) in rows:
        task = T.get(db, task_id)
        if task is None:
            continue
        d = DU.duty_for(db, task)
        full = d.merit if d is not None else 1.0
        kind = _kind(task.template)
        k = seen.get(kind, 0)
        seen[kind] = k + 1
        total += full * (0.5 ** (k // max(1, halves_after)))
        n += 1
    return total, n


def other_merit(db, svc: S.Service) -> float:
    """Merit recorded in the current rank for anything but a duty done (commendations, reports, reprimands...)."""
    row = db.execute("SELECT COALESCE(SUM(d_merit), 0) FROM v_service_change WHERE service_id = ? AND game_time >= ?"
                     " AND (reason IS NULL OR reason NOT LIKE 'duty done:%')", (svc.service_id, _since(svc, db))).fetchone()
    return float(row[0] or 0.0)


def _events(db, kind: str, svc: S.Service, since: float) -> int:
    return db.execute("SELECT COUNT(*) FROM v_visible_event WHERE type = ? AND json_extract(actors_json, '$[0]') = ?"
                      " AND json_extract(payload_json, '$.org') = ? AND game_time >= ?",
                      (f"guard.{kind}", svc.member_id, svc.org_key, since)).fetchone()[0]


def deeds(db, svc: S.Service) -> int:
    since = _since(svc, db)
    booked = db.execute("SELECT COUNT(*) FROM v_visible_event WHERE type = 'guard.booked' AND json_extract(actors_json,"
                        " '$[0]') = ? AND json_extract(payload_json, '$.org') = ? AND game_time >= ? AND"
                        " json_extract(payload_json, '$.wrongful') = 0", (svc.member_id, svc.org_key, since)).fetchone()[0]
    return _events(db, "commended", svc, since) + _events(db, "cleared", svc, since) + booked


def reports(db, svc: S.Service) -> int:
    return db.execute("SELECT COUNT(*) FROM v_visible_event WHERE type = 'guard.written_report' AND"
                      " json_extract(payload_json, '$.service_id') = ? AND game_time >= ?",
                      (svc.service_id, _since(svc, db))).fetchone()[0]


def last_reprimand(db, svc: S.Service) -> float | None:
    return db.execute("SELECT MAX(game_time) FROM v_visible_event WHERE type = 'guard.reprimanded' AND"
                      " json_extract(actors_json, '$[0]') = ? AND json_extract(payload_json, '$.org') = ?",
                      (svc.member_id, svc.org_key)).fetchone()[0]


def _holders(db, org: O.Org, svc: S.Service, rank_key: str) -> list[str]:
    """Who holds ``rank_key`` in this guard now: its leaders from orgs.json and any other serving member."""
    out = [p.name for p in org.leaders if p.rank == rank_key]
    for other in S.find(db, "org_key = ? AND rank_key = ? AND service_id <> ? AND status IN ('probation','active',"
                            "'suspended')", (org.key, rank_key, svc.service_id)):
        row = db.execute("SELECT display_name FROM npc WHERE npc_id = ?", (other.member_id,)).fetchone()
        out.append(row[0] if row else "another member")
    return out


def _jarl_id(db, org: O.Org) -> int | None:
    return profiles.leader_id(db, org, org.jarl.key) if org.jarl else None


# ---- the board ----

def board(db, org: O.Org, svc: S.Service, now: float) -> Board:
    """Check the member for the next rank of their guard; every rule is a Check, in a fixed order."""
    target = org.next_rank(svc.rank_key)
    b = Board(org, svc, target)
    if target is None:
        b.checks.append(Check("rank", False, f"{org.title(svc.rank_key)} is the highest rank in {org.name}"))
        return b
    cfg = config(org)
    need = cfg.need(target.key)
    title = org.title(svc.rank_key)
    b.checks.append(Check("status", svc.status == "active",
                          "past probation" if svc.status == "active" else
                          "still on probation" if svc.status == "probation" else f"status {svc.status}"))
    days = now - rank_start(db, svc)
    b.checks.append(Check("time", days >= target.min_days - 1e-9,
                          f"{days:.0f} of {target.min_days:g} days as {title}"))
    if need.training:
        from livingworld.guard import training as TR
        done = TR.trained(db, svc)
        b.checks.append(Check("training", done, "recruit training finished" if done else "recruit training not finished"))
    dm, n = duty_merit(db, org, svc, cfg.halves_after)
    if need.duties:
        b.checks.append(Check("duties", n >= need.duties, f"{n} of {need.duties} duties done as {title}"))
    b.merit = dm + other_merit(db, svc)
    if need.merit:
        b.checks.append(Check("merit", b.merit >= need.merit - 1e-9,
                              f"merit {b.merit:.1f} of {need.merit:g} earned as {title} (the same duty pays less "
                              f"after {cfg.halves_after} times)"))
    if need.reports:
        r = reports(db, svc)
        b.checks.append(Check("reports", r >= need.reports, f"{r} of {need.reports} written reports as {title}"))
    if need.deeds:
        k = deeds(db, svc)
        b.checks.append(Check("deeds", k >= need.deeds, f"{k} of {need.deeds} deeds (commendations, incidents dealt "
                                                         f"with, arrests booked) as {title}"))
    last = last_reprimand(db, svc)
    clean = last is None or now - last >= cfg.clean_days
    b.checks.append(Check("clean", clean, "no recent reprimand" if clean else
                          f"reprimanded {now - last:.0f} days ago (needs {cfg.clean_days:g} clean days)"))
    boss = DU.superior_id(db, org, svc)
    person = org.superior(svc.rank_key)
    if boss is not None:
        value, why = R.standing(db, boss, svc.member_id)
        b.checks.append(Check("view", value >= need.view - 1e-9,
                              f"{person.name if person else 'their superior'} thinks {value:+.2f} of them "
                              f"(needs {need.view:+.2f}): {why}"))
    elif person is not None:
        b.checks.append(Check("view", False, f"{person.name} has not been met yet"))
    if need.jarl is not None:
        jarl = _jarl_id(db, org)
        if jarl is None:
            b.checks.append(Check("jarl", False, f"{org.jarl.name if org.jarl else 'the Jarl'} has not been met yet"))
        else:
            value, why = R.standing(db, jarl, svc.member_id)
            b.checks.append(Check("jarl", value >= need.jarl - 1e-9,
                                  f"{org.jarl.name} thinks {value:+.2f} of them (needs {need.jarl:+.2f})"))
    if target.seats is not None:
        held = _holders(db, org, svc, target.key)
        free = len(held) < target.seats
        b.checks.append(Check("seat", free, f"{target.seats - len(held)} of {target.seats} {target.title} seat(s) open"
                              if free else f"no {target.title} seat open (held by {', '.join(held)})"))
    return b


def explain(b: Board) -> list[str]:
    if b.target is None:
        return [f"{b.org.name}: {b.checks[0].why}"]
    lines = [f"{b.org.name}: {b.org.title(b.svc.rank_key)} -> {b.target.title}: "
             + ("PASSES the board" if b.ok else f"not yet ({len(b.missing)} of {len(b.checks)} checks to go)")]
    lines += [f"  {'ok ' if c.ok else 'NO '} {c.name}: {c.why}" for c in b.checks]
    return lines


def short(b: Board, limit: int = 3) -> str:
    """One line for an on-screen note: what the next rank still needs."""
    if b.target is None:
        return f"Highest rank: {b.org.title(b.svc.rank_key)}."
    if b.ok:
        return f"Promotion board: ready for {b.target.title}."
    miss = b.missing
    more = f" (+{len(miss) - limit} more)" if len(miss) > limit else ""
    return f"Next rank ({b.target.title}): " + "; ".join(c.why for c in miss[:limit]) + more + "."


# ---- the offer: due, taken, declined ----

def _last(db, kind: str, svc: S.Service) -> dict | None:
    row = db.execute("SELECT event_id, game_time, payload_json FROM v_visible_event WHERE type = ? AND"
                     " json_extract(payload_json, '$.service_id') = ? ORDER BY game_time DESC, event_id DESC LIMIT 1",
                     (f"guard.{kind}", svc.service_id)).fetchone()
    if row is None:
        return None
    return {"event_id": row[0], "gt": row[1], **json.loads(row[2] or "{}")}


def pending(db, svc: S.Service) -> str | None:
    """The rank a passed board has offered and the member has not been given yet, or None."""
    due = _last(db, DUE, svc)
    if due is None or due.get("rank_from") != svc.rank_key:
        return None
    declined = _last(db, DECLINED, svc)
    if declined is not None and declined["gt"] >= due["gt"]:
        return None
    return due.get("rank")


def run(db, org: O.Org, svc: S.Service, now: float) -> str | None:
    """Run the board for one member; on a pass, offer the promotion (once). Returns the rank offered, if any."""
    if not enabled() or svc.status != "active" or pending(db, svc) is not None:
        return None
    declined = _last(db, DECLINED, svc)
    if declined is not None and declined.get("rank_from") == svc.rank_key and \
            now - declined["gt"] < config(org).decline_days:
        return None
    b = board(db, org, svc, now)
    if not b.ok:
        return None
    return offer(db, org, svc, b.target, now, merit=b.merit)


def offer(db, org: O.Org, svc: S.Service, target: O.Rank, now: float, *, merit: float = 0.0,
          source: str = "sim") -> str:
    """Record that a promotion to ``target`` is due (the board passed, or a test command forced it)."""
    S.event(db, DUE, [svc.member_id, DU.superior_id(db, org, svc)],
            {"service_id": svc.service_id, "org": org.key, "rank_from": svc.rank_key, "rank": target.key,
             "merit": round(merit, 2)}, now, None, [], source)
    return target.key


def take(db, org: O.Org, svc: S.Service, boss: int, now: float) -> tuple[str, str] | None:
    """The member is talking to their superior with a promotion due: it takes effect now. Returns (from, to)."""
    to = pending(db, svc)
    if to is None or org.rank(to) is None:
        return None
    frm = svc.rank_key
    S.change(db, svc, f"promoted to {org.title(to)} by the board", now, rank_key=to)
    return frm, to


def announce(db, org: O.Org, svc: S.Service, frm: str, boss: int, now: float, location: str | None,
             witnesses: list[int] | None) -> int:
    """The conversation ended without a refusal: the promotion becomes news."""
    return S.event(db, "promoted", [svc.member_id, boss],
                   {"service_id": svc.service_id, "org": org.key, "reason": "passed the promotion board",
                    "rank_from": frm, "rank": svc.rank_key, "status": svc.status}, now, location, witnesses)


def declines(text: str | None) -> bool:
    return bool(text) and DECLINE_WORDS.search(text) is not None


def decline(db, org: O.Org, svc: S.Service, frm: str, boss: int, now: float) -> S.Service:
    """The member turned it down in the same conversation: back to the old rank; the board waits a while."""
    to = svc.rank_key
    S.event(db, DECLINED, [svc.member_id, boss], {"service_id": svc.service_id, "org": org.key, "rank_from": frm,
                                                  "rank": to}, now, None, [])
    return S.change(db, svc, f"turned down promotion to {org.title(to)}", now, rank_key=frm)
