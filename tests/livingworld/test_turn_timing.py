"""Turn timing log (livingworld/perf/turn_timeline.py): one record per player turn, phases in order,
the right speaker, nothing carried across conversations, and no change to what NPCs say."""
import threading
import time

import pytest

from livingworld.harness import actors
from livingworld.harness.scripted_llm import ScriptedLLM, when_player_says
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, LivingWorld
from livingworld.perf import turn_timeline as tt


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t

    def tick(self, s: float) -> None:
        self.t += s


@pytest.fixture(autouse=True)
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.delenv(tt.ENV_VAR, raising=False)
    monkeypatch.setattr(LivingWorld, "start_workers", False)
    tt.TURN.cancel()
    tt.TURN.finished.clear()
    yield
    tt.TURN.cancel()
    tt.TURN.finished.clear()


def spoken_turn(tl: tt.TurnTimeline, clock: Clock) -> dict:
    tl.start(mic=True)
    for phase, wait in ((tt.SPEECH, 2.0), (tt.PAUSE, 3.0), (tt.TRANSCRIPT, 0.6), (tt.AI_REQUEST, 0.004),
                        (tt.FIRST_TOKEN, 0.8), (tt.FIRST_SENTENCE, 0.4), (tt.VOICED, 0.5)):
        clock.tick(wait)
        tl.mark(phase)
    clock.tick(0.02)
    return tl.line_sent("Fralia Gray-Mane")


# ---- the timeline on its own ------------------------------------------------------------------

def test_a_spoken_turn_lists_every_phase_in_order():
    clock = Clock()
    tl = tt.TurnTimeline(clock)
    record = spoken_turn(tl, clock)
    assert record["speaker"] == "Fralia Gray-Mane" and record["input"] == "mic"
    assert [(a, b) for a, b, _ in record["gaps"]] == list(zip(tt.PHASES, tt.PHASES[1:]))
    assert [round(s, 3) for _, _, s in record["gaps"]] == [2.0, 3.0, 0.6, 0.004, 0.8, 0.4, 0.5, 0.02]
    line = tt.describe(record)
    assert "mic open to speech 2.00 s" in line and "transcript to AI request 0.004 s" in line
    assert "wait after speaking 2.32 s" in line   # pause -> first line in the game


def test_only_the_first_mark_of_a_phase_counts():
    clock = Clock()
    tl = tt.TurnTimeline(clock)
    tl.start(mic=False)
    tl.mark(tt.TRANSCRIPT)
    clock.tick(1.0)
    tl.mark(tt.AI_REQUEST)
    clock.tick(5.0)
    tl.mark(tt.AI_REQUEST)          # a second AI call in the same reply (group talk, tool follow-up)
    record = tl.line_sent("Hod")
    assert record["input"] == "typed"
    assert record["marks"][tt.AI_REQUEST] - record["marks"][tt.TRANSCRIPT] == pytest.approx(1.0)


def test_line_sent_reports_once_and_later_lines_of_the_reply_are_ignored():
    clock = Clock()
    tl = tt.TurnTimeline(clock)
    assert spoken_turn(tl, clock) is not None
    assert tl.line_sent("Fralia Gray-Mane") is None      # second sentence of the same reply
    tl.mark(tt.FIRST_TOKEN)                               # a late mark with no turn open
    assert tl.line_sent("Fralia Gray-Mane") is None
    assert len(tl.finished) == 1


def test_a_greeting_is_not_a_turn():
    tl = tt.TurnTimeline(Clock())
    tl.mark(tt.AI_REQUEST)
    assert tl.line_sent("Gerdur") is None and not tl.finished


def test_a_line_resent_with_fresh_game_events_continues_the_same_turn():
    clock = Clock()
    tl = tt.TurnTimeline(clock)
    tl.start(mic=True)
    clock.tick(4.0)
    tl.mark(tt.TRANSCRIPT)
    clock.tick(0.3)
    tl.start(mic=False)             # the game re-sends the transcript with fresh events
    clock.tick(0.01)
    tl.mark(tt.AI_REQUEST)
    record = tl.line_sent("Gerdur")
    assert record["input"] == "mic" and tt.MIC_OPEN in record["marks"]


def test_an_interruption_keeps_the_speech_heard_before_the_request():
    clock = Clock()
    tl = tt.TurnTimeline(clock)
    tl.mark(tt.SPEECH)              # the player talks over the NPC's line
    clock.tick(0.5)
    tl.start(mic=True)              # the game then asks for the player's input
    clock.tick(1.0)
    tl.mark(tt.PAUSE)
    tl.mark(tt.TRANSCRIPT)
    record = tl.line_sent("Gerdur")
    assert tt.SPEECH in record["marks"]
    # the mic-open mark came after speech started, so that gap is left out rather than shown negative
    assert all(s >= 0 for _, _, s in record["gaps"])


