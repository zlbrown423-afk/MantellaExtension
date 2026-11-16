"""The active timeline's scope is kept in a table (migration 022) and must always match the recursive walk."""
import random

from livingworld.store.timeline import resolve_load
from livingworld.store.world_store import WorldStore


def scope(db):
    return sorted(tuple(r) for r in db.execute("SELECT timeline_id, cutoff_gt FROM v_timeline_scope"))


def walk(db):
    return sorted(tuple(r) for r in db.execute("SELECT timeline_id, cutoff_gt FROM v_timeline_scope_walk"))



def test_scope_table_matches_the_walk_through_many_save_loads():
    store = WorldStore(":memory:")
    rnd = random.Random(7)
    with store.transaction() as db:
        assert scope(db) == walk(db) == [(1, 1e18)]
    gt, nonce, seen, longest = 1.0, 0, [1], 0
    for _ in range(60):
        gt += rnd.uniform(0.05, 1.0)
        nonce += 1
        # write something late on the active timeline so a load from earlier branches
        with store.transaction() as db:
            active = db.execute("SELECT timeline_id FROM timeline WHERE is_active = 1").fetchone()[0]
            db.execute("INSERT INTO hygiene_change (timeline_id, npc_id, game_time, cause) VALUES (?,?,?, 'fight')",
                       (active, player(db), gt + 0.5))
        save = active if rnd.random() < 0.8 else rnd.choice(seen)   # mostly reloads on the line being played
        result = resolve_load(store, nonce, save, gt - rnd.uniform(0, 0.4))
        seen.append(result.timeline_id)
        with store.transaction() as db:
            assert scope(db) == walk(db)
            longest = max(longest, len(scope(db)))
    assert longest > 10      # a real chain was built


def test_scope_table_follows_direct_timeline_writes():
    store = WorldStore(":memory:")
    with store.transaction() as db:
        db.execute("UPDATE timeline SET is_active = 0")
        assert scope(db) == walk(db) == []
        t2 = db.execute("INSERT INTO timeline (parent_id, branch_game_time, is_active) VALUES (1, 4.0, 1)").lastrowid
        assert scope(db) == walk(db) == [(1, 4.0), (t2, 1e18)]
        t3 = db.execute("INSERT INTO timeline (parent_id, branch_game_time, is_active) VALUES (?, 6.0, 0)",
                        (t2,)).lastrowid
        assert scope(db) == [(1, 4.0), (t2, 1e18)]     # an inactive branch changes nothing
        db.execute("UPDATE timeline SET is_active = 0 WHERE timeline_id = ?", (t2,))
        db.execute("UPDATE timeline SET is_active = 1 WHERE timeline_id = ?", (t3,))
        assert scope(db) == walk(db) == [(1, 4.0), (t2, 6.0), (t3, 1e18)]


def test_memory_reads_use_the_scope_table_index():
    store = WorldStore(":memory:")
    with store.transaction() as db:
        plan = " ".join(r[-1] for r in db.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM v_visible_memory WHERE owner_id = 1"))
    assert "timeline_scope" in plan and "CO-ROUTINE" not in plan and "anc" not in plan


def player(db):
    row = db.execute("SELECT npc_id FROM npc WHERE is_player = 1").fetchone()
    if row:
        return row[0]
    return db.execute("INSERT INTO npc (plugin, base_form_id, display_name, is_player, created_gt)"
                      " VALUES ('Skyrim.esm', '0x00000007', 'Player', 1, 1.0)").lastrowid
