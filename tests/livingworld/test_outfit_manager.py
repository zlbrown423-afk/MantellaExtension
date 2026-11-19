"""Outfit manager switches and the 'dressed again' attire record (via=outfit) the game sends after it puts a
follower's clothes back on. The re-dressing itself is Papyrus (Mantella-Spell MantellaRepository LwOutfit*)."""
import sqlite3

import pytest

from livingworld.harness import actors
from livingworld.harness.perception import attire, seen
from livingworld.harness.scripted_llm import ScriptedLLM
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, LivingWorld
from livingworld.perception import outfit
from livingworld.perception.records import KEY_ATTIRE, parse_record

PLAYER = actors.player("Torvar")
LYDIA = actors.actor("Lydia", 0x000A2C8E, 0x000A2C94, 1, "Nord", "FemaleEvenToned")


@pytest.fixture(autouse=True)
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.setenv("MANTELLA_LW_TASKS", "0")
    monkeypatch.delenv(outfit.ENV, raising=False)
    monkeypatch.delenv(outfit.NOTICES_ENV, raising=False)
    monkeypatch.setattr(LivingWorld, "start_workers", False)


def talk(work_dir, day, records):
    llm = ScriptedLLM(replies=["Hm."])
    with HeadlessSession(str(work_dir), llm) as s:
        start = s.start([PLAYER, LYDIA], location="Whiterun", game_days=day, context_extra={KEY_ATTIRE: records})
        s.continue_until_player_turn()
        s.end(game_days=day + 0.001)
        LivingWorld.latest.wait_for_jobs()
    return start


def test_switches_default_on():
    assert outfit.enabled() and outfit.notices()
    assert outfit.start_keys(True) == {outfit.KEY_OUTFIT_ON: True, outfit.KEY_OUTFIT_NOTICES: True}
    assert outfit.start_keys(False) == {outfit.KEY_OUTFIT_ON: False, outfit.KEY_OUTFIT_NOTICES: False}


@pytest.mark.parametrize("value", ["0", "off", "False", " no "])
def test_manager_off_turns_notices_off_too(monkeypatch, value):
    monkeypatch.setenv(outfit.ENV, value)
    assert outfit.start_keys(True) == {outfit.KEY_OUTFIT_ON: False, outfit.KEY_OUTFIT_NOTICES: False}


def test_notices_off_keeps_manager_on(monkeypatch):
    monkeypatch.setenv(outfit.NOTICES_ENV, "0")
    assert outfit.start_keys(True) == {outfit.KEY_OUTFIT_ON: True, outfit.KEY_OUTFIT_NOTICES: False}


def test_start_reply_carries_the_switches(tmp_path, monkeypatch):
    start = talk(tmp_path, 3.5, [attire(PLAYER, "armor", 3.5, location="Whiterun")])
    assert start[outfit.KEY_OUTFIT_ON] is True and start[outfit.KEY_OUTFIT_NOTICES] is True
    monkeypatch.setenv(outfit.ENV, "0")
    start = talk(tmp_path, 3.6, [attire(PLAYER, "armor", 3.6, location="Whiterun")])
    assert start[outfit.KEY_OUTFIT_ON] is False and start[outfit.KEY_OUTFIT_NOTICES] is False


def test_an_outfit_record_parses():
    record = parse_record(attire(LYDIA, "armor", 3.5, body="Steel Plate Armor", via=outfit.VIA))
    assert record.via == "outfit" and record.cover == "armor"


def test_dressed_again_by_the_manager_closes_the_naked_state_without_memories(tmp_path):
    # Lydia walks out of Breezehome naked (the engine's outfit reset); the manager puts her armor back on
    naked = attire(LYDIA, "none", 3.5, location="Whiterun", sex="female", witnesses=[seen(actors.gerdur(), 4.0)])
    talk(tmp_path, 3.5, [naked])
    db = sqlite3.connect(tmp_path / "data" / "livingworld" / "HeadlessTest.sqlite")
    before = db.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
    dressed = attire(LYDIA, "armor", 3.5005, body="Steel Plate Armor", location="Whiterun", via=outfit.VIA)
    talk(tmp_path, 3.5005, [dressed])
    rows = db.execute("SELECT payload_json FROM world_event WHERE type = 'attire' ORDER BY event_id").fetchall()
    assert len(rows) == 2 and '"covered": false' in rows[0][0] and '"covered": true' in rows[1][0]
    assert '"chosen": false' in rows[1][0]
    lydia_dressed_memories = db.execute(
        "SELECT COUNT(*) FROM memory WHERE content LIKE '%Steel Plate%'").fetchone()[0]
    assert lydia_dressed_memories == 0
    assert db.execute("SELECT COUNT(*) FROM memory").fetchone()[0] >= before
