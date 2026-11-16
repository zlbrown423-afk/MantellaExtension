"""Fight bonds (combat/bonds.py): fighting side by side moves relationships a little, slowly, only upward."""
import json

import pytest

from livingworld.combat import bonds
from livingworld.perception.pipeline import PerceptionPipeline
from livingworld.store.world_store import WorldStore


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("MANTELLA_LW_GUARD", "0")
    monkeypatch.delenv("MANTELLA_LW_BONDS", raising=False)
    monkeypatch.delenv("MANTELLA_LW_COMBAT", raising=False)


@pytest.fixture
def pipe():
    return PerceptionPipeline(WorldStore(":memory:"))


def rec(key, ref, name, hp, gt, player=False, loc="Whiterun"):
    return f"v=1|key={key}|type=fight|gt={gt}|loc={loc}|actor={ref}^{ref + 1000}^^^{int(player)}^{name}|hp={hp}"


def fight(tag, gt, brand_hp=0.2):
    return [rec(tag + "1", 14, "Zachary", 0.9, gt, True), rec(tag + "2", 21, "Kolbeinn", 0.9, gt),
            rec(tag + "3", 22, "Brand", brand_hp, gt)]


def nid(pipe, name):
    return pipe.store.scalar("SELECT npc_id FROM npc WHERE display_name = ?", (name,))


def changes(pipe, a, b):
    return [json.loads(r["deltas_json"]) for r in pipe.store.query(
        "SELECT deltas_json FROM relationship_change WHERE from_id = ? AND to_id = ? AND reason = ? ORDER BY change_id",
        (nid(pipe, a), nid(pipe, b), bonds.REASON))]


def test_everyone_in_the_fight_sees_everyone_else_a_little_differently(pipe):
    pipe.ingest(fight("a", 5.0))
    for a in ("Zachary", "Kolbeinn", "Brand"):
        for b in ("Zachary", "Kolbeinn", "Brand"):
            if a != b:
                (d,) = changes(pipe, a, b)
                assert d["familiarity"] > 0 and d["trust"] > 0 and all(v >= 0 for v in d.values())


def test_staying_in_it_badly_hurt_earns_more_respect(pipe):
    pipe.ingest(fight("a", 5.0))
    (toward_brand,) = changes(pipe, "Kolbeinn", "Brand")
    (toward_kolbeinn,) = changes(pipe, "Brand", "Kolbeinn")
    assert toward_brand["respect"] > toward_kolbeinn["respect"]


def test_each_further_fight_counts_for_less(pipe):
    for i in range(3):
        pipe.ingest(fight(f"f{i}", 5.0 + i, brand_hp=0.9))
    ds = changes(pipe, "Kolbeinn", "Brand")
    assert len(ds) == 3 and ds[0]["trust"] > ds[1]["trust"] > ds[2]["trust"] > 0


def test_the_same_fight_sent_twice_changes_nothing_more(pipe):
    pipe.ingest(fight("a", 5.0))
    pipe.ingest(fight("a", 5.0))
    assert len(changes(pipe, "Kolbeinn", "Brand")) == 1


def test_the_change_points_at_the_fights_event_and_memory(pipe):
    pipe.ingest(fight("a", 5.0))
    r = pipe.store.query("SELECT * FROM relationship_change WHERE reason = ? AND from_id = ? LIMIT 1",
                         (bonds.REASON, nid(pipe, "Kolbeinn")))[0]
    assert r["cause_event_id"] is not None and r["cause_mem_id"] is not None


def test_switched_off_leaves_relationships_alone(pipe, monkeypatch):
    monkeypatch.setenv("MANTELLA_LW_BONDS", "0")
    pipe.ingest(fight("a", 5.0))
    assert pipe.store.query("SELECT * FROM relationship_change") == []
    assert len(pipe.store.query("SELECT * FROM memory")) == 2


def test_a_failure_does_not_lose_the_fight(pipe, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(bonds, "apply", boom)
    pipe.ingest(fight("a", 5.0))
    assert len(pipe.store.query("SELECT * FROM world_event WHERE type = 'combat.fight'")) == 1
    assert len(pipe.store.query("SELECT * FROM memory")) == 2
