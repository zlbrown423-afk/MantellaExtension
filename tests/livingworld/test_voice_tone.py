"""Tone wheel + NPCs judging the live voice (livingworld/voice/tone.py, live.py, integration wiring)."""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import Future
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from livingworld.audio.handoff import MicSnapshot, UtteranceAudio
from livingworld.dialogue import asr_hud
from livingworld.integration import LivingWorld
from livingworld.voice import tone as T
from livingworld.voice.baseline import Baseline, Stat
from livingworld.voice.live import (MIN_LINES, LiveVoice, Reading, RollingBaseline, ToneState, latest_dropped,
                                    measure, mic_slug)
from src.game_manager import GameStateManager
from src.http.communication_constants import communication_constants as comm_consts

SR = 16000
RNG = np.random.default_rng(11)


# ---- tone names --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("raw, want", [("Stern", "stern"), ("Tone: Threatening", "threatening"), ("FLIRTY", "flirty"),
                                       ("menacing", "threatening"), ("Neutral", "neutral"), ("", "neutral"),
                                       ("angry-ish", None)])
def test_normalize(raw, want):
    assert T.normalize(raw) == want


def test_switches(monkeypatch):
    for env in (T.TONE_ENV, T.UNPICKED_ENV, T.HUD_ENV):
        monkeypatch.delenv(env, raising=False)
    assert T.enabled() and T.unpicked_enabled() and T.hud_enabled()
    monkeypatch.setenv(T.TONE_ENV, "0")
    monkeypatch.setenv(T.UNPICKED_ENV, "off")
    monkeypatch.setenv(T.HUD_ENV, "false")
    assert not (T.enabled() or T.unpicked_enabled() or T.hud_enabled())


# ---- cues --------------------------------------------------------------------------------------------------
def _keys(cues):
    return [c.key for c in cues]


def test_ordinary_voice_gives_only_steady():
    z = {"loudness_db": 0.3, "pitch_median_st": -0.4, "pitch_range_st": 0.2, "longest_pause_s": 0.0,
         "voiced_ratio": 0.1, "text_rate_syl_s": 0.5}
    assert _keys(T.cues_from(z, 0.0, 2.0)) == ["steady"]


def test_loud_quiet_shout_whisper():
    assert _keys(T.cues_from({"loudness_db": 1.6})) == ["loud"]
    assert _keys(T.cues_from({"loudness_db": 3.5})) == ["shout"]
    assert _keys(T.cues_from({"loudness_db": -1.8})) == ["quiet"]
    assert _keys(T.cues_from({"loudness_db": -3.2})) == ["whisper"]


def test_hesitation_needs_a_real_pause():
    z = {"longest_pause_s": 4.0, "voiced_ratio": -0.5}
    assert "hesitant" in _keys(T.cues_from(z, 0.9, 2.0))
    assert "hesitant" not in _keys(T.cues_from(z, 0.2, 2.0))        # a 0.2 s gap is no falter
    assert "steady" in _keys(T.cues_from(z, 0.2, 2.0))


def test_missing_measurements_give_no_cue():
    assert T.cues_from({k: None for k in ("loudness_db", "pitch_median_st", "voiced_ratio")}) == []


# ---- judging -----------------------------------------------------------------------------------------------
def _c(*keys, s=2):
    return [T._cue(k, s) for k in keys]


def test_hesitant_high_threat_is_a_bluff():
    j = T.judge("threatening", _c("hesitant", "high"))
    assert j.verdict == T.CLASHES
    assert "bluff" in j.reason


def test_low_slow_steady_threat_is_convincing():
    j = T.judge("threatening", _c("low", "slow") + _c("steady", s=1))
    assert j.verdict == T.CONVINCING
    assert j.reason == T.LAND["threatening"]


def test_one_clear_give_away_undermines_mixed_cues():
    j = T.judge("threatening", _c("high", "slow"))     # -2.0 + 1.2: the rising voice gives it away
    assert j.verdict == T.UNCONVINCING and "nervous" in j.reason


