"""Scripted months for checking the rules without the game (``python -m livingworld.lordship.dev scenario``).

Each scenario builds a fresh in-memory world with the player and the three L1 officials, grants a level 1
charter, then plays out months of reported facts and runs the reckoning. Tests assert the outcome bands.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from livingworld.lordship import charter as C
from livingworld.lordship import data as D
from livingworld.lordship import reckoning as R
from livingworld.store.world_store import WorldStore


@dataclass
class Month:
    riders: int
    horses: int = 4
    patrols: int = 13
    report: bool = True


def _npc(db, base_id: str, name: str, player: bool = False) -> int:
    cur = db.execute("INSERT INTO npc (plugin, base_form_id, ref_form_id, display_name, is_player, created_gt)"
                     " VALUES ('Skyrim.esm', ?, NULL, ?, ?, 0)", (base_id, name, int(player)))
    return cur.lastrowid


def world(db, jarl_regard: float = 0.3) -> dict[str, int]:
    """The player and the L1 officials, each knowing the player a little (relationship base rows)."""
    ids = {"player": _npc(db, "0x00000007", "Zachary", player=True)}
    for o in D.officials():
        ids[o.key] = _npc(db, o.base_id, o.name)
        start = jarl_regard if o.key == "balgruuf" else 0.1
        db.execute("INSERT INTO relationship (from_id, to_id, status, trust, affinity, respect, updated_gt)"
                   " VALUES (?,?,'acquaintance',?,?,?,0)", (ids[o.key], ids["player"], start, start, start))
    return ids


SCENARIOS: dict[str, tuple[str, Callable[[int], Month]]] = {
    "loyal": ("keeps every term for six months", lambda m: Month(riders=min(8, 2 + m))),
    "overreach": ("recruits far past the cap with no money", lambda m: Month(riders=4 + 5 * m, patrols=13)),
    "neglect": ("stays small but skips patrols and reports", lambda m: Month(riders=4, patrols=2, report=False)),
    "reconcile": ("overreaches for three months, then cuts back and serves well",
                  lambda m: Month(riders=24 if m <= 3 else 6)),
}


def play(name: str, months: int = 6, out: Callable[[str], None] | None = None) -> list[dict]:
    """Run a scenario; returns one dict per month (band, gap, status, positions, purse, move)."""
    about, plan = SCENARIOS[name]
    store = WorldStore(":memory:")
    rows = []
    with store.transaction() as db:
        ids = world(db)
        charter = C.grant(db, ids["player"], 1.0, source="admin")
        C.declare_watching(db, C.FACT_KINDS, 1.0, source="admin")
        days = D.terms().period_days
        for m in range(1, months + 1):
            month = plan(m)
            at = charter.signed_gt + (m - 1) * days + 1.0
            C.report_fact(db, "strength", at, ids["player"], {"riders": month.riders, "horses": month.horses})
            if month.patrols:
                C.report_fact(db, "patrol", at + 1, ids["player"], {"route": "west_road", "count": month.patrols})
            if month.report:
                C.report_fact(db, "report", at + 2, ids["player"], {})
            result = R.catch_up(db, charter.signed_gt + m * days + 0.001)
            live = C.latest(db)
            snap = R.latest_snapshot(db, live) if live.charter_id == charter.charter_id else None
            positions = {k: p for k, p in db.execute(
                "SELECT official_key, position FROM v_official_stance_now WHERE subject = 'plains_wardens'")}
            row = {"month": m, "riders": month.riders, "band": snap[5] if snap else None,
                   "gap": snap[4] if snap else None, "status": live.status, "purse": C.treasury(db),
                   "positions": positions, "moves": result.moves}
            rows.append(row)
            if out:
                out(f"{name} month {m}: riders {month.riders:>2}  gap {row['gap']:+.2f} {row['band']:<8} "
                    f"charter {row['status']:<12} purse {row['purse']:>5}  "
                    + " ".join(f"{k}={v}" for k, v in sorted(positions.items()))
                    + (f"  MOVE: {'; '.join(result.moves)}" if result.moves else ""))
    return rows
