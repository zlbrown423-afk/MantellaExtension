"""Hygiene (H1): how clean people are, the baths NPCs take off screen, and what others notice.

Game records are simulated exactly as the Living World build of Mantella-Spell writes them.
"""
import sqlite3
from importlib import resources

import pytest

from livingworld.harness import actors
from livingworld.harness.perception import KEY_ATTIRE, KEY_EVENTS, attire, fight, seen, swim
from livingworld.harness.scripted_llm import ScriptedLLM
from livingworld.harness.session import HeadlessSession
from livingworld.hygiene import model
from livingworld.hygiene.profile import habits_for, occupation_defaults
from livingworld.integration import ENV_VAR, HYGIENE_ENV, LivingWorld
from livingworld.perception.attire import UNDRESSED_LOOK_ENV
from livingworld.perception.records import parse_record
from livingworld.store.world_store import WorldStore

PLAYER = actors.player("Torvar")


@pytest.fixture(autouse=True)
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.setattr(LivingWorld, "start_workers", False)


def open_db(work_dir) -> sqlite3.Connection:
    conn = sqlite3.connect(work_dir / "data" / "livingworld" / "HeadlessTest.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def talk(work_dir, npcs, day, events=None, snapshot=None):
    llm = ScriptedLLM(replies=["Hm."])
    context = {KEY_EVENTS: events or []}
    if snapshot is not None:
        context[KEY_ATTIRE] = snapshot
    with HeadlessSession(str(work_dir), llm) as s:
        s.start([PLAYER, *npcs], game_days=day, context_extra=context)
        s.continue_until_player_turn()
        s.end(game_days=day + 0.001)
    return llm.dialogue_calls[0].system_prompt


def npc_id(db, name):
    return db.execute("SELECT npc_id FROM npc WHERE display_name = ?", (name,)).fetchone()[0]


def store_with(*names):
    store = WorldStore(":memory:")
    ids = {}
    with store.transaction() as db:
        for i, name in enumerate(names):
            ids[name] = db.execute(
                "INSERT INTO npc (plugin, base_form_id, display_name, is_player, created_gt) VALUES (?,?,?,?,?)",
                ("Skyrim.esm", f"0x{0x100 + i:08X}", name, int(name == "Player"), 1.0)).lastrowid
    return store, ids


# ---- records and schema --------------------------------------------------------------------------------

def test_fight_and_swim_records_parse():
    f = parse_record(fight(actors.gerdur(), 0.42, 3.5))
    assert f.type == "fight" and f.hp == pytest.approx(0.42)
    s = parse_record(swim(PLAYER, 14.0, 3.5))
    assert s.type == "swim" and s.duration_s == 14.0
    assert parse_record(attire(PLAYER, "none", 3.5, sex="f")).sex == "female"


def test_a_lower_migration_merged_later_is_still_applied(tmp_path):
    """Feature PRs merge in any order: a database that already has a higher version still gets 006."""
    path = str(tmp_path / "w.sqlite")
    conn = sqlite3.connect(path)
    for name in ("001_initial.sql", "002_game_load.sql", "003_perception.sql", "004_social.sql"):
        conn.executescript(resources.files("livingworld.store.migrations").joinpath(name).read_text())
    conn.execute("INSERT INTO schema_version(version, note) VALUES (7, 'a later feature')")
    conn.commit()
    conn.close()
    store = WorldStore(path)
    assert store.scalar("SELECT COUNT(*) FROM schema_version WHERE version = 6") == 1
    assert store.scalar("SELECT COUNT(*) FROM hygiene_change") == 0
    store.close()


# ---- habits --------------------------------------------------------------------------------------------

def test_work_sets_the_defaults():
    miner, steward = occupation_defaults("Miner at Left Hand Mine"), occupation_defaults("Steward of Whiterun")
    assert miner[0] > steward[0] and miner[1] < steward[1]


def test_habits_are_stable_per_person_and_differ_between_people():
    store, ids = store_with("Camilla", "Gerdur", "Sigrid", "Ysolda")
    with store.transaction() as db:
        first = [habits_for(db, i) for i in ids.values()]
        again = [habits_for(db, i) for i in ids.values()]
        db.execute("UPDATE npc SET sex = 'female'")
        same_after_sex = [habits_for(db, i) for i in ids.values()]
    assert first == again == same_after_sex                     # deterministic, and sex is never an input
    assert len({h.hygiene_standard for h in first}) == len(first)


def test_hand_set_traits_win():
    store, ids = store_with("Hod")
    with store.transaction() as db:
        db.execute("INSERT INTO persona_version (npc_id, version, bio, traits_json, source, created_gt)"
                   " VALUES (?,1,'',?, 'admin', 1.0)", (ids["Hod"], '{"bath_interval_days": 6, "hygiene_standard": 0.2}'))
        habits = habits_for(db, ids["Hod"])
    assert habits.bath_interval_days == 6 and habits.hygiene_standard == 0.2


# ---- the model -----------------------------------------------------------------------------------------

def test_dirt_builds_up_and_a_bath_clears_it():
    store, ids = store_with("Hod")
    hod = ids["Hod"]
    with store.transaction() as db:
        model._insert(db, hod, 3.0, "seed")
        habits = habits_for(db, hod)
        later = model.state(db, hod, 6.0, habits)
        assert later.dirt == pytest.approx(min(1.0, 3 * habits.work_dirt_rate))
        model._insert(db, hod, 6.5, "bath")
        assert model.state(db, hod, 6.5, habits).dirt == 0
        assert model.state(db, hod, 6.5, habits).last_bath_gt == 6.5


def test_npcs_bathe_off_screen_at_their_usual_times():
    store, ids = store_with("Camilla")
    camilla = ids["Camilla"]
    with store.transaction() as db:
        model._insert(db, camilla, 3.0, "seed")
        habits = habits_for(db, camilla)
        added = model.catch_up(db, camilla, 30.0, habits)
        baths = [r[0] for r in db.execute("SELECT game_time FROM hygiene_change WHERE cause = 'offscreen_bath'"
                                          " ORDER BY game_time")]
        now = model.state(db, camilla, 30.0, habits)
    assert added == len(baths) >= 27 / (habits.bath_interval_days + 1)
    assert all(b <= 30.0 for b in baths)
    assert now.dirt < habits.threshold + habits.work_dirt_rate      # never left far past their own threshold
    from livingworld.hygiene.profile import WINDOWS
    assert all(abs((b % 1) - WINDOWS[habits.bath_window]) < 1e-9 for b in baths)   # in their part of the day
    with store.transaction() as db:
        assert model.catch_up(db, camilla, 30.0, habits) == 0      # asking again adds nothing


def test_the_player_never_bathes_off_screen():
    store, ids = store_with("Player")
    with store.transaction() as db:
        model._insert(db, ids["Player"], 3.0, "seed")
        assert model.catch_up(db, ids["Player"], 40.0) == 0
        assert model.state(db, ids["Player"], 13.0).label == "filthy"


def test_fights_bloody_people_and_swimming_washes_it_off():
    store, ids = store_with("Player")
    p = ids["Player"]
    with store.transaction() as db:
        model._insert(db, p, 3.0, "seed")
        assert model.record_fight(db, p, 3.2, 0.4, "k1")
        assert not model.record_fight(db, p, 3.2, 0.4, "k1")     # the same record twice counts once
        assert model.state(db, p, 3.25).bloodied
        model.record_swim(db, p, 3.3, 20.0, "k2")
        assert not model.state(db, p, 3.35).bloodied


# ---- in conversation -----------------------------------------------------------------------------------

def test_bloodied_player_is_noticed_once(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5)
    prompt = talk(tmp_path, [actors.gerdur()], 3.6, events=[fight(PLAYER, 0.3, 3.58)])
    assert "Gerdur notices blood on Torvar's clothes" in prompt
    later = talk(tmp_path, [actors.gerdur()], 4.0)
    assert "notices blood on Torvar" not in later              # noticed already; it is a memory now, not news


def test_grimy_player_is_noticed_and_cleaner_after_a_swim(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5)
    dirty = talk(tmp_path, [actors.gerdur()], 12.0)
    assert "Gerdur notices that Torvar is" in dirty
    talk(tmp_path, [actors.gerdur()], 12.2, events=[swim(PLAYER, 30.0, 12.1)])
    db = open_db(tmp_path)
    db.execute("INSERT INTO hygiene_change (timeline_id, npc_id, game_time, cause) VALUES (1, ?, 12.3, 'bath')",
               (npc_id(db, "Torvar"),))
    db.commit()
    clean = talk(tmp_path, [actors.gerdur()], 12.5)
    assert "Torvar has washed and looks much cleaner" in clean


def test_npc_knows_their_own_state(tmp_path):
    talk(tmp_path, [actors.hod()], 3.5, events=[fight(actors.hod(), 0.4, 3.45)])
    prompt = talk(tmp_path, [actors.hod()], 3.55)
    assert "Hod has blood on their clothes and skin from a recent fight." in prompt


def test_hygiene_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv(HYGIENE_ENV, "0")
    talk(tmp_path, [actors.gerdur()], 3.5)
    prompt = talk(tmp_path, [actors.gerdur()], 3.6, events=[fight(PLAYER, 0.3, 3.58)])
    assert "blood" not in prompt


def test_children_are_left_out(tmp_path):
    child = actors.actor("Frodnar", 0x0001347E, 0x0001348B, 0, "Nord", "MaleEvenToned")
    talk(tmp_path, [actors.gerdur()], 3.5, events=[fight(child, 0.3, 3.4)])
    db = open_db(tmp_path)
    assert db.execute("SELECT COUNT(*) FROM hygiene_change h JOIN npc n ON n.npc_id = h.npc_id"
                      " WHERE n.display_name = 'Frodnar'").fetchone()[0] == 0


# ---- undressed wording follows the body mods ----------------------------------------------------------

def test_vanilla_bodies_keep_underclothes(tmp_path, monkeypatch):
    monkeypatch.setenv(UNDRESSED_LOOK_ENV, "underwear")
    talk(tmp_path, [actors.gerdur()], 3.5)
    prompt = talk(tmp_path, [actors.gerdur()], 3.52, snapshot=[
        attire(actors.gerdur(), "none", 3.52, sex="f", witnesses=[seen(actors.hod(), 3.0)])])
    assert "Gerdur has no clothes or armor on right now, only underclothes" in prompt


def test_with_female_nude_bodies_women_are_naked_and_men_keep_underwear(tmp_path):
    talk(tmp_path, [actors.gerdur(), actors.hod()], 3.5)
    prompt = talk(tmp_path, [actors.gerdur(), actors.hod()], 3.52, snapshot=[
        attire(actors.gerdur(), "none", 3.52, sex="f", witnesses=[seen(actors.hod(), 3.0)]),
        attire(actors.hod(), "none", 3.52, sex="m", witnesses=[seen(actors.gerdur(), 3.0)])])
    assert "Gerdur has no clothes or armor on right now, naked" in prompt
    assert "Hod has no clothes or armor on, only underclothes" in prompt


# ---- test command --------------------------------------------------------------------------------------

def test_debug_command_parsing():
    from livingworld.hygiene import debug
    assert debug.parse("Debug, max dirt.") == "dirty"
    assert debug.parse("test filthy on Hod") == "dirty"
    assert debug.parse("debug clean") == "clean"
    assert debug.parse("I need a bath, I'm filthy") is None
    assert debug.parse("max dirt") is None


def test_debug_max_dirt_makes_the_player_filthy_and_noticed_again(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5)
    llm = ScriptedLLM(replies=["Hm.", "By the gods."])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([PLAYER, actors.gerdur()], game_days=3.6)
        s.continue_until_player_turn()
        s.say("Debug, max dirt.")
        s.end(game_days=3.61)
    db = open_db(tmp_path)
    store = WorldStore(str(tmp_path / "data" / "livingworld" / "HeadlessTest.sqlite"))
    with store.transaction() as conn:
        state = model.state(conn, npc_id(conn, "Torvar"), 3.61)
    assert state.label == "filthy" and state.bloodied
    later = talk(tmp_path, [actors.gerdur()], 3.7)
    assert "Gerdur notices that Torvar is filthy" in later
    assert db.execute("SELECT COUNT(*) FROM hygiene_change WHERE cause = 'debug'").fetchone()[0] == 1


def test_debug_clean_on_a_named_npc(tmp_path):
    talk(tmp_path, [actors.hod()], 3.5, events=[fight(actors.hod(), 0.3, 3.45)])
    llm = ScriptedLLM(replies=["Hm.", "Ah."])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([PLAYER, actors.hod()], game_days=3.6)
        s.continue_until_player_turn()
        s.say("debug clean Hod")
        s.end(game_days=3.61)
    db = open_db(tmp_path)
    assert db.execute("SELECT COUNT(*) FROM hygiene_change h JOIN npc n ON n.npc_id = h.npc_id"
                      " WHERE n.display_name = 'Hod' AND h.cause = 'bath'").fetchone()[0] == 1