def test_loud_flirting_is_awkward():
    j = T.judge("flirty", _c("shout"))
    assert j.verdict == T.CLASHES and "awkward" in j.reason


def test_ordinary_voice_is_unreadable_for_a_strong_tone():
    assert T.judge("pleading", []).verdict == T.UNREAD
    assert T.judge("threatening", _c("steady", s=1)).verdict == T.UNREAD   # calm is not menace on its own


def test_polite_and_steady_fits():
    assert T.judge("polite", _c("steady", s=1) + _c("steady", s=1)).verdict in (T.FITS, T.CONVINCING)


def test_speechcraft_forgives_a_small_slip_only():
    slip = _c("quiet", s=1)                        # stern but a little quiet: -0.8
    assert T.judge("stern", slip, speech_skill=15).verdict == T.UNCONVINCING
    assert T.judge("stern", slip, speech_skill=100).verdict == T.UNREAD
    big = _c("hesitant", "high")
    assert T.judge("stern", big, speech_skill=100).verdict == T.CLASHES


def test_not_measured():
    j = T.judge("stern", None, why_unmeasured="typed, not spoken")
    assert j.verdict == T.NOT_MEASURED
    assert "go by the words" in T.note_text("Zachary", j)
    assert T.hud_line(j) == "[Tone] Stern: voice not measured"


def test_note_and_hud_wording():
    j = T.judge("threatening", _c("hesitant", "high"))
    note = T.note_text("Zachary", j)
    assert note.startswith("[Tone of voice] Zachary is trying to sound threatening.")
    assert "hesitant, with long pauses" in note and "bluff" in note
    assert T.hud_line(j) == "[Tone] Threatening: clashes (hesitant, higher pitch)"
    good = T.judge("friendly", _c("lively", "high"))
    assert T.hud_line(good).startswith("[Tone] Friendly: convincing")
    assert "Convincing" in T.note_text("Zachary", good)


def test_unpicked_note_only_for_striking_delivery():
    assert T.unpicked_note("Zachary", _c("loud", s=1) + _c("steady", s=1)) is None
    note = T.unpicked_note("Zachary", _c("shout"))
    assert note and "shouting" in note


def test_every_tone_has_fit_and_wording():
    for tone in T.TONES:
        assert tone in T.FIT and tone in T.LAND
        for key in T.CLASH.get(tone, {}):
            assert key in T._WORDS


# ---- rolling baseline --------------------------------------------------------------------------------------
def test_rolling_baseline_needs_lines_then_scores():
    b = RollingBaseline("mic", 0.0)
    assert b.z({"loudness_db": -40.0})["loudness_db"] is None
    for v in (-50, -51, -49, -50.5, -49.5, -50, -50.2):
        b.add({"loudness_db": float(v)})
    z = b.z({"loudness_db": -45.0})["loudness_db"]
    assert z is not None and z > 3


def test_voice_lab_fallback_never_used_for_loudness():
    lab = Baseline(mic="m", stats={"loudness_db": Stat(-50.0, 1.4, 10), "pitch_median_st": Stat(0.0, 1.0, 10)})
    b = RollingBaseline("m", 9.7, fallback=lab)
    z = b.z({"loudness_db": -40.0, "pitch_median_st": 2.0})
    assert z["loudness_db"] is None                # Voice Lab ran at another gain
    assert z["pitch_median_st"] == pytest.approx(2.0)


def test_dropped_measurements_are_ignored():
    b = RollingBaseline("m", 0.0, dropped={"pitch_median_st"})
    for v in range(MIN_LINES + 1):
        b.add({"pitch_median_st": 0.1 * v})
    assert b.z({"pitch_median_st": 5.0})["pitch_median_st"] is None


