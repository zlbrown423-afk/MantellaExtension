"""Reputation layers (S2): what each person thinks of someone in each area of life, the opinions they
form from it, and what a community or a faction thinks as a whole.

The five layers, each kept in one place:

* personal experience: ``memory`` (perception) - unchanged here;
* personal trust, fear, respect...: ``v_relationship`` - unchanged here;
* standing by domain: ``standing_change`` / ``v_standing``, one holder's view of one subject in one
  domain (generosity, honesty, reliability, gentleness, decency, lawfulness, standing; 'trade' is kept
  free for the civic systems). Written when someone sees a claim's act first-hand (``claims.appraise``)
  or hears it (``claims.hear``), weighted by how much the holder cares about that domain (traits);
* individual opinions: ``belief`` / ``v_belief``, formed when a standing passes a threshold and dropped
  when it falls back (with a gap between the two, so opinions do not flicker);
* community and faction reputation: ``v_community_reputation`` / ``v_faction_reputation``, computed
  from members' own standings when asked, never stored, so they can never drift from what people think.

Numbers only, no LLM. Children never hold or receive a reputation. ``MANTELLA_LW_REPUTATION=0`` turns
all of it off (S1 gossip keeps working).
"""
from __future__ import annotations

import json
import os

from livingworld.social import acts
from livingworld.social.traits import traits_of
from livingworld.store.minors import is_child

REPUTATION_ENV = "MANTELLA_LW_REPUTATION"

DOMAINS = ("generosity", "honesty", "reliability", "gentleness", "decency", "lawfulness", "standing", "trade")

# acts defined elsewhere (guard G2) that still move a standing; Act.domains wins when it is set
EXTRA_DOMAINS: dict[str, dict[str, float]] = {
    "commended": {"standing": 0.15, "reliability": 0.1},
    "reprimanded": {"standing": -0.1, "reliability": -0.1},
    "reinstated": {"standing": 0.05},
}

# which trait (0..1, 0.5 = average) sets how much a holder cares about each domain
DOMAIN_VALUE = {"generosity": "wealth", "honesty": "honest", "reliability": "honor", "gentleness": "family",
                "decency": "modesty", "lawfulness": "law", "standing": "honor", "trade": "wealth"}

FIRSTHAND = 1.0
SECONDHAND = acts.SECONDHAND
VICTIM = 1.6            # it was done to them
BENEFICIARY = 1.5       # it was done for them (a gift, a round, help)
CLOSE_TO_OBJECT = acts.CLOSE_TO_OBJECT
PRIVATE_DECENCY = -0.15  # extra, for touching somewhere private
MIN_DELTA = 0.002

FORM_AT = 0.2           # a standing this far from 0 becomes an opinion...
STRONG_AT = 0.5         # ...a strong one from here...
DROP_AT = 0.1           # ...and is dropped once it falls back under this

STATEMENTS = {
    "generosity": (("{s} is generous", "{s} is very generous with their coin"),
                   ("{s} is tight-fisted", "{s} is a miser")),
    "honesty": (("{s} is honest", "{s} is as honest as they come"),
                ("{s} is not to be trusted around other people's things", "{s} is a thief")),
    "reliability": (("{s} keeps their word", "{s} is someone you can count on"),
                    ("{s} does not keep their word", "{s} lets people down")),
    "gentleness": (("{s} is gentle", "{s} is kind and gentle with people"),
                   ("{s} can be rough with people", "{s} is violent and dangerous")),
    "decency": (("{s} behaves decently", "{s} is very proper"),
                ("{s} takes liberties with people", "{s} has no decency, the way they put hands on people")),
    "lawfulness": (("{s} is law-abiding", "{s} upholds the law"),
                   ("{s} has little respect for the law", "{s} is a criminal")),
    "standing": (("{s} has made something of themselves", "{s} is a person of real standing"),
                 ("{s} has come down in the world", "{s} is in disgrace")),
    "trade": (("{s} is good at their trade", "{s} is a master of their trade"),
              ("{s} is poor at their trade", "{s} is hopeless at their trade")),
}


