"""Continuity (livingworld/continuity/): shared history and place memories, over a real database and the real
fight pipeline. The integration hooks are covered by the in-game checklist."""
import pytest

from livingworld.continuity import service as C
from livingworld.perception.pipeline import PerceptionPipeline
from livingworld.store.world_store import WorldStore


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("MANTELLA_LW_GUARD", "0")


@pytest.fixture
def pipe():
    return PerceptionPipeline(WorldStore(":memory:"))


def rec(key, ref, name, hp, gt, loc, player=False):
    return f"v=1|key={key}|type=fight|gt={gt}|loc={loc}|actor={ref}^{ref + 1000}^^^{int(player)}^{name}|hp={hp}"


def fight(tag, gt, loc="Whiterun", with_brand=True):
    r = [rec(tag + "1", 14, "Zachary", 0.9, gt, loc, True), rec(tag + "2", 21, "Kolbeinn", 0.9, gt, loc)]
    if with_brand:
        r.append(rec(tag + "3", 22, "Brand", 0.5, gt, loc))
    return r


def nid(pipe, name):
    return pipe.store.scalar("SELECT npc_id FROM npc WHERE display_name = ?", (name,))


def history(pipe, who, others, now=10.0):
    with pipe.store.reading() as db:
        return C.shared_history(db, nid(pipe, who), {nid(pipe, n): n for n in others}, now)


def test_a_patrol_companion_is_recognised_with_how_many_times_and_where(pipe):
    pipe.ingest(fight("a", 5.0, "Bleak Falls Barrow"))
    pipe.ingest(fight("b", 8.0, "Whiterun"))
    (line,) = history(pipe, "Kolbeinn", ["Zachary"])
    assert "fought beside Zachary 2 times" in line and "near Whiterun" in line
    assert "not who the enemies were" in line          # nothing invented about the fight


def test_someone_who_was_not_in_the_fight_is_not_a_companion(pipe):
    pipe.ingest(fight("a", 5.0, with_brand=False))
    pipe.ingest([rec("x", 40, "Hrolf", 1.0, 9.0, "Whiterun")])   # Hrolf exists, never fought beside Kolbeinn
    assert history(pipe, "Kolbeinn", ["Hrolf"]) == []
    assert history(pipe, "Brand", ["Zachary"]) == []


def test_only_people_in_the_conversation_get_a_line_and_never_the_npc_themselves(pipe):
    pipe.ingest(fight("a", 5.0))
    lines = history(pipe, "Kolbeinn", ["Kolbeinn", "Brand"])
    assert len(lines) == 1 and "Brand" in lines[0]


def test_a_fight_in_the_future_of_the_current_time_is_not_known_yet(pipe):
    pipe.ingest(fight("a", 20.0))
    assert history(pipe, "Kolbeinn", ["Zachary"], now=10.0) == []


def put_memory(pipe, owner, content, loc, gt, importance=0.5):
    with pipe.store.transaction() as db:
        timeline = db.execute("SELECT timeline_id FROM timeline WHERE is_active = 1").fetchone()[0]
        return db.execute(
            "INSERT INTO memory (timeline_id, owner_id, kind, content, about_ids_json, location, occurred_gt,"
            " importance, valence, intensity, created_by, source_kind, confidence)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (timeline, owner, "episodic", content, "[]", loc, gt, importance, 0.0, 0.3, "perception", "experienced",
             0.9)).lastrowid


def topic(pipe, who, places, skip=(), now=10.0):
    with pipe.store.reading() as db:
        return C.topic_lines(db, nid(pipe, who), places, set(skip), now)


def test_a_place_the_player_names_brings_up_that_npcs_memory_of_it(pipe):
    pipe.ingest(fight("a", 5.0))
    m = put_memory(pipe, nid(pipe, "Kolbeinn"), "The player told me about the ruined watchtower at Rorikstead.", None, 6.0)
    put_memory(pipe, nid(pipe, "Kolbeinn"), "I bought a sweetroll.", None, 7.0)
    lines, ids = topic(pipe, "Kolbeinn", ["Rorikstead"])
    assert ids == [m] and "watchtower" in lines[0]


def test_a_memory_the_prompt_already_holds_is_not_repeated(pipe):
    pipe.ingest(fight("a", 5.0))
    m = put_memory(pipe, nid(pipe, "Kolbeinn"), "We camped at Rorikstead.", "Rorikstead", 6.0)
    assert topic(pipe, "Kolbeinn", ["Rorikstead"], skip=[m]) == ([], [])


def test_other_npcs_memories_never_leak(pipe):
    pipe.ingest(fight("a", 5.0))
    put_memory(pipe, nid(pipe, "Brand"), "I was robbed at Rorikstead.", "Rorikstead", 6.0)
    assert topic(pipe, "Kolbeinn", ["Rorikstead"]) == ([], [])


def test_at_most_two_place_memories_a_turn(pipe):
    pipe.ingest(fight("a", 5.0))
    for i in range(5):
        put_memory(pipe, nid(pipe, "Kolbeinn"), f"Visit {i} to Rorikstead.", "Rorikstead", 5.0 + i * 0.1)
    assert len(topic(pipe, "Kolbeinn", ["Rorikstead"])[0]) == 2