def test_gain_change_clears_loudness_on_load():
    old = RollingBaseline("m", 0.0)
    for v in range(8):
        old.add({"loudness_db": -50.0 + v * 0.1, "pitch_median_st": 0.1 * v})
    data = json.loads(json.dumps(old.to_json()))
    same, other = RollingBaseline("m", 0.5), RollingBaseline("m", 9.7)
    same.load_json(data)
    other.load_json(data)
    assert len(same.values["loudness_db"]) == 8
    assert len(other.values["loudness_db"]) == 0 and len(other.values["pitch_median_st"]) == 8


def test_latest_dropped_reads_newest_validation(tmp_path):
    for name, rows in (("20261101-1", "tilt_db,dropped\n"), ("20261116-1", "rate_syl_s,dropped\nloudness_db,kept\n")):
        d = tmp_path / "voice_lab" / name
        d.mkdir(parents=True)
        (d / "validation.csv").write_text("measurement,verdict\n" + rows, encoding="utf-8")
    assert latest_dropped(tmp_path) == {"rate_syl_s"}
    assert latest_dropped(tmp_path / "nothing") == set()


# ---- live readings -----------------------------------------------------------------------------------------
def _voice(f0=140.0, dur=2.0, amp=0.1, syl=4.0):
    t = np.arange(int(dur * SR)) / SR
    sig = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 8))
    env = 0.5 - 0.5 * np.cos(2 * np.pi * syl * t)
    return (amp * sig * env / 2).astype(np.float32)


def _utt(audio: np.ndarray, text: str = "hello there friend", end: float | None = None, overlap=None,
         gain=0.0, mic="Microphone (Steam Streaming Mic") -> UtteranceAudio:
    pad = RNG.normal(0, 0.0005, int(0.3 * SR)).astype(np.float32)
    a = np.concatenate([pad, audio, pad])
    nblk = len(a) // 512
    probs = np.zeros(nblk, np.float32)
    lo, hi = len(pad) // 512, (len(pad) + len(audio)) // 512
    probs[lo:hi] = 0.9
    fut: Future = Future()
    fut.set_result(text)
    end = time.time() if end is None else end
    snap = MicSnapshot(mic, "MME", SR, gain, True, -70.0, 50.0, 0, False, False, 0)
    return UtteranceAudio("u" + str(RNG.integers(1e9)), end - len(a) / SR, end, 0, len(a), a, SR, a, SR, probs,
                          512 / SR, "vad", False, 1.5, fut, overlap, snap)


def test_measure_real_audio():
    values, phonation, why = measure(_utt(_voice(dur=2.5)))
    assert why == ""
    assert values["loudness_db"] is not None and values["pitch_median_st"] is not None
    assert phonation and phonation > 1.5


def test_measure_marks_npc_overlap():
    values, _, why = measure(_utt(_voice(), overlap=True))
    assert "NPC" in why and values["loudness_db"] is None


def _live(tmp_path, measure_fn=measure) -> LiveVoice:
    return LiveVoice(tmp_path, subscribe=lambda fn: (lambda: None), start_thread=False, measure_fn=measure_fn)


def test_live_voice_learns_then_hears_a_shout(tmp_path):
    lv = _live(tmp_path)
    for i in range(MIN_LINES + 2):
        u = _utt(_voice(f0=140 + i, amp=0.05), text=f"ordinary line number {i} said calmly")
        r = Reading(u.utterance_id, u.speech_end_wall)
        lv.process(u, r)
    loud = _utt(_voice(f0=140, amp=0.6), text="get out of my house now")
    r = Reading(loud.utterance_id, loud.speech_end_wall)
    lv.process(loud, r)
    assert r.done.is_set()
    assert r.cues is not None and {"shout", "loud"} & {c.key for c in r.cues}
    saved = tmp_path / "baseline" / f"{mic_slug('Microphone (Steam Streaming Mic')}.ingame.json"
    lv.flush()
    assert saved.exists()
    data = json.loads(saved.read_text())
    assert "audio" not in json.dumps(data) and len(data["values"]["loudness_db"]) == MIN_LINES + 3