def enabled() -> bool:
    return os.environ.get(REPUTATION_ENV, "").strip().lower() not in {"0", "false", "no", "off"}


def domains_for(act_name: str) -> dict[str, float]:
    a = acts.ACTS.get(act_name)
    if a is not None and a.domains:
        return dict(a.domains)
    return dict(EXTRA_DOMAINS.get(act_name, {}))


def _cares(traits: dict[str, float], domain: str) -> float:
    """0.5..1.5: how much this person weighs a domain."""
    return 0.5 + float(traits.get(DOMAIN_VALUE.get(domain, ""), 0.5))


def deltas(db, holder: int, claim: dict, magnitude: float, believed: float, firsthand: bool,
           close_to_object: bool) -> dict[str, float]:
    """How ``holder``'s standing of the claim's subject changes, per domain."""
    base = domains_for(claim["act"])
    if not base:
        return {}
    if claim.get("sensitivity") == "private" and claim["act"] in ("touched", "grabbed", "held"):
        base["decency"] = base.get("decency", 0.0) + PRIVATE_DECENCY
    traits = traits_of(db, holder)
    scale = (0.5 + magnitude) * believed * (FIRSTHAND if firsthand else SECONDHAND)
    harms = acts.act(claim["act"]).harms_object or claim.get("valence", 0) < 0
    if claim.get("object_id") == holder and harms:
        scale *= VICTIM
    elif claim.get("object_id") == holder and claim.get("valence", 0) > 0:
        scale *= BENEFICIARY
    elif close_to_object and harms:
        scale *= CLOSE_TO_OBJECT * (0.75 + 0.5 * float(traits.get("loyal", 0.5)))
    out = {}
    for domain, value in base.items():
        d = value * scale * _cares(traits, domain)
        if abs(d) >= MIN_DELTA:
            out[domain] = round(max(-1.0, min(1.0, d)), 4)
    return out


def record(db, timeline: int, holder: int, claim: dict, magnitude: float, believed: float, firsthand: bool,
           close_to_object: bool, gt: float, event_id: int | None = None, reason: str = "") -> dict[str, float]:
    """Write holder's change of standing towards the claim's subject, then review their opinions."""
    subject = claim["subject_id"]
    if not enabled() or holder == subject or is_child(db, holder) or is_child(db, subject):
        return {}
    if db.execute("SELECT is_player FROM npc WHERE npc_id = ?", (holder,)).fetchone()[0]:
        return {}
    changes = deltas(db, holder, claim, magnitude, believed, firsthand, close_to_object)
    for domain, d in changes.items():
        db.execute("INSERT INTO standing_change (timeline_id, holder_id, subject_id, domain, delta, firsthand, claim_id,"
                   " cause_event_id, reason, game_time) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (timeline, holder, subject, domain, d, int(firsthand), claim.get("claim_id"), event_id,
                    reason or ("saw it" if firsthand else "heard it"), gt))
    if changes:
        review(db, timeline, holder, subject, gt)
    return changes


def adjust(db, timeline: int, holder: int, subject: int, changes: dict[str, float], gt: float, claim_id: int | None,
           event_id: int | None, reason: str) -> None:
    """Write already-worked-out standing changes (a correction) and review opinions."""
    if not enabled() or not changes:
        return
    for domain, d in changes.items():
        if abs(d) >= MIN_DELTA:
            db.execute("INSERT INTO standing_change (timeline_id, holder_id, subject_id, domain, delta, firsthand,"
                       " claim_id, cause_event_id, reason, game_time) VALUES (?,?,?,?,?,0,?,?,?,?)",
                       (timeline, holder, subject, domain, round(max(-1.0, min(1.0, d)), 4), claim_id, event_id,
                        reason, gt))
    review(db, timeline, holder, subject, gt)


# ---- beliefs ----

def _statement(domain: str, value: float, name: str) -> str:
    good, bad = STATEMENTS.get(domain, (("{s} is well thought of", "{s} is very well thought of"),
                                        ("{s} is poorly thought of", "{s} is badly thought of")))
    forms = good if value > 0 else bad
    return forms[1 if abs(value) >= STRONG_AT else 0].format(s=name)


