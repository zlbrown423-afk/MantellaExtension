"""Squad membership rules. All functions take an open connection; the caller owns the transaction."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

from livingworld.squad import DEFAULT_FORMATION, FORMATIONS, MAX_SQUAD

logger = logging.getLogger("livingworld.squad")

LIVE = ("asked", "active")


@dataclass(frozen=True)
class Squad:
    squad_id: int
    leader_id: int
    formation: str
    formed_gt: float


@dataclass(frozen=True)
class Member:
    npc_id: int
    status: str
    asked_gt: float
    joined_gt: float | None


def active_squad(db, leader_id: int) -> Squad | None:
    r = db.execute("SELECT squad_id, leader_id, formation, formed_gt FROM squad"
                   " WHERE leader_id = ? AND disbanded_gt IS NULL", (leader_id,)).fetchone()
    return Squad(r[0], r[1], r[2], r[3]) if r else None


def squad_of(db, npc_id: int) -> Squad | None:
    """The live squad this NPC is asked into or serves in."""
    r = db.execute("SELECT s.squad_id, s.leader_id, s.formation, s.formed_gt FROM squad_member m"
                   " JOIN squad s ON s.squad_id = m.squad_id"
                   " WHERE m.npc_id = ? AND m.status IN ('asked','active') AND s.disbanded_gt IS NULL",
                   (npc_id,)).fetchone()
    return Squad(r[0], r[1], r[2], r[3]) if r else None


def members(db, squad_id: int, statuses: Iterable[str] = LIVE) -> list[Member]:
    marks = ",".join("?" for _ in tuple(statuses))
    rows = db.execute(f"SELECT npc_id, status, asked_gt, joined_gt FROM squad_member"
                      f" WHERE squad_id = ? AND status IN ({marks}) ORDER BY member_row",
                      (squad_id, *tuple(statuses))).fetchall()
    return [Member(r[0], r[1], r[2], r[3]) for r in rows]


def _eligible(db, npc_id: int, leader_id: int) -> str | None:
    """Why this NPC cannot be asked, or None."""
    row = db.execute("SELECT is_player, is_child FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()
    if row is None:
        return "unknown actor"
    if npc_id == leader_id or row["is_player"]:
        return "is the leader"
    if row["is_child"]:
        return "a child"
    live = squad_of(db, npc_id)
    if live is not None:
        return "already in a squad" if live.leader_id != leader_id else "already asked"
    return None


def ask(db, leader_id: int, npc_ids: Iterable[int], game_time: float, formation: str | None = None) -> list[int]:
    """The leader asked these NPCs to come along. Forms the squad if there is none. Returns who was newly asked;
    anyone ineligible is skipped and logged, and the squad never grows past ``MAX_SQUAD``."""
    squad = active_squad(db, leader_id)
    asked: list[int] = []
    for npc_id in npc_ids:
        why = _eligible(db, npc_id, leader_id)
        if why is not None:
            logger.log(23, f"Living World squad: npc {npc_id} not asked: {why}")
            continue
        if squad is None:
            cur = db.execute("INSERT INTO squad (leader_id, formation, formed_gt) VALUES (?,?,?)",
                             (leader_id, formation if formation in FORMATIONS else DEFAULT_FORMATION, game_time))
            squad = Squad(cur.lastrowid, leader_id, formation or DEFAULT_FORMATION, game_time)
            logger.log(23, f"Living World squad: squad {squad.squad_id} formed")
        if len(members(db, squad.squad_id)) >= MAX_SQUAD:
            logger.log(23, f"Living World squad: squad {squad.squad_id} is full; npc {npc_id} not asked")
            continue
        db.execute("INSERT INTO squad_member (squad_id, npc_id, status, asked_gt) VALUES (?,?, 'asked', ?)",
                   (squad.squad_id, npc_id, game_time))
        asked.append(npc_id)
    return asked


def answer(db, npc_id: int, agreed: bool, game_time: float) -> str | None:
    """An asked NPC replied. Returns the new status ('active' or 'declined'), or None if they were not asked."""
    row = db.execute("SELECT member_row FROM squad_member WHERE npc_id = ? AND status = 'asked'", (npc_id,)).fetchone()
    if row is None:
        return None
    if agreed:
        db.execute("UPDATE squad_member SET status = 'active', joined_gt = ? WHERE member_row = ?", (game_time, row[0]))
        logger.log(23, f"Living World squad: npc {npc_id} joined")
        return "active"
    db.execute("UPDATE squad_member SET status = 'declined', ended_gt = ?, reason = 'declined' WHERE member_row = ?",
               (game_time, row[0]))
    logger.log(23, f"Living World squad: npc {npc_id} declined")
    _disband_if_empty(db, npc_id, game_time)
    return "declined"


def _squad_row_of(db, member_row_npc: int) -> int | None:
    r = db.execute("SELECT squad_id FROM squad_member WHERE npc_id = ? ORDER BY member_row DESC LIMIT 1",
                   (member_row_npc,)).fetchone()
    return r[0] if r else None


def _disband_if_empty(db, npc_id: int, game_time: float) -> None:
    squad_id = _squad_row_of(db, npc_id)
    if squad_id is not None and not members(db, squad_id):
        db.execute("UPDATE squad SET disbanded_gt = ? WHERE squad_id = ? AND disbanded_gt IS NULL", (game_time, squad_id))
        logger.log(23, f"Living World squad: squad {squad_id} disbanded (no members left)")


def dismiss(db, leader_id: int, npc_ids: Iterable[int] | None, game_time: float, reason: str = "dismissed",
            status: str = "dismissed") -> list[int]:
    """End membership: the given NPCs, or everyone when ``npc_ids`` is None. Returns who left."""
    squad = active_squad(db, leader_id)
    if squad is None:
        return []
    wanted = None if npc_ids is None else set(npc_ids)
    left = []
    for m in members(db, squad.squad_id):
        if wanted is not None and m.npc_id not in wanted:
            continue
        db.execute("UPDATE squad_member SET status = ?, ended_gt = ?, reason = ? WHERE npc_id = ? AND status IN ('asked','active')",
                   (status, game_time, reason, m.npc_id))
        left.append(m.npc_id)
    if left:
        logger.log(23, f"Living World squad: {len(left)} left squad {squad.squad_id} ({reason})")
    if not members(db, squad.squad_id):
        db.execute("UPDATE squad SET disbanded_gt = ? WHERE squad_id = ?", (game_time, squad.squad_id))
        logger.log(23, f"Living World squad: squad {squad.squad_id} disbanded")
    return left


def conversation_ended(db, leader_id: int, game_time: float) -> list[int]:
    """The conversation that held the squad ended: the hold is gone, so membership ends. Unanswered asks are dropped too."""
    return dismiss(db, leader_id, None, game_time, reason="conversation ended", status="dropped")


def set_formation(db, leader_id: int, formation: str) -> bool:
    """True when the squad's formation changed."""
    squad = active_squad(db, leader_id)
    if squad is None or formation not in FORMATIONS or squad.formation == formation:
        return False
    db.execute("UPDATE squad SET formation = ? WHERE squad_id = ?", (formation, squad.squad_id))
    logger.log(23, f"Living World squad: squad {squad.squad_id} formation {squad.formation} -> {formation}")
    return True


def report(db) -> list[dict]:
    """Every squad, with its members and why they left, for the developer tools."""
    out = []
    for s in db.execute("SELECT squad_id, leader_id, formation, formed_gt, disbanded_gt FROM squad ORDER BY squad_id DESC LIMIT 20"):
        rows = db.execute("SELECT m.npc_id, n.display_name, m.status, m.reason FROM squad_member m JOIN npc n ON n.npc_id = m.npc_id"
                          " WHERE m.squad_id = ? ORDER BY m.member_row", (s[0],)).fetchall()
        out.append({"squad_id": s[0], "leader_id": s[1], "formation": s[2], "formed_gt": s[3], "disbanded_gt": s[4],
                    "members": [dict(r) for r in rows]})
    return out
