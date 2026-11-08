"""Perception prototype: the player grabs Gerdur's arm while Hod watches, and drinks an ale.

The game side is simulated with records built exactly as the Living World MantellaListenerScript
writes them (``livingworld/harness/perception.py``). Everything after the record arrives is the real
code path: Mantella's /mantella route, the store, recall into each NPC's own prompt.
"""
import json
import sqlite3
from livingworld.store.world_store import _migration_files

LATEST = max(v for v, _ in _migration_files())

import pytest

from livingworld.harness import actors
from livingworld.harness.perception import KEY_EVENTS, consume, grab, seen
from livingworld.harness.scripted_llm import ScriptedLLM
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, KEY_LOAD_NONCE, KEY_LOADED_GAME_DAYS, KEY_TIMELINE_ID, LivingWorld
from livingworld.perception.formation import Bond, grab_for_target
from livingworld.perception.records import BadRecord, body_part, grab_area, item_category, parse_record
from livingworld.store.world_store import WorldStore

FOREARM = "NPC L Forearm [LLar]"


@pytest.fixture(autouse=True)
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.setenv("MANTELLA_LW_TASKS", "0")   # these tests count every world event; tasks add their own
    monkeypatch.setattr(LivingWorld, "start_workers", False)


def open_db(work_dir) -> sqlite3.Connection:
    conn = sqlite3.connect(work_dir / "data" / "livingworld" / "HeadlessTest.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def alvor():
    # placeholder form ids: only needs to be a third, distinct NPC
    return actors.actor("Alvor", 0x00013480, 0x0001348D, 0, "Nord", "MaleNord")


PLAYER = actors.player("Torvar")


def grab_scene(day=3.51, key="g1", hod_los=True):
    """Torvar grabs Gerdur's left forearm for 4.2 s. Hod (her husband, rank 4) stands 4 m away;
    Alvor is 30 m away, too far to see it."""
    return grab(PLAYER, actors.gerdur(), FOREARM, 4.2, day, key=key, witnesses=[
        seen(actors.hod(), 4.0, los=hod_los, rank_to_actor=0, rank_to_target=4),
        seen(alvor(), 30.0, rank_to_actor=0, rank_to_target=1),
    ])


def drink_scene(day=3.52, key="d1"):
    return consume(PLAYER, "Nord Mead", day, key=key,
                   witnesses=[seen(actors.gerdur(), 2.0), seen(actors.hod(), 5.0)])


def talk(work_dir, npcs, day, records=None, llm=None, extra=None):
    llm = llm or ScriptedLLM(replies=["Hm."])
    with HeadlessSession(str(work_dir), llm) as s:
        s.start([PLAYER, *npcs], game_days=day, extra=extra, context_extra={KEY_EVENTS: records or []})
        s.continue_until_player_turn()
        s.end(game_days=day + 0.01)
    return llm


def memories(db, name):
    return db.execute("SELECT m.* FROM memory m JOIN npc n ON n.npc_id = m.owner_id WHERE n.display_name = ?"
                      " AND m.created_by = 'perception' ORDER BY m.mem_id", (name,)).fetchall()


# ---- records -----------------------------------------------------------------------------------

def test_body_part_names_come_from_skeleton_nodes():
    assert body_part("NPC L Forearm [LLar]") == "left forearm"
    assert body_part("NPC R UpperArm [RUar]") == "right upper arm"
    assert body_part("NPC L Clavicle [LClv]") == "left shoulder"
    assert body_part("NPC Head [Head]") == "head"
    # a spine body covers the whole torso, front and back, so the node alone stays vague
    assert body_part("NPC Spine2 [Spn2]") == "upper body"
    assert body_part("NPC Spine1 [Spn1]") == "waist"
    assert "spine" not in body_part("NPC Spine [Spn0]")
    assert body_part("SomeWeirdNode") is None
    assert body_part(None) is None


def test_area_from_hand_position_wins_over_node():
    rec = parse_record(grab(actors.player("Torvar"), actors.gerdur(), node="NPC Spine1 [Spn1]", seconds=2.0,
                            game_days=3.5, area="Left Shoulder"))
    assert rec.area == "left shoulder"
    assert grab_area(rec) == "left shoulder"
    memory = grab_for_target(rec, Bond())
    assert "by my left shoulder" in memory.content and "spine" not in memory.content.lower()


def test_unknown_area_falls_back_to_node():
    rec = parse_record(grab(actors.player("Torvar"), actors.gerdur(), node="NPC Spine2 [Spn2]", seconds=2.0,
                            game_days=3.5, area="kidneys please"))
    assert rec.area is None and grab_area(rec) == "upper body"
    rec = parse_record(grab(actors.player("Torvar"), actors.gerdur(), node="NPC Pelvis [Pelv]", seconds=2.0,
                            game_days=3.5))
    assert rec.area is None and grab_area(rec) == "hips"


def test_sensitive_area_from_position_reads_as_threatening():
    rec = parse_record(grab(actors.player("Torvar"), actors.gerdur(), node="NPC Pelvis [Pelv]", seconds=2.0,
                            game_days=3.5, area="backside"))
    memory = grab_for_target(rec, Bond(status="friend"))
    assert "backside" in memory.content and memory.valence <= -0.5


def test_item_categories_treat_alcohol_separately():
    assert item_category("Nord Mead", True) == "alcohol"
    assert item_category("Alto Wine", True) == "alcohol"
    assert item_category("Black-Briar Mead", True) == "alcohol"
    assert item_category("Jug of Milk", True) == "drink"
    assert item_category("Sweet Roll", True) == "food"
    assert item_category("Potion of Healing", False) == "potion"


def test_record_round_trips_and_bad_records_are_refused():
    record = parse_record(grab_scene())
    assert (record.actor.name, record.target.name, record.duration_s) == ("Torvar", "Gerdur", 4.2)
    assert [(w.actor.name, w.distance_m, w.had_los, w.rank_to_target) for w in record.witnesses] == \
        [("Hod", 4.0, True, 4), ("Alvor", 30.0, True, 1)]
    with pytest.raises(BadRecord):
        parse_record("v=1|key=x|type=grab|gt=1|actor=1^2^^^1^Torvar")   # no target
    with pytest.raises(BadRecord):
        parse_record("v=9|key=x|type=grab")


def test_record_as_papyrus_writes_it():
    # Papyrus prints floats with six decimals and form ids as signed ints (light plugins are negative)
    raw = ("v=1|key=812345-3|type=grab|gt=3.512000|loc=Riverwood|actor=20^7^^^1^Torvar"
           "|target=78985^78972^Skyrim.esm^Skyrim.esm^0^Gerdur|node=NPC L Forearm [LLar]|hand=L|dur=4.213000"
           "|trank=0|w=4.002100^1^0^4^78986^78973^Skyrim.esm^Skyrim.esm^0^Hod"
           "|w=12.500000^0^0^^-33554431^-33554430^^^0^Some Guard")
    record = parse_record(raw)
    assert record.duration_s == pytest.approx(4.213) and record.target.name == "Gerdur"
    assert record.witnesses[0].rank_to_target == 4
    assert record.witnesses[1].rank_to_target is None and not record.witnesses[1].had_los
    assert record.witnesses[1].actor.identity().ref.plugin.startswith("light:")


def test_papyrus_recased_values_are_accepted():
    # Papyrus's string table is case-insensitive: "grab" arrived as "Grab" in the first in-game test
    grab_rec = parse_record(grab_scene().replace("type=grab", "type=Grab").replace("hand=L", "hand=l"))
    assert grab_rec.type == "grab" and grab_rec.hand == "L"
    drink = parse_record(drink_scene().replace("type=consume", "type=Consume").replace("via=higgs_mouth", "via=Higgs_Mouth"))
    assert drink.type == "consume" and drink.via == "higgs_mouth"


def test_recased_grab_goes_all_the_way_to_memories(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5)
    talk(tmp_path, [actors.gerdur()], 3.55, [grab_scene().replace("type=grab", "type=Grab")])
    db = open_db(tmp_path)
    assert len(memories(db, "Gerdur")) == 1 and len(memories(db, "Hod")) == 1
    assert json.loads(db.execute("SELECT payload_json FROM world_event").fetchone()[0])["hand"] == "L"


def test_recased_equip_drink_is_still_confirmed(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5)
    talk(tmp_path, [actors.gerdur()], 3.55, [consume(PLAYER, "Nord Mead", 3.52, via="Equip", witnesses=[seen(actors.gerdur(), 2.0)])
                                             .replace("type=consume", "type=Consume")])
    payload = json.loads(open_db(tmp_path).execute("SELECT payload_json FROM world_event").fetchone()[0])
    assert payload["via"] == "equip" and payload["confirmed"] is True


# ---- the target scenario -----------------------------------------------------------------------

def test_one_event_three_perspectives(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5)                       # meet Gerdur first
    talk(tmp_path, [actors.gerdur()], 3.55, [grab_scene()])       # event arrives with the next request
    db = open_db(tmp_path)

    # A. objective world event
    events = db.execute("SELECT * FROM world_event").fetchall()
    assert len(events) == 1
    event = events[0]
    payload = json.loads(event["payload_json"])
    assert event["type"] == "physical_interaction" and event["source"] == "game"
    assert payload["interaction"] == "grab" and payload["body_part"] == "left forearm"
    assert payload["nearby_but_unseen"] == ["Alvor"]
    assert event["duration_s"] == 4.2 and event["game_key"] == "g1"
    how = {r["display_name"]: r["how"] for r in db.execute(
        "SELECT n.display_name, w.how FROM event_witness w JOIN npc n USING (npc_id)")}
    assert how == {"Torvar": "participated", "Gerdur": "participated", "Hod": "saw"}

    # B. Gerdur experienced it
    (g,) = memories(db, "Gerdur")
    assert g["source_kind"] == "experienced"
    assert g["content"] == "Torvar grabbed me by my left forearm for about 4 seconds in Riverwood."
    assert g["valence"] < 0 and g["event_id"] == event["event_id"]

    # C. Hod witnessed it, does not know why, and reacts as her husband
    (h,) = memories(db, "Hod")
    assert h["source_kind"] == "witnessed"
    assert h["content"] == "I saw Torvar grab Gerdur by the left forearm for about 4 seconds in Riverwood."
    assert "don't know why" in h["interpretation"]
    assert h["content"] != g["content"]

    # nobody else gets a memory: not Alvor (too far), not the player
    assert memories(db, "Alvor") == [] and memories(db, "Torvar") == []

    # local-rule reactions, no LLM: both trust Torvar a little less
    trust = {r["display_name"]: r["trust"] for r in db.execute(
        "SELECT n.display_name, r.trust FROM v_relationship r JOIN npc n ON n.npc_id = r.from_id"
        " JOIN npc p ON p.npc_id = r.to_id AND p.is_player = 1")}
    assert trust["Gerdur"] < 0 and trust["Hod"] < 0


def test_each_npc_recalls_only_their_own_view_in_a_later_conversation(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5)
    talk(tmp_path, [actors.gerdur()], 3.55, [grab_scene()])
    gerdur_llm = talk(tmp_path, [actors.gerdur()], 3.6)
    hod_llm = talk(tmp_path, [actors.hod()], 3.7)

    gerdur_prompt = gerdur_llm.dialogue_calls[0].system_prompt
    hod_prompt = hod_llm.dialogue_calls[0].system_prompt
    assert "Torvar grabbed me by my left forearm" in gerdur_prompt
    assert "it happened to me" in gerdur_prompt
    assert "I saw Torvar" not in gerdur_prompt
    assert "Gerdur by the left forearm" in hod_prompt and "I saw it myself" in hod_prompt
    assert "grabbed me" not in hod_prompt


def test_first_prompt_after_the_event_already_holds_it_and_invites_bringing_it_up(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5)
    llm = talk(tmp_path, [actors.gerdur()], 3.55, [grab_scene()])
    prompt = llm.dialogue_calls[0].system_prompt
    assert "Torvar grabbed me" in prompt
    assert "may bring up something they recently saw or went through without being asked" in prompt


def test_hod_without_line_of_sight_gets_nothing(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5)
    talk(tmp_path, [actors.gerdur()], 3.55, [grab_scene(hod_los=False)])
    db = open_db(tmp_path)
    assert memories(db, "Hod") == []
    assert len(memories(db, "Gerdur")) == 1
    assert "Hod" in json.loads(db.execute("SELECT payload_json FROM world_event").fetchone()[0])["nearby_but_unseen"]


def test_drinking_mead_enters_the_same_pipeline(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5)
    llm = talk(tmp_path, [actors.gerdur()], 3.55, [drink_scene()])
    db = open_db(tmp_path)
    event = db.execute("SELECT * FROM world_event").fetchone()
    payload = json.loads(event["payload_json"])
    assert event["type"] == "consumption"
    assert payload["category"] == "alcohol" and payload["confirmed"] is True and payload["item"] == "Nord Mead"
    (g,) = memories(db, "Gerdur")
    (h,) = memories(db, "Hod")
    assert g["content"] == "I saw Torvar drink some Nord Mead in Riverwood."
    assert h["confidence"] == g["confidence"] == 0.95
    assert g["source_kind"] == h["source_kind"] == "witnessed"
    assert "Nord Mead" in llm.dialogue_calls[0].system_prompt


def test_a_record_sent_twice_is_stored_once(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5)
    record = grab_scene()
    talk(tmp_path, [actors.gerdur()], 3.55, [record, record])
    talk(tmp_path, [actors.gerdur()], 3.6, [record])
    db = open_db(tmp_path)
    assert db.execute("SELECT COUNT(*) FROM world_event").fetchone()[0] == 1
    assert len(memories(db, "Gerdur")) == 1


def test_a_bad_record_is_skipped_and_the_conversation_carries_on(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5)
    llm = talk(tmp_path, [actors.gerdur()], 3.55, ["garbage", "v=1|key=z|type=dance|gt=1|actor=1^2^^^1^X", grab_scene()])
    assert llm.dialogue_calls
    assert open_db(tmp_path).execute("SELECT COUNT(*) FROM world_event").fetchone()[0] == 1


def test_loading_a_save_from_before_the_grab_forgets_it(tmp_path):
    load = lambda timeline, nonce, day: {KEY_TIMELINE_ID: timeline, KEY_LOAD_NONCE: nonce, KEY_LOADED_GAME_DAYS: day}  # noqa: E731
    talk(tmp_path, [actors.gerdur()], 3.5, extra=load(0, 1, 3.4))
    talk(tmp_path, [actors.gerdur()], 3.55, [grab_scene()], extra=load(1, 1, 3.4))
    # load a save made at day 3.505, just before the grab at 3.51
    llm = talk(tmp_path, [actors.gerdur()], 3.6, extra=load(1, 2, 3.505))
    assert "grabbed me" not in llm.dialogue_calls[0].system_prompt
    db = open_db(tmp_path)
    assert db.execute("SELECT COUNT(*) FROM world_event").fetchone()[0] == 1          # nothing deleted
    assert db.execute("SELECT COUNT(*) FROM v_visible_event").fetchone()[0] == 0
    trust = db.execute("SELECT trust FROM v_relationship r JOIN npc n ON n.npc_id = r.from_id"
                       " WHERE n.display_name = 'Gerdur'").fetchone()
    assert trust is None or trust[0] == 0


# ---- migration -----------------------------------------------------------------------------------

def test_migration_keeps_existing_memories(tmp_path):
    from livingworld.store import world_store
    path = str(tmp_path / "old.sqlite")
    files = world_store._migration_files()
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    for version, sql in files:
        if version <= 2:
            conn.executescript(f"BEGIN;\n{sql}\nCOMMIT;")
    conn.execute("INSERT INTO npc (plugin, base_form_id, display_name, created_gt) VALUES ('Skyrim.esm','0x1','Gerdur',1)")
    conn.execute("INSERT INTO memory (owner_id, kind, content, occurred_gt, importance, created_by)"
                 " VALUES (1,'episodic','I met Torvar at the mill.',1,0.5,'extraction')")
    conn.close()

    store = WorldStore(path)
    assert store.schema_version() == LATEST
    assert tuple(store.query("SELECT content, created_by, source_kind FROM memory")[0]) == \
        ("I met Torvar at the mill.", "extraction", None)
    assert store.query("SELECT rowid FROM memory_fts WHERE memory_fts MATCH 'mill'")[0][0] == 1
    assert store.query("PRAGMA foreign_key_check") == []
