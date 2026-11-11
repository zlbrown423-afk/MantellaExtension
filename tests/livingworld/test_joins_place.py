"""Three fixes from the 2026-11-10 play session (new game, Whiterun):

- Joins: Mantella's tool-calling model added everyone nearby (a cow included) on the first line of
  nearly every conversation. AddToConversation is now kept only for people the player asked for.
- Place: NPCs out in the street talked as if they were in their shop. The prompt now says outdoors or
  indoors.
- Held actions: Irileth and Caius took the player on but no armour came. Their EnlistRecruit arrived as
  a tool call (an action-only reply), which never reached the guard code; it now rides on the next line.
"""
import pytest

from livingworld.dialogue import joins, place
from livingworld.harness import actors
from livingworld.harness.scripted_llm import ScriptedLLM
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, KEY_GUARD_ISSUE, LivingWorld
from livingworld.memory import jobs
from src.http.communication_constants import communication_constants as comm_consts

from tests.livingworld.test_guard import PLAYER, caius, open_db, service, whiterun_guard

ADD = joins.ADD_ACTION
TOOLS = {"advanced_actions_enabled": True, "disabled_actions": []}   # AddToConversation is off by default
WEATHER = {comm_consts.KEY_CONTEXT_WEATHER: {"mantella_weather_classification": 0}}


@pytest.fixture(autouse=True)
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    for env in (joins.JOINS_ENV, place.PLACE_ENV):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(LivingWorld, "start_workers", False)
    monkeypatch.setattr(jobs, "BACKOFF_SECONDS", (0, 0, 0, 0, 0))


def add(*names):
    return {"identifier": ADD, "arguments": {"source": list(names)}}


# ---- joins ----

def test_nobody_is_added_when_the_player_did_not_ask():
    kept, dropped = joins.keep_asked([add("Whiterun Guard", "Severio Pelagia", "Cow", "Belethor")], "")
    assert kept == [] and dropped == ["Whiterun Guard", "Severio Pelagia", "Cow", "Belethor"]


def test_a_named_person_is_kept_and_the_rest_dropped():
    kept, dropped = joins.keep_asked([add("Whiterun Guard", "Belethor", "Severio Pelagia")], "Belethor, what do you think?")
    assert kept == [add("Belethor")] and dropped == ["Whiterun Guard", "Severio Pelagia"]


def test_a_generic_guard_is_called_by_role_not_by_town():
    assert joins.keep_asked([add("Whiterun Guard")], "Guard! Over there!")[0] == [add("Whiterun Guard")]
    assert joins.keep_asked([add("Whiterun Guard")], "I like Whiterun.")[0] == []


def test_calling_a_group_over_keeps_everyone():
    action = add("Hulda", "Olava the Feeble")
    assert joins.keep_asked([action], "Everyone, listen to this.")[0] == [action]
    assert joins.keep_asked([action], "Why don't you join us?")[0] == [action]


def test_other_actions_pass_through():
    other = {"identifier": "mantella_npc_follow", "arguments": {"source": "Lydia"}}
    assert joins.keep_asked([other, add("Cow")], "")[0] == [other]


def tool_session(tmp_path, tool_rules, npcs, nearby=(), say=None, context_extra=None):
    llm = ScriptedLLM(replies=["Hm."], tool_rules=tool_rules)
    with HeadlessSession(str(tmp_path), llm, settings=TOOLS) as s:
        nearby_ctx = {comm_consts.KEY_CONTEXT_NPCS_NEARBY: [{"name": n, "distance": 3.0} for n in nearby]}
        s.start([PLAYER, *npcs], location="Whiterun", game_days=3.4,
                events=["Torvar: I want to speak with you. (Start Mantella conversation)"],
                context_extra={**nearby_ctx, **(context_extra or {})})
        s.continue_until_player_turn()
        if say:
            s.say(say)
        s.end(game_days=3.41)
        LivingWorld.latest.wait_for_jobs()
        return s, llm


def sent_actions(s):
    return [a for _, r in s.exchanges
            for a in ((r.get(comm_consts.KEY_REPLYTYPE_NPCACTION) or {}).get(comm_consts.KEY_ACTOR_ACTIONS) or [])]


