"""Getting to know someone: familiarity from talks and shared moments, and the status that follows from it.

Before this, nothing turned an ordinary conversation into a relationship change, so after 61 hours of play
Gisli was still numerically a stranger (2026-11-23 audit). Rules, not the model, decide here:

* **A meaningful talk** (the NPC heard at least ``MIN_LINES`` different player lines of at least ``MIN_WORDS``
  words) adds familiarity, with diminishing returns: ``TALK_STEP * (1 - familiarity)``. Greetings, one-liners
  and the same line said again do not count. Only familiarity moves: trust, affinity and attraction come from
  what happens (perception, claims, consent), never from how much was said.
* **A finished intimate scene** is shared history: ``SCENE_STEP * (1 - familiarity)`` familiarity. It grants no
  trust, affection or commitment; perception already records how the scene itself went.
* Each row is written once per conversation or event (the reason names it), on that conversation's or
  event's timeline, so a reload neither repeats nor leaks it.

**Status** is worked out when it is read (``effective_status``), so it is always true to the active timeline
and never stored ahead of the evidence. A status set by kinship, romance, rivalry or the seed
(courting/partner/spouse/ex/rival/enemy, or friend and up) stands; otherwise someone becomes an acquaintance
once they know the player a little, a friend only with real familiarity *and* liking *and* trust, a close
friend with more of each. Attraction and intimacy never raise a status. ``MANTELLA_LW_FAMILIARITY=0`` turns
the writing off (status reading stays).
"""
from __future__ import annotations

import os
import re

from src import utils

logger = utils.get_logger()

ENV = "MANTELLA_LW_FAMILIARITY"
_OFF = {"0", "false", "no", "off"}

MIN_LINES = 2           # different player lines the NPC heard
MIN_WORDS = 3           # words in a line for it to count
TALK_STEP = 0.05        # familiarity from one meaningful talk, before diminishing returns
SCENE_STEP = 0.05       # familiarity from a finished intimate scene
TALK_REASON = "got to know each other a little more (talk, conversation {conv_id})"
SCENE_REASON = "shared history (intimate scene, event {event_id})"

KEPT = ("courting", "partner", "spouse", "ex", "rival", "enemy", "friend", "close_friend")
ACQUAINTANCE_AT = 0.05                       # familiarity
FRIEND_AT = (0.35, 0.3, 0.2)                 # familiarity, affinity, trust
CLOSE_FRIEND_AT = (0.6, 0.55, 0.45)
_RANK = {"stranger": 0, "acquaintance": 1, "friend": 2, "close_friend": 3}


def enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() not in _OFF


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", (text or "").lower()).strip()


def meaningful_lines(texts: list[str]) -> int:
    """How many different player lines of at least ``MIN_WORDS`` words."""
    return len({_norm(t) for t in texts if len(_norm(t).split()) >= MIN_WORDS})


def _familiarity(db, npc_id: int, player_id: int) -> float:
    row = db.execute("SELECT familiarity FROM v_relationship WHERE from_id = ? AND to_id = ?",
                     (npc_id, player_id)).fetchone()
    return float(row[0]) if row else 0.0


def _add(db, timeline: int, npc_id: int, player_id: int, step: float, reason: str, gt: float,
         event_id: int | None = None) -> float | None:
    if db.execute("SELECT 1 FROM relationship_change WHERE from_id = ? AND to_id = ? AND reason = ?",
                  (npc_id, player_id, reason)).fetchone():
        return None   # already counted (the same conversation or event processed again)
    if not db.execute("SELECT 1 FROM relationship WHERE from_id = ? AND to_id = ?", (npc_id, player_id)).fetchone():
        db.execute("INSERT INTO relationship (from_id, to_id, status, updated_gt) VALUES (?,?, 'stranger', ?)",
                   (npc_id, player_id, gt))
    delta = round(step * (1.0 - _familiarity(db, npc_id, player_id)), 4)
    if delta < 0.001:
        return None
    db.execute("INSERT INTO relationship_change (timeline_id, from_id, to_id, deltas_json, cause_event_id, reason,"
               " game_time) VALUES (?,?,?,?,?,?,?)",
               (timeline, npc_id, player_id, f'{{"familiarity": {delta}}}', event_id, reason, gt))
    return delta


