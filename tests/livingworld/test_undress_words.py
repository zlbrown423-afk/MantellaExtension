"""Undress from words: an NPC who says she is taking something off does it, after the player's yes
(livingworld/dialogue/undress_words.py)."""
import pytest

from livingworld.actions import UNDRESS
from livingworld.dialogue import undress_words as U
from livingworld.dialogue.undress_words import Offer, UndressPlanner, UndressWatcher, normalize_item, npc_takes_off
from livingworld.harness import actors
from livingworld.harness.scripted_llm import ScriptedLLM, when_player_says
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, KEY_HINTS, LivingWorld
from src.actions.function_manager import FunctionManager
from src.http.communication_constants import communication_constants as comm_consts

PLAYER = actors.player("Zachary")


@pytest.mark.parametrize("line, part, word", [
    ("I suppose it might be time to take a dip without my tunic.", "body", "tunic"),   # Fralia, 2026-11-12
    ("Let me slip out of this dress.", "body", "dress"),
    ("I'll take off my belted tunic.", "body", "tunic"),
    ("I'll take my boots off.", "feet", "boots"),
    ("Let me take off my hat.", "head", "hat"),
    ("Fine, I'll strip.", "all", "clothes"),
    ("I'll undress, if you don't mind.", "all", "clothes"),
    ("I'll get out of these wet clothes.", "all", "clothes"),
    ("Here goes, off with my clothes!", "all", "clothes"),
])
def test_taking_something_off_now(line, part, word):
    assert npc_takes_off(line) == Offer(part, word)


@pytest.mark.parametrize("line", [
    "Just trying to enjoy the water without losing my tunic.",   # the line before, in the same session
    "Alright, let me join you properly.",
    "Shall I take off my tunic?",
    "I won't take off my tunic.",
    "No, I'll keep my dress on.",
    "I'll undress once we are home.",
    "Maybe I'll take off my hat later.",
    "Let me put my tunic back on.",
    "That tunic of yours is lovely.",
])
def test_not_taking_anything_off(line):
    assert npc_takes_off(line) is None


@pytest.mark.parametrize("value, part", [
    (None, "all"), ("", "all"), ("tunic", "body"), ("Belted Tunic", "body"), ("her boots", "feet"),
    ("helmets", "head"), ("body", "body"), ("everything", "all"), ("nonsense", "all"),
])
def test_tool_call_item_names_a_part(value, part):
    assert normalize_item(value) == part


def _ask(planner, gate=None):
    w = UndressWatcher(planner, [UNDRESS], gate=gate, pronoun_of=lambda _n: "her")
    line: list = []
    w.keep("Fralia Gray-Mane", "I suppose it might be time to take a dip without my tunic.", line)
    return line


def test_yes_puts_undress_for_that_piece_on_her_next_line():
    p = UndressPlanner(player_name="Zachary")
    assert _ask(p) == []
    assert p.take_notices() == ["Fralia is taking off her tunic. Let Fralia? Say yes or no."]
    note = p.player_said("Yes.")
    assert "Zachary is happy for Fralia Gray-Mane to take off her tunic" in note
    line: list = []
    UndressWatcher(p, [UNDRESS]).keep("Fralia Gray-Mane", "Here goes!", line)
    assert line == [{"identifier": UNDRESS, "arguments": {"source": "Fralia Gray-Mane", "item": "body"}}]


def test_no_keeps_it_on_and_is_not_asked_again():
    p = UndressPlanner(player_name="Zachary")
    _ask(p)
    p.take_notices()
    assert "kept her tunic on" in p.player_said("No, leave it.")
    _ask(p)
    assert p.take_notices() == []
    line: list = []
    UndressWatcher(p, [UNDRESS]).keep("Fralia Gray-Mane", "Here goes!", line)
    assert line == []


def test_she_can_still_change_her_mind_after_the_yes():
    p = UndressPlanner(player_name="Zachary")
    _ask(p)
    p.player_said("Yes.")
    line: list = []
    UndressWatcher(p, [UNDRESS]).keep("Fralia Gray-Mane", "Actually, I'd rather not. Not here.", line)
    assert line == []


def test_an_ignored_question_lapses():
    p = UndressPlanner()
    _ask(p)
    p.player_said("How's the water?")
    p.player_said("Lovely day.")
    assert p.question is None and p.go is None


