"""The diplomatic journal: what the player has legitimately learned about the talks, and an everything view for
checking the rules.

Player view (the default) is built only from statements the player holds (``dip_awareness`` of the player): who
supports or opposes negotiation, what conditions were set, what was promised, how invitations stand, what
happened recently. "Confirmed" means the player heard the person say it; everything else is marked as a report
or a rumour, with who passed it on. Private convictions never appear here.

``--gm`` shows everything, including every person's seeded and stated convictions, clearly labelled.

Command line, never imported by the server (opened read-only, like ``social/who_knows.py``)::

    python -m livingworld.diplomacy.journal --db World.sqlite
    python -m livingworld.diplomacy.journal --db World.sqlite --gm
    python -m livingworld.diplomacy.journal --db World.sqlite --json
"""
from __future__ import annotations

import argparse
import json
import sys

from livingworld.diplomacy import common as K
from livingworld.diplomacy import meetings as M

STANCE_KINDS = ("position", "support", "opposition", "refusal", "agreement", "proposal")
TALK_ISSUES = ("negotiation", "ceasefire", "alliance", "meeting")


def _player(db) -> int | None:
    row = db.execute("SELECT npc_id FROM npc WHERE is_player = 1").fetchone()
    return row[0] if row else None


def _label(row) -> str:
    if row["origin"] in ("said", "witnessed") and row["about_name"] is None and row["subject_id"] == row["speaker_id"]:
        return "confirmed" if row["modality"] in ("asserted", "conditional") else row["modality"]
    if row["origin"] == "courier":
        return "by courier"
    return "rumour" if row["modality"] == "rumor" or row["origin"] == "rumor" else "report"


def _stance_word(row) -> str:
    s = row["stance"]
    if row["kind"] == "refusal" or (s is not None and s <= -0.2):
        return "against"
    if row["kind"] == "agreement" or (s is not None and s >= 0.2):
        return "for"
    return "undecided"


def player_view(db, player_id: int | None = None, now: float | None = None) -> dict:
    player_id = player_id if player_id is not None else _player(db)
    if player_id is None:
        return {"error": "no player in this world yet"}
    known = db.execute(
        "SELECT d.*, a.origin, a.told_by_id, a.confidence AS held, a.learned_gt FROM v_dip_awareness a"
        " JOIN v_dip_statement d ON d.stmt_id = a.stmt_id WHERE a.holder_id = ?"
        " ORDER BY d.game_time, d.stmt_id", (player_id,)).fetchall()
    def who(i):
        return "you" if i == player_id else K.name_of(db, i)
    def subj(r):
        return r["about_name"] or who(r["subject_id"])
    stances: dict[tuple, dict] = {}
    for r in known:
        if r["kind"] in STANCE_KINDS or (r["kind"] == "report" and r["stance"] is not None):
            if r["issue"] in TALK_ISSUES and r["subject_id"] != player_id:
                stances[(subj(r), r["issue"])] = {"who": subj(r), "issue": r["issue"], "stance": _stance_word(r),
                                                  "said": r["summary"], "status": _label(r),
                                                  "via": None if r["origin"] in ("said", "witnessed")
                                                  else who(r["told_by_id"] or r["speaker_id"])}
    supporters = [s for s in stances.values() if s["stance"] == "for"]
    opponents = [s for s in stances.values() if s["stance"] == "against"]
    conditions = [{"who": subj(r), "issue": r["issue"], "condition": r["condition"] or r["summary"], "status": _label(r)}
                  for r in known if r["kind"] == "condition" or (r["condition"] and r["kind"] != "report")]
    stmt_ids = {r["stmt_id"] for r in known}
    promises = []
    for c in db.execute("SELECT * FROM v_dip_commitment ORDER BY made_gt").fetchall():
        if c["stmt_id"] in stmt_ids or player_id in (c["debtor_id"], c["creditor_id"]):
            promises.append({"by": who(c["debtor_id"]), "to": who(c["creditor_id"]) if c["creditor_id"] else "everyone",
                             "promise": c["description"], "condition": c["condition"], "status": c["status"]})
    meetings = []
    for m in db.execute("SELECT * FROM v_dip_meeting ORDER BY proposed_gt").fetchall():
        invs = db.execute("SELECT * FROM v_dip_invitation WHERE meeting_id = ?", (m["meeting_id"],)).fetchall()
        if m["stmt_id"] not in stmt_ids and m["proposed_by_id"] != player_id and \
                not any(i["stmt_id"] in stmt_ids or i["invited_by_id"] == player_id for i in invs):
            continue
        answers = []
        for i in invs:
            # the newest answer the player has actually heard of
            known_change = db.execute(
                "SELECT x.status, x.conditions FROM dip_invitation_change x JOIN v_timeline_scope s"
                " ON s.timeline_id = x.timeline_id AND x.game_time <= s.cutoff_gt WHERE x.inv_id = ? AND"
                " (x.stmt_id IS NULL OR x.stmt_id IN (SELECT stmt_id FROM v_dip_awareness WHERE holder_id = ?))"
                " ORDER BY x.game_time DESC, x.change_id DESC LIMIT 1", (i["inv_id"], player_id)).fetchone()
            answers.append({"invitee": who(i["invitee_id"]), "invited_by": who(i["invited_by_id"]),
                            "answer": known_change["status"] if known_change else "unknown",
                            "conditions": known_change["conditions"] if known_change else None})
        att = [{"who": who(a["npc_id"]), "status": a["status"]} for a in
               db.execute("SELECT * FROM v_dip_attendance WHERE meeting_id = ?", (m["meeting_id"],))]
        meetings.append({"venue": m["venue"], "status": m["status"], "proposed_by": who(m["proposed_by_id"]),
                         "invitations": answers, "attendance": att,
                         "unresolved": [u["summary"] for u in M.unresolved(db, m["meeting_id"])]})
    recent = [{"who": subj(r), "said": r["summary"], "kind": r["kind"], "issue": r["issue"], "status": _label(r),
               "where": "private" if r["audience"] == "private" else "in front of others",
               "via": None if r["origin"] in ("said", "witnessed") else who(r["told_by_id"] or r["speaker_id"]),
               "quote": r["quote"], "game_time": r["game_time"]} for r in known[-12:]][::-1]
    return {"supporters": supporters, "opponents": opponents, "conditions": conditions, "promises": promises,
            "meetings": meetings, "recent": recent,
            "confirmed": sum(1 for r in known if _label(r) == "confirmed"),
            "rumours": sum(1 for r in known if _label(r) in ("rumour", "report", "by courier"))}


