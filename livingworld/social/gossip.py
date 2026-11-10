"""Gossip: news moving between people while the game is not watching.

When the server is next contacted (a conversation starts), each community is caught up from its last
tick to the current game time in steps. In each step, every member who holds a claim may pass it to
another member, with a chance set by how often they talk (household, kin, friends, village),
how gossipy the teller is, how interesting the claim is and how recent it is. Each telling is a
``gossip_exchange`` world event; the listener gets their own copy of the claim and changes their view
(``claims.hear``). The same game time always rolls the same dice, so replays are reproducible.

Numbers only, no LLM. Children are never members, so they never tell or hear anything.
"""
from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import dataclass

from livingworld.fog.service import gossip_carries_name
from livingworld.social import acts, claims, reputation
from livingworld.social.traits import traits_of

STEP_DAYS = 0.25          # one step per quarter day (morning, afternoon, evening, night)
MAX_STEPS = 60            # a long absence is covered in at most this many (longer) steps
MAX_TELLINGS_PER_STEP = 2 # per teller
FADE_DAYS = 7.0           # older news is less interesting...
FORGET_DAYS = 30.0        # ...and stops spreading
MIN_BELIEVED = 0.3        # people do not pass on what they hardly believe

# reputation (S2): retelling and setting things straight
EXAGGERATE_BASE = 0.15    # chance per telling that an average (honest 0.5) teller makes it bigger
EXAGGERATE_PER_DRINK = 0.07  # extra chance per drink still in them (up to 3)
PLAY_DOWN = 0.15          # chance an honest teller (0.65+) plays it down a little
CORRECT_OVER = 0.15       # a copy this much bigger than what a witness saw gets corrected...
CORRECT_RATE = 0.5        # ...at this share of the usual chance they talk

# chance per day that a teller mentions a claim of interest 1.0 to a listener, by how they know each other
CONTACT_HOUSEHOLD = 0.9
CONTACT_CLOSE = 0.6
CONTACT_RIVAL = 0.05
CONTACT_VILLAGE = 0.2


@dataclass
class CatchUp:
    community: str
    steps: int = 0
    tellings: int = 0
    from_gt: float | None = None
    to_gt: float | None = None
    ms: float = 0.0


def _rng(timeline: int, community: str, step_gt: float) -> random.Random:
    digest = hashlib.sha256(f"{timeline}:{community}:{step_gt:.5f}".encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "little"))


def _last_tick(db, community: str) -> float | None:
    return db.execute("SELECT MAX(game_time) FROM v_visible_event WHERE type = 'sim_tick'"
                      " AND json_extract(payload_json, '$.community') = ?", (community,)).fetchone()[0]


def _members(db, community_id: int) -> list[int]:
    return [r[0] for r in db.execute(
        "SELECT m.npc_id FROM membership m JOIN npc n ON n.npc_id = m.npc_id"
        " WHERE m.community_id = ? AND n.is_child = 0 AND n.is_player = 0 ORDER BY m.npc_id", (community_id,))]


def _households(db, members: list[int]) -> dict[int, set[int]]:
    out: dict[int, set[int]] = {m: set() for m in members}
    for npc_id, community_id in db.execute(
            "SELECT m.npc_id, m.community_id FROM membership m JOIN community c ON c.community_id = m.community_id"
            " WHERE c.kind = 'household'"):
        if npc_id in out:
            out[npc_id].add(community_id)
    return out


def _gossipy(db, npc_id: int) -> float:
    row = db.execute("SELECT traits_json FROM persona_version WHERE npc_id = ? ORDER BY version DESC LIMIT 1",
                     (npc_id,)).fetchone()
    try:
        return float(json.loads(row[0]).get("gossipy", 0.5)) if row else 0.5
    except (ValueError, TypeError, AttributeError):
        return 0.5


def _contact(db, teller: int, listener: int, households: dict[int, set[int]]) -> tuple[float, bool]:
    """(chance per day they talk about news, whether they are close)."""
    b = claims.bond(db, teller, listener)
    close = claims.is_close(b)
    if b["status"] in ("rival", "enemy"):
        return CONTACT_RIVAL, False
    if households.get(teller, set()) & households.get(listener, set()):
        return CONTACT_HOUSEHOLD, True
    if close:
        return CONTACT_CLOSE, True
    return CONTACT_VILLAGE, False


def _interest(claim, now_gt: float) -> float:
    age = now_gt - claim["claim_gt"]
    if age > FORGET_DAYS:
        return 0.0
    value = acts.act(claim["act"]).interest * (0.5 + claim["heard_magnitude"])
    if age > FADE_DAYS:
        value *= 0.5
    return min(1.0, value)


