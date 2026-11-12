"""Planted memories (livingworld/memory/plant.py): an NPC is told by hand what the game did that Living World never
saw (2026-11-12: Fralia Gray-Mane should know the player freed Thorald and got him away safely, and feel for him)."""
import json

import pytest

from livingworld.memory import plant
from livingworld.memory.recall import opinion_line
from livingworld.store.timeline import resolve_load
from livingworld.store.world_store import WorldStore


@pytest.fixture
def world(tmp_path):
    store = WorldStore(str(tmp_path / "data" / "livingworld" / "Hero1.sqlite"))
    with store.transaction() as db:
        for npc_id, name, ref, player in ((1, "Hero", "Skyrim.esm|0x00000014", 1),
                                          (71, "Fralia Gray-Mane", "Skyrim.esm|0x0001A684", 0),
                                          (92, "Thorald Gray-Mane", "Skyrim.esm|0x00039C8D", 0)):
            db.execute("INSERT INTO npc (npc_id, plugin, base_form_id, ref_form_id, display_name, is_player, created_gt)"
                       " VALUES (?, 'Skyrim.esm', '0x0', ?, ?, ?, 1.0)", (npc_id, ref, name, player))
        db.execute("INSERT INTO relationship (from_id, to_id, status, updated_gt) VALUES (71, 1, 'acquaintance', 1.0)")
        # a run of grabs left her wary: the raw sum is past -1, the view shows -1
        for gt in (2.0, 2.1, 2.2):
            db.execute("INSERT INTO relationship_change (timeline_id, from_id, to_id, deltas_json, reason, game_time)"
                       " VALUES (1, 71, 1, '{\"trust\": -0.5, \"fear\": 0.2}', 'grabbed', ?)", (gt,))
            db.execute("INSERT INTO standing_change (timeline_id, holder_id, subject_id, domain, delta, firsthand,"
                       " reason, game_time) VALUES (1, 71, 1, 'decency', -0.4, 1, 'saw it', ?)", (gt,))
        from livingworld.social import reputation
        reputation.review(db, 1, 71, 1, 2.2)
        db.execute("INSERT INTO memory (timeline_id, owner_id, kind, content, occurred_gt, importance, created_by)"
                   " VALUES (1, 71, 'episodic', 'I worry for Thorald.', 3.0, 0.5, 'extraction')")
        db.execute("INSERT INTO knowledge (timeline_id, holder_id, subject_id, predicate, value, origin, learned_gt)"
                   " VALUES (1, 71, 71, 'son_status', 'missing', 'self', 3.0)")
    yield store
    store.close()


SPEC = {
    "npc": "Fralia Gray-Mane",
    "memories": [{"content": "The Hero freed my son Thorald.", "importance": 1.0, "valence": 0.9,
                  "about": ["player", "Thorald Gray-Mane"]}],
    "feelings": {"affinity": 0.85, "trust": 0.7, "attraction": 0.85, "fear": 0.0},
    "standing": {"reliability": 0.6, "decency": 0.0},
    "facts": [{"predicate": "son_status", "value": "safe"}],
    "summary": "Fralia now knows Thorald is safe.",
}


def feelings(store):
    return dict(store.query("SELECT * FROM v_relationship WHERE from_id = 71 AND to_id = 1")[0])


def test_plant_reaches_the_targets_and_running_again_changes_nothing(world):
    assert feelings(world)["trust"] == -1.0
    r = plant.plant(world, SPEC)
    assert r.at == pytest.approx(3.0 + plant.EPSILON_DAYS)   # just after her newest memory
    f = feelings(world)
    assert (f["affinity"], f["trust"], f["attraction"], f["fear"]) == pytest.approx((0.85, 0.7, 0.85, 0.0))
    mem = world.query("SELECT * FROM memory WHERE created_by = 'admin'")
    assert len(mem) == 1 and json.loads(mem[0]["about_ids_json"]) == [1, 92]
    beliefs = [r[0] for r in world.query("SELECT statement FROM v_belief WHERE holder_id = 71 AND statement <> ''")]
    assert beliefs == ["Hero is someone you can count on"]          # "has no decency" dropped
    facts = [tuple(r) for r in world.query("SELECT predicate, value FROM v_known_facts WHERE holder_id = 71")]
    assert facts == [("son_status", "safe")]

    again = plant.plant(world, SPEC)
    assert again.memories_added == [] and again.deltas == {} and again.standing_deltas == {}
    assert again.facts_added == [] and again.facts_dropped == []
    assert world.scalar("SELECT COUNT(*) FROM memory WHERE created_by = 'admin'") == 1


