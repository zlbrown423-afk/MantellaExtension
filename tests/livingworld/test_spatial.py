"""Spatial context (livingworld/spatial/): a fight set against the assignment a member had. Real database and
pipeline; the assignment lookup is faked (the guard duty tables are tested elsewhere). Nothing here proves the
game reports the location the way expected: that is in the in-game checklist."""
import pytest

from livingworld.perception.pipeline import PerceptionPipeline
from livingworld.spatial import geo, service as SP
from livingworld.store.world_store import WorldStore

ROAD = SP.Assignment(7, 0, "drive off the bandits on the road to Riverwood", ("Riverwood", "Riverwood Road"), "fight", True)
CAMP = SP.Assignment(8, 0, "clear out the bandits at Halted Stream Camp", ("Halted Stream Camp",), "fight", False)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("MANTELLA_LW_GUARD", "0")
    monkeypatch.delenv("MANTELLA_LW_SPATIAL", raising=False)
    monkeypatch.delenv("MANTELLA_LW_COMBAT", raising=False)


@pytest.fixture
def pipe():
    return PerceptionPipeline(WorldStore(":memory:"))


def rec(key, ref, name, hp, loc, player=False, gt=5.0):
    return f"v=1|key={key}|type=fight|gt={gt}|loc={loc}|actor={ref}^{ref + 1000}^^^{int(player)}^{name}|hp={hp}"


def fight(loc, gt=5.0, tag="a"):
    return [rec(tag + "1", 14, "Zachary", 0.9, loc, True, gt), rec(tag + "2", 21, "Kolbeinn", 0.9, loc, gt=gt),
            rec(tag + "3", 22, "Brand", 0.5, loc, gt=gt)]


def npc_id(pipe, name):
    return pipe.store.scalar("SELECT npc_id FROM npc WHERE display_name = ?", (name,))


def assign(pipe, monkeypatch, table):
    """table: member name -> Assignment (giver_id filled from the 'giver' npc)."""
    def lookup(db, member):
        name = db.execute("SELECT display_name FROM npc WHERE npc_id = ?", (member,)).fetchone()[0]
        a = table.get(name)
        return None if a is None else SP.Assignment(a.task_id, db.execute(
            "SELECT npc_id FROM npc WHERE display_name = 'Caius'").fetchone()[0], a.title, a.where, a.need, a.open_country)
    monkeypatch.setattr(SP, "guard_assignment", lookup)


def make_giver(pipe):
    pipe.ingest([rec("g1", 30, "Caius", 1.0, "Whiterun", gt=1.0)])   # makes him an npc
    return npc_id(pipe, "Caius")


def reports(pipe):
    return pipe.store.query("SELECT * FROM field_report ORDER BY report_id")


# ---- geo ----

@pytest.mark.parametrize("where,loc,open_c,want", [
    (("Riverwood",), "Riverwood", False, "assigned"),
    (("Halted Stream Camp",), "halted stream camp", False, "assigned"),
    (("Riverwood", "Riverwood Road"), "Skyrim", True, "open_road"),
    (("Halted Stream Camp",), "Skyrim", False, "wilds"),
    (("Halted Stream Camp",), None, False, "wilds"),
    (("Riverwood",), "Chillfurrow Farm", True, "elsewhere"),
    ((), "Whiterun", False, "elsewhere"),
])
def test_relation(where, loc, open_c, want):
    assert geo.relation(where, loc, open_c) == want


# ---- recording ----

def test_a_fight_away_from_the_assigned_place_is_recorded_as_elsewhere(pipe, monkeypatch):
    make_giver(pipe)
    assign(pipe, monkeypatch, {"Kolbeinn": CAMP})
    pipe.ingest(fight("Bleak Falls Barrow"))
    (r,) = reports(pipe)
    assert r["relation"] == "elsewhere" and r["location"] == "Bleak Falls Barrow"
    assert r["assigned"] == "Halted Stream Camp" and r["told_gt"] is None
    assert r["member_id"] == npc_id(pipe, "Kolbeinn")


def test_members_without_an_assignment_and_the_player_leave_no_report(pipe, monkeypatch):
    make_giver(pipe)
    assign(pipe, monkeypatch, {"Kolbeinn": CAMP})
    pipe.ingest(fight("Whiterun"))
    assert [r["member_id"] for r in reports(pipe)] == [npc_id(pipe, "Kolbeinn")]


