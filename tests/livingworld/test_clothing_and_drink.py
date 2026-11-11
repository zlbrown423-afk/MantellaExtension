"""Clothing awareness and drinking: NPCs notice who has no clothes or armor on, remember it, and
get merrier, then tipsy, then drunk as they drink. Both are perception only: neither changes a
relationship, and drink never changes what an NPC is willing to do.

Game records are simulated exactly as the Living World build of Mantella-Spell writes them.
"""
import json
import sqlite3

import pytest

from livingworld.harness import actors
from livingworld.harness.perception import KEY_ATTIRE, KEY_EVENTS, attire, consume, seen
from livingworld.harness.scripted_llm import ScriptedLLM
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, KEY_LOAD_NONCE, KEY_LOADED_GAME_DAYS, KEY_TIMELINE_ID, LivingWorld
from livingworld.perception import intoxication
from livingworld.perception.intoxication import CONSENT_UNCHANGED, level_from, stage_for
from livingworld.perception.records import BadRecord, is_alcohol, parse_record

PLAYER = actors.player("Torvar")
HOUR = 1 / 24


@pytest.fixture(autouse=True)
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.setenv("MANTELLA_LW_TASKS", "0")   # these tests count every world event; tasks add their own
    monkeypatch.setattr(LivingWorld, "start_workers", False)


