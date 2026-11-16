"""The context growth benchmark (livingworld/perf/context_bench.py): its synthetic worlds are read back by the
real recall code, and measuring a real world never touches the original file."""
from __future__ import annotations

import hashlib
import pathlib

from livingworld.memory.recall import recall_for
from livingworld.perf import context_bench as bench
from livingworld.store.world_store import WorldStore


def _world(tmp_path, scale=0.3):
    store = WorldStore(str(tmp_path / "w.sqlite"))
    with store.transaction() as db:
        player = bench._npc(db, "Torvar", 0x7, 1.0, player=True)
        group = [bench._npc(db, name, form, 1.0) for name, form in (("Gerdur", 0x1A675), ("Hod", 0x1A677))]
    now = bench.grow(store, group, player, scale) + 0.05
    return store, player, group, now


def test_synthetic_history_is_what_recall_reads(tmp_path):
    store, player, group, now = _world(tmp_path)
    recall = recall_for(store, group[0], now, player, mark_rehearsed=False, about_ids=[player] + group)
    assert recall.memories and recall.facts and recall.heard and recall.seen
    assert recall.last_spoke
    # the history sits on a chain of save-load timelines, all visible from the active one
    assert store.scalar("SELECT COUNT(*) FROM v_timeline_scope") > 2
    store.close()


def test_history_grows_with_the_scale(tmp_path):
    small, *_ = _world(tmp_path / "a", 0.2)
    large, *_ = _world(tmp_path / "b", 1.0)
    a, b = bench.row_counts(small), bench.row_counts(large)
    assert b["memory"] > 3 * a["memory"] and b["relationship_change"] > 3 * a["relationship_change"]
    small.close()
    large.close()


def test_measuring_a_real_world_reads_a_copy_only(tmp_path):
    store, *_ = _world(tmp_path, 0.2)
    store.close()
    original = pathlib.Path(store.path)
    before = hashlib.sha256(original.read_bytes()).hexdigest()
    work = tmp_path / "work"
    work.mkdir()
    result = bench.real_world_copy(str(original), str(work), reps=2)
    assert result.recall and all(t.statements > 0 for t in result.recall.values())
    assert hashlib.sha256(original.read_bytes()).hexdigest() == before


def test_timing_counts_statements_and_restores_the_connection(tmp_path):
    store, player, group, now = _world(tmp_path, 0.2)
    t = bench.timed(lambda: store.query("SELECT 1"), 3, store)
    assert t.statements == 1
    with bench.section_timer() as parts:
        recall_for(store, group[0], now, player, mark_rehearsed=False, about_ids=[player] + group)
    assert "memories (pick)" in parts and "opinion of player (v_relationship)" in parts
    from livingworld.memory import recall as R
    assert R._pick_memories.__name__ == "_pick_memories"   # wrappers removed
    store.close()


def test_the_server_never_imports_the_benchmark():
    root = pathlib.Path(__file__).resolve().parents[2]
    for path in list((root / "livingworld").rglob("*.py")) + list((root / "src").rglob("*.py")):
        if path.name == "context_bench.py":
            continue
        assert "context_bench" not in path.read_text(encoding="utf-8", errors="ignore"), path