def catch_up(db, community_key: str, now_gt: float, budget_ms: float | None = None) -> CatchUp:
    """Advance gossip in one community up to ``now_gt``. Call inside a transaction."""
    started = time.perf_counter()
    result = CatchUp(community_key)
    row = db.execute("SELECT community_id, location FROM community WHERE key = ?", (community_key,)).fetchone()
    if not row:
        return result
    community_id, location = row
    members = _members(db, community_id)
    timeline = db.execute("SELECT timeline_id FROM timeline WHERE is_active = 1").fetchone()[0]
    last = _last_tick(db, community_key)
    if last is None:
        # first time: start from the oldest news any member holds, or from now
        first = db.execute(
            "SELECT MIN(claim_gt) FROM v_claim_knowledge WHERE holder_id IN (SELECT npc_id FROM membership"
            " WHERE community_id = ?)", (community_id,)).fetchone()[0]
        last = min(first, now_gt) if first is not None else now_gt
    if now_gt - last < STEP_DAYS:
        return result   # not enough time has passed for anyone to have talked
    step = max(STEP_DAYS, (now_gt - last) / MAX_STEPS)
    result.from_gt = last
    households = _households(db, members)
    gossipy = {m: _gossipy(db, m) for m in members}
    t = last
    while t + step <= now_gt + 1e-9:
        t += step
        result.steps += 1
        result.tellings += _step(db, timeline, community_key, location, members, households, gossipy, t, step)
        if budget_ms is not None and (time.perf_counter() - started) * 1000 > budget_ms:
            break
    _tick(db, timeline, community_key, location, t)
    result.to_gt = t
    result.ms = (time.perf_counter() - started) * 1000
    return result


def _tick(db, timeline: int, community: str, location: str | None, gt: float) -> None:
    db.execute("INSERT INTO world_event (timeline_id, type, location, payload_json, source, game_time)"
               " VALUES (?, 'sim_tick', ?, ?, 'sim', ?)", (timeline, location, json.dumps({"community": community}), gt))


def _step(db, timeline: int, community: str, location: str | None, members: list[int],
          households: dict[int, set[int]], gossipy: dict[int, float], t: float, step: float) -> int:
    rng = _rng(timeline, community, t)
    member_set = set(members)
    tellings = 0
    for teller in members:
        held = db.execute(
            "SELECT * FROM v_claim_knowledge WHERE holder_id = ? AND learned_gt <= ? AND claim_gt <= ?"
            " AND will_share <> 'no' AND believed >= ? AND sensitivity <> 'secret' ORDER BY claim_id",
            (teller, t, t, MIN_BELIEVED)).fetchall()
        told = 0
        for claim in sorted(held, key=lambda c: -_interest(c, t)):
            if told >= MAX_TELLINGS_PER_STEP:
                break
            interest = _interest(claim, t)
            if interest <= 0:
                continue
            knowers = {r[0] for r in db.execute("SELECT holder_id FROM v_claim_knowledge WHERE claim_id = ?",
                                                (claim["claim_id"],))}
            for listener in members:
                if listener == teller or listener in knowers or listener == claim["subject_id"]:
                    continue
                contact, close = _contact(db, teller, listener, households)
                if claim["will_share"] == "trusted_only" and not close:
                    continue
                if claim["sensitivity"] == "private" and not close:
                    contact *= 0.3
                per_day = contact * interest * (0.5 + gossipy.get(teller, 0.5))
                chance = 1 - (1 - min(0.95, per_day)) ** step
                if rng.random() >= chance:
                    continue
                _tell(db, timeline, location, teller, listener, claim, t, rng)
                knowers.add(listener)
                told += 1
                tellings += 1
                if told >= MAX_TELLINGS_PER_STEP:
                    break
        if reputation.enabled():
            tellings += _corrections(db, timeline, location, teller, members, households, t, step, rng)
    return tellings


def distort(db, teller: int, magnitude: float, rng: random.Random, now_gt: float) -> float:
    """How big the teller makes it sound. Honest people keep to what they know or play it down a little;
    others may make it bigger, more so when they have been drinking. Returns the retold magnitude."""
    if not reputation.enabled():
        return magnitude
    honest = traits_of(db, teller)["honest"]
    drinks = 0.0
    try:
        from livingworld.perception.intoxication import drink_state
        drinks = min(3.0, float(drink_state(db, teller, now_gt).level or 0.0))
    except Exception:
        drinks = 0.0
    exaggerate = EXAGGERATE_BASE * (1.0 - honest) * 2 + EXAGGERATE_PER_DRINK * drinks
    roll = rng.random()
    if roll < exaggerate:
        magnitude += rng.uniform(0.1, 0.25)
    elif honest >= 0.65 and roll > 1.0 - PLAY_DOWN:
        magnitude -= rng.uniform(0.03, 0.08)
    return round(max(0.05, min(1.0, magnitude)), 3)


