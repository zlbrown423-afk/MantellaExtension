"""Developer-only guard career tools. Never reachable from the game or from conversation.

Run on the PC with the game's database, while the Mantella server is NOT running:

    python -m livingworld.guard.dev --db "<save folder>/data/livingworld/<world>.sqlite" orgs
    python -m livingworld.guard.dev --db ... status
    python -m livingworld.guard.dev --db ... roster whiterun
    python -m livingworld.guard.dev --db ... explain-recruit whiterun --leader "Commander Caius" [--bounty 40] [--thane]
    python -m livingworld.guard.dev --db ... enlist whiterun [--leader "Commander Caius"] [--force-rules]
    python -m livingworld.guard.dev --db ... set-rank optio | promote | demote
    python -m livingworld.guard.dev --db ... dismiss | reinstate whiterun
    python -m livingworld.guard.dev --db ... thane whiterun
    python -m livingworld.guard.dev --db ... reset whiterun --yes

"Now" is the latest game time in the database unless ``--now`` is given. Everything written here is on the
active save timeline and marked as coming from the developer (``source = 'admin'`` events, reasons starting
"developer"), so loading an older save undoes it like anything else. ``reset`` hides the player's service in
that guard from the rules; claims and memories it already caused stay.
"""
from __future__ import annotations

import argparse
import sys

from livingworld.guard import debug as D, orgs as O, profiles, recruit as R, store as S
from livingworld.guard.service import run as run_command
from livingworld.social import claims, processor
from livingworld.store.world_store import WorldStore
from livingworld.work.dev import _name, _npc, _player, server_running


def _now(db, given: float | None) -> float:
    if given is not None:
        return given
    return db.execute("SELECT MAX(gt) FROM (SELECT MAX(game_time) AS gt FROM world_event UNION ALL"
                      " SELECT MAX(game_time) FROM service_change)").fetchone()[0] or 0.0


def _org(key: str | None) -> O.Org:
    org = O.by_key(key) if key else O.orgs()[0]
    if org is None:
        raise SystemExit(f"No guard {key!r}; see 'orgs'.")
    return org


def _player_or_exit(db) -> int:
    player = _player(db)
    if player is None:
        raise SystemExit("No player in this database yet (talk to someone in game first).")
    return player


def run(argv: list[str], out=print) -> int:
    p = argparse.ArgumentParser(prog="python -m livingworld.guard.dev", description=__doc__.split("\n\n")[0])
    p.add_argument("--db", required=True, help="path to the world's .sqlite file")
    p.add_argument("--now", type=float, help="game days to treat as now (default: latest in the database)")
    p.add_argument("--force", action="store_true", help="write even if a Mantella server seems to be running")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("orgs")
    sub.add_parser("status")
    sub.add_parser("roster").add_argument("org", nargs="?")
    e = sub.add_parser("explain-recruit"); e.add_argument("org", nargs="?"); e.add_argument("--leader")
    e.add_argument("--bounty", type=int); e.add_argument("--thane", action="store_true")
    n = sub.add_parser("enlist"); n.add_argument("org", nargs="?"); n.add_argument("--leader")
    n.add_argument("--force-rules", action="store_true", help="enlist even if the rules say no")
    sub.add_parser("set-rank").add_argument("rank")
    sub.add_parser("promote")
    sub.add_parser("demote")
    sub.add_parser("dismiss")
    sub.add_parser("reinstate").add_argument("org", nargs="?")
    sub.add_parser("thane").add_argument("org", nargs="?")
    r = sub.add_parser("reset"); r.add_argument("org", nargs="?"); r.add_argument("--yes", action="store_true")
    args = p.parse_args(argv)

    writes = args.cmd not in ("orgs", "status", "roster", "explain-recruit")
    if writes and not args.force and server_running():
        out("The Mantella server seems to be running (port 4999). Stop it first, or pass --force.")
        return 2
    store = WorldStore(args.db)
    try:
        with store.transaction() as db:
            now = _now(db, args.now)
            code = _dispatch(db, args, now, out)
            if writes:
                processor.process(db, {"appraisal": claims.appraise})
            return code
    finally:
        store.close()


