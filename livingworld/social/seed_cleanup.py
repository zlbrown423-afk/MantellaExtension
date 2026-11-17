"""One-off, opt-in clean-up of a world that was seeded by the OLD Riverwood seed.

Removing a row from ``data/riverwood.json`` does not remove it from a world that already has it, and the
old seed re-wrote its values at every server start. This tool brings such a world in line with the
approved seed file, and only touches what it can PROVE the old seed wrote and play never changed.

It is a command line tool and is never imported by the server (a test enforces that)::

    python -m livingworld.social.seed_cleanup report  --db World.sqlite [--out report.json]
    python -m livingworld.social.seed_cleanup apply   --db World.sqlite --report report.json --confirm XXXX-XXXX-XXXX
    python -m livingworld.social.seed_cleanup restore BACKUP_FOLDER --db World.sqlite --confirm restore

``report`` opens the database read-only and changes nothing. ``apply`` needs the report, the typed
confirmation code printed with it, an unchanged database, and Mantella stopped. It first makes a full,
verified backup; if that fails nothing is changed. All changes go in one transaction.

Ownership: a row is seed-owned only when its values equal what the frozen installed seed
(``data/seed_history``) writes AND no gameplay evidence exists for it. Anything else is listed under
"needs your decision" and nothing at all is applied until that list is empty.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from importlib import resources
from pathlib import Path
from typing import Any

from livingworld.social import seed as seedmod
from livingworld.social import traits as traitsmod

INSTALLED_SEED = "seed_history/riverwood_e62b682.json"
MIN_SCHEMA, MAX_SCHEMA = 17, 26           # versions this tool was written and tested for (022 only adds timeline_scope; 023 only conversation_resume; 026 only fade_clock and views)
DEFAULT_BACKUP_ROOT = r"D:\ModdingTools\LWBackups"
EPS = 1e-9
REL_COLS = ("kinship", "status", "trust", "affinity", "respect", "familiarity", "attraction")


class Refused(Exception):
    """The tool will not run; nothing was changed."""


# ---------------------------------------------------------------- seeds

def installed_seed() -> dict[str, Any]:
    text = resources.files("livingworld.social").joinpath("data").joinpath(INSTALLED_SEED).read_text(encoding="utf-8")
    return json.loads(text)


def approved_seed(installed: dict[str, Any]) -> dict[str, Any] | None:
    """The packaged seed file that carries the same first community as the installed one."""
    key = installed["communities"][0]["key"]
    for s in seedmod.seeds():
        if s["communities"][0]["key"] == key:
            return s
    return None


def _rows_of(seed: dict[str, Any] | None, ids: dict[str, int]) -> dict[tuple[int, int], tuple]:
    """(from_id, to_id) -> (kinship, status, trust, affinity, respect, familiarity, attraction), as the seed writes them."""
    out: dict[tuple[int, int], tuple] = {}
    if not seed:
        return out
    for r in seed["relationships"]:
        a, b = ids.get(r["a"]), ids.get(r["b"])
        if a is None or b is None:
            continue
        back = r.get("back") or {}
        vals = (float(r.get("trust", 0)), float(r.get("affinity", 0)), float(r.get("respect", 0)),
                float(r.get("familiarity", 0)), float(r.get("attraction", 0)))
        out[(a, b)] = (r.get("kinship"), r.get("status", "acquaintance"), *vals)
        if not r.get("one_way"):
            out[(b, a)] = (back.get("kinship", r.get("kinship")), back.get("status", r.get("status", "acquaintance")), *vals)
    return out


def _memberships_of(seed: dict[str, Any] | None, ids: dict[str, int]) -> dict[tuple[int, str], tuple[str | None, float]]:
    out = {}
    if not seed:
        return out
    for p in seed["people"]:
        npc = ids.get(p["key"])
        if npc is None:
            continue
        for key, m in (p.get("member_of") or {}).items():
            out[(npc, key)] = (m.get("role"), float(m.get("influence", 0.3)))
    return out


def _gossip_of(seed: dict[str, Any] | None, ids: dict[str, int]) -> dict[int, float]:
    out = {}
    if not seed:
        return out
    for p in seed["people"]:
        npc = ids.get(p["key"])
        if npc is not None:
            g = {**({"gossipy": p["gossipy"]} if "gossipy" in p else {}), **(p.get("traits") or {})}.get("gossipy")
            if g is not None:
                out[npc] = float(g)
    return out


def _same(a: tuple, b: tuple) -> bool:
    return len(a) == len(b) and all((abs(x - y) < EPS) if isinstance(x, float) and isinstance(y, float) else x == y
                                    for x, y in zip(a, b))


# ---------------------------------------------------------------- planning (read only)

def open_readonly(path: str) -> sqlite3.Connection:
    if not os.path.isfile(path):
        raise Refused(f"no such database: {path}")
    conn = sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True, timeout=1.0)
    conn.row_factory = sqlite3.Row
    return conn


def check_database(db: sqlite3.Connection) -> int:
    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise Refused("the database failed SQLite's integrity check")
    try:
        version = db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
    except sqlite3.DatabaseError as e:
        raise Refused(f"not a Living World database ({e})")
    if not MIN_SCHEMA <= version <= MAX_SCHEMA:
        raise Refused(f"database schema is {version}; this tool was written for {MIN_SCHEMA} to {MAX_SCHEMA}")
    return version


def build_plan(db: sqlite3.Connection, installed: dict[str, Any] | None = None,
               approved: dict[str, Any] | None | bool = True) -> dict[str, Any]:
    """Compare the database with the installed seed and the approved seed. Writes nothing."""
    installed = installed or installed_seed()
    approved = approved_seed(installed) if approved is True else approved
    if approved is None:
        raise Refused("no approved seed file with the same community was found")
    version = check_database(db)
    ids_i = seedmod._person_ids(db, installed)
    ids_a = seedmod._person_ids(db, approved)
    changes: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    names = {r[0]: r[1] for r in db.execute("SELECT npc_id, display_name FROM npc")}

    # a person with two database identities for the same Skyrim reference cannot be matched safely
    for p in installed["people"]:
        n = db.execute("SELECT COUNT(*) FROM npc WHERE plugin = ? AND base_form_id = ? AND is_child = 0",
                       (p["plugin"], seedmod._local_id(p["base_id"]))).fetchone()[0] if _has_col(db, "npc", "is_child") else 0
        if n > 1:
            decisions.append({"table": "npc", "what": p["key"], "why": f"{n} database identities for one Skyrim reference"})

    def label(a: int, b: int | None = None) -> str:
        return names.get(a, str(a)) + (f" -> {names.get(b, str(b))}" if b is not None else "")

    # ---- relationships
    old_rel, new_rel = _rows_of(installed, ids_i), _rows_of(approved, ids_a)
    for (a, b), seeded in sorted(old_rel.items()):
        row = db.execute("SELECT " + ",".join(REL_COLS) + " FROM relationship WHERE from_id = ? AND to_id = ?", (a, b)).fetchone()
        if row is None:
            continue
        have = tuple(float(v) if i >= 2 else v for i, v in enumerate(row))
        target = new_rel.get((a, b))
        if target is not None and _same(have, target):
            continue
        evidence = _evidence(db, a, b)
        entry = {"table": "relationship", "key": [a, b], "what": label(a, b), "old": list(have)}
        if not _same(have, seeded):
            if target is None:
                decisions.append({**entry, "why": "values equal neither the installed seed nor the approved seed (origin unknown)"})
            else:
                decisions.append({**entry, "why": "values differ from the installed seed, so they may have been changed by hand"})
            continue
        if evidence:
            decisions.append({**entry, "why": "gameplay evidence exists: " + ", ".join(evidence)})
            continue
        if target is None:
            changes.append({**entry, "action": "delete", "why": "equals the installed seed row; no gameplay evidence; not in the approved seed"})
        else:
            changes.append({**entry, "action": "update", "new": list(target), "why": "equals the installed seed row; no gameplay evidence"})

    # ---- memberships
    old_m, new_m = _memberships_of(installed, ids_i), _memberships_of(approved, ids_a)
    community_ids = {r[0]: r[1] for r in db.execute("SELECT key, community_id FROM community")}
    for (npc, key), seeded in sorted(old_m.items()):
        cid = community_ids.get(key)
        if cid is None:
            continue
        row = db.execute("SELECT role, influence FROM membership WHERE npc_id = ? AND community_id = ?", (npc, cid)).fetchone()
        if row is None:
            continue
        have = (row[0], float(row[1]))
        target = new_m.get((npc, key))
        if target is not None and _same(have, target):
            continue
        entry = {"table": "membership", "key": [npc, key], "what": f"{label(npc)} in {key}", "old": list(have)}
        if not _same(have, seeded):
            decisions.append({**entry, "why": "values differ from the installed seed, so they may have been changed"})
        elif target is None:
            changes.append({**entry, "action": "delete", "why": "equals the installed seed; not in the approved seed"})
        else:
            changes.append({**entry, "action": "update", "new": list(target), "why": "equals the installed seed"})

    # ---- gossipiness
    old_g, new_g = _gossip_of(installed, ids_i), _gossip_of(approved, ids_a)
    for npc, seeded in sorted(old_g.items()):
        row = db.execute("SELECT persona_id, bio, traits_json FROM persona_version WHERE npc_id = ?"
                         " ORDER BY version DESC LIMIT 1", (npc,)).fetchone()
        if row is None:
            continue
        traits = json.loads(row["traits_json"])
        have = traits.get("gossipy")
        if have is None:
            continue
        if npc in new_g:
            target, how = new_g[npc], "the approved seed's value"
        elif row["bio"]:
            ident = db.execute("SELECT plugin, base_form_id FROM npc WHERE npc_id = ?", (npc,)).fetchone()
            target = traitsmod.from_bio(row["bio"], f"{ident[0]}:{ident[1]}")["gossipy"]
            how = "the value worked out from their stored bio (what they had before the seed overwrote it)"
        else:
            target, how = None, "no value (the game's own default applies)"
        if target is not None and abs(have - target) < EPS:
            continue
        entry = {"table": "persona_version", "key": [row["persona_id"], npc], "what": f"{label(npc)} gossipy", "old": have}
        if abs(have - seeded) > EPS:
            decisions.append({**entry, "why": "not the installed seed's value, so it may have been set by hand"})
        else:
            changes.append({**entry, "action": "set" if target is not None else "remove_key", "new": target,
                            "why": f"equals the installed seed; becomes {how}"})

    plan = {"schema_version": version, "installed_seed": INSTALLED_SEED, "changes": changes, "needs_decision": decisions,
            "untouched": ["community", "place", "claim", "knowledge", "memory", "world_event", "relationship_change",
                          "romance_bond_change", "every other table"]}
    plan["fingerprint"] = fingerprint(plan)
    return plan


def _has_col(db, table: str, col: str) -> bool:
    return any(r[1] == col for r in db.execute(f"PRAGMA table_info({table})"))


def _evidence(db, a: int, b: int) -> list[str]:
    out = []
    n = db.execute("SELECT COUNT(*) FROM relationship_change WHERE (from_id = ? AND to_id = ?) OR (from_id = ? AND to_id = ?)",
                   (a, b, b, a)).fetchone()[0]
    if n:
        out.append(f"{n} relationship_change row(s)")
    lo, hi = min(a, b), max(a, b)
    n = db.execute("SELECT COUNT(*) FROM romance_bond_change WHERE a_id = ? AND b_id = ?", (lo, hi)).fetchone()[0]
    if n:
        out.append(f"{n} romance record(s)")
    return out


def fingerprint(plan: dict[str, Any]) -> str:
    body = {k: plan[k] for k in ("schema_version", "installed_seed", "changes", "needs_decision")}
    h = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:12]
    return f"{h[:4]}-{h[4:8]}-{h[8:12]}"


def render(plan: dict[str, Any], db_name: str, dry: bool = True) -> str:
    lines = [f"RIVERWOOD SEED CLEANUP - {'DRY RUN (nothing has been changed)' if dry else 'APPLIED'}",
             f"World database : {db_name}   schema {plan['schema_version']}   installed seed: {plan['installed_seed']}", ""]
    lines.append(f"PLANNED CHANGES ({len(plan['changes'])})")
    for i, c in enumerate(plan["changes"], 1):
        new = f" -> {c['new']}" if "new" in c else ""
        lines += [f"  [{i}] {c['action'].upper():<10} {c['table']:<16} {c['what']}   {c['old']}{new}", f"        why: {c['why']}"]
    lines += ["", f"NEEDS YOUR DECISION ({len(plan['needs_decision'])})  - nothing is applied until this is empty"]
    for d in plan["needs_decision"]:
        lines.append(f"  - {d['table']}: {d['what']}: {d['why']}")
    lines += ["", "NEVER TOUCHED: " + ", ".join(plan["untouched"]), ""]
    if dry:
        lines += ["To apply: stop Mantella, then run", "  python -m livingworld.social.seed_cleanup apply --db <world.sqlite> "
                  f"--report <this report.json> --confirm {plan['fingerprint']}",
                  "A verified backup is made first. Running 'report' again afterwards should show 0 planned changes."]
    return "\n".join(lines)


# ---------------------------------------------------------------- backup / apply / restore

def _tables(db) -> list[str]:
    return [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def _counts(db) -> dict[str, int]:
    return {t: db.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in _tables(db)}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def make_backup(db_path: str, root: str, plan: dict[str, Any], now: dt.datetime | None = None) -> Path:
    """Full copy of the world with SQLite's own backup, verified. Raises Refused (nothing changed) on any mismatch."""
    now = now or dt.datetime.now(dt.timezone.utc)
    folder = Path(root) / f"{Path(db_path).stem}-{now.strftime('%Y%m%dT%H%M%SZ')}"
    folder.mkdir(parents=True, exist_ok=False)
    target = folder / Path(db_path).name
    src = sqlite3.connect(db_path, timeout=1.0)
    try:
        src.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        dest = sqlite3.connect(target)
        try:
            src.backup(dest)
        finally:
            dest.close()
        live_counts = _counts(src)
    finally:
        src.close()
    check = sqlite3.connect(target)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise Refused("the backup failed its integrity check; nothing was changed")
        if _counts(check) != live_counts:
            raise Refused("the backup's row counts differ from the live database; nothing was changed")
    finally:
        check.close()
    (folder / "row_counts.json").write_text(json.dumps(live_counts, indent=1), encoding="utf-8")
    (folder / "plan.json").write_text(json.dumps(plan, indent=1, default=str), encoding="utf-8")
    (folder / "backup.json").write_text(json.dumps({"database": target.name, "sha256": _sha256(target),
                                                    "made_utc": now.isoformat(), "fingerprint": plan["fingerprint"]}, indent=1),
                                        encoding="utf-8")
    return folder


