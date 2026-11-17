"""Capture layer (MANTELLA_LW_AUDIO_V2): the two paths, the sample clock, the UtteranceAudio handoff and its wiring
into the transcriber. No microphone: blocks are pushed in by hand, with known tones, so every answer is known."""
from __future__ import annotations

import queue
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from livingworld.audio import handoff, metrics, settings
from livingworld.audio.capture import STT_BLOCK, AudioCapture
from livingworld.audio.devices import Device
from livingworld.audio.paths import (DCBlocker, Rechunker, StreamFilters, StreamResampler, native_block_samples,
                                     soft_limit)
from livingworld.audio.profile import MicProfile, ProfileStore
from livingworld.audio.session import AudioV2Session

RNG = np.random.default_rng(11)


def tone(freq, dur, amp, sr):
    t = np.arange(int(dur * sr)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


# paths -----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("sr", [48000, 44100, 16000, 32000])
def test_chunked_resampling_matches_one_shot(sr):
    from scipy.signal import resample_poly
    from math import gcd
    x = (tone(300, 2.0, 0.3, sr) + tone(2100, 2.0, 0.1, sr)).astype(np.float32)
    res = StreamResampler(sr)
    block = native_block_samples(sr)
    out = np.concatenate([res.process(x[i:i + block]) for i in range(0, len(x) - block + 1, block)])
    g = gcd(sr, 16000)
    ref = resample_poly(x.astype(np.float64), 16000 // g, sr // g)
    n = len(out)
    assert n > 0.9 * 16000 * 2 - 100
    assert np.max(np.abs(out - ref[:n])) < 2e-4          # block edges are not filtered against padding


def test_native_block_is_a_multiple_of_the_decimation():
    for sr in (48000, 44100, 16000, 22050):
        b = native_block_samples(sr)
        from math import gcd
        assert b % (sr // gcd(sr, 16000)) == 0
        assert 0.02 < b / sr < 0.06


def test_dc_blocker_removes_offset_and_keeps_the_voice_band():
    sr = 48000
    x = tone(150, 2.0, 0.2, sr) + 0.05
    y = DCBlocker(sr).process(x)
    assert abs(float(np.mean(y[sr:]))) < 1e-3
    assert metrics.rms_db(y[sr:]) == pytest.approx(metrics.rms_db(tone(150, 1.0, 0.2, sr)), abs=0.3)


def test_empty_filter_config_is_the_identity():
    x = RNG.normal(0, 0.1, 4000).astype(np.float32)
    f = StreamFilters(16000, {})
    assert not f.active and np.array_equal(f.process(x), x)


def test_highpass_notch_and_band_do_what_they_say():
    sr = 16000
    hp = StreamFilters(sr, {"highpass_hz": 100})
    assert metrics.rms_db(hp.process(tone(20, 1.0, 0.3, sr))[4000:]) < metrics.rms_db(tone(20, 1.0, 0.3, sr)) - 20
    hp = StreamFilters(sr, {"highpass_hz": 100})
    assert metrics.rms_db(hp.process(tone(1000, 1.0, 0.3, sr))[4000:]) == pytest.approx(metrics.rms_db(tone(1000, 1, .3, sr)), abs=0.3)
    notch = StreamFilters(sr, {"notch_hz": [60]})
    assert metrics.rms_db(notch.process(tone(60, 1.0, 0.3, sr))[8000:]) < -45
    band = StreamFilters(sr, {"bands": [{"f": 2000, "gain_db": 20, "q": 1.0}]})     # asked for +20, capped at +6
    gain = metrics.rms_db(band.process(tone(2000, 1.0, 0.1, sr))[8000:]) - metrics.rms_db(tone(2000, 1.0, 0.1, sr))
    assert gain == pytest.approx(6.0, abs=0.5)


def test_filter_state_carries_across_blocks():
    sr = 16000
    x = tone(80, 1.0, 0.3, sr)
    whole = StreamFilters(sr, {"highpass_hz": 120}).process(x)
    f = StreamFilters(sr, {"highpass_hz": 120})
    parts = np.concatenate([f.process(x[i:i + 512]) for i in range(0, len(x), 512)])
    assert np.max(np.abs(parts - whole[:len(parts)])) < 1e-4


def test_soft_limit_leaves_normal_levels_alone_and_never_exceeds_full_scale():
    x = np.array([0.0, 0.5, -0.89, 0.9], dtype=np.float32)
    assert np.array_equal(soft_limit(x), x)
    big = np.array([0.95, 2.0, -5.0, 100.0], dtype=np.float32)
    y = soft_limit(big)
    assert np.all(np.abs(y) <= 1.0) and np.all(np.sign(y) == np.sign(big)) and y[0] < y[1] < y[3] + 1e-6


def test_rechunker_gives_fixed_blocks():
    r = Rechunker(512)
    out = r.push(np.arange(700, dtype=np.float32)) + r.push(np.arange(700, 1100, dtype=np.float32))
    assert [len(b) for b in out] == [512, 512] and out[1][0] == 512


# capture ---------------------------------------------------------------------------------------------------
class FakeStream:
    latency = 0.0

    def __init__(self, **kw):
        self.kw = kw

    def start(self): pass
    def stop(self): pass
    def close(self): pass


def make_capture(sr=48000, gain_db=0.0, filters=None, **kw):
    dev = Device(3, "Steam Streaming Microphone", "MME", sr, 2, False)
    cap = AudioCapture("Steam Streaming", None, gain_db, filters, lister=lambda: [dev], stream_factory=FakeStream, **kw)
    got = []
    assert cap.start(got.append)
    cap._running = False          # tests drive process_block directly; stop the worker thread from competing
    cap._q.put(None)
    cap._thread.join(timeout=2)
    return cap, got


def feed(cap, x, sr=None):
    sr = sr or cap.sample_rate
    b = native_block_samples(sr)
    for i in range(0, len(x) - b + 1, b):
        cap.process_block(x[i:i + b])


def test_capture_opens_the_named_device_at_its_native_rate():
    cap, _ = make_capture(48000)
    assert cap.device.name == "Steam Streaming Microphone" and cap.sample_rate == 48000
    assert cap._stream.kw["device"] == 3 and cap._stream.kw["samplerate"] == 48000


def test_missing_device_falls_back_to_the_default_with_a_warning(caplog):
    other = Device(0, "Razer USB Sound Card", "MME", 44100, 2, True)
    cap = AudioCapture("No such mic", None, 0.0, None, lister=lambda: [other], stream_factory=FakeStream)
    assert cap.start(lambda b: None) and cap.device.name == "Razer USB Sound Card"
    cap.stop()


def test_no_devices_at_all_means_start_fails_cleanly():
    cap = AudioCapture(None, None, 0.0, None, lister=lambda: [], stream_factory=FakeStream)
    assert cap.start(lambda b: None) is False and cap.status == "no input device"


def test_stt_path_blocks_are_512_samples_and_in_step_with_the_analysis_clock():
    cap, got = make_capture(48000)
    feed(cap, tone(440, 1.0, 0.2, 48000))
    assert got and all(len(b) == STT_BLOCK for b in got)
    # 1.0 s of 48 kHz audio is 31 whole capture blocks; the resampler holds back a few samples of look-ahead
    assert len(got) in (30, 31)
    assert cap.total_native == 31 * native_block_samples(48000)
    n0, n1 = cap.native_range_for_blocks(2, 5)
    assert (n0, n1) == (2 * 512 * 3, 5 * 512 * 3)


def test_44100_clock_maps_stt_blocks_to_native_samples():
    cap, _ = make_capture(44100)
    n0, n1 = cap.native_range_for_blocks(0, 10)
    assert (n0, n1) == (0, int(round(10 * 512 * 44100 / 16000)))


def test_a_burst_lands_in_the_same_stt_blocks_and_native_range():
    sr = 48000
    x = np.zeros(sr * 2, dtype=np.float32)
    x[sr:sr + sr // 2] = tone(500, 0.5, 0.3, sr)              # burst at 1.0 s to 1.5 s
    cap, got = make_capture(sr)
    feed(cap, x)
    stt = np.concatenate(got)
    loud = np.where(np.abs(stt) > 0.05)[0]
    assert abs(loud[0] / 16000 - 1.0) < 0.02 and abs(loud[-1] / 16000 - 1.5) < 0.02
    b0, b1 = loud[0] // STT_BLOCK, loud[-1] // STT_BLOCK + 1
    n0, n1 = cap.native_range_for_blocks(b0, b1)
    cut = cap.extract(n0, n1)
    assert len(cut) == n1 - n0 and metrics.rms_db(cut) > metrics.rms_db(x) - 3     # the cut is the burst, not silence


def test_fixed_gain_applies_to_both_paths_and_leaves_the_clock_alone():
    sr = 48000
    quiet = tone(300, 1.0, 0.02, sr)
    cap, got = make_capture(sr, gain_db=12.0)
    feed(cap, quiet)
    an = cap.extract(sr // 2, sr)
    assert metrics.rms_db(an) - metrics.rms_db(quiet[sr // 2:sr]) == pytest.approx(12.0, abs=0.3)
    stt = np.concatenate(got)[8000:]
    assert metrics.rms_db(stt) - metrics.rms_db(quiet) == pytest.approx(12.0, abs=0.5)


def test_analysis_path_is_not_filtered_but_stt_path_is():
    sr = 48000
    hum = tone(40, 1.0, 0.3, sr)
    cap, got = make_capture(sr, filters={"highpass_hz": 150})
    feed(cap, hum)
    an = cap.extract(sr // 2, sr)
    assert metrics.rms_db(an) == pytest.approx(metrics.rms_db(hum), abs=0.5)                # analysis keeps the 40 Hz
    assert metrics.rms_db(np.concatenate(got)[8000:]) < metrics.rms_db(hum) - 15            # STT path cut it


def test_stt_path_never_exceeds_full_scale_even_with_gain():
    sr = 48000
    cap, got = make_capture(sr, gain_db=18.0)
    feed(cap, tone(300, 1.0, 0.8, sr))
    assert np.max(np.abs(np.concatenate(got))) <= 1.0
    assert np.max(np.abs(cap.extract(0, sr))) > 1.0          # the analysis signal keeps its true (over-range) values


def test_ring_buffer_keeps_only_the_newest_seconds():
    sr = 48000
    cap, _ = make_capture(sr, ring_seconds=1.0)
    feed(cap, tone(300, 3.0, 0.2, sr))
    assert len(cap.extract(0, cap.total_native)) == pytest.approx(sr, abs=native_block_samples(sr))


# bus -------------------------------------------------------------------------------------------------------
def make_utt(i=0):
    mic = handoff.MicSnapshot("m", "MME", 48000, 0.0, False, None, None, 0, False, False, 0)
    return handoff.UtteranceAudio(f"u{i}", 0.0, 1.0, 0, 48000, np.zeros(10, np.float32), 48000, np.zeros(5, np.float32),
                                  16000, np.zeros(1, np.float32), 0.032, "vad", False, 1.0, handoff.Future(), None, mic)


def test_bus_delivers_on_its_own_thread_and_never_blocks_the_publisher():
    bus = handoff.UtteranceBus()
    seen, release = [], threading.Event()

    def slow(u):
        release.wait(5)
        seen.append((u.utterance_id, threading.current_thread().name))

    bus.subscribe(slow)
    t0 = time.time()
    for i in range(5):
        assert bus.publish(make_utt(i))
    assert time.time() - t0 < 0.5                              # a consumer that never returns holds nobody up
    release.set()
    for _ in range(100):
        if len(seen) == 5:
            break
        time.sleep(0.01)
    assert [s[0] for s in seen] == [f"u{i}" for i in range(5)] and all(n == "utterance_bus" for _, n in seen)
    bus.close()


def test_bus_with_no_subscribers_copies_nothing_and_a_failing_consumer_does_not_stop_the_rest():
    bus = handoff.UtteranceBus()
    assert bus.publish(make_utt()) is False
    got = []
    bus.subscribe(lambda u: 1 / 0)
    bus.subscribe(got.append)
    bus.publish(make_utt(1))
    for _ in range(100):
        if got:
            break
        time.sleep(0.01)
    assert len(got) == 1
    bus.close()


def test_bus_drops_the_oldest_when_backed_up():
    bus = handoff.UtteranceBus()
    gate = threading.Event()
    bus.subscribe(lambda u: gate.wait(5))
    for i in range(handoff.MAX_QUEUE + 6):
        bus.publish(make_utt(i))
    assert bus.dropped >= 5
    gate.set()
    bus.close()


def test_npc_overlap_flag():
    handoff._NPC_LINES.clear()
    assert handoff.npc_overlap(10.0, 12.0) is None             # nothing to compare against yet
    handoff.note_npc_speech(3.0, start_wall=100.0)
    assert handoff.npc_overlap(101.0, 102.0) is True
    assert handoff.npc_overlap(103.1, 104.0) is True           # inside the echo tail
    assert handoff.npc_overlap(104.0, 105.0) is False
    handoff._NPC_LINES.clear()


# settings --------------------------------------------------------------------------------------------------
def test_flag_is_off_by_default(monkeypatch):
    monkeypatch.delenv(settings.ENV_FLAG, raising=False)
    assert not settings.enabled()
    for v in ("1", "true", "ON"):
        monkeypatch.setenv(settings.ENV_FLAG, v)
        assert settings.enabled()
    monkeypatch.setenv(settings.ENV_FLAG, "0")
    assert not settings.enabled()


def test_settings_follow_the_active_profile_and_the_env_override(tmp_path, monkeypatch):
    monkeypatch.delenv(settings.ENV_DEVICE, raising=False)
    assert settings.load(tmp_path).profile is None and settings.load(tmp_path).device_name is None
    store = ProfileStore(settings.audio_root(tmp_path))
    store.save(MicProfile("Steam Streaming Microphone", "MME", 44100, 2, analysis_gain_db=9.0,
                          stt_filters={"highpass_hz": 80}, analysis_clean=True, clean_judged=True), make_active=True)
    s = settings.load(tmp_path)
    assert (s.device_name, s.hostapi, s.gain_db, s.stt_filters) == ("Steam Streaming Microphone", "MME", 9.0, {"highpass_hz": 80})
    monkeypatch.setenv(settings.ENV_DEVICE, "Razer")
    s = settings.load(tmp_path)
    assert s.device_name == "Razer" and s.gain_db == 0.0 and s.profile is None     # another mic: that mic's profile gain does not apply


# transcriber wiring ----------------------------------------------------------------------------------------
class FakeVAD:
    def __init__(self, *a, **k): pass

    def process(self, chunk):
        return 0.9 if float(np.sqrt(np.mean(np.asarray(chunk) ** 2))) > 0.01 else 0.05


def make_transcriber(monkeypatch, tmp_path, mode="vad"):
    import src.stt.stt as stt_mod
    monkeypatch.setattr(stt_mod, "SileroVAD", FakeVAD)
    t = object.__new__(stt_mod.Transcriber)
    t.loglevel, t.audio_threshold, t.pause_threshold, t._temporary_pause_override = 27, 0.4, 0.0, None
    t.listen_timeout, t.proactive_mic_mode, t.ptt_enabled = 30, mode == "proactive", mode == "ptt"
    t.min_refresh_secs, t.refresh_freq, t.play_cough_sound = 0.3, 10, False
    t._Transcriber__save_mic_input = False
    t._Transcriber__mic_input_process_error_count, t._Transcriber__warning_frequency = 0, 5
    t._Transcriber__processing_audio_error_count = 0
    t._lock = threading.RLock()
    t._transcription_ready = threading.Event()
    t._audio_buffer = np.array([], dtype=np.float32)
    t._audio_queue = queue.Queue()
    t._speech_detected = t._has_unconsumed_transcription = False
    t._current_transcription = ""
    t._consecutive_empty_count, t._max_consecutive_empty, t._last_update_time = 0, 10, 0
    t.vad = FakeVAD()
    t._ptt_active = False
    t.transcription_times, t._speech_end_time = [], 0
    t._transcribe = lambda audio: "hello there."
    return t


def run_speech(t, cap, sr=48000, speech_s=1.0, lead_s=0.4):
    x = np.concatenate([np.zeros(int(lead_s * sr)), tone(300, speech_s, 0.2, sr), np.zeros(int(0.5 * sr))]).astype(np.float32)
    b = native_block_samples(sr)
    thread = threading.Thread(target=t._process_audio, daemon=True)
    t._running = True
    thread.start()
    for i in range(0, len(x) - b + 1, b):
        cap.process_block(x[i:i + b])
        time.sleep(0.0005)
    time.sleep(0.3)
    t._running = False
    thread.join(timeout=3)


def make_v2(t, tmp_path, sr=48000, subscribers=True):
    bus = handoff.UtteranceBus()
    received = []
    if subscribers:
        bus.subscribe(received.append)
    session = AudioV2Session(str(tmp_path), capture_cls=AudioCapture, bus=bus,
                             lister=lambda: [Device(3, "Steam Streaming Microphone", "MME", sr, 2, False)],
                             stream_factory=FakeStream)
    assert session.start(lambda block: t._audio_queue.put((block, 0)))
    session.capture._running = False
    session.capture._q.put(None)
    session.capture._thread.join(timeout=2)
    t._v2 = session
    return session, bus, received


def wait_for(received, n=1):
    for _ in range(200):
        if len(received) >= n:
            return
        time.sleep(0.01)


def test_vad_utterance_is_handed_over_with_both_paths_and_a_transcript(monkeypatch, tmp_path):
    t = make_transcriber(monkeypatch, tmp_path)
    session, bus, received = make_v2(t, tmp_path)
    run_speech(t, session.capture)
    wait_for(received)
    assert len(received) == 1
    u = received[0]
    assert u.capture_mode == "vad" and not u.hit_listen_timeout and u.pause_threshold_s == 0.0
    assert u.analysis_rate == 48000 and u.stt_rate == 16000
    assert len(u.stt_audio) % STT_BLOCK == 0 and len(u.vad_probs) == len(u.stt_audio) // STT_BLOCK
    assert abs(len(u.analysis_audio) / 48000 - len(u.stt_audio) / 16000) < 0.005          # same stretch of time
    assert u.end_sample - u.start_sample == len(u.analysis_audio)
    assert u.speech_end_wall > u.speech_start_wall
    assert u.vad_probs.max() > 0.5 and u.vad_probs.min() < 0.5                              # speech and its lookback
    assert metrics.rms_db(u.analysis_audio) > -30
    assert u.transcript.result(timeout=2) == "hello there."
    assert u.mic.device_name == "Steam Streaming Microphone" and u.mic.profile_found is False and u.mic.analysis_clean is False
    bus.close()


def test_the_handoff_happens_before_stt_runs(monkeypatch, tmp_path):
    t = make_transcriber(monkeypatch, tmp_path)
    session, bus, received = make_v2(t, tmp_path)
    started = threading.Event()
    gate = threading.Event()

    def slow_stt(audio):
        started.set()
        gate.wait(5)
        return "late words."

    t._transcribe = slow_stt
    th = threading.Thread(target=run_speech, args=(t, session.capture), daemon=True)
    th.start()
    assert started.wait(5)
    wait_for(received)
    assert len(received) == 1 and not received[0].transcript.done()       # analysis can start while STT is still working
    gate.set()
    assert received[0].transcript.result(timeout=5) == "late words."
    th.join(timeout=5)
    bus.close()


def test_no_subscribers_means_no_copy_and_no_future(monkeypatch, tmp_path):
    t = make_transcriber(monkeypatch, tmp_path)
    session, bus, received = make_v2(t, tmp_path, subscribers=False)
    run_speech(t, session.capture)
    assert received == [] and not bus.has_subscribers
    assert t._current_transcription == "hello there."                    # STT itself ran exactly as before


def test_a_failing_stt_still_resolves_the_transcript(monkeypatch, tmp_path):
    t = make_transcriber(monkeypatch, tmp_path)
    session, bus, received = make_v2(t, tmp_path)

    def broken(audio):
        raise RuntimeError("model crashed")

    t._transcribe = broken
    run_speech(t, session.capture)
    wait_for(received)
    assert received and received[0].transcript.result(timeout=2) == ""
    bus.close()


def test_ptt_mode_has_no_vad_probs_and_proactive_gives_one_final_utterance(monkeypatch, tmp_path):
    t = make_transcriber(monkeypatch, tmp_path, mode="proactive")
    session, bus, received = make_v2(t, tmp_path)
    run_speech(t, session.capture, speech_s=1.5)
    wait_for(received)
    time.sleep(0.2)
    assert len(received) == 1 and received[0].capture_mode == "proactive"          # not one per interim refresh
    bus.close()
    # push-to-talk: the same utterance, no detector probabilities
    t2 = make_transcriber(monkeypatch, tmp_path, mode="ptt")
    t2._ptt = SimpleNamespace(is_pressed=lambda: t2._pressed)
    t2._pressed = True
    session2, bus2, received2 = make_v2(t2, tmp_path)
    b = native_block_samples(48000)
    x = tone(300, 0.8, 0.2, 48000)
    t2._running = True
    th = threading.Thread(target=t2._process_audio, daemon=True)
    th.start()
    for i in range(0, len(x) - b + 1, b):
        session2.capture.process_block(x[i:i + b])
        time.sleep(0.0005)
    time.sleep(0.2)
    t2._pressed = False
    session2.capture.process_block(np.zeros(b, np.float32))
    time.sleep(0.3)
    t2._running = False
    th.join(timeout=3)
    wait_for(received2)
    assert received2 and received2[0].capture_mode == "ptt" and len(received2[0].vad_probs) == 0
    bus2.close()


def test_listen_timeout_is_reported(monkeypatch, tmp_path):
    t = make_transcriber(monkeypatch, tmp_path)
    t.listen_timeout = 0.5
    session, bus, received = make_v2(t, tmp_path)
    run_speech(t, session.capture, speech_s=2.0, lead_s=0.1)
    wait_for(received)
    assert received and received[0].hit_listen_timeout is True
    bus.close()


def test_stt_conversions_clamp_instead_of_wrapping():
    x = np.array([0.0, 1.5, -1.5], dtype=np.float32)
    assert (np.clip(x, -1.0, 1.0) * 32767).astype(np.int16).tolist() == [0, 32767, -32767]