def after_talk(db, conv_id: int, player_id: int) -> dict[int, float]:
    """Familiarity for each NPC in ``conv_id`` who heard a meaningful talk with the player. Returns npc -> delta."""
    conv = db.execute("SELECT timeline_id, COALESCE(ended_gt, started_gt) FROM conversation WHERE conv_id = ?",
                      (conv_id,)).fetchone()
    if conv is None:
        return {}
    timeline, gt = conv
    out = {}
    people = db.execute("SELECT p.npc_id, p.joined_seq, p.left_seq FROM participant p JOIN npc n ON n.npc_id = p.npc_id"
                        " WHERE p.conv_id = ? AND n.is_player = 0 AND COALESCE(n.is_child, 0) = 0",
                        (conv_id,)).fetchall()
    for npc_id, joined, left in people:
        texts = [r[0] for r in db.execute(
            "SELECT text FROM v_utterance WHERE conv_id = ? AND speaker_id = ? AND seq > ? AND (? IS NULL OR seq <= ?)",
            (conv_id, player_id, joined or 0, left, left))]
        if meaningful_lines(texts) < MIN_LINES:
            continue
        delta = _add(db, timeline, npc_id, player_id, TALK_STEP, TALK_REASON.format(conv_id=conv_id), gt)
        if delta:
            out[npc_id] = delta
    return out


def after_scene(db, event_id: int, npc_ids: list[int], player_id: int) -> dict[int, float]:
    """Familiarity (shared history) for each NPC in a finished intimate scene with the player."""
    row = db.execute("SELECT timeline_id, game_time FROM world_event WHERE event_id = ?", (event_id,)).fetchone()
    if row is None:
        return {}
    out = {}
    for npc_id in npc_ids:
        if npc_id == player_id:
            continue
        delta = _add(db, row[0], npc_id, player_id, SCENE_STEP, SCENE_REASON.format(event_id=event_id), row[1],
                     event_id)
        if delta:
            out[npc_id] = delta
    return out


def effective_status(db, npc_id: int, other_id: int) -> str:
    """The status to show and use now: the stored one where kinship, romance, rivalry or the seed set it,
    else what familiarity, affinity and trust on the active timeline add up to (never below the stored one)."""
    row = db.execute("SELECT r.status, r.kinship, v.familiarity, v.affinity, v.trust FROM relationship r"
                     " JOIN v_relationship v ON v.from_id = r.from_id AND v.to_id = r.to_id"
                     " WHERE r.from_id = ? AND r.to_id = ?", (npc_id, other_id)).fetchone()
    if row is None:
        return "stranger"
    stored, kinship, fam, aff, trust = row
    if kinship or stored in KEPT:
        return stored
    earned = "stranger"
    if fam >= ACQUAINTANCE_AT:
        earned = "acquaintance"
    if fam >= FRIEND_AT[0] and aff >= FRIEND_AT[1] and trust >= FRIEND_AT[2]:
        earned = "friend"
    if fam >= CLOSE_FRIEND_AT[0] and aff >= CLOSE_FRIEND_AT[1] and trust >= CLOSE_FRIEND_AT[2]:
        earned = "close_friend"
    return earned if _RANK.get(earned, 0) > _RANK.get(stored, 0) else stored


def backfill(db, npc_id: int, player_id: int) -> list[tuple[str, float]]:
    """Apply the talk and scene rules to this NPC's history visible on the active timeline, oldest first.
    Idempotent like the live rules (the reasons name each conversation and event)."""
    done = []
    convs = db.execute(
        "SELECT c.conv_id, c.started_gt FROM conversation c JOIN v_timeline_scope s ON s.timeline_id = c.timeline_id"
        " AND COALESCE(c.ended_gt, c.started_gt) <= s.cutoff_gt"
        " WHERE c.kind = 'player' AND EXISTS (SELECT 1 FROM participant p WHERE p.conv_id = c.conv_id AND p.npc_id = ?)"
        " ORDER BY c.started_gt", (npc_id,)).fetchall()
    events = db.execute(
        "SELECT e.event_id, e.game_time FROM v_visible_event e JOIN event_witness w ON w.event_id = e.event_id"
        " WHERE e.type = 'romance.scene' AND w.npc_id = ? AND w.how = 'participated'"
        " AND COALESCE(json_extract(e.payload_json, '$.ongoing'), 0) = 0"
        " AND EXISTS (SELECT 1 FROM event_witness p WHERE p.event_id = e.event_id AND p.npc_id = ?)"
        " ORDER BY e.game_time", (npc_id, player_id)).fetchall()
    items = sorted([(c[1], "talk", c[0]) for c in convs] + [(e[1], "scene", e[0]) for e in events])
    for _, kind, ref in items:
        if kind == "talk":
            delta = after_talk(db, ref, player_id).get(npc_id)
        else:
            delta = after_scene(db, ref, [npc_id], player_id).get(npc_id)
        if delta:
            done.append((f"{kind} {ref}", delta))
    return done
