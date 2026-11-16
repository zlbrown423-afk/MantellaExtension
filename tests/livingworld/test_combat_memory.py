"""Shared fights (livingworld/combat/): the game's ``fight`` records of one fight become one event and a
separate first-person memory for each NPC who fought beside the player. These tests feed records through the real
perception pipeline and database. They do not prove that the game sends these records the way expected:
that is in the in-game checklist.
"""
import json

import pytest

from livingworld.combat import fights as F
from livingworld.perception.pipeline import PerceptionPipeline
from livingworld.perception.records import parse_record
from livingworld.store.world_store import WorldStore


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setenv("MANTELLA_LW_GUARD", "0")      # no guard-duty side effects: this file is about memories
    monkeypatch.delenv("MANTELLA_LW_COMBAT", raising=False)


@pytest.fixture
def pipe():
    return PerceptionPipeline(WorldStore(":memory:"))


def rec(key, ref, name, hp, gt=5.0, loc="Whiterun", player=False):
    return f"v=1|key={key}|type=fight|gt={gt}|loc={loc}|actor={ref}^{ref + 1000}^^^{int(player)}^{name}|hp={hp}"


def patrol_fight(gt=5.0, loc="the northern camp"):
    return [rec("f1", 14, "Zachary", 0.9, gt, loc, player=True), rec("f2", 21, "Kolbeinn", 0.95, gt, loc),
            rec("f3", 22, "Brand", 0.2, gt, loc), rec("f4", 23, "Sigurd", 0.7, gt, loc)]


def memories(pipe):
    return {r["name"]: r["content"] for r in pipe.store.query(
        "SELECT n.display_name AS name, m.content FROM memory m JOIN npc n ON n.npc_id = m.owner_id")}


def test_a_fight_becomes_one_event_and_each_npc_remembers_it_their_own_way(pipe):
    pipe.ingest(patrol_fight())
    events = pipe.store.query("SELECT * FROM world_event WHERE type = 'combat.fight'")
    assert len(events) == 1
    payload = json.loads(events[0]["payload_json"])
    assert payload["foes"] == "unknown" and payload["outcome"] == "unknown"
    assert len(payload["participants"]) == 4
    mem = memories(pipe)
    assert set(mem) == {"Kolbeinn", "Brand", "Sigurd"}      # the player keeps their own memories
    assert "without a scratch" in mem["Kolbeinn"]
    assert "nearly killed" in mem["Brand"]
    assert "a few knocks" in mem["Sigurd"]
    assert "Brand" not in mem["Brand"] and "Kolbeinn" in mem["Brand"] and "Zachary" in mem["Brand"]
    assert "near the northern camp" in mem["Sigurd"]


def test_memories_never_claim_what_the_game_did_not_say(pipe):
    pipe.ingest(patrol_fight())
    for text in memories(pipe).values():
        low = text.lower()
        assert not any(word in low for word in ("bandit", "vampire", "won", "victory", "killed them", "we beat", "died"))


def test_the_player_alone_or_npcs_without_the_player_make_no_shared_fight(pipe):
    pipe.ingest([rec("a", 14, "Zachary", 0.5, player=True)])
    pipe.ingest([rec("b", 21, "Kolbeinn", 0.5, gt=9.0)])
    assert pipe.store.scalar("SELECT COUNT(*) FROM world_event WHERE type = 'combat.fight'") == 0
    assert pipe.store.scalar("SELECT COUNT(*) FROM memory") == 0


def test_the_same_fight_sent_twice_is_stored_once(pipe):
    pipe.ingest(patrol_fight())
    pipe.ingest(patrol_fight())
    assert pipe.store.scalar("SELECT COUNT(*) FROM world_event WHERE type = 'combat.fight'") == 1
    assert pipe.store.scalar("SELECT COUNT(*) FROM memory") == 3


def test_two_fights_in_different_places_or_times_stay_separate(pipe):
    pipe.ingest(patrol_fight(gt=5.0, loc="the northern camp"))
    pipe.ingest([rec("g1", 14, "Zachary", 0.8, 6.0, "the cave", player=True), rec("g2", 21, "Kolbeinn", 0.9, 6.0, "the cave")])
    assert pipe.store.scalar("SELECT COUNT(*) FROM world_event WHERE type = 'combat.fight'") == 2
    kolbeinn = pipe.store.query("SELECT content FROM memory m JOIN npc n ON n.npc_id = m.owner_id"
                                " WHERE n.display_name = 'Kolbeinn' ORDER BY m.mem_id")
    assert len(kolbeinn) == 2 and "the cave" in kolbeinn[1]["content"]


def test_hygiene_still_sees_every_fight_record(pipe):
    pipe.ingest(patrol_fight())
    assert pipe.store.scalar("SELECT COUNT(*) FROM npc") == 4


def test_a_squad_member_remembers_the_patrol(pipe):
    from livingworld.squad import service as SQ
    pipe.ingest([rec("s0", 14, "Zachary", 1.0, 1.0, player=True)])   # registers the player
    with pipe.store.transaction() as db:
        player = db.execute("SELECT npc_id FROM npc WHERE is_player = 1").fetchone()[0]
    pipe.ingest(patrol_fight())
    with pipe.store.transaction() as db:
        brand = db.execute("SELECT npc_id FROM npc WHERE display_name = 'Brand'").fetchone()[0]
        SQ.ask(db, player, [brand], 6.0)
        SQ.answer(db, brand, True, 6.1)
    pipe.ingest([rec("h1", 14, "Zachary", 0.9, 8.0, "the cave", player=True), rec("h2", 22, "Brand", 0.5, 8.0, "the cave")])
    last = pipe.store.query("SELECT content FROM memory WHERE owner_id = ? ORDER BY mem_id DESC LIMIT 1", (brand,))[0]["content"]
    assert "out on patrol with Zachary" in last


def test_combat_memory_can_be_switched_off(pipe, monkeypatch):
    monkeypatch.setenv("MANTELLA_LW_COMBAT", "0")
    pipe.ingest(patrol_fight())
    assert pipe.store.scalar("SELECT COUNT(*) FROM world_event WHERE type = 'combat.fight'") == 0


def test_grouping_rules():
    records = [parse_record(r) for r in [rec("a", 1, "A", 1, 5.0, "X"), rec("b", 2, "B", 1, 5.004, "X"),
                                         rec("c", 3, "C", 1, 5.004, "Y"), rec("d", 4, "D", 1, 5.2, "X")]]
    groups = [[r.key for r in g] for g in F.group(records)]
    assert groups == [["a", "b"], ["c"], ["d"]]


@pytest.mark.parametrize("hp,phrase", [(1.0, "scratch"), (0.7, "knocks"), (0.4, "hurt badly"), (0.1, "nearly killed")])
def test_wound_bands(hp, phrase):
    assert phrase in F.wound(hp)
    assert F.wound(None) is None


def test_memory_shape():
    solo = F.memory_for(None, [], None, None)
    assert solo["content"] == "I was in a fight." and 0.3 <= solo["importance"] <= 0.85
    hurt = F.memory_for(0.1, ["A", "B", "C"], "Whiterun", "Zachary")
    calm = F.memory_for(1.0, ["A", "B", "C"], "Whiterun", None)
    assert hurt["importance"] > calm["importance"] and hurt["valence"] < 0 < calm["valence"]
    assert hurt["intensity"] > calm["intensity"]
    assert "A, B and C" in hurt["content"] and "on patrol with Zachary" in hurt["content"]