def open_db(work_dir) -> sqlite3.Connection:
    conn = sqlite3.connect(work_dir / "data" / "livingworld" / "HeadlessTest.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def talk(work_dir, npcs, day, events=None, snapshot=None, extra=None):
    llm = ScriptedLLM(replies=["Hm."])
    context = {KEY_EVENTS: events or []}
    if snapshot is not None:
        context[KEY_ATTIRE] = snapshot
    with HeadlessSession(str(work_dir), llm) as s:
        s.start([PLAYER, *npcs], game_days=day, extra=extra, context_extra=context)
        s.continue_until_player_turn()
        s.end(game_days=day + 0.001)
    return llm.dialogue_calls[0].system_prompt


def memories(db, name):
    return db.execute("SELECT m.* FROM memory m JOIN npc n ON n.npc_id = m.owner_id WHERE n.display_name = ?"
                      " AND m.created_by = 'perception' ORDER BY m.mem_id", (name,)).fetchall()


def dressed(actor, day):
    return attire(actor, "clothing", day, body="Farm Clothes")


def undressed(actor, day, witnesses=None):
    return attire(actor, "none", day, witnesses=witnesses)


def gerdur_drinks(day, item="Nord Mead"):
    return consume(actors.gerdur(), item, day, via="action", witnesses=[seen(actors.hod(), 3.0)])


# ---- records ----------------------------------------------------------------------------------------

def test_attire_record_parses_and_bad_cover_is_refused():
    record = parse_record(attire(PLAYER, "Armor", 3.5, body="Iron Armor"))
    assert record.type == "attire" and record.cover == "armor" and record.body == "Iron Armor"
    with pytest.raises(BadRecord):
        parse_record(attire(PLAYER, "naked", 3.5))


def test_skooma_is_not_a_drink():
    assert is_alcohol("Nord Mead") and is_alcohol("Alto Wine") and is_alcohol("Black-Briar Mead")
    assert not is_alcohol("Skooma") and not is_alcohol("Milk") and not is_alcohol("Water")


# ---- clothing ----------------------------------------------------------------------------------------

def test_player_undressed_is_seen_remembered_and_noticed(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5)
    prompt = talk(tmp_path, [actors.gerdur()], 3.52, snapshot=[
        undressed(PLAYER, 3.52, witnesses=[seen(actors.gerdur(), 2.0), seen(actors.hod(), 40.0)]),
        dressed(actors.gerdur(), 3.52)])
    db = open_db(tmp_path)
    (event,) = db.execute("SELECT * FROM world_event WHERE type = 'attire'").fetchall()
    assert json.loads(event["payload_json"])["covered"] is False
    (g,) = memories(db, "Gerdur")
    assert g["content"] == "I saw Torvar with no clothes or armor on, only underclothes in Riverwood."
    assert g["source_kind"] == "witnessed"
    assert memories(db, "Hod") == []          # 40 m away: did not see it
    assert "Gerdur can see that Torvar has no clothes or armor on" in prompt
    assert "does not change what Gerdur wants or is willing to do" in prompt


def test_staying_undressed_is_one_event_and_newcomers_remember_it_once(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5)
    talk(tmp_path, [actors.gerdur()], 3.52, snapshot=[undressed(PLAYER, 3.52, witnesses=[seen(actors.gerdur(), 2.0)])])
    talk(tmp_path, [actors.gerdur()], 3.53, snapshot=[undressed(PLAYER, 3.53, witnesses=[seen(actors.gerdur(), 2.0)])])
    talk(tmp_path, [actors.gerdur()], 3.54, snapshot=[
        undressed(PLAYER, 3.54, witnesses=[seen(actors.gerdur(), 2.0), seen(actors.hod(), 5.0)])])
    db = open_db(tmp_path)
    assert db.execute("SELECT COUNT(*) FROM world_event WHERE type = 'attire'").fetchone()[0] == 1
    assert len(memories(db, "Gerdur")) == 1
    assert len(memories(db, "Hod")) == 1


def test_dressing_again_closes_it_and_a_later_undress_is_new(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5)
    talk(tmp_path, [actors.gerdur()], 3.52, snapshot=[undressed(PLAYER, 3.52, witnesses=[seen(actors.gerdur(), 2.0)])])
    prompt = talk(tmp_path, [actors.gerdur()], 3.53, snapshot=[dressed(PLAYER, 3.53)])
    talk(tmp_path, [actors.gerdur()], 3.6, snapshot=[undressed(PLAYER, 3.6, witnesses=[seen(actors.gerdur(), 2.0)])])
    db = open_db(tmp_path)
    covered = [json.loads(r[0])["covered"] for r in
               db.execute("SELECT payload_json FROM world_event WHERE type = 'attire' ORDER BY game_time")]
    assert covered == [False, True, False]
    assert len(memories(db, "Gerdur")) == 2
    assert "can see that Torvar has no clothes" not in prompt      # dressed now
    assert "I saw Torvar with no clothes" in prompt                # but remembers it


def test_dressed_people_store_nothing(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5, snapshot=[dressed(PLAYER, 3.5), dressed(actors.gerdur(), 3.5)])
    talk(tmp_path, [actors.gerdur()], 3.51, snapshot=[dressed(PLAYER, 3.51), dressed(actors.gerdur(), 3.51)])
    assert open_db(tmp_path).execute("SELECT COUNT(*) FROM world_event").fetchone()[0] == 0


def test_npc_knows_their_own_state_and_who_saw(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5)
    prompt = talk(tmp_path, [actors.gerdur()], 3.52, snapshot=[
        undressed(actors.gerdur(), 3.52, witnesses=[seen(actors.hod(), 3.0)]), dressed(PLAYER, 3.52)])
    db = open_db(tmp_path)
    (own,) = memories(db, "Gerdur")
    assert own["content"] == "I had no clothes or armor on, nothing at all in Riverwood. Hod saw me."
    assert own["source_kind"] == "experienced"
    assert len(memories(db, "Hod")) == 1
    assert "Gerdur has no clothes or armor on right now" in prompt


def test_clothing_never_changes_relationships(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5)
    talk(tmp_path, [actors.gerdur()], 3.52, snapshot=[
        undressed(PLAYER, 3.52, witnesses=[seen(actors.gerdur(), 2.0, rank_to_actor=0), seen(actors.hod(), 5.0)])])
    assert open_db(tmp_path).execute("SELECT COUNT(*) FROM relationship_change").fetchone()[0] == 0


def test_loading_an_older_save_forgets_the_undress(tmp_path):
    load = lambda timeline, nonce, day: {KEY_TIMELINE_ID: timeline, KEY_LOAD_NONCE: nonce, KEY_LOADED_GAME_DAYS: day}  # noqa: E731
    talk(tmp_path, [actors.gerdur()], 3.5, extra=load(0, 1, 3.4))
    talk(tmp_path, [actors.gerdur()], 3.52, extra=load(1, 1, 3.4),
         snapshot=[undressed(PLAYER, 3.52, witnesses=[seen(actors.gerdur(), 2.0)])])
    prompt = talk(tmp_path, [actors.gerdur()], 3.6, extra=load(1, 2, 3.51))
    assert "no clothes" not in prompt


# ---- drinking ----------------------------------------------------------------------------------------

def test_alcohol_wears_off_over_game_time():
    assert level_from([], 3.5) == 0
    assert level_from([(3.5, 1)], 3.5) == 1
    three_quick = [(3.5, 1), (3.5 + 0.2 * HOUR, 1), (3.5 + 0.4 * HOUR, 1)]
    assert stage_for(level_from(three_quick, 3.5 + 0.5 * HOUR)) == "tipsy"
    assert stage_for(level_from(three_quick, 3.5 + 12 * HOUR)) is None
    assert stage_for(0.5) is None and stage_for(1) == "merry" and stage_for(4.5) == "drunk" and stage_for(7) == "very drunk"


def test_gerdur_gets_tipsy_from_her_own_drinks(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5)
    drinks = [gerdur_drinks(3.5 + i * 0.1 * HOUR) for i in range(3)]
    prompt = talk(tmp_path, [actors.gerdur()], 3.5 + 0.4 * HOUR, events=drinks)
    assert "Gerdur has had 3 drinks in the last few hours and is tipsy" in prompt
    assert CONSENT_UNCHANGED.format(name="Gerdur") in prompt
    (first, *_) = memories(open_db(tmp_path), "Gerdur")
    assert first["content"] == "I drank some Nord Mead in Riverwood."


def test_she_sobers_up_by_the_next_day(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5)
    talk(tmp_path, [actors.gerdur()], 3.5 + 0.4 * HOUR, events=[gerdur_drinks(3.5 + i * 0.1 * HOUR) for i in range(5)])
    prompt = talk(tmp_path, [actors.gerdur()], 4.5)
    assert "drinks in the last few hours" not in prompt


def test_skooma_and_food_do_not_count(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5)
    prompt = talk(tmp_path, [actors.gerdur()], 3.51, events=[
        consume(actors.gerdur(), "Skooma", 3.505, is_food=False, via="action"),
        consume(actors.gerdur(), "Sweetroll", 3.506, via="action")])
    assert "drinks in the last few hours" not in prompt


def test_npc_notices_how_much_the_player_has_drunk(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5)
    drinks = [consume(PLAYER, "Ale", 3.5 + i * 0.1 * HOUR, witnesses=[seen(actors.gerdur(), 2.0)]) for i in range(4)]
    prompt = talk(tmp_path, [actors.gerdur()], 3.5 + 0.5 * HOUR, events=drinks)
    assert "Gerdur has seen Torvar have 4 drinks in the last few hours." in prompt
    assert "Gerdur has had" not in prompt     # Gerdur herself is sober


def test_drink_never_changes_relationships_or_willingness(tmp_path):
    """The hard line: intoxication is speech and mood only. It writes no relationship change, and the
    module exposes nothing a decision about undressing or intimacy could read."""
    talk(tmp_path, [actors.gerdur()], 3.5)
    talk(tmp_path, [actors.gerdur()], 3.5 + HOUR, events=[gerdur_drinks(3.5 + i * 0.05 * HOUR) for i in range(8)])
    db = open_db(tmp_path)
    assert db.execute("SELECT COUNT(*) FROM relationship_change").fetchone()[0] == 0
    public = {n for n in dir(intoxication) if not n.startswith("_")} - {"CONSENT_UNCHANGED"}
    assert not any(word in n.lower() for n in public for word in ("consent", "willing", "arous", "undress", "sex"))
    state = intoxication.DrinkState(8, 8, "very drunk")
    assert set(vars(state)) == {"level", "recent_drinks", "stage"}
    assert "still refuses" in state.prompt_line("Gerdur")


def test_drink_carries_weight_in_company(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5)
    prompt = talk(tmp_path, [actors.gerdur()], 3.5 + 0.5 * HOUR,
                  events=[gerdur_drinks(3.5 + i * 0.05 * HOUR) for i in range(5)])
    assert "is drunk" in prompt
    assert "Loose-lipped" in prompt and "gossip" in prompt and "sing, dance" in prompt
    assert CONSENT_UNCHANGED.format(name="Gerdur") in prompt


def test_what_a_drunk_npc_sees_is_hazy(tmp_path):
    talk(tmp_path, [actors.hod()], 3.5)
    hod_drinks = [consume(actors.hod(), "Nord Mead", 3.5 + i * 0.05 * HOUR, via="action") for i in range(5)]
    talk(tmp_path, [actors.hod()], 3.5 + 0.3 * HOUR, events=hod_drinks)
    talk(tmp_path, [actors.hod()], 3.5 + 0.4 * HOUR,
         snapshot=[undressed(PLAYER, 3.5 + 0.35 * HOUR, witnesses=[seen(actors.hod(), 2.0)])])
    seen_it = [m for m in memories(open_db(tmp_path), "Hod") if m["source_kind"] == "witnessed"]
    assert len(seen_it) == 1
    assert seen_it[0]["content"].endswith("I'd had a lot to drink.")
    assert seen_it[0]["confidence"] < 0.95


# ---- an NPC choosing to undress (Undress / Dress actions) -------------------------------------------

def frodnar():
    # a child, recognised from Mantella's character list
    return actors.actor("Frodnar", 0x0001347E, 0x0001348B, 0, "Nord", "MaleEvenToned")


def test_npc_who_chose_to_undress_and_witnesses_remember_it_as_her_choice(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5)
    talk(tmp_path, [actors.gerdur()], 3.52, events=[
        attire(actors.gerdur(), "none", 3.52, via="action", witnesses=[seen(actors.hod(), 3.0)])],
        snapshot=[undressed(actors.gerdur(), 3.52, witnesses=[seen(actors.hod(), 3.0)])])
    db = open_db(tmp_path)
    (event,) = db.execute("SELECT * FROM world_event WHERE type = 'attire'").fetchall()
    assert json.loads(event["payload_json"])["chosen"] is True
    (own,) = memories(db, "Gerdur")
    assert own["content"] == "I chose to take off my clothes in Riverwood, all of them. Hod saw me."
    (h,) = memories(db, "Hod")
    assert h["content"] == "I saw Gerdur take off their clothes in Riverwood, leaving them naked."
    assert db.execute("SELECT COUNT(*) FROM relationship_change").fetchone()[0] == 0


def test_dressing_by_choice_closes_it(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5)
    talk(tmp_path, [actors.gerdur()], 3.52, events=[attire(actors.gerdur(), "none", 3.52, via="action")])
    prompt = talk(tmp_path, [actors.gerdur()], 3.53, events=[
        attire(actors.gerdur(), "clothing", 3.53, body="Farm Clothes", via="action")],
        snapshot=[dressed(actors.gerdur(), 3.53)])
    db = open_db(tmp_path)
    covered = [json.loads(r[0])["covered"] for r in
               db.execute("SELECT payload_json FROM world_event WHERE type = 'attire' ORDER BY game_time")]
    assert covered == [False, True]
    assert "Gerdur has no clothes or armor on right now" not in prompt


def test_a_child_undressing_record_is_ignored(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5)
    talk(tmp_path, [actors.gerdur()], 3.52, events=[
        attire(frodnar(), "none", 3.52, via="action", witnesses=[seen(actors.gerdur(), 2.0)])])
    db = open_db(tmp_path)
    assert db.execute("SELECT COUNT(*) FROM world_event WHERE type = 'attire'").fetchone()[0] == 0
    assert memories(db, "Gerdur") == []


def test_npc_knows_who_else_is_nearby(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5)
    prompt = talk(tmp_path, [actors.gerdur()], 3.51, snapshot=[
        dressed(PLAYER, 3.51), dressed(actors.gerdur(), 3.51), dressed(actors.hod(), 3.51), dressed(frodnar(), 3.51)])
    assert "Also nearby, not part of this conversation: Hod." in prompt
    alone = talk(tmp_path, [actors.gerdur()], 3.52, snapshot=[dressed(PLAYER, 3.52), dressed(actors.gerdur(), 3.52)])
    assert "No one else is close by" in alone
    no_snapshot = talk(tmp_path, [actors.gerdur()], 3.53)
    assert "Also nearby" not in no_snapshot and "No one else is close by" not in no_snapshot