def test_her_limits_are_never_overridden():
    p = UndressPlanner()
    line = _ask(p, gate=lambda name, part: "her own limit: not where other people can see")
    assert line == [] and p.take_notices() == [] and p.question is None
    assert p.take_notes() == ["Fralia Gray-Mane thought better of taking off her tunic and keeps it on "
                              "(her own limit: not where other people can see)."]
    p.player_said("Yes.")
    assert p.go is None


def test_no_question_when_the_ai_already_called_undress():
    p = UndressPlanner()
    line = [{"identifier": UNDRESS, "arguments": {"source": "Fralia Gray-Mane"}}]
    UndressWatcher(p, [UNDRESS]).keep("Fralia Gray-Mane", "Let me slip out of this tunic.", line)
    assert p.take_notices() == [] and len(line) == 1


def test_no_question_when_undress_is_not_loaded():
    p = UndressPlanner()
    UndressWatcher(p, []).keep("Fralia Gray-Mane", "Let me slip out of this tunic.", [])
    assert p.take_notices() == []


# ---------------------------------------------------------------- through the whole server

@pytest.fixture
def living_world_on(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.setenv("MANTELLA_LW_TASKS", "0")
    monkeypatch.delenv(U.ENV, raising=False)
    monkeypatch.setattr(LivingWorld, "start_workers", False)


@pytest.fixture(autouse=True)
def reset_actions():
    yield
    FunctionManager._actions.clear()
    FunctionManager._last_tool_calls = []


def _dip(tmp_path, answer):
    llm = ScriptedLLM(rules=[when_player_says("tunic", "You have a point there! I suppose it might be time to "
                                                       "take a dip without my tunic.")],
                      replies=["Here goes nothing!"])
    with HeadlessSession(str(tmp_path), llm, settings={"disabled_actions": ""}) as s:
        s.start([PLAYER, actors.gerdur()], location="Whiterun", game_days=3.5)
        s.continue_until_player_turn()
        first = s.say("And your tunic.")
        second = s.say(answer)
        s.end(game_days=3.51)
        keys = [r.get(comm_consts.KEY_REPLYTYPE_NPCTALK) or {} for _, r in s.exchanges]
    return first, second, keys, llm


def test_in_game_flow_yes(living_world_on, monkeypatch, tmp_path):
    monkeypatch.setenv("MANTELLA_LW_PERSONALITY", "0")   # no profile, so no limits of hers in play
    first, second, keys, llm = _dip(tmp_path, "Yes.")
    assert not any(a for line in first for a in line.actions)
    hints = [h for k in keys for h in (k.get(KEY_HINTS) or [])]
    assert "Gerdur is taking off her tunic. Let Gerdur? Say yes or no." in hints
    sent = [a for line in second for a in line.actions]
    assert {"identifier": UNDRESS, "arguments": {"source": "Gerdur", "item": "body"}} in sent
    assert "Zachary is happy for Gerdur to take off her tunic" in llm.dialogue_calls[-1].last_user_text


def test_in_game_flow_her_limit_with_a_stranger(living_world_on, tmp_path):
    # Gerdur's generated profile says "nothing intimate with someone she does not know well", and she has
    # never met Zachary: no question, the model hears she thought better of it, nothing comes off.
    _, second, keys, llm = _dip(tmp_path, "Yes.")
    assert not any("tunic" in h for k in keys for h in (k.get(KEY_HINTS) or []))
    assert not any(a.get("identifier") == UNDRESS for line in second for a in line.actions)
    assert "Gerdur thought better of taking off her tunic" in llm.dialogue_calls[-1].last_user_text


def test_in_game_flow_no(living_world_on, monkeypatch, tmp_path):
    monkeypatch.setenv("MANTELLA_LW_PERSONALITY", "0")
    _, second, _, _ = _dip(tmp_path, "No.")
    assert not any(a.get("identifier") == UNDRESS for line in second for a in line.actions)


def test_switched_off(living_world_on, monkeypatch, tmp_path):
    monkeypatch.setenv(U.ENV, "0")
    _, second, keys, _ = _dip(tmp_path, "Yes.")
    assert not any(a.get("identifier") == UNDRESS for line in second for a in line.actions)
    assert not any("tunic" in h for k in keys for h in (k.get(KEY_HINTS) or []))