def test_reading_for_matches_transcript_and_consumes(tmp_path):
    lv = _live(tmp_path, measure_fn=lambda utt: ({"loudness_db": -50.0}, 2.0, ""))
    now = time.time()
    for text, end in (("first line", now - 5), ("Stand aside, or else.", now - 1)):
        u = _utt(_voice(dur=0.5), text=text, end=end)
        lv._on_utterance(u)
        utt, r = lv._q.get_nowait()
        lv.process(utt, r)
    r = lv.reading_for("first line", wait_s=0.1, now=now)
    assert r is not None and r.transcript == "first line"
    r2 = lv.reading_for("Stand aside or else", wait_s=0.1, now=now)   # punctuation added by the STT fix-up
    assert r2 is not None and r2.transcript == "Stand aside, or else."
    assert lv.reading_for("anything", wait_s=0.1, now=now) is None


def test_reading_for_ignores_old_utterances(tmp_path):
    lv = _live(tmp_path, measure_fn=lambda utt: ({}, None, "too short"))
    u = _utt(_voice(dur=0.5), text="old", end=time.time() - 120)
    lv._on_utterance(u)
    lv.process(*lv._q.get_nowait())
    assert lv.reading_for("old", wait_s=0.05) is None


def test_reading_for_waits_for_the_measurement(tmp_path):
    lv = _live(tmp_path, measure_fn=lambda utt: (time.sleep(0.2), ({"loudness_db": -50.0}, 2.0, ""))[1])
    u = _utt(_voice(dur=0.5), text="wait for me")
    lv._on_utterance(u)
    item = lv._q.get_nowait()
    threading.Thread(target=lv.process, args=item, daemon=True).start()
    r = lv.reading_for("wait for me", wait_s=2.0)
    assert r is not None and r.done.is_set()


def test_tone_state_lasts_until_cleared_or_idle():
    s = ToneState(max_idle_s=60)
    s.set("stern", 40, now=100.0)
    assert s.current(now=150.0) == "stern"
    s.touch(now=150.0)
    assert s.current(now=200.0) == "stern"
    assert s.current(now=300.0) is None
    s.set("pleading", now=0.0)
    s.set("neutral", now=1.0)
    assert s.current(now=2.0) is None
    s.set("flirty", now=5.0)
    s.clear()
    assert s.current(now=6.0) is None
    assert s.speech_skill == 40                    # kept from the last pick that sent it


# ---- integration -------------------------------------------------------------------------------------------
def _context(player="Zachary"):
    p = SimpleNamespace(name=player)
    npcs = SimpleNamespace(get_player_character=lambda: p)
    return SimpleNamespace(npcs_in_conversation=npcs)


def _lw(tmp_path, monkeypatch, audio_v2=False) -> LivingWorld:
    monkeypatch.setattr(LivingWorld, "start_workers", False)
    monkeypatch.delenv(T.TONE_ENV, raising=False)
    monkeypatch.setenv("MANTELLA_LW_AUDIO_V2", "1" if audio_v2 else "0")
    return LivingWorld(str(tmp_path), enabled=True)


def test_tone_set_reply_is_harmless_and_picks(tmp_path, monkeypatch):
    lw = _lw(tmp_path, monkeypatch)
    reply = lw.tone_set({comm_consts.KEY_REQUESTTYPE: T.REQUEST_SET, T.KEY_TONE: "Threatening", T.KEY_SPEECH: 35})
    assert reply[comm_consts.KEY_REPLYTYPE] == comm_consts.KEY_REPLYTTYPE_INITCOMPLETED
    assert lw._tone_state.current() == "threatening" and lw._tone_state.speech_skill == 35
    lw.tone_set({T.KEY_TONE: "nonsense"})
    assert lw._tone_state.current() == "threatening"
    lw.tone_set({T.KEY_TONE: "Neutral"})
    assert lw._tone_state.current() is None


