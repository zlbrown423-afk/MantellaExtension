"""Misheard speech: the NPC's prompt holds a rule for garbled lines, and a player line with likely
mishearings carries a note naming them. The player's words themselves are never rewritten."""
import pytest

from livingworld.dialogue import hearing
from livingworld.harness import actors
from livingworld.harness.scripted_llm import ScriptedLLM
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, LivingWorld

PLAYER = actors.player("Torvar")


@pytest.fixture(autouse=True)
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.delenv(hearing.HEARING_ENV, raising=False)
    monkeypatch.setattr(LivingWorld, "start_workers", False)


# ---- the notes ----
def test_thane_of_whiterun():
    assert hearing.notes("As stain of white run, I order you to stand down.") == [
        '"stain of white run" may be "thane of Whiterun"']


def test_thane_as_a_form_of_address():
    assert hearing.notes("Yes, my stain.") == ['"my stain" may be "thane"']
    assert hearing.notes("There's a stain on my shirt.") == []


def test_names_and_places():
    found = hearing.notes("Have you seen Kai's near River would? Jar Balgruuf wants him.")
    assert '"Kai\'s" may be "Caius"' in found
    assert '"River would" may be "Riverwood"' in found
    assert hearing.notes("Back to River wood.") == []   # only split in two: no need to point it out
    assert '"Jar Balgruuf" may be "Jarl"' in found


def test_correct_words_are_left_alone():
    assert hearing.notes("I am thane of Whiterun and I serve Jarl Balgruuf.") == []
    assert hearing.notes("Seven septims for the mead? Fine.") == []
    assert hearing.notes("") == []


def test_near_miss_of_someone_in_the_scene():
    assert hearing.notes("Gerdar, where is your husband?", ["Gerdur", "Hod"]) == ['"Gerdar" may be "Gerdur"']
    assert hearing.notes("Gerdur, where is your husband?", ["Gerdur", "Hod"]) == []
    assert hearing.notes("I have seven coins.", ["Sven"]) == []


def test_note_text():
    assert hearing.note_text("Hello there.", ["Gerdur"], "Torvar") is None
    note = hearing.note_text("As stain of white run", ["Gerdur"], "Torvar")
    assert note == ("Torvar's words were hard to make out; possibly misheard: "
                    '"stain of white run" may be "thane of Whiterun".')


def test_rule_mentions_the_listener():
    lines = hearing.prompt_lines(["Gerdur"], "Torvar")
    assert "what Gerdur hears" in lines[0]
    assert any("short question" in line for line in lines)
    assert "what those listening hear" in hearing.prompt_lines(["Gerdur", "Hod"], "Torvar")[0]


# ---- in a conversation ----
def test_prompt_holds_the_rule_and_the_line_carries_the_note(tmp_path):
    llm = ScriptedLLM(replies=["Who are you?", "Thane, you say?"])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([PLAYER, actors.gerdur()], game_days=2.0)
        s.continue_until_player_turn()
        s.say("As stain of white run, I need your help.")
        call = llm.dialogue_calls[-1]
        assert "Hearing Torvar:" in call.system_prompt
        user = call.last_user_text
        assert '"stain of white run" may be "thane of Whiterun"' in user
        assert "As stain of white run, I need your help." in user   # the words themselves are kept


def test_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv(hearing.HEARING_ENV, "0")
    llm = ScriptedLLM(replies=["Who are you?", "What?"])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([PLAYER, actors.gerdur()], game_days=2.0)
        s.continue_until_player_turn()
        s.say("As stain of white run, I need your help.")
        call = llm.dialogue_calls[-1]
        assert "Hearing Torvar:" not in call.system_prompt
        assert "may be" not in call.last_user_text


def test_riften_words():
    assert hearing.notes("I could catch him outside the pond pron.") == ['"pond pron" may be "Pawned Prawn"']
    assert hearing.notes("Ma'avin sent me to look for him.") == ['"Ma\'avin" may be "Maven"']
    assert hearing.notes("Looking for a gentleman named Wald.") == ['"named Wald" may be "named Vald"']
    assert hearing.notes("I opened the vault door.") == []


def test_the_rule_gives_no_word_to_parrot():
    rule = " ".join(hearing.prompt_lines(["Laila"], "Titus"))
    assert "Thane, you say" not in rule and "Never ask about a word they did not say" in rule
