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

from livingworld.social import acts, claims

STEP_DAYS = 0.25          # one step per quarter day (morning, afternoon, evening, night)
MAX_STEPS = 60            # a long absence is covered in at most this many (longer) steps
MAX_TELLINGS_PER_STEP = 2 # per teller
FADE_DAYS = 7.0           # older news is less interesting...
FORGET_DAYS = 30.0        # ...and stops spreading
MIN_BELIEVED = 0.3        # people do not pass on what they hardly believe

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
                _tell(db, timeline, location, teller, listener, claim, t)
                knowers.add(listener)
                told += 1
                tellings += 1
                if told >= MAX_TELLINGS_PER_STEP:
                    break
    return tellings


def _tell(db, timeline: int, location: str | None, teller: int, listener: int, claim, t: float) -> None:
    event_id = db.execute(
        "INSERT INTO world_event (timeline_id, type, location, actors_json, payload_json, source, game_time)"
        " VALUES (?, 'gossip_exchange', ?, ?, ?, 'sim', ?)",
        (timeline, location, json.dumps([teller, listener]), json.dumps({"claim_id": claim["claim_id"]}), t)).lastrowid
    db.execute("INSERT INTO event_witness (event_id, npc_id, how) VALUES (?,?, 'participated'), (?,?, 'participated')",
               (event_id, teller, event_id, listener))
    claims.hear(db, timeline, listener, teller, dict(claim), claim["know_id"], claim["believed"],
                claim["heard_magnitude"], t, event_id=event_id)