def test_cancel_drops_the_open_turn():
    clock = Clock()
    tl = tt.TurnTimeline(clock)
    tl.start(mic=True)
    tl.mark(tt.TRANSCRIPT)
    tl.cancel()
    assert tl.line_sent("Gerdur") is None and not tl.finished


def test_off_switch(monkeypatch):
    monkeypatch.setenv(tt.ENV_VAR, "0")
    clock = Clock()
    tl = tt.TurnTimeline(clock)
    assert spoken_turn(tl, clock) is None and not tl.finished


def test_marks_from_many_threads_make_one_consistent_turn():
    tl = tt.TurnTimeline()
    tl.start(mic=True)
    barrier = threading.Barrier(16)
    errors = []

    def worker(i):
        try:
            barrier.wait()
            for _ in range(200):
                tl.mark(tt.PHASES[1 + i % 7])
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    record = tl.line_sent("Gerdur")
    assert not errors
    assert set(record["marks"]) == set(tt.PHASES)
    assert len(tl.finished) == 1


def test_a_mark_costs_microseconds():
    tl = tt.TurnTimeline()
    tl.start(mic=True)
    n = 20000
    started = time.perf_counter()
    for _ in range(n):
        tl.mark(tt.FIRST_TOKEN)
    per_mark_us = (time.perf_counter() - started) / n * 1e6
    print(f"turn timing: {per_mark_us:.2f} us per mark")
    assert per_mark_us < 50


# ---- through the server -----------------------------------------------------------------------

def talk(work_dir, llm, people, *lines):
    with HeadlessSession(str(work_dir), llm) as s:
        s.start(people, location="Riverwood", game_days=5.0)
        s.continue_until_player_turn()
        for line in lines:
            s.say(line)
        s.end(game_days=5.1)
    return s


def test_one_record_per_typed_turn_naming_the_npc_who_answered(tmp_path):
    llm = ScriptedLLM(replies=["Welcome to Riverwood.", "The mill is busy.", "Aye."])
    s = talk(tmp_path, llm, [actors.player("Torvar"), actors.gerdur()], "How is the mill?", "Goodbye for now, friend.")
    records = list(tt.TURN.finished)
    assert len(records) >= 1
    assert all(r["speaker"] == "Gerdur" and r["input"] == "typed" for r in records)
    assert all(tt.TRANSCRIPT in r["marks"] and tt.LINE_SENT in r["marks"] for r in records)
    assert tt.AI_REQUEST in records[0]["marks"]   # the goodbye line that ends the talk needs no AI call
    assert all(s_ >= 0 for r in records for _, _, s_ in r["gaps"])


def test_group_talk_records_the_npc_who_spoke_first(tmp_path):
    llm = ScriptedLLM(replies=["Hello.", "Aye."], rules=[when_player_says("Hod, how", "The mill keeps me busy.")])
    talk(tmp_path, llm, [actors.player("Torvar"), actors.gerdur(), actors.hod()], "Hod, how is the mill?")
    assert [r["speaker"] for r in tt.TURN.finished] == ["Hod"]


def test_nothing_carries_into_the_next_conversation(tmp_path):
    tt.TURN.start(mic=True)
    tt.TURN.mark(tt.TRANSCRIPT)          # a turn left open, then the conversation ends
    llm = ScriptedLLM(replies=["Welcome."])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([actors.player("Torvar"), actors.gerdur()], location="Riverwood", game_days=5.0)
        s.end(game_days=5.1)             # the end request cancels it
    assert tt.TURN.line_sent("Gerdur") is None and not tt.TURN.finished


def test_replies_are_the_same_with_the_log_on_or_off(tmp_path, monkeypatch):
    def lines(sub, flag):
        monkeypatch.setenv(tt.ENV_VAR, flag)
        llm = ScriptedLLM(replies=["Welcome to Riverwood.", "Hello there."],
                          rules=[when_player_says("spiders", "Spiders? I'll keep the broom handy.")])
        s = talk(tmp_path / sub, llm, [actors.player("Torvar"), actors.gerdur(), actors.hod()],
                 "Spiders terrify me.", "Hod, say hello.")
        return [(l.speaker, l.text) for l in s.lines], [c.messages for c in llm.dialogue_calls]

    on = lines("on", "1")
    off = lines("off", "0")
    assert on == off