def test_each_member_is_set_against_their_own_assignment(pipe, monkeypatch):
    make_giver(pipe)
    assign(pipe, monkeypatch, {"Kolbeinn": CAMP, "Brand": ROAD})
    pipe.ingest(fight("Halted Stream Camp"))
    rel = {npc_id(pipe, "Kolbeinn"): "assigned", npc_id(pipe, "Brand"): "elsewhere"}
    assert {r["member_id"]: r["relation"] for r in reports(pipe)} == rel


def test_open_country_counts_for_road_work_only(pipe, monkeypatch):
    make_giver(pipe)
    assign(pipe, monkeypatch, {"Kolbeinn": ROAD, "Brand": CAMP})
    pipe.ingest(fight("Skyrim"))
    rel = {r["member_id"]: r["relation"] for r in reports(pipe)}
    assert rel == {npc_id(pipe, "Kolbeinn"): "open_road", npc_id(pipe, "Brand"): "wilds"}


def test_the_same_fight_twice_gives_one_report(pipe, monkeypatch):
    make_giver(pipe)
    assign(pipe, monkeypatch, {"Kolbeinn": CAMP})
    pipe.ingest(fight("Bleak Falls Barrow"))
    pipe.ingest(fight("Bleak Falls Barrow"))
    assert len(reports(pipe)) == 1


def test_a_failing_lookup_does_not_lose_the_fight_or_its_memories(pipe, monkeypatch):
    make_giver(pipe)
    def boom(db, member):
        raise RuntimeError("no duty tables")
    monkeypatch.setattr(SP, "guard_assignment", boom)
    pipe.ingest(fight("Whiterun"))
    assert len(pipe.store.query("SELECT * FROM world_event WHERE type = 'combat.fight'")) == 1
    assert len(pipe.store.query("SELECT * FROM memory")) == 2
    assert reports(pipe) == []


def test_switched_off_records_nothing(pipe, monkeypatch):
    monkeypatch.setenv("MANTELLA_LW_SPATIAL", "0")
    make_giver(pipe)
    assign(pipe, monkeypatch, {"Kolbeinn": CAMP})
    pipe.ingest(fight("Bleak Falls Barrow"))
    assert reports(pipe) == []


# ---- telling the giver ----

def tell(pipe, giver_id, now=6.0):
    with pipe.store.transaction() as db:
        return SP.notes_for(db, giver_id, now)


def test_the_giver_is_told_it_was_not_the_place_and_only_once(pipe, monkeypatch):
    caius = make_giver(pipe)
    assign(pipe, monkeypatch, {"Kolbeinn": CAMP, "Brand": CAMP})
    pipe.ingest(fight("Bleak Falls Barrow"))
    (note,) = tell(pipe, caius)
    assert "Bleak Falls Barrow" in note and "Halted Stream Camp" in note and "not that place" in note
    assert "Kolbeinn" in note and "Brand" in note            # two members, one note
    assert tell(pipe, caius) == []


def test_a_fight_at_the_assigned_place_does_not_claim_success(pipe, monkeypatch):
    caius = make_giver(pipe)
    assign(pipe, monkeypatch, {"Kolbeinn": CAMP})
    pipe.ingest(fight("Halted Stream Camp"))
    (note,) = tell(pipe, caius)
    assert "That is the place you meant" in note and "for them to say" in note
    assert "dealt with" in note and "bandits are dead" not in note


def test_open_road_is_hedged(pipe, monkeypatch):
    caius = make_giver(pipe)
    assign(pipe, monkeypatch, {"Kolbeinn": ROAD})
    pipe.ingest(fight("Skyrim"))
    (note,) = tell(pipe, caius)
    assert "nobody said so" in note


def test_other_npcs_are_not_told(pipe, monkeypatch):
    make_giver(pipe)
    assign(pipe, monkeypatch, {"Kolbeinn": CAMP})
    pipe.ingest(fight("Bleak Falls Barrow"))
    assert tell(pipe, npc_id(pipe, "Brand")) == []


def test_no_more_than_the_limit_in_one_note_and_the_rest_wait_or_are_marked(pipe, monkeypatch):
    caius = make_giver(pipe)
    assign(pipe, monkeypatch, {"Kolbeinn": CAMP})
    for i, place in enumerate(["Place A", "Place B", "Place C", "Place D", "Place E"]):
        pipe.ingest(fight(place, gt=5.0 + i, tag=f"t{i}"))
    assert len(tell(pipe, caius)) == 3
