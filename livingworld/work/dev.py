"""Developer-only task tools. Never reachable from the game or from conversation.

Run on the PC with the game's database, while the Mantella server is NOT running:

    python -m livingworld.work.dev --db "<save folder>/data/livingworld/<world>.sqlite" list
    python -m livingworld.work.dev --db ... explain Gerdur
    python -m livingworld.work.dev --db ... create mill_firewood --giver Gerdur
    python -m livingworld.work.dev --db ... offer mill_firewood --giver Gerdur
    python -m livingworld.work.dev --db ... give 1 4            # the worker hands over 4 of the item
    python -m livingworld.work.dev --db ... complete 1 | fail 1
    python -m livingworld.work.dev --db ... deadline 1 --in 0.5  # days from now
    python -m livingworld.work.dev --db ... advance --days 3     # run upkeep as if days had passed
    python -m livingworld.work.dev --db ... reset [--npc Gerdur]

"Now" is the latest game time in the database unless ``--now`` is given. Everything this tool writes is
on the active save timeline and marked as coming from the developer (``source = 'admin'`` events, reasons
starting "developer"/"forced"), so it is easy to spot and an older save undoes it like anything else.
``reset`` hides tasks from the task rules (offers, reliability, prompt lines); memories and relationship
changes they already caused stay, so load an older save to undo those too.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys

from livingworld.social import claims, processor
from livingworld.store.world_store import WorldStore
from livingworld.work import conditions as C, consequences, needs, progress, tasks as T
from livingworld.work.templates import by_key, person_key, templates

MANTELLA_PORT = 4999


def server_running(port: int = MANTELLA_PORT) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _npc(db, name: str) -> int:
    row = db.execute("SELECT npc_id FROM npc WHERE lower(display_name) = lower(?) AND is_child = 0"
                     " ORDER BY npc_id LIMIT 1", (name,)).fetchone()
    if not row:
        raise SystemExit(f"No adult NPC called {name!r} in this database (they must have been met in play first).")
    return row[0]


def _player(db) -> int | None:
    row = db.execute("SELECT npc_id FROM npc WHERE is_player = 1").fetchone()
    return row[0] if row else None


def _name(db, npc_id: int | None) -> str:
    if npc_id is None:
        return "-"
    return db.execute("SELECT display_name FROM npc WHERE npc_id = ?", (npc_id,)).fetchone()[0]


def _now(db, given: float | None) -> float:
    if given is not None:
        return given
    return db.execute("SELECT MAX(gt) FROM (SELECT MAX(game_time) AS gt FROM world_event UNION ALL"
                      " SELECT MAX(game_time) FROM task_change)").fetchone()[0] or 0.0


def _task(db, task_id: int) -> T.Task:
    task = T.get(db, task_id)
    if task is None:
        raise SystemExit(f"No task {task_id} on the active timeline.")
    return task


def _describe(db, task: T.Task) -> str:
    deadline = f", due day {task.deadline_gt:.2f}" if task.deadline_gt is not None else ""
    return (f"#{task.task_id} {task.template} [{task.state}] giver {_name(db, task.giver_id)}, "
            f"worker {_name(db, task.assignee_id)}, {task.progress:g}/{task.need_count} {task.units}{deadline}")


def _admin_event(db, kind: str, actors: list[int], payload: dict, gt: float) -> int:
    return db.execute("INSERT INTO world_event (timeline_id, type, actors_json, payload_json, source, game_time)"
                      " VALUES (?,?,?,?, 'admin', ?)",
                      (T._timeline(db), kind, json.dumps(actors), json.dumps(payload), gt)).lastrowid


def run(argv: list[str], out=print) -> int:
    p = argparse.ArgumentParser(prog="python -m livingworld.work.dev", description=__doc__.split("\n\n")[0])
    p.add_argument("--db", required=True, help="path to the world's .sqlite file")
    p.add_argument("--now", type=float, help="game days to treat as now (default: latest in the database)")
    p.add_argument("--force", action="store_true", help="write even if a Mantella server seems to be running")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").add_argument("--all", action="store_true", help="include reset tasks")
    sub.add_parser("templates")
    sub.add_parser("show").add_argument("task_id", type=int)
    e = sub.add_parser("explain"); e.add_argument("giver"); e.add_argument("--worker"); e.add_argument("--hour", type=int); e.add_argument("--weather")
    c = sub.add_parser("create"); c.add_argument("template"); c.add_argument("--giver", required=True); c.add_argument("--assignee")
    o = sub.add_parser("offer"); o.add_argument("template"); o.add_argument("--giver", required=True)
    g = sub.add_parser("give"); g.add_argument("task_id", type=int); g.add_argument("count", type=float)
    sub.add_parser("complete").add_argument("task_id", type=int)
    sub.add_parser("fail").add_argument("task_id", type=int)
    d = sub.add_parser("deadline"); d.add_argument("task_id", type=int); d.add_argument("--in", dest="days", type=float, required=True)
    a = sub.add_parser("advance"); a.add_argument("--days", type=float, required=True); a.add_argument("--hour", type=int); a.add_argument("--weather")
    r = sub.add_parser("reset"); r.add_argument("--npc"); r.add_argument("--yes", action="store_true")
    args = p.parse_args(argv)

    writes = args.cmd not in ("list", "templates", "show", "explain")
    if writes and not args.force and server_running():
        out("The Mantella server seems to be running (port 4999). Stop it first, or pass --force.")
        return 2
    store = WorldStore(args.db)
    try:
        with store.transaction() as db:
            now = _now(db, args.now)
            return _dispatch(db, args, now, out)
    finally:
        store.close()


def _dispatch(db, args, now: float, out) -> int:
    cmd = args.cmd
    if cmd == "list":
        rows = T.find(db, include_reset=args.all)
        for task in rows:
            out(_describe(db, task))
        out(f"{len(rows)} task(s); now = day {now:.2f}, {C.month(now)}")
        return 0
    if cmd == "templates":
        for t in templates():
            out(f"{t.key}: {t.giver} needs someone to {t.title} ({t.need_count} {t.units}); helpers {', '.join(t.helpers)}")
        return 0
    if cmd == "show":
        task = _task(db, args.task_id)
        out(_describe(db, task))
        for h in T.history(db, task.task_id):
            extra = "".join(f", {k} {h[k]}" for k in ("d_progress", "deadline_gt", "mood") if h[k])
            out(f"  day {h['game_time']:.3f}  {h['state'] or '-':<12} {h['reason']}{extra}")
        if task.assignee_id is not None:
            out(f"  conduct so far: {', '.join(consequences.conduct(db, task, task.assignee_id, now).words()) or '-'}")
        return 0
    if cmd == "explain":
        giver = _npc(db, args.giver)
        worker = _npc(db, args.worker) if args.worker else _player(db)
        if person_key(db, giver) is None:
            out(f"{args.giver} is not in any seed file, so has no work templates.")
        for line in needs.explain(db, giver, now, args.hour, args.weather, worker):
            out(line)
        return 0
    if cmd in ("create", "offer"):
        t = by_key(args.template)
        if t is None:
            raise SystemExit(f"No template {args.template!r}; see 'templates'.")
        giver = _npc(db, args.giver)
        task = next((x for x in T.open_for_giver(db, giver) if x.template == t.key and x.state in ("available", "offered")),
                    None) or T.create(db, t, giver, now, source="admin")
        if cmd == "offer":
            T.change(db, task, None, "forced offer by developer", now, source="admin")
            out(f"{_describe(db, task)}: {_name(db, giver)} will offer it in the next conversation with the player")
        elif args.assignee:
            worker = _npc(db, args.assignee)
            deadline = now + t.deadline_days if t.deadline_days else None
            task = T.change(db, task, "accepted", "developer assigned it", now, assignee_id=worker,
                            deadline_gt=deadline, source="admin")
            out(_describe(db, task))
        else:
            out(_describe(db, task))
        return 0
    task = _task(db, args.task_id) if hasattr(args, "task_id") else None
    if cmd == "give":
        if task.assignee_id is None:
            raise SystemExit("Nobody has taken this task on yet.")
        _admin_event(db, "item_given", [task.assignee_id, task.giver_id],
                     {"item": task.need_item, "count": args.count}, now)
        processor.process(db, {"work": progress.consume})
    elif cmd == "complete":
        if task.assignee_id is None:
            raise SystemExit("Nobody has taken this task on yet; use create --assignee first.")
        if task.state == "accepted":
            task = T.change(db, task, "in_progress", "developer started it", now, source="admin")
        consequences.completed(db, task, now, "developer completed it", source="admin")
    elif cmd == "fail":
        if task.assignee_id is None:
            T.change(db, task, "expired", "developer failed it (nobody had taken it)", now, source="admin")
        else:
            consequences.let_down(db, task, "failed", now, "developer failed it", source="admin")
    elif cmd == "deadline":
        T.change(db, task, None, "developer moved the deadline", now, deadline_gt=now + args.days, source="admin")
    elif cmd == "advance":
        result = needs.upkeep(db, now + args.days, args.hour, args.weather)
        _admin_event(db, "sim_tick", [], {"developer_advance_days": args.days}, now + args.days)
        out(f"day {now:.2f} -> {now + args.days:.2f}: opened {result.opened}, expired {result.expired}, "
            f"lapsed {result.lapsed}, paused {result.paused}, resumed {result.resumed}")
    elif cmd == "reset":
        giver = _npc(db, args.npc) if args.npc else None
        rows = [x for x in T.find(db) if giver is None or giver in (x.giver_id, x.assignee_id)]
        if not args.yes:
            out(f"This hides {len(rows)} task(s) from the task rules on the current timeline. Run again with --yes.")
            return 1
        for x in rows:
            T.change(db, x, None, "reset by developer", now, source="admin")
        out(f"reset {len(rows)} task(s)")
        return 0
    processor.process(db, {"appraisal": claims.appraise})
    if task is not None:
        out(_describe(db, T.get(db, task.task_id)))
    return 0


def main() -> None:
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
