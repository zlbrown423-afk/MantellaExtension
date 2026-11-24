"""Diplomatic promises: made, kept, withdrawn, lapsed or broken.

A promise is only ever made by the person themselves (their own line, asserted or conditional). Its status only
changes from that person's own later words (fulfilment, retraction), from game time passing its due date
(lapsed), or from something the game saw (an accepted meeting they did not attend: broken). Each change is a new
row, so an older save shows the promise as it stood then.

Consequences are deliberately small and only in the ``reliability`` reputation domain of the person the promise
was made to: trust and affection stay where the conversation left them.
"""
from __future__ import annotations

from livingworld.diplomacy import common as K

KEPT_RELIABILITY = 0.08
BROKEN_RELIABILITY = -0.2
LAPSED_RELIABILITY = -0.15


def make(db, timeline: int, stmt_id: int, debtor: int, creditor: int | None, issue: str, description: str,
         condition: str | None, due_days: float | None, audience: str, gt: float,
         meeting_id: int | None = None) -> int | None:
    """Record a promise (once per statement). Returns commit_id, or None if this statement already made one."""
    if db.execute("SELECT 1 FROM dip_commitment WHERE stmt_id = ?", (stmt_id,)).fetchone():
        return None
    due = gt + float(due_days) if due_days else None
    commit_id = db.execute(
        "INSERT INTO dip_commitment (timeline_id, stmt_id, debtor_id, creditor_id, issue, description, condition,"
        " due_gt, audience, meeting_id, made_gt) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (timeline, stmt_id, debtor, creditor, issue, description, condition, due, audience, meeting_id, gt)).lastrowid
    db.execute("INSERT INTO dip_commitment_change (timeline_id, commit_id, status, reason, stmt_id, game_time)"
               " VALUES (?,?, 'open', 'promised', ?, ?)", (timeline, commit_id, stmt_id, gt))
    return commit_id


def latest_open(db, debtor: int, issue: str | None = None, meeting_id: int | None = None) -> dict | None:
    sql = "SELECT * FROM v_dip_commitment WHERE debtor_id = ? AND status = 'open'"
    args: list = [debtor]
    if meeting_id is not None:
        sql += " AND meeting_id = ?"
        args.append(meeting_id)
    elif issue and issue != "other":
        sql += " AND issue = ?"
        args.append(issue)
    row = db.execute(sql + " ORDER BY made_gt DESC, commit_id DESC LIMIT 1", tuple(args)).fetchone()
    return dict(row) if row else None


def set_status(db, timeline: int, commit: dict, status: str, reason: str, gt: float,
               stmt_id: int | None = None) -> None:
    """Move a promise to a new status and apply its (small) consequence once."""
    if commit.get("status") == status:
        return
    db.execute("INSERT INTO dip_commitment_change (timeline_id, commit_id, status, reason, stmt_id, game_time)"
               " VALUES (?,?,?,?,?,?)", (timeline, commit["commit_id"], status, reason, stmt_id, gt))
    debtor, creditor = commit["debtor_id"], commit["creditor_id"]
    K.event(db, timeline, "diplomacy.commitment",
            {"key": f"commit:{commit['commit_id']}:{status}", "commit_id": commit["commit_id"], "status": status,
             "debtor": debtor, "creditor": creditor}, gt)
    if creditor is None:
        return
    what = commit["description"]
    who = K.name_of(db, debtor)
    if status == "kept":
        K.standing(db, timeline, creditor, debtor, "reliability", KEPT_RELIABILITY,
                   f"diplomacy: kept promise {commit['commit_id']}", gt)
    elif status in ("lapsed", "broken"):
        delta = BROKEN_RELIABILITY if status == "broken" else LAPSED_RELIABILITY
        K.standing(db, timeline, creditor, debtor, "reliability", delta,
                   f"diplomacy: {status} promise {commit['commit_id']}", gt)
        verb = "did not keep it" if status == "broken" else "the time we agreed has passed without it"
        K.remember(db, timeline, creditor, f"{who} promised me that they {what}, and {verb}.", [debtor], gt, 0.6,
                   -0.4, "experienced")


def on_statement(db, timeline: int, stmt: dict) -> str | None:
    """A first-hand fulfilment or retraction by the debtor. Returns what changed, for the log."""
    if stmt["kind"] == "fulfilment":
        commit = latest_open(db, stmt["subject_id"], stmt["issue"])
        if commit:
            set_status(db, timeline, commit, "kept", "said it was done", stmt["game_time"], stmt["stmt_id"])
            return f"kept {commit['commit_id']}"
    elif stmt["kind"] == "retraction":
        commit = latest_open(db, stmt["subject_id"], stmt["issue"])
        if commit:
            set_status(db, timeline, commit, "withdrawn", "took it back", stmt["game_time"], stmt["stmt_id"])
            return f"withdrew {commit['commit_id']}"
    return None


def lapse_due(db, now: float) -> int:
    """Open promises whose due date has passed lapse, at their due time (so an older save does not see it)."""
    timeline = K.active_timeline(db)
    n = 0
    for row in db.execute("SELECT * FROM v_dip_commitment WHERE status = 'open' AND due_gt IS NOT NULL AND due_gt <= ?"
                          " ORDER BY due_gt, commit_id", (now,)).fetchall():
        set_status(db, timeline, dict(row), "lapsed", "the agreed time passed", row["due_gt"])
        n += 1
    return n
