"""Claims: things that happened (or were said to have happened) that people can pass on.

``appraise`` turns a perceived ``world_event`` into one claim plus a copy for each person who perceived
it (``knowledge`` rows with ``origin='witnessed'``). Their opinion change was already made by the
perception pipeline, so nothing else changes here. ``hear`` gives a listener their own copy of a claim
someone told them and moves the listener's view of the claim's subject, by how much they believe it.

Children never hold, tell or appear in claims (``store/minors.py``).
"""
from __future__ import annotations

import json

from livingworld.perception.records import SENSITIVE_PARTS
from livingworld.social import acts
from livingworld.store.minors import is_child

CLOSE_STATUSES = {"friend", "close_friend", "courting", "partner", "spouse"}
WITNESS_CAP = 1.0
HEARSAY_CAP = 0.9        # nobody is as sure of hearsay as of their own eyes


# ---- reading relationships ----

def bond(db, holder: int, other: int | None) -> dict:
    """holder's view of other from v_relationship; neutral when there is no row."""
    if other is None or other == holder:
        return {"status": "stranger", "kinship": None, "trust": 0.0, "affinity": 0.0}
    row = db.execute("SELECT status, kinship, trust, affinity FROM v_relationship WHERE from_id = ? AND to_id = ?",
                     (holder, other)).fetchone()
    if not row:
        return {"status": "stranger", "kinship": None, "trust": 0.0, "affinity": 0.0}
    return {"status": row[0], "kinship": row[1], "trust": row[2], "affinity": row[3]}


def is_close(b: dict) -> bool:
    return b["kinship"] is not None or b["status"] in CLOSE_STATUSES or b["trust"] >= 0.4


def credibility(db, listener: int, teller: int) -> float:
    """How much listener believes what teller says, 0.1..0.95."""
    b = bond(db, listener, teller)
    value = 0.5 + 0.4 * b["trust"]
    if b["kinship"] is not None or b["status"] in ("spouse", "partner"):
        value += 0.15
    if b["status"] in ("rival", "enemy"):
        value -= 0.25
    return max(0.1, min(0.95, value))


# ---- events -> claims ----

def _grab_claim(payload: dict) -> tuple[str, str, float, str]:
    interaction = payload.get("interaction") or "grab"
    act_name = {"touch": "touched", "hold": "held"}.get(interaction, "grabbed")
    part = payload.get("body_part")
    detail = (f" on the {part}" if act_name == "touched" else f" by the {part}") if part else ""
    magnitude = {"touched": 0.2, "grabbed": 0.4, "held": 0.7}[act_name]
    sensitive = bool(part) and part.split(" ")[-1] in SENSITIVE_PARTS
    if sensitive:
        magnitude += 0.2
    return act_name, detail, min(1.0, magnitude), "private" if sensitive else "public"


def appraise(db, event) -> int | None:
    """Make a claim from one event and give each perceiver a copy. Returns the claim id, or None when the
    event is not something people talk about."""
    etype = event["type"]
    payload = json.loads(event["payload_json"] or "{}")
    actors = json.loads(event["actors_json"] or "[]")
    if not actors:
        return None
    subject = actors[0]
    obj = actors[1] if len(actors) > 1 else None
    if is_child(db, subject) or is_child(db, obj):
        return None
    valence = 0.0
    if etype == "physical_interaction":
        act_name, detail, magnitude, sensitivity = _grab_claim(payload)
        valence = -0.3 if act_name == "touched" else -0.6
    elif etype == "consumption" and payload.get("category") == "alcohol":
        act_name, detail, magnitude, sensitivity = "drank", f" {payload.get('item') or 'drink'}", 0.15, "public"
        obj = None
    elif etype == "trade" and payload.get("party"):
        # the buyer stood a round: the claim is about the payer's generosity
        act_name, sensitivity, magnitude = "bought_round", "public", 0.2
        party = _party_id(db, event["event_id"], subject)
        if payload.get("payer") == "player" and party is not None:
            subject, obj = party, actors[0]
        else:
            obj = party
        detail, valence = "", 0.3
        if is_child(db, obj):
            return None
    else:
        return None

    claim_id = db.execute(
        "INSERT INTO claim (timeline_id, subject_id, act, object_id, detail, valence, magnitude, location, game_time,"
        " truth_event_id, sensitivity) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (event["timeline_id"], subject, act_name, obj, detail, valence, magnitude, event["location"],
         event["game_time"], event["event_id"], sensitivity)).lastrowid
    perceivers = db.execute(
        "SELECT w.npc_id, m.confidence, n.is_player FROM event_witness w JOIN npc n ON n.npc_id = w.npc_id"
        " LEFT JOIN memory m ON m.event_id = w.event_id AND m.owner_id = w.npc_id"
        " WHERE w.event_id = ? AND n.is_child = 0", (event["event_id"],)).fetchall()
    for npc_id, confidence, is_player in perceivers:
        if is_player or npc_id == subject or is_child(db, npc_id):
            continue
        share = "yes"
        if npc_id == obj and sensitivity == "private":
            share = "trusted_only"   # embarrassing for them: only told to people they are close to
        _hold(db, event["timeline_id"], npc_id, claim_id, subject, "witnessed", None, None, event["event_id"], None,
              magnitude, min(WITNESS_CAP, confidence if confidence is not None else 0.9), share, event["game_time"])
    return claim_id


