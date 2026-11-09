"""The mood meter: the NPC's reply carries a mood tag that is never spoken, is stored, and is sent to
the game with their line; the start reply tells the game to mute vanilla chatter."""
import sqlite3

import pytest

from livingworld import mood as moods
from livingworld.harness import actors
from livingworld.harness.perception import KEY_EVENTS, consume
from livingworld.harness.scripted_llm import ScriptedLLM, when_player_says
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, KEY_MOOD, KEY_QUIET_CHATTER, MOOD_ENV, QUIET_ENV, LivingWorld
from src.http.communication_constants import communication_constants as comm_consts

PLAYER = actors.player("Torvar")


@pytest.fixture(autouse=True)
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.delenv(MOOD_ENV, raising=False)
    monkeypatch.delenv(QUIET_ENV, raising=False)
    monkeypatch.setattr(LivingWorld, "start_workers", False)


def talk_replies(session):
    return [reply[comm_consts.KEY_REPLYTYPE_NPCTALK] for _, reply in session.exchanges
            if reply.get(comm_consts.KEY_REPLYTYPE) == comm_consts.KEY_REPLYTYPE_NPCTALK
            and reply.get(comm_consts.KEY_REPLYTYPE_NPCTALK)]


def feed_all(chunks):
    seen = []
    f = moods.MoodFilter(seen.append)
    text = "".join(f.feed(c) for c in chunks)
    return text, seen


# ---- the filter ----
def test_tag_is_cut_out_and_reported():
    text, seen = feed_all(["<mood wary 4> Who are you?"])
    assert text == "Who are you?"
    assert seen == [moods.Mood("wary", 4)]


def test_tag_split_across_chunks():
    text, seen = feed_all(["<mo", "od: amused,", " 7/10>", " Ha", "! Good one."])
    assert text == "Ha! Good one."
    assert seen == [moods.Mood("amused", 7)]


def test_a_colon_after_the_tag_or_at_the_start_is_not_spoken():
    # 2026-11-08: Caius said "<mood wary 6>: What is it?", then ": I am here, speak your mind!"
    assert feed_all(["<mood wary 6>", ":", " What is it?"]) == ("What is it?", [moods.Mood("wary", 6)])
    assert feed_all([": I am here, ", "speak your mind!"])[0] == "I am here, speak your mind!"
    assert feed_all(["Aye. Listen: ", "the gate."])[0] == "Aye. Listen: the gate."


def test_other_angle_brackets_pass_through():
    text, seen = feed_all(["I <3 mead. ", "2 < 3 and 5 > 4."])
    assert text == "I <3 mead. 2 < 3 and 5 > 4."
    assert seen == []


def test_number_is_clamped_and_label_optional():
    assert moods.parse("<mood 14>") == moods.Mood("delighted", 10)
    assert moods.parse("<Mood=Very Angry 1>") == moods.Mood("very angry", 1)
    assert moods.parse("<mood>") is None


def test_meter_text():
    assert moods.meter_text("Gerdur", moods.Mood("wary", 4)) == "Gerdur: wary [====------]"
    assert moods.meter_text("Gerdur", moods.Mood("merry", 8), "tipsy") == "Gerdur: merry [========--] tipsy"


# ---- in a conversation ----
def test_mood_is_hidden_stored_and_sent_to_the_game(tmp_path):
    llm = ScriptedLLM(replies=["<mood wary 4> Who are you?"],
                      rules=[when_player_says("fine", "<mood wary 4> Fine then."),
                             when_player_says("idiot", "<mood furious 1> Get out of my sight!")])
    with HeadlessSession(str(tmp_path), llm) as s:
        start = s.start([PLAYER, actors.gerdur()], game_days=2.0)
        assert start[KEY_QUIET_CHATTER] is True
        lines = s.continue_until_player_turn()
        assert [l.text for l in lines] == ["Who are you?"]
        assert talk_replies(s)[-1][KEY_MOOD] == "Gerdur: wary [====------]"

        s.say("Fine.")
        assert KEY_MOOD not in talk_replies(s)[-1]          # unchanged, so not shown again

        lines = s.say("You idiot.")
        assert lines[0].text == "Get out of my sight!"
        assert talk_replies(s)[-1][KEY_MOOD] == "Gerdur: furious [=---------]"
        s.end(game_days=2.01)

    prompt = llm.dialogue_calls[0].system_prompt
    assert "<mood wary 4>" in prompt and "Gerdur" in prompt
    db = sqlite3.connect(tmp_path / "data" / "livingworld" / "HeadlessTest.sqlite")
    row = db.execute("SELECT e.mood_json FROM emotion_state e JOIN npc n USING (npc_id)"
                     " WHERE n.display_name = 'Gerdur'").fetchone()
    assert '"furious"' in row[0]
    lines = db.execute("SELECT text FROM utterance").fetchall()
    assert not any("<mood" in t for (t,) in lines)