def test_an_older_save_does_not_see_it_and_a_later_one_does(world):
    plant.plant(world, dict(SPEC, at=5.0))
    resolve_load(world, 1, 1, 4.0)          # a save from before day 5
    assert world.scalar("SELECT COUNT(*) FROM v_visible_memory WHERE created_by = 'admin'") == 0
    assert feelings(world)["attraction"] == 0.0
    resolve_load(world, 2, 1, 6.0)          # the save it was made on, or a later one
    assert world.scalar("SELECT COUNT(*) FROM v_visible_memory WHERE created_by = 'admin'") == 1
    assert feelings(world)["attraction"] == pytest.approx(0.85)


def test_planted_on_the_oldest_timeline_that_holds_the_moment(world):
    resolve_load(world, 1, 1, 2.5)          # branch 2 from day 2.5; 'I worry' at day 3 is not on it
    with world.transaction() as db:
        db.execute("INSERT INTO memory (timeline_id, owner_id, kind, content, occurred_gt, importance, created_by)"
                   " VALUES (2, 71, 'episodic', 'Market day.', 2.6, 0.2, 'extraction')")
    resolve_load(world, 2, 2, 2.7)          # branch 3 from 2 at 2.7
    r = plant.plant(world, dict(SPEC, at=2.55))
    assert r.timeline_id == 2
    r = plant.plant(world, dict(SPEC, at=2.0, memories=[{"content": "Earlier."}]))
    assert r.timeline_id == 1


def test_summary_paragraph_is_added_once_and_noted_for_rollback(world, tmp_path):
    folder = tmp_path / "conversations" / "Hero1" / "Fralia Gray-Mane - 01A684"
    folder.mkdir(parents=True)
    (folder / "Fralia Gray-Mane_summary_1.txt").write_text("[Day 2]\nShe worried for Thorald.\n", encoding="utf-8")
    r = plant.plant(world, SPEC, str(tmp_path / "conversations"), "Hero1")
    assert r.summary_added
    text = (folder / "Fralia Gray-Mane_summary_1.txt").read_text(encoding="utf-8")
    assert text.endswith("She worried for Thorald.\n\nFralia now knows Thorald is safe.\n")
    assert world.scalar("SELECT folder FROM summary_line") == "Hero1/Fralia Gray-Mane - 01A684"
    plant.plant(world, SPEC, str(tmp_path / "conversations"), "Hero1")
    assert (folder / "Fralia Gray-Mane_summary_1.txt").read_text(encoding="utf-8") == text


def test_dry_run_changes_nothing(world, tmp_path):
    with pytest.raises(plant._DryRun) as dry:
        plant.plant(world, SPEC, dry_run=True)
    assert dry.value.result.memories_added
    assert world.scalar("SELECT COUNT(*) FROM memory WHERE created_by = 'admin'") == 0
    assert feelings(world)["attraction"] == 0.0


def test_npc_by_ref_and_unknown_names(world):
    with world.reading() as db:
        assert plant.find_npc(db, "01A684")[0] == 71
        assert plant.find_npc(db, "fralia gray-mane")[0] == 71
        with pytest.raises(SystemExit):
            plant.find_npc(db, "Nobody")
    with pytest.raises(SystemExit):
        plant.plant(world, dict(SPEC, feelings={"love": 1.0}))


def test_opinion_line_names_attraction_and_keeps_consent(world):
    plant.plant(world, SPEC)
    line = opinion_line(world, 71, 1)
    assert "strongly attracted" in line and "Wanting someone is not a yes" in line
    with world.transaction() as db:
        db.execute("INSERT INTO relationship_change (timeline_id, from_id, to_id, deltas_json, reason, game_time)"
                   " VALUES (1, 71, 1, '{\"attraction\": -0.5}', 'cooled', 3.5)")
    assert "I find them attractive" in opinion_line(world, 71, 1)