def review(db, timeline: int, holder: int, subject: int, gt: float) -> list[str]:
    """Form, change or drop holder's opinions of subject from their current standings. Returns new statements."""
    name = db.execute("SELECT display_name FROM npc WHERE npc_id = ?", (subject,)).fetchone()[0]
    formed = []
    for domain, value, evidence, firsthand in db.execute(
            "SELECT domain, value, evidence, firsthand FROM v_standing WHERE holder_id = ? AND subject_id = ?",
            (holder, subject)).fetchall():
        current = db.execute("SELECT statement, stance FROM v_belief WHERE holder_id = ? AND about_id = ? AND topic = ?",
                             (holder, subject, domain)).fetchone()
        if current is None and abs(value) < FORM_AT:
            continue
        if current is not None and abs(value) < DROP_AT:
            statement = ""      # no longer held
        elif current is None or abs(value) >= FORM_AT or (current[1] > 0) != (value > 0):
            statement = _statement(domain, value, name)
        else:
            continue            # between the two thresholds: keep what they think
        if current is not None and current[0] == statement:
            continue
        if current is None and not statement:
            continue
        evidence_rows = db.execute(
            "SELECT DISTINCT claim_id FROM standing_change WHERE holder_id = ? AND subject_id = ? AND domain = ?"
            " AND claim_id IS NOT NULL ORDER BY change_id DESC LIMIT 5", (holder, subject, domain)).fetchall()
        confidence = min(1.0, 0.35 + 0.1 * evidence + (0.25 if firsthand else 0.0))
        db.execute("INSERT INTO belief (timeline_id, holder_id, about_id, topic, statement, stance, confidence,"
                   " evidence_json, formed_gt) VALUES (?,?,?,?,?,?,?,?,?)",
                   (timeline, holder, subject, domain, statement, round(max(-1.0, min(1.0, value)), 3),
                    round(confidence, 2), json.dumps([{"claim": r[0]} for r in evidence_rows]), gt))
        if statement:
            formed.append(statement)
    return formed


def belief_lines(db, holder: int, about_ids: list[int], limit: int = 3) -> list[str]:
    """'What I've come to think: Torvar is generous (I've seen it myself).' for prompts; strongest first."""
    if not enabled() or not about_ids:
        return []
    marks = ",".join("?" * len(about_ids))
    rows = db.execute(
        f"SELECT b.about_id, b.topic, b.statement, b.stance FROM v_belief b WHERE b.holder_id = ?"
        f" AND b.about_id IN ({marks}) ORDER BY ABS(b.stance) DESC, b.formed_gt DESC LIMIT ?",
        (holder, *about_ids, limit)).fetchall()
    lines = []
    for about, topic, statement, stance in rows:
        seen = db.execute("SELECT firsthand FROM v_standing WHERE holder_id = ? AND subject_id = ? AND domain = ?",
                          (holder, about, topic)).fetchone()
        source = "I've seen it myself" if seen and seen[0] else "from what I've heard"
        lines.append(f"What I've come to think: {statement} ({source}).")
    return lines


# ---- community and faction views ----

def community_view(db, community_key: str, subject: int) -> dict[str, tuple[float, float]]:
    """domain -> (value, reach) for what a community thinks of subject."""
    if not enabled():
        return {}
    return {r[0]: (r[1], r[2]) for r in db.execute(
        "SELECT r.domain, r.value, r.reach FROM v_community_reputation r JOIN community c"
        " ON c.community_id = r.community_id WHERE c.key = ? AND r.subject_id = ?", (community_key, subject))}


def faction_score(db, community_key: str, subject: int) -> tuple[float, float] | None:
    row = db.execute("SELECT score, reach FROM v_faction_reputation WHERE community_key = ? AND subject_id = ?",
                     (community_key, subject)).fetchone()
    return (row[0], row[1]) if row else None


