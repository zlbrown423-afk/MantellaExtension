"""The one-off Riverwood seed clean-up tool: proves it removes only what the old seed wrote, keeps all
gameplay history, is idempotent, backs up first, and can restore."""
import ast
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from livingworld.social import seed as seedmod
from livingworld.social import seed_cleanup as sc
from livingworld.social import traits
from livingworld.store.world_store import WorldStore

FIXTURE = Path(__file__).parent / "fixtures" / "riverwood_approved.json"
PEOPLE = {"delphine": "Delphine", "orgnar": "Orgnar", "embry": "Embry"}
BIO = {"delphine": "Delphine runs the Sleeping Giant Inn and hears every rumour in Riverwood.",
       "orgnar": "Orgnar is the barkeep at the Sleeping Giant Inn.",
       "embry": "Embry is a lumberjack who drinks at the inn."}


@pytest.fixture
def approved():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def make_world(tmp_path):
    """A world the OLD seed has been written to: Delphine, Orgnar and Embry seen, as in the real database."""
    path = tmp_path / "data" / "World.sqlite"
    store = WorldStore(str(path))
    installed = sc.installed_seed()
    with store.transaction() as db:
        for p in installed["people"]:
            if p["key"] in PEOPLE:
                cur = db.execute("INSERT INTO npc (plugin, base_form_id, display_name, race, sex, created_gt)"
                                 " VALUES (?,?,?,?,?,?)", (p["plugin"], seedmod._local_id(p["base_id"]), PEOPLE[p["key"]],
                                                           "Nord", "female" if p["key"] == "delphine" else "male", 1.0))
                db.execute("INSERT INTO persona_version (npc_id, version, bio, traits_json, source, created_gt)"
                           " VALUES (?,?,?,?,?,?)", (cur.lastrowid, 1, BIO[p["key"]],
                                                     json.dumps(traits.from_bio(BIO[p["key"]], f"{p['plugin']}:{seedmod._local_id(p['base_id'])}")),
                                                     "mantella_csv", 1.0))
        ids = seedmod._person_ids(db, installed)
        seedmod.Seeder._apply(db, installed, ids, 5.0)
        _old_seed_traits(db, installed, ids)
    store.close()
    return path


def _old_seed_traits(db, seed, ids):
    """The old seed (e62b682, the one installed on the PC) let the seed file win over the bio: it wrote gossipy
    (0.5 when unset) and its traits over whatever the bio gave. Today's seeder only fills missing traits, so the
    fixture repeats the old write itself to build the world the clean-up tool has to repair."""
    for p in seed["people"]:
        npc_id = ids.get(p["key"])
        if npc_id is None:
            continue
        for trait, value in {"gossipy": float(p.get("gossipy", 0.5)), **(p.get("traits") or {})}.items():
            db.execute("UPDATE persona_version SET traits_json = json_set(traits_json, ?, ?) WHERE npc_id = ?",
                       (f"$.{trait}", float(value), npc_id))