def _tell(db, timeline: int, location: str | None, teller: int, listener: int, claim, t: float,
          rng: random.Random | None = None) -> None:
    heard = claim["heard_magnitude"]
    told = distort(db, teller, heard, rng, t) if rng is not None else heard
    payload = {"claim_id": claim["claim_id"]}
    if abs(told - heard) >= 0.005:
        payload["distorted"] = round(told - heard, 3)
    event_id = db.execute(
        "INSERT INTO world_event (timeline_id, type, location, actors_json, payload_json, source, game_time)"
        " VALUES (?, 'gossip_exchange', ?, ?, ?, 'sim', ?)",
        (timeline, location, json.dumps([teller, listener]), json.dumps(payload), t)).lastrowid
    db.execute("INSERT INTO event_witness (event_id, npc_id, how) VALUES (?,?, 'participated'), (?,?, 'participated')",
               (event_id, teller, event_id, listener))
    claims.hear(db, timeline, listener, teller, dict(claim), claim["know_id"], claim["believed"], told, t,
                event_id=event_id)
    for subject in (claim["subject_id"], claim["object_id"]):   # fog: gossip about the player carries their name
        if subject is not None:
            gossip_carries_name(db, teller, listener, subject, t)


def _corrections(db, timeline: int, location: str | None, witness: int, members: list[int],
                 households: dict[int, set[int]], t: float, step: float, rng: random.Random) -> int:
    """A witness who hears a blown-up version of what they saw sets the teller's listener straight:
    the listener's copy is replaced by the true size, and the extra they made of it is taken back."""
    done = 0
    seen = db.execute("SELECT * FROM v_claim_knowledge WHERE holder_id = ? AND origin = 'witnessed'"
                      " AND learned_gt <= ? AND sensitivity <> 'secret' ORDER BY claim_id", (witness, t)).fetchall()
    member_set = set(members)
    for truth in seen:
        if t - truth["claim_gt"] > FORGET_DAYS:
            continue
        wrong = db.execute(
            "SELECT * FROM v_claim_knowledge WHERE claim_id = ? AND origin = 'told' AND learned_gt <= ?"
            " AND heard_magnitude - ? >= ? ORDER BY know_id",
            (truth["claim_id"], t, truth["magnitude"], CORRECT_OVER)).fetchall()
        for copy in wrong:
            listener = copy["holder_id"]
            if listener == witness or listener not in member_set or listener == truth["subject_id"]:
                continue
            contact, _ = _contact(db, witness, listener, households)
            chance = 1 - (1 - min(0.95, contact * CORRECT_RATE)) ** step
            if rng.random() >= chance:
                continue
            _correct(db, timeline, location, witness, truth["know_id"], listener, dict(copy), truth["magnitude"], t)
            done += 1
            if done >= MAX_TELLINGS_PER_STEP:
                return done
    return done


def _correct(db, timeline: int, location: str | None, witness: int, witness_know_id: int, listener: int, copy: dict,
             true_mag: float, t: float) -> None:
    event_id = db.execute(
        "INSERT INTO world_event (timeline_id, type, location, actors_json, payload_json, source, game_time)"
        " VALUES (?, 'gossip_correction', ?, ?, ?, 'sim', ?)",
        (timeline, location, json.dumps([witness, listener]),
         json.dumps({"claim_id": copy["claim_id"], "from": copy["heard_magnitude"], "to": true_mag}), t)).lastrowid
    db.execute("INSERT INTO event_witness (event_id, npc_id, how) VALUES (?,?, 'participated'), (?,?, 'participated')",
               (event_id, witness, event_id, listener))
    believed = copy["believed"] or 0.0
    new_id = claims._hold(db, timeline, listener, copy["claim_id"], copy["subject_id"], "told", witness,
                          witness_know_id, event_id, None, true_mag, max(believed, claims.credibility(db, listener, witness)),
                          copy["will_share"] or "yes", t)
    db.execute("UPDATE knowledge SET superseded_by = ? WHERE know_id = ?", (new_id, copy["know_id"]))
    # take back the part of the listener's reaction that came from the exaggeration
    close = copy["object_id"] is not None and claims.is_close(claims.bond(db, listener, copy["object_id"]))
    if copy["subject_id"] == listener:
        return
    too_much = acts.listener_deltas(copy["act"], copy["valence"], copy["heard_magnitude"], believed, close)
    right = acts.listener_deltas(copy["act"], copy["valence"], true_mag, believed, close)
    back = {k: round(right.get(k, 0.0) - v, 4) for k, v in too_much.items() if abs(right.get(k, 0.0) - v) >= 0.001}
    if back:
        name = db.execute("SELECT display_name FROM npc WHERE npc_id = ?", (witness,)).fetchone()[0]
        db.execute("INSERT INTO relationship_change (timeline_id, from_id, to_id, deltas_json, cause_event_id, reason,"
                   " game_time) VALUES (?,?,?,?,?,?,?)",
                   (timeline, listener, copy["subject_id"], json.dumps(back), event_id,
                    f"{name} set the story straight: claim {copy['claim_id']}", t))
    rep_too_much = reputation.deltas(db, listener, copy, copy["heard_magnitude"], believed, False, close)
    rep_right = reputation.deltas(db, listener, copy, true_mag, believed, False, close)
    rep_back = {k: rep_right.get(k, 0.0) - v for k, v in rep_too_much.items()}
    reputation.adjust(db, timeline, listener, copy["subject_id"], rep_back, t, copy["claim_id"], event_id,
                      "set straight by a witness")
