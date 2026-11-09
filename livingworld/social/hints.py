"""Player knowledge hints (S2): what the player overhears as in-game notifications, at the level set by
``MANTELLA_LW_INFO_LEVEL``:

* ``minimal``: no hints. The player learns only by seeing things and by asking people.
* ``balanced`` (default): at most one short hint per conversation start, only about the player or
  someone the player knows well, phrased as hearsay that names who was talking, so the player knows whom to ask
  ("You catch Hilde murmuring about you.", "You overhear Hilde talking about Hod.").
* ``informative``: up to three hints with names and the gist ("Hod has heard that you helped Gerdur."),
  plus one line on what the place thinks of the player in the first conversation after a game load.

Hints only come from claims people in the current settlement actually hold. Each claim is hinted once
(``player_notice``), and an overheard hint becomes the player's own ``knowledge`` row (origin
``overheard``), which no NPC prompt ever reads. Things the player saw happen are never hinted.
"""
from __future__ import annotations

import os

from livingworld.social import claims, reputation

INFO_ENV = "MANTELLA_LW_INFO_LEVEL"
LEVELS = ("minimal", "balanced", "informative")
DEFAULT_LEVEL = "balanced"
MAX_HINTS = {"minimal": 0, "balanced": 1, "informative": 3}
KNOWS_WELL_CONVERSATIONS = 3   # someone the player has talked with this often counts as known well
FRESH_DAYS = 14.0              # older news is not worth a hint
MIN_BELIEVED = 0.3


def level() -> str:
    value = os.environ.get(INFO_ENV, "").strip().lower()
    return value if value in LEVELS else DEFAULT_LEVEL


def known_well(db, player_id: int) -> set[int]:
    return {r[0] for r in db.execute(
        "SELECT b.npc_id FROM participant a JOIN participant b ON b.conv_id = a.conv_id AND b.npc_id <> a.npc_id"
        " JOIN npc n ON n.npc_id = b.npc_id WHERE a.npc_id = ? AND n.is_child = 0"
        " GROUP BY b.npc_id HAVING COUNT(DISTINCT a.conv_id) >= ?", (player_id, KNOWS_WELL_CONVERSATIONS))}


def _members(db, community_keys: list[str]) -> list[int]:
    if not community_keys:
        return []
    marks = ",".join("?" * len(community_keys))
    return [r[0] for r in db.execute(
        f"SELECT DISTINCT m.npc_id FROM membership m JOIN community c ON c.community_id = m.community_id"
        f" JOIN npc n ON n.npc_id = m.npc_id WHERE c.key IN ({marks}) AND n.is_child = 0 AND n.is_player = 0",
        tuple(community_keys))]


def candidates(db, player_id: int, community_keys: list[str], now_gt: float, include_known: bool = True) -> list[dict]:
    """Claims people here hold that the player could overhear, most worth hearing first."""
    members = _members(db, community_keys)
    if not members:
        return []
    about = {player_id} | (known_well(db, player_id) if include_known else set())
    m_marks, a_marks = ",".join("?" * len(members)), ",".join("?" * len(about))
    rows = db.execute(
        f"SELECT k.claim_id, k.subject_id, k.object_id, k.act, k.detail, k.valence, k.magnitude, k.location,"
        f" k.sensitivity, k.claim_gt, k.truth_event_id, k.holder_id, k.origin, k.believed, k.heard_magnitude,"
        f" COUNT(*) OVER (PARTITION BY k.claim_id) AS holders"
        f" FROM v_claim_knowledge k WHERE k.holder_id IN ({m_marks}) AND k.sensitivity <> 'secret'"
        f" AND k.believed >= ? AND k.claim_gt >= ? AND k.claim_gt <= ?"
        f" AND (k.subject_id IN ({a_marks}) OR k.object_id IN ({a_marks}))"
        f" AND k.claim_id NOT IN (SELECT claim_id FROM v_player_notice WHERE claim_id IS NOT NULL)"
        f" AND k.claim_id NOT IN (SELECT claim_id FROM v_claim_knowledge WHERE holder_id = ?)"
        f" AND NOT EXISTS (SELECT 1 FROM event_witness w WHERE w.event_id = k.truth_event_id AND w.npc_id = ?)"
        f" ORDER BY k.claim_id, k.believed DESC",
        (*members, MIN_BELIEVED, now_gt - FRESH_DAYS, now_gt + 1e-9, *about, *about, player_id, player_id)).fetchall()
    best: dict[int, dict] = {}
    for row in rows:
        r = dict(row)
        if r["claim_id"] not in best:
            best[r["claim_id"]] = r
    out = list(best.values())
    out.sort(key=lambda c: (c["subject_id"] != player_id and c["object_id"] != player_id,
                            -c["holders"], -abs(c["valence"]) * c["magnitude"], -c["claim_gt"]))
    return out


def _place_name(db, community_keys: list[str]) -> str:
    row = db.execute("SELECT name FROM community WHERE key = ?", (community_keys[0],)).fetchone() if community_keys else None
    return row[0] if row else "local"


def _name(db, npc_id: int) -> str:
    return db.execute("SELECT display_name FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()[0]


def pick(db, timeline: int, player_id: int, community_keys: list[str], now_gt: float,
         after_load: bool = False) -> list[str]:
    """The hint lines to show now, recorded so they never repeat. Call inside a transaction."""
    lvl = level()
    if lvl == "minimal" or not reputation.enabled() or not community_keys:
        return []
    place = _place_name(db, community_keys)
    lines: list[str] = []
    for c in candidates(db, player_id, community_keys, now_gt)[:MAX_HINTS[lvl]]:
        about_player = player_id in (c["subject_id"], c["object_id"])
        if lvl == "balanced":
            # name who was talking, so the player has someone to ask; the gist stays theirs to find out
            who = _name(db, c["holder_id"]) if c["holder_id"] != player_id else f"{place} folk"
            text = (f"You catch {who} murmuring about you." if about_player
                    else f"You overhear {who} talking about {_name(db, c['subject_id'])}.")
        else:
            said = claims.sentence(db, c, player_id, second_person=True)
            holder = _name(db, c["holder_id"])
            how = "saw that" if c["origin"] == "witnessed" else "has heard that"
            text = f"{holder} {how} {said}."
        db.execute("INSERT INTO player_notice (timeline_id, claim_id, level, kind, community, shown_gt, text)"
                   " VALUES (?,?,?, 'hint', ?,?,?)", (timeline, c["claim_id"], lvl, community_keys[0], now_gt, text))
        if c["holder_id"] != player_id:
            claims._hold(db, timeline, player_id, c["claim_id"], c["subject_id"], "overheard", c["holder_id"], None,
                         None, None, c["heard_magnitude"] or c["magnitude"], 0.5, "no", now_gt)
        lines.append(text)
    if lvl == "informative" and after_load:
        said = reputation.summary(db, community_keys[0], player_id)
        if said:
            text = f"What {place} makes of you: {said}."
            db.execute("INSERT INTO player_notice (timeline_id, level, kind, community, shown_gt, text)"
                       " VALUES (?, 'informative', 'summary', ?,?,?)", (timeline, community_keys[0], now_gt, text))
            lines.append(text)
    return lines
