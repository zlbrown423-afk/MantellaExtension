"""A leader who says they take the player on, without the EnlistRecruit prefix or tool call, still enlists them
(the rules permitting), as seen in game on 2026-11-12: "I'll enlist you as a recruit... Here's your armor." """
from livingworld.guard import enlist_words as EW
from livingworld.harness.scripted_llm import ScriptedLLM, when_player_says
from livingworld.harness.session import HeadlessSession
from livingworld.integration import KEY_GUARD_ISSUE, LivingWorld
from src.http.communication_constants import communication_constants as comm_consts

from tests.livingworld.test_guard import DAY, PLAYER, caius, living_world_inline, open_db, service  # noqa: F401

SAID = when_player_says("ready to start", "Very well, Zachary. I'll enlist you as a recruit for a probationary period "
                        "of seven days. Here's your armor. Wear it with pride.")


def run(tmp_path, npcs=None, rule=SAID):
    llm = ScriptedLLM(rules=[rule], replies=["Hm."])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([PLAYER, *(npcs or [caius()])], location="Whiterun", game_days=DAY)
        s.continue_until_player_turn()
        s.say("I'm ready to start now.")
        s.end(game_days=DAY + 0.01)
        LivingWorld.latest.wait_for_jobs()
        return [r[comm_consts.KEY_REPLYTYPE_NPCTALK][KEY_GUARD_ISSUE] for _, r in s.exchanges
                if KEY_GUARD_ISSUE in (r.get(comm_consts.KEY_REPLYTYPE_NPCTALK) or {})]


def test_saying_it_enlists_and_hands_over_the_uniform(tmp_path):
    issued = run(tmp_path)
    svc = service(open_db(tmp_path))
    assert svc is not None and svc["rank_key"] == "recruit" and svc["status"] == "probation"
    assert len(issued) == 1 and "Whiterun Guard's Armor" in issued[0]


def test_the_rules_still_decide(tmp_path):
    issued = run(tmp_path, npcs=[caius(bounty=200)])
    assert service(open_db(tmp_path)) is None and issued == []


def test_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv(EW.ENV, "0")
    assert run(tmp_path) == [] and service(open_db(tmp_path)) is None


def test_what_counts():
    yes = ["I'll enlist you as a recruit for a probationary period of seven days.",
           "Here's your armor.", "Here is your uniform, recruit.", "Welcome to the Whiterun Guard.",
           "I hereby welcome you as a recruit of the Whiterun Guard.", "You're one of us now.",
           "Very well, I'll take you on.",
           # 2026-11-12 in game, after a reload to before enlisting
           "Enlist in the Whiterun guard it is.", "I will provide you with your gear and your duties as a recruit.",
           "Excellent, Zachary. I will get you suited up in your new armor."]
    no = ["If you've enlisted as a recruit, then yes, you will receive your armor and gear.",
          "Will you swear to serve the Jarl?", "I won't enlist the likes of you.",
          "I'll enlist you once you've proven yourself.", "We can get you your armor and begin your duties.",
          "Prove yourself first, and I'll take you on.", "Training is essential for a guard.",
          "If you're here to start your recruit training, then let's get you geared up.",
          "Let's head to the barracks where I can hand over your gear.",
          "We can stop by after you're geared up.", "Your gear will be provided to you, Zachary.",
          "If you're looking for your gear, best ask Commander Caius."]
    assert [t for t in yes if not EW.says_enlisted(t)] == []
    assert [t for t in no if EW.says_enlisted(t)] == []