def test_every_reply_is_reminded_to_carry_a_tag_and_the_reminder_is_not_stored(tmp_path):
    # 2026-11-08: only the first reply of each conversation had a tag (9 of 89), so grabbing someone
    # mid-conversation never moved their meter. Each call now ends with a reminder that is not kept.
    llm = ScriptedLLM(replies=["<mood wary 4> Who are you?"],
                      rules=[when_player_says("hands off", "<mood angry 2> Hands off me!")])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([PLAYER, actors.gerdur()], game_days=2.0)
        s.continue_until_player_turn()
        s.say("Fine.")
        lines = s.say("Hands off, then.")
        assert lines[0].text == "Hands off me!"
        assert talk_replies(s)[-1][KEY_MOOD] == "Gerdur: angry [==--------]"
        s.end(game_days=2.01)
    calls = llm.dialogue_calls
    assert len(calls) >= 3
    for call in calls:
        assert "start with a mood tag for how Gerdur feels" in call.messages[-1]["content"]
        assert sum("start with a mood tag" in str(m.get("content")) for m in call.messages) == 1


def run_once(tmp_path, day, reply="<mood calm 5> Hello.", context_extra=None):
    llm = ScriptedLLM(replies=[reply])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([PLAYER, actors.gerdur()], game_days=day, context_extra=context_extra)
        s.continue_until_player_turn()
        replies = talk_replies(s)
        s.end(game_days=day + 0.001)
    return llm.dialogue_calls[0].system_prompt, replies


def test_mood_carries_into_the_next_conversation_only_for_a_while(tmp_path):
    run_once(tmp_path, 2.0, "<mood annoyed 3> What now?")
    soon, _ = run_once(tmp_path, 2.2)
    assert "still feeling annoyed (3/10)" in soon
    later, _ = run_once(tmp_path, 3.5)
    assert "still feeling" not in later


def test_mood_from_a_later_point_is_ignored_after_loading_an_older_save(tmp_path):
    run_once(tmp_path, 5.0, "<mood annoyed 3> What now?")
    earlier, _ = run_once(tmp_path, 4.9)
    assert "still feeling" not in earlier


def test_drink_stage_shows_on_the_meter(tmp_path):
    drinks = [consume(actors.gerdur(), "Nord Mead", 3.0 + i * 0.001, is_food=False, via="action") for i in range(3)]
    _, replies = run_once(tmp_path, 3.01, "<mood merry 8> Another!", {KEY_EVENTS: drinks})
    assert replies[0][KEY_MOOD] == "Gerdur: merry [========--] tipsy"


def test_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv(MOOD_ENV, "0")
    monkeypatch.setenv(QUIET_ENV, "0")
    llm = ScriptedLLM(replies=["Hello."])
    with HeadlessSession(str(tmp_path), llm) as s:
        start = s.start([PLAYER, actors.gerdur()], game_days=2.0)
        s.continue_until_player_turn()
        assert KEY_QUIET_CHATTER not in start
        assert all(KEY_MOOD not in r for r in talk_replies(s))
        s.end(game_days=2.01)
    assert "<mood" not in llm.dialogue_calls[0].system_prompt


def test_off_without_living_world(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_VAR, "0")
    llm = ScriptedLLM(replies=["Hello."])
    with HeadlessSession(str(tmp_path), llm) as s:
        start = s.start([PLAYER, actors.gerdur()], game_days=2.0)
        s.continue_until_player_turn()
        assert KEY_QUIET_CHATTER not in start
        s.end(game_days=2.01)
    assert "<mood" not in llm.dialogue_calls[0].system_prompt


def test_group_talk_gives_each_npc_their_own_meter(tmp_path):
    llm = ScriptedLLM(replies=["<mood cheerful 7> Hod, say hello.", "<mood grumpy 3> Hello."])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([PLAYER, actors.gerdur(), actors.hod()], game_days=2.0)
        lines = s.continue_until_player_turn()
        assert all("<mood" not in l.text for l in lines)
        meters = {r[KEY_MOOD] for r in talk_replies(s) if KEY_MOOD in r}
        s.end(game_days=2.01)
    assert meters == {"Gerdur: cheerful [=======---]", "Hod: grumpy [===-------]"}