def _party_id(db, event_id: int, buyer: int) -> int | None:
    """The person the round was for: the other participant in the trade who is not the seller."""
    payload = json.loads(db.execute("SELECT payload_json FROM world_event WHERE event_id = ?", (event_id,)).fetchone()[0])
    name = payload.get("party")
    if not name:
        return None
    hit = db.execute("SELECT npc_id FROM npc WHERE display_name = ? AND npc_id <> ? ORDER BY is_player DESC LIMIT 1",
                     (name, buyer)).fetchone()
    return hit[0] if hit else None


def _hold(db, timeline: int, holder: int, claim_id: int, subject: int, origin: str, told_by: int | None,
          parent: int | None, event_id: int | None, utt_id: int | None, magnitude: float, believed: float,
          share: str, gt: float) -> int:
    return db.execute(
        "INSERT INTO knowledge (timeline_id, holder_id, subject_id, predicate, value, origin, told_by_id, parent_know_id,"
        " source_utt_id, source_event_id, confidence, privacy, learned_gt, claim_id, heard_magnitude, believed,"
        " will_share) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (timeline, holder, subject, f"claim:{claim_id}", "", origin, told_by, parent, utt_id, event_id,
         round(believed, 3), "shareable" if share == "yes" else "private", gt, claim_id, round(magnitude, 3),
         round(believed, 3), share)).lastrowid


# ---- hearing a claim from someone ----

def hear(db, timeline: int, listener: int, teller: int, claim: dict, teller_know_id: int | None,
         teller_believed: float, magnitude: float, gt: float, event_id: int | None = None,
         utt_id: int | None = None) -> int | None:
    """listener is told claim by teller. Writes listener's copy and their change of view. Returns know_id."""
    if is_child(db, listener) or is_child(db, teller):
        return None
    believed = min(HEARSAY_CAP, teller_believed * credibility(db, listener, teller))
    prior = bond(db, listener, claim["subject_id"])
    if (claim["valence"] < 0 and prior["affinity"] >= 0.5) or (claim["valence"] > 0 and prior["affinity"] <= -0.5):
        believed *= 0.7   # people discount news that goes against what they already think of someone
    know_id = _hold(db, timeline, listener, claim["claim_id"], claim["subject_id"], "told", teller, teller_know_id,
                    event_id, utt_id, magnitude, believed, "yes", gt)
    if claim["subject_id"] != listener:
        close = claim["object_id"] is not None and is_close(bond(db, listener, claim["object_id"]))
        deltas = acts.listener_deltas(claim["act"], claim["valence"], magnitude, believed, close)
        if deltas:
            db.execute("INSERT OR IGNORE INTO relationship (from_id, to_id, status, updated_gt) VALUES (?,?,?,?)",
                       (listener, claim["subject_id"], "stranger", gt))
            teller_name = db.execute("SELECT display_name FROM npc WHERE npc_id = ?", (teller,)).fetchone()[0]
            db.execute("INSERT INTO relationship_change (timeline_id, from_id, to_id, deltas_json, cause_event_id,"
                       " reason, game_time) VALUES (?,?,?,?,?,?,?)",
                       (timeline, listener, claim["subject_id"], json.dumps(deltas), event_id,
                        f"heard from {teller_name}: claim {claim['claim_id']}", gt))
    return know_id


# ---- words ----

def sentence(db, claim: dict, viewer: int) -> str:
    """'Torvar grabbed Sven by the left forearm in Riverwood', with the viewer as 'me'."""
    def name(npc_id):
        if npc_id == viewer:
            return "me"
        return db.execute("SELECT display_name FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()[0]
    subject = "I" if claim["subject_id"] == viewer else name(claim["subject_id"])
    place = f" in {claim['location']}" if claim.get("location") else ""
    if claim["act"] == "reported":
        return f"{subject} {claim['detail'].strip()}"
    if claim["act"] == "drank":
        return f"{subject} was drinking{claim['detail']}{place}"
    obj = f" {name(claim['object_id'])}" if claim["object_id"] is not None else ""
    return f"{subject} {acts.act(claim['act']).verb}{obj}{claim['detail']}{place}"