def summary(db, community_key: str, subject: int, min_reach: float = 0.15) -> str | None:
    """'well liked, known to be generous' - a community's view in words, or None when too few know them."""
    view = community_view(db, community_key, subject)
    if not view:
        return None
    parts = []
    regard = view.get("regard")
    if regard and regard[1] >= min_reach:
        if regard[0] >= 0.25:
            parts.append("well liked")
        elif regard[0] >= 0.08:
            parts.append("thought well of")
        elif regard[0] <= -0.25:
            parts.append("disliked")
        elif regard[0] <= -0.08:
            parts.append("viewed with some suspicion")
    known = []
    for domain in DOMAINS:
        value, reach = view.get(domain, (0.0, 0.0))
        if reach >= min_reach and abs(value) >= 0.12:
            known.append((abs(value) * reach, _known_for(domain, value)))
    known.sort(reverse=True)
    parts += [k for _, k in known[:2]]
    return ", ".join(parts) or None


def _known_for(domain: str, value: float) -> str:
    good = {"generosity": "known to be generous", "honesty": "thought honest", "reliability": "known to keep their word",
            "gentleness": "thought gentle", "decency": "thought decent", "lawfulness": "thought law-abiding",
            "standing": "a person of standing", "trade": "good at their trade"}
    bad = {"generosity": "thought tight-fisted", "honesty": "said to be a thief", "reliability": "said to let people down",
           "gentleness": "said to be violent", "decency": "said to take liberties with people",
           "lawfulness": "said to be a criminal", "standing": "in disgrace", "trade": "poor at their trade"}
    return (good if value > 0 else bad)[domain]


# ---- factions from guard orgs ----

FACTION_STANCES = {
    # a hold guard cares most about the law and violence, little about generosity
    "guard": {"lawfulness": 2.5, "gentleness": 1.5, "honesty": 1.5, "reliability": 1.2, "standing": 1.0,
              "decency": 1.0, "generosity": 0.5, "regard": 1.0},
}


def sync_guard_factions(db) -> int:
    """Each configured guard org becomes a 'faction' community whose members are the guards met in play
    (``guard_profile``) and its named leaders. Returns the number of memberships written."""
    try:
        from livingworld.guard import orgs
    except Exception:   # guard package missing or broken: no faction views, nothing else affected
        return 0
    has_profiles = db.execute("SELECT 1 FROM sqlite_master WHERE name = 'guard_profile'").fetchone()
    if not has_profiles:
        return 0
    written = 0
    for org in orgs.orgs():
        db.execute("INSERT INTO community (key, name, kind, location) VALUES (?,?, 'faction', ?)"
                   " ON CONFLICT(key) DO UPDATE SET name = excluded.name, kind = 'faction'",
                   (org.key, org.name, org.hold))
        cid = db.execute("SELECT community_id FROM community WHERE key = ?", (org.key,)).fetchone()[0]
        for domain, weight in FACTION_STANCES["guard"].items():
            db.execute("INSERT INTO community_stance (community_id, domain, weight) VALUES (?,?,?)"
                       " ON CONFLICT(community_id, domain) DO NOTHING", (cid, domain, weight))
        leaders = {}
        for person in org.leaders:
            row = db.execute("SELECT npc_id FROM npc WHERE plugin = ? AND base_form_id = ? ORDER BY npc_id LIMIT 1",
                             (person.plugin, orgs.local_id(person.base_id))).fetchone()
            if row:
                leaders[row[0]] = person.role or "leader"
        members = {r[0]: ("guard", 0.3) for r in db.execute(
            "SELECT p.npc_id FROM guard_profile p JOIN npc n ON n.npc_id = p.npc_id"
            " WHERE p.org_key = ? AND n.is_child = 0 AND n.is_player = 0", (org.key,))}
        for npc_id, role in leaders.items():
            members[npc_id] = (role, 0.8)
        for npc_id, (role, influence) in members.items():
            db.execute("INSERT INTO membership (npc_id, community_id, role, influence) VALUES (?,?,?,?)"
                       " ON CONFLICT(npc_id, community_id) DO UPDATE SET role = excluded.role,"
                       " influence = excluded.influence", (npc_id, cid, role, influence))
            written += 1
    return written
