"""Fewer "How about you?" replies (livingworld/dialogue/turn_back.py)."""
from types import SimpleNamespace

import pytest

from livingworld.dialogue import turn_back
from livingworld.dialogue.turn_back import TurnBackTrimmer, find, strip


@pytest.mark.parametrize("text, kept", [
    ("I'm well enough. How about you?", "I'm well enough."),
    ("Busy day at the mill. And you?", "Busy day at the mill."),
    ("I'm doing fine, and you?", "I'm doing fine."),
    ("Can't complain, what about you?", "Can't complain."),
    ("Long day. What about yourself, traveler?", "Long day."),
    ("Long day. What of you, friend?", "Long day."),
    ("Not bad at all. How about you, Zachary?", "Not bad at all."),
    ("Not bad at all. So how about you then?", "Not bad at all."),
    ("Quiet, thankfully. And yourself?", "Quiet, thankfully."),
    ("Quiet, thankfully. And how are you?", "Quiet, thankfully."),
    ("Weather's turning — and you?", "Weather's turning."),
    ("How about you?", ""),
    ("*smiles* Good. How about you, my friend?", "*smiles* Good."),
])
def test_strip_cuts_the_bare_hand_back(text, kept):
    assert find(text) is not None
    assert strip(text) == kept


@pytest.mark.parametrize("text", [
    "How about you come with me to the Sleeping Giant?",
    "What about you and Hod, have you made up?",
    "How about you, ever been to Solitude?",
    "Was it you?",
    "How are you?",
    "I asked how you were, and you said fine.",
    "And you thought I wouldn't notice?",
    "What do you think about the war?",
    "I'll see you at the inn.",
])
def test_specific_questions_are_left_alone(text):
    assert find(text) is None
    assert strip(text) == text


def test_first_hand_back_is_kept_then_repeats_are_cut():
    t = TurnBackTrimmer(gap=4)
    t.start_reply()
    assert t.trim("Gerdur", "Fine. How about you?") == "Fine. How about you?"
    for _ in range(3):   # the next three replies
        t.start_reply()
        assert t.trim("Gerdur", "Busy. And you?") == "Busy."
    t.start_reply()      # fourth reply after the kept one: allowed again
    assert t.trim("Gerdur", "Tired. And you?") == "Tired. And you?"
    assert t.cut == 3


def test_second_hand_back_in_one_reply_is_cut():
    t = TurnBackTrimmer()
    t.start_reply()
    assert t.trim("Hod", "Well, how about you?") == "Well, how about you?"
    assert t.trim("Hod", "Fine day. And yourself?") == "Fine day."


def test_speakers_are_counted_apart():
    t = TurnBackTrimmer()
    t.start_reply()
    assert t.trim("Hod", "Good. And you?") == "Good. And you?"
    t.start_reply()
    assert t.trim("Gerdur", "Good. And you?") == "Good. And you?"
    t.start_reply()
    assert t.trim("Hod", "Good. And you?") == "Good."


def test_never_leaves_the_npc_silent():
    t = TurnBackTrimmer()
    t.start_reply()
    t.trim("Hod", "Good. And you?")
    t.start_reply()
    assert t.trim("Hod", "And you?") == "And you?"     # nothing said yet this reply: keep it
    t.start_reply()
    t.trim("Hod", "Same old.")
    assert t.trim("Hod", "And you?") == ""             # already said something: drop it


def test_lines_without_a_hand_back_pass_through():
    t = TurnBackTrimmer()
    t.start_reply()
    assert t.trim("Hod", "The mill won't run itself.") == "The mill won't run itself."
    assert t.cut == 0


def test_off_switch(monkeypatch):
    monkeypatch.setenv(turn_back.TURN_BACK_ENV, "0")
    assert not turn_back.enabled()
    monkeypatch.setenv(turn_back.TURN_BACK_ENV, "1")
    assert turn_back.enabled()
    monkeypatch.delenv(turn_back.TURN_BACK_ENV)
    assert turn_back.enabled()


def test_prompt_rule_names_the_player_and_offers_alternatives():
    lines = turn_back.prompt_lines(["Gerdur"], "Zachary")
    text = " ".join(lines)
    assert "Zachary" in text and "How about you?" in text and "specific" in text


# ---- wiring ----
class _Speaker:
    def __init__(self, name, player=False):
        self.name = name
        self.is_player_character = player


def _lw(on=True):
    from livingworld.integration import LivingWorld
    lw = LivingWorld.__new__(LivingWorld)
    lw.turn_back = on
    lw._turn_back = TurnBackTrimmer()
    lw.errors = 0
    return lw


def _content(name, text, system=False, player=False):
    return SimpleNamespace(speaker=_Speaker(name, player), text=text, is_system_generated_sentence=system)


def test_living_world_trims_repeats():
    lw = _lw()
    lw.reply_started()
    first = _content("Gerdur", "Fine. And you?")
    lw.trim_turn_back(first)
    assert first.text == "Fine. And you?"
    lw.reply_started()
    second = _content("Gerdur", "Busy. And you?")
    lw.trim_turn_back(second)
    assert second.text == "Busy."


def test_living_world_leaves_system_player_and_off_alone():
    lw = _lw()
    for c in (_content("Gerdur", "Busy. And you?", system=True), _content("Zachary", "Busy. And you?", player=True)):
        lw.reply_started()
        lw._turn_back.trim(c.speaker.name, "x. And you?")   # use up the allowance
        lw.reply_started()
        lw.trim_turn_back(c)
        assert c.text == "Busy. And you?"
    off = _lw(on=False)
    for _ in range(3):
        off.reply_started()
        c = _content("Gerdur", "Busy. And you?")
        off.trim_turn_back(c)
        assert c.text == "Busy. And you?"


def test_output_manager_runs_the_trim_before_tts():
    """generate_sentence hands each spoken line to Living World before it is voiced."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "src" / "output_manager.py").read_text(encoding="utf-8")
    gen = src[src.index("def generate_sentence"):src.index("def __should_stream_first_line")]
    assert gen.index("living_world.trim_turn_back(content)") < gen.index("text = ' ' + content.text")
    proc = src[src.index("async def process_response"):]
    assert proc.index("reply_started()") < proc.index("streaming_call(")
