"""Fog: the player sees a named NPC as a description until they learn the name, and NPCs do not know the
player's name until they are told it. Places an NPC mentions get their map marker added.

The game side is simulated: a fog game build sends a description ('Nord woman') with each actor.
"""
import sqlite3

import pytest

from livingworld import fog
from livingworld.fog import names, places
from livingworld.fog import service as fogs
from livingworld.harness import actors
from livingworld.harness.scripted_llm import ScriptedLLM
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, LivingWorld
from livingworld.memory import jobs
from livingworld.store import world_store
from livingworld.store.timeline import resolve_load
from livingworld.store.world_store import WorldStore


def labelled(actor, label):
    return {**actor, fog.KEY_FOG_LABEL: label}


PLAYER = labelled(actors.player("Torvar"), "Nord man")


def gerdur():
    return labelled(actors.gerdur(), "Nord woman")


def hod():
    return labelled(actors.hod(), "Nord man")


@pytest.fixture(autouse=True)
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.setattr(LivingWorld, "start_workers", False)
    monkeypatch.setattr(jobs, "BACKOFF_SECONDS", (0, 0, 0, 0, 0))


def open_db(work_dir) -> sqlite3.Connection:
    conn = sqlite3.connect(work_dir / "data" / "livingworld" / "HeadlessTest.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def knows(db, knower, subject):
    return db.execute(
        "SELECT k.name FROM v_name_known k JOIN npc a ON a.npc_id = k.knower_id JOIN npc b ON b.npc_id = k.subject_id"
        " WHERE a.display_name = ? AND b.display_name = ?", (knower, subject)).fetchone()


class Spy:
    def __init__(self, lw):
        self.keys = []
        original = lw.line_reply_keys

        def capture(sentence):
            keys = original(sentence)
            self.keys.append(dict(keys))
            return keys
        lw.line_reply_keys = capture

    def all(self, key):
        return [v for k in self.keys for v in (k.get(key) or [])]


# ---- text rules ----

def test_call_names():
    assert names.call_names("Gerdur") == ["Gerdur"]
    assert names.call_names("Lucan Valerius") == ["Lucan Valerius", "Lucan"]
    assert names.call_names("Jarl Balgruuf the Greater") == ["Jarl Balgruuf the Greater", "Balgruuf"]


def test_says_name():
    assert names.says_name("I'm Gerdur. My husband Hod runs the mill.", "Gerdur")
    assert names.says_name("Lucan's the name, trader.", "Lucan Valerius")
    assert not names.says_name("Gerdurson? Never heard of him.", "Gerdur")
    assert not names.says_name("the hod is full", "Hod")                      # NPC lines: case matters
    assert names.says_name("hey hod, how's the mill", "Hod", ignore_case=True)  # the player's: it does not


def test_player_introduction():
    assert names.player_introduction("My name is Torvar.", "Torvar") == "Torvar"
    assert names.player_introduction("my name is torvar", "Torvar") == "Torvar"
    assert names.player_introduction("You can call me Wolf.", "Torvar") == "Wolf"
    assert names.player_introduction("I'm Torvar, from Falkreath.", "Torvar") == "Torvar"
    assert names.player_introduction("I'm tired.", "Torvar") is None
    assert names.player_introduction("Call me whatever you like.", "Torvar") is None


# ---- in a conversation ----

def test_npc_saying_their_name_reveals_it_and_the_prompts_say_who_knows_what(tmp_path):
    llm = ScriptedLLM(replies=["Welcome, traveller. I'm Gerdur.", "Torvar, then. Good to meet you."])
    with HeadlessSession(str(tmp_path), llm) as s:
        reply = s.start([PLAYER, gerdur()], game_days=2.0)
        assert reply[fog.KEY_FOG_ON] is True and reply[fog.KEY_FOG_KNOWN] == []
        spy = Spy(LivingWorld.latest)
        s.continue_until_player_turn()
        first = llm.dialogue_calls[0].system_prompt
        assert "Torvar does not know your name yet: to them you are just a Nord woman" in first
        assert "You have never been told the name of the Nord man you are talking to" in first
        assert spy.all(fog.KEY_FOG_REVEAL) == ["Gerdur"]
        s.say("My name is Torvar.")
        said = llm.dialogue_calls[-1].last_user_text
        assert "Torvar now knows Gerdur's name." in said      # she said it in her line
        assert "Gerdur now knows Torvar's name." in said      # he said it just now
        s.end(game_days=2.01)
    db = open_db(tmp_path)
    assert knows(db, "Torvar", "Gerdur")["name"] == "Gerdur"
    assert knows(db, "Gerdur", "Torvar")["name"] == "Torvar"
    with HeadlessSession(str(tmp_path), ScriptedLLM(replies=["Back again?"])) as s:
        assert s.start([PLAYER, gerdur()], game_days=3.0)[fog.KEY_FOG_KNOWN] == ["Gerdur"]
        s.end(game_days=3.01)


def test_someone_present_names_the_other(tmp_path):
    llm = ScriptedLLM(replies=["Gerdur: This is my husband, Hod.", "Hod: Wolf, is it? Welcome."])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([PLAYER, gerdur(), hod()], game_days=2.0)
        spy = Spy(LivingWorld.latest)
        s.continue_until_player_turn()
        assert "Hod" in spy.all(fog.KEY_FOG_REVEAL)
        s.say("Call me Wolf.")
        s.end(game_days=2.01)
    db = open_db(tmp_path)
    assert knows(db, "Torvar", "Hod")["name"] == "Hod"
    assert knows(db, "Gerdur", "Torvar")["name"] == "Wolf"     # the name the player gave
    assert knows(db, "Torvar", "Gerdur") is None                # nobody said hers


def test_an_alias_is_what_the_npc_calls_the_player(tmp_path):
    llm = ScriptedLLM(replies=["Hm.", "Wolf. Fine.", "What now, Wolf?"])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([PLAYER, gerdur()], game_days=2.0)
        s.continue_until_player_turn()
        s.say("Call me Wolf.")
        assert "Gerdur now knows Torvar by the name Wolf" in llm.dialogue_calls[-1].last_user_text
        s.end(game_days=2.01)
    with HeadlessSession(str(tmp_path), llm) as s:   # the next conversation's prompt holds it
        s.start([PLAYER, gerdur()], game_days=3.0)
        s.continue_until_player_turn()
        assert "Torvar told you their name is Wolf" in llm.dialogue_calls[-1].system_prompt
        s.end(game_days=3.01)


def test_an_older_game_build_changes_nothing(tmp_path):
    llm = ScriptedLLM(replies=["I'm Gerdur."])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([actors.player("Torvar"), actors.gerdur()], game_days=2.0)
        spy = Spy(LivingWorld.latest)
        s.continue_until_player_turn()
        prompt = llm.dialogue_calls[0].system_prompt
        assert "never been told" not in prompt and "does not know your name" not in prompt
        assert spy.all(fog.KEY_FOG_REVEAL) == []
        s.end(game_days=2.01)


def test_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv(fog.ENV, "0")
    llm = ScriptedLLM(replies=["I'm Gerdur."])
    with HeadlessSession(str(tmp_path), llm) as s:
        reply = s.start([PLAYER, gerdur()], game_days=2.0)
        assert reply[fog.KEY_FOG_ON] is False
        s.continue_until_player_turn()
        assert "never been told" not in llm.dialogue_calls[0].system_prompt
        s.end(game_days=2.01)


def test_a_place_an_npc_mentions_goes_on_the_map_once(tmp_path, monkeypatch):
    barrow = places.Marker("Bleak Falls Barrow", "Skyrim.esm", "0x000130C8")
    monkeypatch.setattr(places, "markers", lambda: (barrow,))
    places._matcher.cache_clear()
    llm = ScriptedLLM(replies=["The claw was taken to Bleak Falls Barrow.", "Bleak Falls Barrow is up the mountain."])
    try:
        with HeadlessSession(str(tmp_path), llm) as s:
            s.start([PLAYER, gerdur()], game_days=2.0)
            spy = Spy(LivingWorld.latest)
            s.continue_until_player_turn()
            s.say("Where is that?")
            assert spy.all(fog.KEY_FOG_MARKER_IDS) == [0x130C8]
            assert spy.all(fog.KEY_FOG_MARKER_PLUGINS) == ["Skyrim.esm"]
            assert spy.all(fog.KEY_FOG_MARKER_NAMES) == ["Bleak Falls Barrow"]
            s.end(game_days=2.01)
    finally:
        places._matcher.cache_clear()


def test_place_names_shared_by_several_markers_are_left_out(monkeypatch):
    raw = [{"name": "Abandoned Shack", "plugin": "Skyrim.esm", "form_id": "0x1"},
           {"name": "Abandoned Shack", "plugin": "Skyrim.esm", "form_id": "0x2"},
           {"name": "Riverwood", "plugin": "Skyrim.esm", "form_id": "0x3"}]
    import json
    from importlib import resources

    class FakeFile:
        def joinpath(self, _):
            return self

        def read_text(self, encoding=None):
            return json.dumps(raw)
    monkeypatch.setattr(resources, "files", lambda _pkg: FakeFile())
    places.markers.cache_clear()
    try:
        assert [m.name for m in places.markers()] == ["Riverwood"]
    finally:
        places.markers.cache_clear()


# ---- store ----

def test_a_name_learned_after_the_save_is_forgotten_on_loading_it():
    store = WorldStore(":memory:")
    with store.transaction() as db:
        db.execute("INSERT INTO npc (npc_id, plugin, base_form_id, display_name, is_player, created_gt)"
                   " VALUES (1,'Skyrim.esm','0x7','Torvar',1,1.0), (2,'Skyrim.esm','0x1347C','Gerdur',0,1.0)")
        fogs.learn(db, 1, 2, "Gerdur", "introduced", 3.0)
    resolve_load(store, 1, 1, 3.5)
    with store.reading() as db:
        assert fogs.known_name(db, 1, 2) == "Gerdur"
    with store.transaction() as db:   # something after the save's time, so loading it branches
        db.execute("INSERT INTO conversation (conv_id, location, kind, started_gt, ended_gt) VALUES (9,'Riverwood','player',4.0,4.1)")
    resolve_load(store, 2, 1, 2.0)
    with store.reading() as db:
        assert fogs.known_name(db, 1, 2) is None


def test_people_already_talked_to_count_as_met(monkeypatch):
    every = world_store._migration_files()
    monkeypatch.setattr(world_store, "_migration_files", lambda: [m for m in every if m[0] < 12])
    store = WorldStore(":memory:")
    with store.transaction() as db:
        db.execute("INSERT INTO npc (npc_id, plugin, base_form_id, display_name, is_player, created_gt)"
                   " VALUES (1,'Skyrim.esm','0x7','Torvar',1,1.0), (2,'Skyrim.esm','0x1347C','Gerdur',0,1.0),"
                   " (3,'Skyrim.esm','0x1347D','Hod',0,1.0)")
        db.execute("INSERT INTO conversation (conv_id, location, kind, started_gt) VALUES (1,'Riverwood','player',2.0)")
        db.execute("INSERT INTO participant (conv_id, npc_id) VALUES (1,1), (1,2)")
    monkeypatch.setattr(world_store, "_migration_files", lambda: every)
    store.migrate()
    with store.reading() as db:
        assert fogs.known_name(db, 1, 2) == "Gerdur"
        assert fogs.known_name(db, 2, 1) == "Torvar"
        assert fogs.known_name(db, 1, 3) is None


def test_hints_and_gossip_use_what_the_viewer_knows():
    store = WorldStore(":memory:")
    with store.transaction() as db:
        db.execute("INSERT INTO npc (npc_id, plugin, base_form_id, display_name, is_player, created_gt, stranger_label)"
                   " VALUES (1,'Skyrim.esm','0x7','Torvar',1,1.0,'Nord man'), (2,'Skyrim.esm','0x1347C','Gerdur',0,1.0,'Nord woman'),"
                   " (3,'Skyrim.esm','0x1347D','Hod',0,1.0,'Nord man')")
        assert fogs.shown_name(db, 1, 2) == "a Nord woman"
        assert fogs.shown_name(db, 2, 1) == "a stranger (a Nord man)"
        assert fogs.shown_name(db, 2, 3) == "Hod"                  # NPCs know each other
        fogs.learn(db, 2, 1, "Wolf", "introduced", 2.0)
        assert fogs.shown_name(db, 2, 1) == "Wolf"
        assert fogs.gossip_carries_name(db, 2, 3, 1, 2.5)
        assert fogs.known_name(db, 3, 1) == "Wolf"