def test_tone_notes_without_measurements_go_by_words(tmp_path, monkeypatch):
    lw = _lw(tmp_path, monkeypatch)
    assert lw.tone_notes("hello", _context()) == []          # no tone picked, nothing measured: no note
    lw.tone_set({T.KEY_TONE: "Stern"})
    notes = lw.tone_notes("Hand it over.", _context(), from_mic=False)
    assert len(notes) == 1 and "Zachary is trying to sound stern" in notes[0] and "go by the words" in notes[0]
    assert lw.tone_hud_lines() == ["[Tone] Stern: voice not measured"]
    assert lw.tone_hud_lines() == []


def test_tone_notes_with_a_reading(tmp_path, monkeypatch):
    lw = _lw(tmp_path, monkeypatch)
    lw.tone_set({T.KEY_TONE: "Threatening"})
    fake = MagicMock()
    fake.reading_for.return_value = SimpleNamespace(cues=_c("hesitant", "high"), why="")
    lw._live_voice = fake
    notes = lw.tone_notes("Give me your gold or else", _context())
    assert "bluff" in notes[0]
    assert lw.tone_hud_lines() == ["[Tone] Threatening: clashes (hesitant, higher pitch)"]


def test_striking_voice_without_a_pick(tmp_path, monkeypatch):
    lw = _lw(tmp_path, monkeypatch)
    lw._tone_parts()
    fake = MagicMock()
    fake.reading_for.return_value = SimpleNamespace(cues=_c("shout"), why="")
    lw._live_voice = fake
    assert "shouting" in lw.tone_notes("Get out!", _context())[0]
    assert lw.tone_hud_lines() == []
    monkeypatch.setenv(T.UNPICKED_ENV, "0")
    assert lw.tone_notes("Get out!", _context()) == []


def test_conversation_end_clears_the_pick(tmp_path, monkeypatch):
    lw = _lw(tmp_path, monkeypatch)
    lw.tone_set({T.KEY_TONE: "Flirty"})
    lw.conv_id = None
    lw.conversation_ended(None)
    assert lw._tone_state.current() is None


def test_off_switch(tmp_path, monkeypatch):
    monkeypatch.setattr(LivingWorld, "start_workers", False)
    monkeypatch.setenv(T.TONE_ENV, "0")
    lw = LivingWorld(str(tmp_path), enabled=True)
    lw.tone_set({T.KEY_TONE: "Stern"})
    assert lw._tone_state is None
    assert lw.tone_notes("hello", _context()) == []
    off = LivingWorld(str(tmp_path), enabled=False)
    assert off.tone_set({T.KEY_TONE: "Stern"})[comm_consts.KEY_REPLYTYPE] == comm_consts.KEY_REPLYTTYPE_INITCOMPLETED
    assert off.tone_notes("hello", _context()) is None


def test_hud_line_rides_on_the_reply_even_with_speech_hud_off(monkeypatch):
    gm = GameStateManager.__new__(GameStateManager)
    talk = MagicMock()
    talk.pop_lw_heard.return_value = "Give it here"
    gm._GameStateManager__talk = talk
    lw = LivingWorld.__new__(LivingWorld)
    lw.enabled, lw.errors, lw.asr_hud = True, 0, False
    lw._tone_hud = ["[Tone] Stern: convincing (loud)"]
    gm._GameStateManager__living_world = lw
    reply = {comm_consts.KEY_REPLYTYPE: comm_consts.KEY_REPLYTYPE_NPCTALK}
    monkeypatch.setattr(gm, "_GameStateManager__player_input", lambda j: dict(reply))
    out = gm.player_input({})
    assert out[asr_hud.KEY_HEARD] == ["[Tone] Stern: convincing (loud)"]
    assert out[asr_hud.KEY_SHOWS] >= 1
    lw.asr_hud = True
    lw._tone_hud = ["[Tone] Stern: fits"]
    out = gm.player_input({})
    assert out[asr_hud.KEY_HEARD] == ['[Mic] "Give it here"', "[Tone] Stern: fits"]