def conn(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def table_dump(path, skip=()):
    c = conn(path)
    out = {}
    for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        if t not in skip:
            out[t] = sorted((tuple(r) for r in c.execute(f'SELECT * FROM "{t}"')), key=repr)
    c.close()
    return out


def plan_for(path, approved):
    db = sc.open_readonly(str(path))
    try:
        return sc.build_plan(db, approved=approved)
    finally:
        db.close()


def run_apply(tmp_path, path, approved, plan=None):
    plan = plan or plan_for(path, approved)
    report = tmp_path / "report.json"
    report.write_text(json.dumps(plan, default=str))
    return sc.apply_plan(str(path), str(report), plan["fingerprint"], str(tmp_path / "backups"), approved)


def ids(path):
    c = conn(path)
    out = {r["display_name"]: r["npc_id"] for r in c.execute("SELECT npc_id, display_name FROM npc")}
    c.close()
    return out


def test_report_matches_the_plan_in_the_real_world(tmp_path, approved):
    plan = plan_for(make_world(tmp_path), approved)
    kinds = sorted((c["table"], c["action"]) for c in plan["changes"])
    assert kinds.count(("relationship", "delete")) == 4
    assert kinds.count(("membership", "update")) == 5
    assert sum(1 for t, _ in kinds if t == "persona_version") == 3
    assert len(plan["changes"]) == 12 and plan["needs_decision"] == []
    delphine = next(c for c in plan["changes"] if c["what"] == "Delphine gossipy")
    assert delphine["old"] == 0.15 and delphine["action"] == "set"


def test_report_writes_nothing(tmp_path, approved):
    path = make_world(tmp_path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    plan_for(path, approved)
    sc.main(["report", "--db", str(path), "--approved-seed", str(FIXTURE), "--out", str(tmp_path / "r.json")])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_apply_removes_seed_rows_and_keeps_everything_else(tmp_path, approved):
    path = make_world(tmp_path)
    before = table_dump(path)
    folder, _ = run_apply(tmp_path, path, approved)
    after = table_dump(path)
    changed = {t for t in before if before[t] != after[t]}
    assert changed == {"relationship", "membership", "persona_version"}
    assert after["relationship"] == []
    assert {r[3] for r in after["membership"]} <= {0.5}
    c = conn(path)
    names = ids(path)
    g = json.loads(c.execute("SELECT traits_json FROM persona_version WHERE npc_id = ?", (names["Delphine"],)).fetchone()[0])["gossipy"]
    assert abs(g - traits.from_bio(BIO["delphine"], "Skyrim.esm:0x00013478")["gossipy"]) < 1e-9
    other = {k for k in json.loads(c.execute("SELECT traits_json FROM persona_version WHERE npc_id = ?", (names["Delphine"],)).fetchone()[0])}
    assert other >= {"gossipy"}
    assert (folder / "backup.json").exists()


def test_relationship_with_gameplay_history_is_kept_and_nothing_is_applied(tmp_path, approved):
    path = make_world(tmp_path)
    n = ids(path)
    c = conn(path)
    c.execute("INSERT INTO relationship_change (timeline_id, from_id, to_id, deltas_json, reason, game_time)"
              " VALUES (1,?,?,'{\"trust\":-0.1}','argued',9.0)", (n["Delphine"], n["Orgnar"]))
    c.commit()
    c.close()
    plan = plan_for(path, approved)
    assert any("relationship_change" in d["why"] for d in plan["needs_decision"])
    with pytest.raises(sc.Refused):
        run_apply(tmp_path, path, approved, plan)
    assert len(table_dump(path)["relationship"]) == 4


def test_a_romance_record_blocks_removal(tmp_path, approved):
    path = make_world(tmp_path)
    n = ids(path)
    a, b = sorted((n["Delphine"], n["Orgnar"]))
    c = conn(path)
    c.execute("INSERT INTO romance_bond_change (timeline_id, a_id, b_id, stage_to, reason, game_time) VALUES (1,?,?,'courting','test',9)", (a, b))
    c.commit()
    c.close()
    plan = plan_for(path, approved)
    assert any("romance" in d["why"] for d in plan["needs_decision"])


def test_values_that_match_neither_seed_are_ambiguous_not_guessed(tmp_path, approved):
    path = make_world(tmp_path)
    n = ids(path)
    c = conn(path)
    c.execute("UPDATE relationship SET trust = 0.77 WHERE from_id = ? AND to_id = ?", (n["Orgnar"], n["Embry"]))
    c.execute("UPDATE membership SET influence = 0.9 WHERE npc_id = ? AND role = 'owner'", (n["Delphine"],))
    c.execute("UPDATE persona_version SET traits_json = json_set(traits_json, '$.gossipy', 0.42) WHERE npc_id = ?", (n["Embry"],))
    c.commit()
    c.close()
    plan = plan_for(path, approved)
    assert {d["table"] for d in plan["needs_decision"]} == {"relationship", "membership", "persona_version"}
    assert not any(c["what"] in ("Orgnar -> Embry", "Embry gossipy") for c in plan["changes"])


def test_membership_in_another_community_is_not_touched(tmp_path, approved):
    path = make_world(tmp_path)
    n = ids(path)
    c = conn(path)
    cid = c.execute("INSERT INTO community (key, name, kind) VALUES ('whiterun_guard','Whiterun Guard','faction')").lastrowid
    c.execute("INSERT INTO membership (npc_id, community_id, role, influence) VALUES (?,?,'guard',0.2)", (n["Orgnar"], cid))
    c.commit()
    c.close()
    plan = plan_for(path, approved)
    assert not any("whiterun_guard" in str(x["what"]) for x in plan["changes"] + plan["needs_decision"])


def test_other_persona_traits_and_bio_are_identical(tmp_path, approved):
    path = make_world(tmp_path)
    c = conn(path)
    before = {r["npc_id"]: (r["bio"], {k: v for k, v in json.loads(r["traits_json"]).items() if k != "gossipy"})
              for r in c.execute("SELECT * FROM persona_version")}
    c.close()
    run_apply(tmp_path, path, approved)
    c = conn(path)
    after = {r["npc_id"]: (r["bio"], {k: v for k, v in json.loads(r["traits_json"]).items() if k != "gossipy"})
             for r in c.execute("SELECT * FROM persona_version")}
    assert before == after


def test_apply_twice_changes_nothing_the_second_time(tmp_path, approved):
    path = make_world(tmp_path)
    run_apply(tmp_path, path, approved)
    assert plan_for(path, approved)["changes"] == []
    snap = hashlib.sha256(path.read_bytes()).hexdigest()
    folder, _ = run_apply(tmp_path, path, approved, plan_for(path, approved))
    assert folder is None and hashlib.sha256(path.read_bytes()).hexdigest() == snap


def test_apply_is_refused_without_the_right_confirmation_or_after_the_world_changed(tmp_path, approved):
    path = make_world(tmp_path)
    plan = plan_for(path, approved)
    report = tmp_path / "report.json"
    report.write_text(json.dumps(plan, default=str))
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    for bad in ("", "0000-0000-0000"):
        with pytest.raises(sc.Refused):
            sc.apply_plan(str(path), str(report), bad, str(tmp_path / "b"), approved)
    c = conn(path)
    c.execute("UPDATE membership SET influence = 0.45 WHERE influence = 0.4")
    c.commit()
    c.close()
    changed = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(sc.Refused):
        sc.apply_plan(str(path), str(report), plan["fingerprint"], str(tmp_path / "b"), approved)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == changed != before


def test_apply_is_refused_while_another_connection_holds_the_database(tmp_path, approved):
    path = make_world(tmp_path)
    plan = plan_for(path, approved)
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sc.Refused):
            run_apply(tmp_path, path, approved, plan)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert len(table_dump(path)["relationship"]) == 4
    assert not (tmp_path / "backups").exists()


def test_backup_then_restore_gives_back_the_same_world(tmp_path, approved):
    path = make_world(tmp_path)
    before = table_dump(path)
    folder, _ = run_apply(tmp_path, path, approved)
    assert table_dump(path) != before
    counts = sc.restore(str(folder), str(path), "restore")
    assert table_dump(path) == before
    assert counts == json.loads((folder / "row_counts.json").read_text())
    assert list(folder.glob("before-restore-*.sqlite"))
    with pytest.raises(sc.Refused):
        sc.restore(str(folder), str(path), "yes")


def test_a_failure_in_the_middle_rolls_everything_back(tmp_path, approved, monkeypatch):
    path = make_world(tmp_path)
    before = table_dump(path)
    real = sc._execute

    def boom(conn_, changes):
        real(conn_, changes[:3])
        raise RuntimeError("forced")
    monkeypatch.setattr(sc, "_execute", boom)
    with pytest.raises(RuntimeError):
        run_apply(tmp_path, path, approved)
    assert table_dump(path) == before


def test_a_failed_backup_verification_stops_before_any_change(tmp_path, approved, monkeypatch):
    path = make_world(tmp_path)
    before = table_dump(path)
    monkeypatch.setattr(sc, "_counts", lambda db: {"x": len(sc._tables(db)) + id(db) % 7})
    with pytest.raises(sc.Refused):
        run_apply(tmp_path, path, approved)
    assert table_dump(path) == before


def test_wrong_schema_version_is_refused(tmp_path, approved):
    path = make_world(tmp_path)
    c = conn(path)
    c.execute("INSERT INTO schema_version(version, note) VALUES (99, 'future')")
    c.commit()
    c.close()
    with pytest.raises(sc.Refused):
        plan_for(path, approved)


def test_the_server_never_imports_the_tool():
    root = Path(sc.__file__).parents[1]
    offenders = []
    for f in root.rglob("*.py"):
        if f.name in ("seed_cleanup.py",) or "tests" in f.parts:
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, (ast.Import, ast.ImportFrom)) else []
            mod = getattr(node, "module", None) or ""
            if any("seed_cleanup" in n for n in names) or "seed_cleanup" in mod:
                offenders.append(str(f))
    assert offenders == []
