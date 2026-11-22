"""Developer-only lordship tools. Never reachable from the game.

    python -m livingworld.lordship.dev scenario [loyal|overreach|neglect|reconcile|all] [--months 6]
    python -m livingworld.lordship.dev --db "<world>.sqlite" status
    python -m livingworld.lordship.dev --db "<world>.sqlite" catch-up [--now DAYS]

Scenarios run on a fresh in-memory world and never touch a save. With --db, run while the Mantella server is
NOT running; "now" is the latest game time in the database unless --now is given.
"""
from __future__ import annotations

import argparse
import sys

from livingworld.lordship import debug as LD
from livingworld.lordship import reckoning as R
from livingworld.lordship import scenarios as S


def run(argv: list[str], out=print) -> int:
    p = argparse.ArgumentParser(prog="python -m livingworld.lordship.dev", description=__doc__.split("\n\n")[0])
    p.add_argument("--db", help="path to the world's .sqlite file")
    p.add_argument("--now", type=float)
    sub = p.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("scenario")
    sc.add_argument("name", nargs="?", default="all", choices=["all", *S.SCENARIOS])
    sc.add_argument("--months", type=int, default=6)
    sub.add_parser("status")
    sub.add_parser("catch-up")
    a = p.parse_args(argv)
    if a.cmd == "scenario":
        for name in (S.SCENARIOS if a.name == "all" else [a.name]):
            out(f"== {name}: {S.SCENARIOS[name][0]}")
            S.play(name, a.months, out)
        return 0
    if not a.db:
        p.error("--db is needed for status and catch-up")
    from livingworld.store.world_store import WorldStore
    store = WorldStore(a.db)
    with store.transaction() as db:
        now = a.now if a.now is not None else (db.execute("SELECT COALESCE(MAX(game_time), 0) FROM world_event")
                                               .fetchone()[0] or 0.0)
        if a.cmd == "catch-up":
            result = R.catch_up(db, now)
            out(f"reckoned {result.periods} month(s), paid {result.paid}; {'; '.join(result.moves) or 'no moves'}")
        out(LD.status_text(db, now))
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