def test_conversation_adds_the_tone_note(default_context, default_chat_manager, default_rememberer, llm_client, monkeypatch):
    from src.conversation.conversation import Conversation
    from tests.livingworld.test_asr_hud import FakeSTT
    calls: list[tuple[str, bool]] = []
    lw = MagicMock()
    lw.tone_notes.side_effect = lambda text, ctx, from_mic=True: (calls.append((text, from_mic)) or ["[Tone of voice] X"])
    for name in ("hearing_note", "guard_notes", "fog_notes", "touch_notes", "undress_notes", "trip_notes",
                 "side_job_notes", "body_events", "personality_notes", "scene_notes"):
        getattr(lw, name).return_value = None
    default_chat_manager.living_world = lw
    conv = Conversation(default_context, default_chat_manager, default_rememberer, llm_client, FakeSTT(["Back off."]), True, False)
    monkeypatch.setattr(conv, "_Conversation__start_generating_npc_sentences", lambda: None)
    conv.process_player_input("")
    conv.process_player_input("typed line")
    assert calls == [("Back off.", True), ("typed line", False)]


def test_tone_pick_is_not_stuck_behind_a_player_waiting_to_speak():
    """The wheel is used while a player input request holds the conversation lock waiting for speech."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.http.routes.mantella_route import mantella_route

    class Game:
        picked: list = []

        def living_world_tone_set(self, j):
            self.picked.append(j[T.KEY_TONE])
            return {comm_consts.KEY_REPLYTYPE: comm_consts.KEY_REPLYTTYPE_INITCOMPLETED}

    route = object.__new__(mantella_route)
    route._mantella_route__game = Game()
    route._mantella_route__request_lock = threading.Lock()
    route._mantella_route__recorder = None
    route._config = SimpleNamespace(show_http_debug_messages=False)
    route._can_route_be_used = lambda: True
    app = FastAPI()
    route.add_route_to_server(app)
    route._mantella_route__request_lock.acquire()          # a player input request is waiting for speech
    try:
        started = time.monotonic()
        resp = TestClient(app).post("/mantella", json={comm_consts.KEY_REQUESTTYPE: T.REQUEST_SET, T.KEY_TONE: "Stern"})
        assert time.monotonic() - started < 2
    finally:
        route._mantella_route__request_lock.release()
    assert resp.json()[comm_consts.KEY_REPLYTYPE] == comm_consts.KEY_REPLYTTYPE_INITCOMPLETED
    assert Game.picked == ["Stern"]


def test_a_long_falter_mid_line_is_heard_as_hesitant(tmp_path):
    lv = _live(tmp_path)
    for i in range(MIN_LINES + 2):
        u = _utt(_voice(f0=140 + i, amp=0.05, dur=2.4), text=f"an ordinary line number {i} said calmly")
        lv.process(u, Reading(u.utterance_id, u.speech_end_wall))
    gap = RNG.normal(0, 0.0005, int(1.2 * SR)).astype(np.float32)
    u = _utt(np.concatenate([_voice(f0=140, amp=0.05, dur=1.4), gap, _voice(f0=140, amp=0.05, dur=1.0)]),
             text="I will hurt you if you do not move")
    a, b = (int(0.3 * SR) + int(1.4 * SR)) // 512 + 1, (int(0.3 * SR) + int(2.6 * SR)) // 512 - 1
    u.vad_probs[a:b] = 0.05                        # Silero drops during the silence, as it does for real
    r = Reading(u.utterance_id, u.speech_end_wall)
    lv.process(u, r)
    assert "hesitant" in {c.key for c in r.cues}
    assert T.judge("threatening", r.cues).verdict in (T.UNCONVINCING, T.CLASHES)