def test_a_tool_model_adding_everyone_on_the_first_line_is_stopped(tmp_path):
    rules = [("Start Mantella conversation", [("AddToConversation", {"source": ["Belethor", "Cow"]})])]
    s, _ = tool_session(tmp_path, rules, [caius()], nearby=["Belethor", "Cow"])
    assert not [a for a in sent_actions(s) if a["identifier"] == ADD]


def test_switching_asked_joins_off(tmp_path, monkeypatch):
    monkeypatch.setenv(joins.JOINS_ENV, "0")
    rules = [("Start Mantella conversation", [("AddToConversation", {"source": ["Belethor"]})])]
    s, _ = tool_session(tmp_path, rules, [caius()], nearby=["Belethor"])
    assert [a for a in sent_actions(s) if a["identifier"] == ADD]


# ---- an enlistment by tool call still hands over the uniform ----

def test_enlisting_by_tool_call_hands_over_the_armour(tmp_path):
    rules = [("join the guard", [("EnlistRecruit", {"source": "Commander Caius"})])]
    s, _ = tool_session(tmp_path, rules, [caius()], say="I want to join the guard.")
    assert [a for a in sent_actions(s) if a["identifier"] == "mantella_lw_guard_enlist"]   # still sent as before
    issued = [r[comm_consts.KEY_REPLYTYPE_NPCTALK][KEY_GUARD_ISSUE] for _, r in s.exchanges
              if KEY_GUARD_ISSUE in (r.get(comm_consts.KEY_REPLYTYPE_NPCTALK) or {})]
    assert len(issued) == 1 and "Whiterun Guard's Armor" in issued[0]
    svc = service(open_db(tmp_path))
    assert svc["rank_key"] == "recruit" and svc["status"] == "probation"


def test_a_tool_call_enlistment_is_only_handled_once(tmp_path):
    rules = [("join the guard", [("EnlistRecruit", {"source": "Commander Caius"})])]
    llm = ScriptedLLM(replies=["Welcome. Now, your armour.", "Off you go.", "Stay sharp."], tool_rules=rules)
    with HeadlessSession(str(tmp_path), llm, settings=TOOLS) as s:
        s.start([PLAYER, caius()], location="Whiterun", game_days=3.4)
        s.continue_until_player_turn()
        s.say("I want to join the guard.")
        s.say("Thank you.")
        s.end(game_days=3.41)
        LivingWorld.latest.wait_for_jobs()
    issued = [r for _, r in s.exchanges if KEY_GUARD_ISSUE in (r.get(comm_consts.KEY_REPLYTYPE_NPCTALK) or {})]
    assert len(issued) == 1
    assert open_db(tmp_path).execute("SELECT COUNT(*) FROM service").fetchone()[0] == 1


# ---- place ----

def test_place_lines():
    out = place.prompt_lines("Whiterun", outdoors=True)
    assert out[0].startswith("Outdoors in Whiterun") and "own shop" in out[1]
    assert place.prompt_lines("The Bannered Mare", outdoors=False)[0] == "Indoors, in The Bannered Mare."
    assert "does not say which" in place.prompt_lines("Whiterun", outdoors=False)[0]


def first_prompt(tmp_path, context_extra=None, location="Whiterun"):
    llm = ScriptedLLM(replies=["Hm."])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([PLAYER, whiterun_guard()], location=location, game_days=3.4, context_extra=context_extra)
        s.continue_until_player_turn()
        s.end(game_days=3.41)
        LivingWorld.latest.wait_for_jobs()
    return llm.dialogue_calls[0].system_prompt


def test_an_npc_in_the_street_is_told_they_are_outdoors(tmp_path):
    prompt = first_prompt(tmp_path, WEATHER)
    assert "Where Whiterun Guard is right now:\n- Outdoors in Whiterun" in prompt


def test_an_npc_indoors_is_told_where(tmp_path):
    assert "- Indoors, in Dragonsreach." in first_prompt(tmp_path, location="Dragonsreach")


def test_switching_place_off(tmp_path, monkeypatch):
    monkeypatch.setenv(place.PLACE_ENV, "0")
    assert "right now:\n- Outdoors" not in first_prompt(tmp_path, WEATHER)
