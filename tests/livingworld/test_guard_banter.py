"""Barracks banter: bully guards heckle the player on duty (rarely, one-on-one only), gossip guards pass news."""
import pytest

from livingworld.guard import banter as BT
from livingworld.harness import actors
from livingworld.harness.perception import KEY_ATTIRE, attire
from livingworld.harness.scripted_llm import ScriptedLLM
from livingworld.harness.session import HeadlessSession
from livingworld.integration import LivingWorld

from tests.livingworld.test_guard import DAY, ENLIST, PLAYER, caius, living_world_inline, npc_id, open_db, whiterun_guard  # noqa: F401
from tests.livingworld.test_guard_duty import enlisted, given_duty, talk

YSOLDA = actors.actor("Ysolda", 0x00013BAB, 0x00013BAC, 1, "Nord", "FemaleYoungEager")
ARMOR = "Whiterun Guard's Armor"


@pytest.fixture
def all_bullies(monkeypatch):
    monkeypatch.setattr(BT, "BULLY_SHARE", 1.0)
    monkeypatch.setattr(BT, "HECKLE_CHANCE", 1.0)


@pytest.fixture
def all_gossips(monkeypatch):
    monkeypatch.setattr(BT, "BULLY_SHARE", 0.0)
    monkeypatch.setattr(BT, "BULLY_FLOOR_EVERY", 10_000)
    monkeypatch.setattr(BT, "GOSSIP_SHARE", 1.0)


def heckles(tmp_path):
    return open_db(tmp_path).execute("SELECT COUNT(*) FROM world_event WHERE type = 'guard.heckle'").fetchone()[0]


def in_uniform(work_dir, day, npcs):
    llm = ScriptedLLM(rules=[ENLIST], replies=["Hm."])
    with HeadlessSession(str(work_dir), llm) as s:
        s.start([PLAYER, *npcs], location="Whiterun", game_days=day,
                context_extra={KEY_ATTIRE: [attire(PLAYER, "armor", day, body=ARMOR, location="Whiterun")]})
        s.continue_until_player_turn()
        s.end(game_days=day + 0.001)
        LivingWorld.latest.wait_for_jobs()
    return llm.dialogue_calls[0].system_prompt if llm.dialogue_calls else ""


def test_temperaments_are_rare_and_fixed_by_who_the_guard_is():
    picks = [BT.rolled("whiterun_guard", "Skyrim.esm", f"{0x1A000 + i:08X}") for i in range(20000)]
    bully = picks.count(BT.BULLY) / len(picks)
    gossip = picks.count(BT.GOSSIP) / len(picks)
    assert 0.07 < bully < 0.09 and 0.13 < gossip < 0.15
    assert BT.rolled("whiterun_guard", "Skyrim.esm", "0001A6B2") == BT.rolled("whiterun_guard", "Skyrim.esm", "0001A6B2")


def test_a_bully_heckles_the_player_on_duty_then_leaves_them_be_for_days(tmp_path, all_bullies):
    enlisted(tmp_path)
    given_duty(tmp_path, DAY + 0.01)
    prompt = talk(tmp_path, DAY + 0.02, npcs=[whiterun_guard()])
    assert "heckles Torvar, who is on duty" in prompt
    assert "The watch comes first" in prompt and "never lays a hand on Torvar" in prompt
    assert heckles(tmp_path) == 1
    db = open_db(tmp_path)
    gid = npc_id(db, "Whiterun Guard")
    assert '"temperament": "bully"' in db.execute("SELECT traits_json FROM guard_profile WHERE npc_id = ?",
                                                  (gid,)).fetchone()[0]
    prompt = talk(tmp_path, DAY + 0.05, npcs=[whiterun_guard()])
    assert "heckles Torvar" not in prompt and "mean streak" in prompt
    assert heckles(tmp_path) == 1


def test_no_heckling_when_the_conversation_is_not_one_on_one(tmp_path, all_bullies):
    enlisted(tmp_path)
    given_duty(tmp_path, DAY + 0.01)
    prompt = talk(tmp_path, DAY + 0.02, npcs=[YSOLDA, whiterun_guard()])
    assert heckles(tmp_path) == 0 and "heckles Torvar" not in prompt


def test_off_duty_out_of_uniform_no_heckling_but_in_uniform_yes(tmp_path, all_bullies):
    enlisted(tmp_path)
    prompt = talk(tmp_path, DAY + 0.01, npcs=[whiterun_guard()])
    assert heckles(tmp_path) == 0 and "mean streak" in prompt
    prompt = in_uniform(tmp_path, DAY + 0.02, [whiterun_guard(ref=0x0001A6B3)])
    assert "heckles Torvar, who is in the uniform of the Whiterun Guard" in prompt
    assert heckles(tmp_path) == 1


def test_not_a_member_no_heckling(tmp_path, all_bullies):
    prompt = in_uniform(tmp_path, DAY, [whiterun_guard()])
    assert heckles(tmp_path) == 0 and "heckles Torvar" not in prompt and "mean streak" not in prompt


def test_a_sergeant_is_not_heckled(tmp_path, all_bullies):
    enlisted(tmp_path)
    talk(tmp_path, DAY + 0.005, say="debug guard rank sergeant")
    in_uniform(tmp_path, DAY + 0.02, [whiterun_guard()])
    assert heckles(tmp_path) == 0


def test_test_command_forces_a_heckle_from_a_plain_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(BT, "BULLY_SHARE", 0.0)
    monkeypatch.setattr(BT, "BULLY_FLOOR_EVERY", 10_000)
    enlisted(tmp_path)
    given_duty(tmp_path, DAY + 0.01)
    talk(tmp_path, DAY + 0.015, say="debug guard bully")
    prompt = talk(tmp_path, DAY + 0.02, npcs=[whiterun_guard()])
    assert "heckles Torvar" in prompt and heckles(tmp_path) == 1


def test_a_gossip_passes_on_barracks_news_once_and_warns_about_bullies(tmp_path, all_gossips, monkeypatch):
    enlisted(tmp_path)
    prompt = talk(tmp_path, DAY + 0.01, npcs=[whiterun_guard()])
    assert "worst gossip in the Whiterun Guard" in prompt
    assert "Torvar joined the Whiterun Guard as a recruit" in prompt
    prompt = talk(tmp_path, DAY + 0.02, npcs=[whiterun_guard()])
    assert "worst gossip" in prompt and "joined the Whiterun Guard as a recruit" not in prompt
    # a bully met later: the gossip warns about them by name
    monkeypatch.setattr(BT, "BULLY_SHARE", 1.0)
    monkeypatch.setattr(BT, "GOSSIP_SHARE", 0.0)
    talk(tmp_path, DAY + 0.03, npcs=[whiterun_guard(ref=0x0001A6B3)])
    prompt = talk(tmp_path, DAY + 0.04, npcs=[whiterun_guard()])
    assert "may warn Torvar about" in prompt


def test_switching_banter_off(tmp_path, all_bullies, monkeypatch):
    monkeypatch.setenv("MANTELLA_LW_GUARD_BANTER", "0")
    enlisted(tmp_path)
    given_duty(tmp_path, DAY + 0.01)
    prompt = talk(tmp_path, DAY + 0.02, npcs=[whiterun_guard()])
    assert heckles(tmp_path) == 0 and "mean streak" not in prompt and "heckles" not in prompt