def gm_view(db) -> dict:
    positions = [dict(r) | {"name": K.name_of(db, r["npc_id"])} for r in
                 db.execute("SELECT npc_id, dimension, layer, value, confidence, source FROM v_political_position"
                            " ORDER BY npc_id, layer, dimension")]
    statements = [dict(r) | {"speaker": K.name_of(db, r["speaker_id"]), "subject": r["about_name"] or
                             K.name_of(db, r["subject_id"]),
                             "holders": db.execute("SELECT COUNT(*) FROM v_dip_awareness WHERE stmt_id = ?",
                                                   (r["stmt_id"],)).fetchone()[0]}
                  for r in db.execute("SELECT * FROM v_dip_statement ORDER BY game_time, stmt_id")]
    commitments = [dict(r) for r in db.execute("SELECT * FROM v_dip_commitment ORDER BY made_gt")]
    meetings = [dict(r) for r in db.execute("SELECT * FROM v_dip_meeting ORDER BY proposed_gt")]
    invitations = [dict(r) for r in db.execute("SELECT * FROM v_dip_invitation ORDER BY invited_gt")]
    return {"positions": positions, "statements": statements, "commitments": commitments, "meetings": meetings,
            "invitations": invitations}


def render(view: dict) -> str:
    if "error" in view:
        return view["error"]
    out = [f"Diplomatic journal ({view['confirmed']} confirmed, {view['rumours']} reports or rumours)", ""]
    def section(title, rows, fmt):
        out.append(title)
        out.extend(f"  - {fmt(r)}" for r in rows) if rows else out.append("  (nothing yet)")
        out.append("")
    via = lambda r: f", via {r['via']}" if r.get("via") else ""   # noqa: E731
    section("Who supports talks", view["supporters"], lambda r: f"{r['who']} on {r['issue']}: {r['said']} [{r['status']}{via(r)}]")
    section("Who opposes them", view["opponents"], lambda r: f"{r['who']} on {r['issue']}: {r['said']} [{r['status']}{via(r)}]")
    section("Conditions", view["conditions"], lambda r: f"{r['who']} ({r['issue']}): {r['condition']} [{r['status']}]")
    section("Promises", view["promises"],
            lambda r: f"{r['by']} to {r['to']}: {r['promise']}" + (f" if {r['condition']}" if r["condition"] else "")
            + f" [{r['status']}]")
    section("Meetings", view["meetings"],
            lambda r: f"{r['venue']} ({r['status']}, proposed by {r['proposed_by']}): "
            + ("; ".join(f"{i['invitee']} {i['answer']}" + (f" ({i['conditions']})" if i["conditions"] else "")
                         for i in r["invitations"]) or "no invitations")
            + (f"; unresolved: {', '.join(r['unresolved'])}" if r["unresolved"] else ""))
    section("Recent", view["recent"], lambda r: f"{r['who']} {r['said']} ({r['where']}) [{r['status']}{via(r)}]")
    return "\n".join(out).rstrip()


def main(argv: list[str] | None = None) -> int:
    from livingworld.social.who_knows import Refused, snapshot
    ap = argparse.ArgumentParser(prog="python -m livingworld.diplomacy.journal", description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", required=True, help="the world database; opened read-only")
    ap.add_argument("--timeline", type=int, help="look at this save timeline instead of the active one")
    ap.add_argument("--gm", action="store_true", help="everything, including private convictions")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        db = snapshot(args.db, args.timeline)
    except Refused as e:
        print(f"journal: {e}", file=sys.stderr)
        return 2
    view = gm_view(db) if args.gm else player_view(db)
    print(json.dumps(view, indent=2, default=str) if (args.json or args.gm) else render(view))
    return 0


if __name__ == "__main__":
    sys.exit(main())