def apply_plan(db_path: str, report_path: str, confirm: str, backup_root: str,
               approved: dict[str, Any] | None = None) -> tuple[Path | None, dict[str, Any]]:
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    if report.get("fingerprint") != confirm.strip():
        raise Refused("the confirmation code does not match the report; nothing was changed")
    if report["needs_decision"]:
        raise Refused("the report has items that need your decision; resolve them and make a new report")
    ro = open_readonly(db_path)
    try:
        current = build_plan(ro, approved=approved if approved is not None else True)
    finally:
        ro.close()
    if current["fingerprint"] != report["fingerprint"]:
        raise Refused("the database or the seed files changed since the report was made; make a new report")
    if not current["changes"]:
        return None, current                      # already clean: idempotent
    conn = sqlite3.connect(db_path, timeout=0.2, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        try:
            conn.execute("PRAGMA locking_mode = EXCLUSIVE")
            conn.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError:
            raise Refused("the database is in use (is Mantella still running?); nothing was changed")
        conn.execute("ROLLBACK")
        conn.close()
        conn = None
        folder = make_backup(db_path, backup_root, current)
        conn = sqlite3.connect(db_path, timeout=0.2, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA locking_mode = EXCLUSIVE")
            conn.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError:
            raise Refused("the database became busy after the backup; nothing was changed")
        try:
            _execute(conn, current["changes"])
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return folder, current
    finally:
        if conn is not None:
            conn.close()


def _execute(conn: sqlite3.Connection, changes: list[dict[str, Any]]) -> None:
    for c in changes:
        t, k, act = c["table"], c["key"], c["action"]
        if t == "relationship" and act == "delete":
            n = conn.execute("DELETE FROM relationship WHERE from_id = ? AND to_id = ?", k).rowcount
        elif t == "relationship":
            kin, status, trust, aff, resp, fam, attr = c["new"]
            n = conn.execute("UPDATE relationship SET kinship = ?, status = ?, trust = ?, affinity = ?, respect = ?,"
                             " familiarity = ?, attraction = ? WHERE from_id = ? AND to_id = ?",
                             (kin, status, trust, aff, resp, fam, attr, *k)).rowcount
        elif t == "membership":
            cid = conn.execute("SELECT community_id FROM community WHERE key = ?", (k[1],)).fetchone()[0]
            if act == "delete":
                n = conn.execute("DELETE FROM membership WHERE npc_id = ? AND community_id = ?", (k[0], cid)).rowcount
            else:
                n = conn.execute("UPDATE membership SET role = ?, influence = ? WHERE npc_id = ? AND community_id = ?",
                                 (c["new"][0], c["new"][1], k[0], cid)).rowcount
        elif t == "persona_version":
            if act == "set":
                n = conn.execute("UPDATE persona_version SET traits_json = json_set(traits_json, '$.gossipy', ?)"
                                 " WHERE persona_id = ?", (c["new"], k[0])).rowcount
            else:
                n = conn.execute("UPDATE persona_version SET traits_json = json_remove(traits_json, '$.gossipy')"
                                 " WHERE persona_id = ?", (k[0],)).rowcount
        else:
            raise Refused(f"unknown change {t}/{act}")
        if n != 1:
            raise RuntimeError(f"expected to change one row for {c['what']}, changed {n}")


def restore(backup_folder: str, db_path: str, confirm: str) -> dict[str, int]:
    if confirm != "restore":
        raise Refused("restore needs --confirm restore; nothing was changed")
    folder = Path(backup_folder)
    meta = json.loads((folder / "backup.json").read_text(encoding="utf-8"))
    backup = folder / meta["database"]
    if _sha256(backup) != meta["sha256"]:
        raise Refused("the backup file's checksum does not match; nothing was changed")
    live = Path(db_path)
    if live.exists():
        probe = sqlite3.connect(live, timeout=0.2, isolation_level=None)
        try:
            probe.execute("PRAGMA locking_mode = EXCLUSIVE")
            probe.execute("BEGIN EXCLUSIVE")
            probe.execute("ROLLBACK")
            safety = folder / f"before-restore-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.sqlite"
            dest = sqlite3.connect(safety)
            try:
                probe.backup(dest)
            finally:
                dest.close()
        except sqlite3.OperationalError:
            raise Refused("the database is in use (is Mantella still running?); nothing was changed")
        finally:
            probe.close()
    for suffix in ("-wal", "-shm"):
        Path(str(live) + suffix).unlink(missing_ok=True)
    shutil.copyfile(backup, live)
    check = sqlite3.connect(live)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise Refused("the restored database failed its integrity check")
        counts = _counts(check)
    finally:
        check.close()
    saved = json.loads((folder / "row_counts.json").read_text(encoding="utf-8"))
    if counts != saved:
        raise Refused("restored row counts differ from the list saved at backup time")
    return counts


# ---------------------------------------------------------------- command line

def _load(path: str | None):
    return json.loads(Path(path).read_text(encoding="utf-8")) if path else True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="seed_cleanup", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report", help="dry run: read-only, writes nothing to the database")
    r.add_argument("--db", required=True)
    r.add_argument("--approved-seed", help="use this seed file as the approved one (to preview before it is installed)")
    r.add_argument("--out", help="where to save the report (default: next to this command, seed_cleanup_report.json)")
    a = sub.add_parser("apply", help="back up, then apply the reviewed report")
    a.add_argument("--db", required=True)
    a.add_argument("--report", required=True)
    a.add_argument("--confirm", required=True, help="the confirmation code printed with the report")
    a.add_argument("--approved-seed", help="the same file that was given to report, if any")
    a.add_argument("--backup-root", default=DEFAULT_BACKUP_ROOT)
    s = sub.add_parser("restore", help="put a backup back")
    s.add_argument("backup_folder")
    s.add_argument("--db", required=True)
    s.add_argument("--confirm", required=True, help="type: restore")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "report":
            db = open_readonly(args.db)
            try:
                plan = build_plan(db, approved=_load(args.approved_seed))
            finally:
                db.close()
            out = Path(args.out or "seed_cleanup_report.json")
            out.write_text(json.dumps(plan, indent=1, default=str), encoding="utf-8")
            print(render(plan, Path(args.db).name))
            print(f"\nReport saved to {out}\nConfirmation code: {plan['fingerprint']}")
        elif args.cmd == "apply":
            folder, plan = apply_plan(args.db, args.report, args.confirm, args.backup_root, _load(args.approved_seed))
            if folder is None:
                print("Nothing to do: the world already matches the approved seed.")
            else:
                print(render(plan, Path(args.db).name, dry=False))
                print(f"\nBackup: {folder}\nTo undo: python -m livingworld.social.seed_cleanup restore \"{folder}\" --db \"{args.db}\" --confirm restore")
        else:
            counts = restore(args.backup_folder, args.db, args.confirm)
            print("Restored. Row counts:", json.dumps(counts))
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