def _leader(db, org: O.Org, name: str | None) -> int:
    if name:
        return _npc(db, name)
    for person in org.leaders:
        if person.recruits:
            npc_id = profiles.leader_id(db, org, person.key)
            if npc_id is not None:
                return npc_id
    raise SystemExit(f"No leader of {org.name} has been met in play yet; pass --leader.")


def _dispatch(db, args, now: float, out) -> int:
    cmd = args.cmd
    if cmd == "orgs":
        for o in O.orgs():
            out(f"{o.key}: {o.name} ({o.hold}), crime faction {o.crime_plugin} {o.crime_faction}; ranks "
                + ", ".join(r.title for r in o.ranks) + "; leaders " + ", ".join(x.name for x in o.leaders))
        return 0
    player = _player_or_exit(db)
    if cmd == "status":
        for svc in S.history(db, player):
            o = O.by_key(svc.org_key)
            out(f"#{svc.service_id} {o.name if o else svc.org_key}: {o.title(svc.rank_key) if o else svc.rank_key}, "
                f"{svc.status}, joined day {svc.joined_gt:.2f} via {svc.path}, recruiter {_name(db, svc.recruiter_id)}")
            for c in S.changes(db, svc.service_id):
                out(f"  day {c['game_time']:.3f}  {c['rank_key'] or '-':<10} {c['status'] or '-':<10} {c['reason']}")
        out(f"now = day {now:.2f}")
        return 0
    org = _org(getattr(args, "org", None))
    if cmd == "roster":
        for prof in profiles.roster(db, org.key):
            rank = org.title(prof.leader_rank) if prof.is_leader else "Guard"
            out(f"{_name(db, prof.npc_id)}" + (f" ({prof.given_name})" if prof.given_name else "") + f": {rank}")
        return 0
    if cmd == "explain-recruit":
        d = R.decide(db, org, _leader(db, org, args.leader), player, now, bounty=args.bounty, thane=args.thane)
        for line in R.explain(d):
            out(line)
        return 0
    if cmd == "enlist":
        leader = _leader(db, org, args.leader) if (args.leader or not args.force_rules) else None
        if leader is not None and not args.force_rules:
            d = R.decide(db, org, leader, player, now)
            if not d.ok:
                for line in R.explain(d):
                    out(line)
                out("Not enlisted (pass --force-rules to do it anyway).")
                return 1
        if S.current(db, player) is not None:
            raise SystemExit("The player already serves; dismiss first.")
        svc = S.enlist(db, org.key, player, "admin", now, rank_key=org.ranks[0].key, recruiter_id=leader,
                       source="admin", reason="developer: enlisted")
        out(f"enlisted: #{svc.service_id} {org.name}, {org.title(svc.rank_key)}")
        return 0
    if cmd == "reset":
        rows = [s for s in S.history(db, player, org.key)]
        if not args.yes:
            out(f"This hides {len(rows)} service record(s) in {org.name} from the rules. Run again with --yes.")
            return 1
        for svc in rows:
            S.change(db, svc, "reset by developer", now, source="admin")
        out(f"reset {len(rows)} service record(s)")
        return 0
    svc = S.current(db, player)
    if cmd in ("promote", "demote"):
        if svc is None:
            raise SystemExit("The player does not serve anywhere.")
        o = _org(svc.org_key)
        i = o.rank_index(svc.rank_key) + (1 if cmd == "promote" else -1)
        if not 0 <= i < len(o.ranks):
            raise SystemExit(f"No rank {'above' if cmd == 'promote' else 'below'} {o.title(svc.rank_key)}.")
        out(run_command(db, D.Command("rank", o.key, o.ranks[i].key), o, player, now, None))
        return 0
    verb = {"set-rank": "rank"}.get(cmd, cmd)
    out(run_command(db, D.Command(verb, org.key, getattr(args, "rank", None)), org, player, now, None))
    return 0


def main() -> None:
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
