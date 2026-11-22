"""Institutions: the Jarl's court, chartered companies and (later) guards, temples and settlements."""
from __future__ import annotations

from dataclasses import dataclass

KINDS = ("court", "guard", "company", "settlement", "temple", "guild")


@dataclass(frozen=True)
class Institution:
    inst_id: int
    key: str
    name: str
    kind: str
    hold: str | None
    head_id: int | None
    founded_gt: float


def active_timeline(db) -> int:
    return db.execute("SELECT timeline_id FROM timeline WHERE is_active = 1").fetchone()[0]


def get(db, key: str) -> Institution | None:
    """The institution with this key that the current save can see (the earliest founding wins)."""
    r = db.execute("SELECT inst_id, key, name, kind, hold, head_id, founded_gt FROM v_institution WHERE key = ?"
                   " ORDER BY founded_gt, inst_id LIMIT 1", (key,)).fetchone()
    return Institution(*tuple(r)) if r else None


def ensure(db, key: str, name: str, kind: str, gt: float, hold: str | None = None,
           head_id: int | None = None) -> Institution:
    """The institution, founded now on the active timeline if this save has not seen it yet."""
    if kind not in KINDS:
        raise ValueError(f"unknown institution kind {kind!r}")
    found = get(db, key)
    if found is not None:
        return found
    cur = db.execute("INSERT INTO institution (timeline_id, key, name, kind, hold, head_id, founded_gt)"
                     " VALUES (?,?,?,?,?,?,?)", (active_timeline(db), key, name, kind, hold, head_id, gt))
    return Institution(cur.lastrowid, key, name, kind, hold, head_id, gt)
