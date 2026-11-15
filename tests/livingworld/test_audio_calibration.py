"""Mic calibration: metrics, profile store and the calibrator driven with synthetic audio (no device, no VAD library).
Known inputs give known answers: a sine of amplitude A has RMS A/sqrt(2); a hum is a 60 Hz harmonic series."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from livingworld.audio import metrics as M
from livingworld.audio.profile import MicProfile, ProfileStore, profile_key
from tools.voice_lab.calibration import CAL_PAGE, Calibrator

SR = 16000
BS = 512
RNG = np.random.default_rng(3)


def tone(freq, dur, amp, sr=SR):
    t = np.arange(int(dur * sr)) / sr
    return amp * np.sin(2 * np.pi * freq * t)


def speechlike(dur, amp=0.1, sr=SR):
    """Harmonic tone with a 4 Hz syllable envelope: loud syllables, near-silent gaps."""
    t = np.arange(int(dur * sr)) / sr
    sig = sum(np.sin(2 * np.pi * 150 * k * t) / k for k in range(1, 6))
    env = np.clip(np.sin(2 * np.pi * 0.8 * t), 0, None) ** 0.5     # words of ~0.6 s with pauses between
    return amp * sig * env / 2


def probs_for(audio, bs=BS, thresh_db=-45):
    n = len(audio) // bs
    blocks = audio[:n * bs].reshape(n, bs)
    lv = 10 * np.log10(np.mean(blocks ** 2, axis=1) + 1e-12)
    return (lv > thresh_db).astype(np.float32)


# metrics ---------------------------------------------------------------------------------------------------
def test_levels_of_a_sine():
    x = tone(440, 1.0, 0.5)
    assert M.rms_db(x) == pytest.approx(-9.03, abs=0.1)        # 0.5 / sqrt(2)
    assert M.peak_db(x) == pytest.approx(-6.02, abs=0.05)
    assert M.rms_db(np.zeros(100)) == M.FLOOR_DB


def test_window_levels_follow_a_level_step():
    x = np.concatenate([tone(300, 1.0, 0.01), tone(300, 1.0, 0.1)])
    w = M.window_db(x, SR)
    assert len(w) == 20 and w[0] == pytest.approx(M.rms_db(tone(300, 1, 0.01)), abs=0.5)
    assert w[-1] - w[0] == pytest.approx(20.0, abs=0.5)


def test_clipping_counts_full_scale_and_flat_tops():
    x = tone(200, 1.0, 0.3)
    assert not M.clipping(x).any
    y = np.clip(tone(200, 1.0, 2.0), -0.95, 0.95).astype(np.float32)   # flat tops, below full scale
    c = M.clipping(y)
    assert c.full_scale == 0 and c.flat_top_runs > 0 and c.any
    z = x.copy()
    z[100:105] = 1.0
    assert M.clipping(z).full_scale == 5


def test_noise_floor_ignores_a_click():
    x = RNG.normal(0, 0.001, SR * 10)
    x[5000:5100] = 0.9
    assert M.noise_floor_db(x, SR) == pytest.approx(-60, abs=1.5)


def test_snr_rating_bands():
    assert [M.snr_rating(v) for v in (30, 20, 5, None)] == ["good", "usable", "poor", "unknown"]


def test_speech_level_needs_enough_speech():
    x = tone(300, 1.0, 0.1)
    assert M.speech_level_db(x, np.ones(len(x), bool)) == pytest.approx(M.rms_db(x), abs=0.1)
    assert M.speech_level_db(x, np.zeros(len(x), bool)) is None


def test_third_octave_puts_a_tone_in_its_band():
    bands = M.third_octave_db(tone(1000, 2.0, 0.2), SR)
    assert max(bands, key=bands.get) == "1000"
    assert bands["1000"] == pytest.approx(M.rms_db(tone(1000, 2, 0.2)), abs=1.0)


def test_hum_detected_at_60_and_not_in_plain_noise():
    hum = sum(tone(60 * h, 3.0, 0.01 / h) for h in range(1, 5)) + RNG.normal(0, 0.0005, SR * 3)
    assert M.detect_hum(hum, SR).base_hz == 60
    assert M.detect_hum(RNG.normal(0, 0.001, SR * 3), SR).base_hz is None


def test_recommended_gain_is_limited():
    assert M.recommended_gain_db(-33.0) == 10.0
    assert M.recommended_gain_db(-60.0) == 18.0
    assert M.recommended_gain_db(-5.0) == -12.0
    assert M.recommended_gain_db(None) is None


def _speech_with_gaps(gap_noise):
    audio = speechlike(8.0) + RNG.normal(0, gap_noise, SR * 8)
    return audio.astype(np.float32), probs_for(audio)


def test_upstream_clean_when_gaps_match_the_room():
    audio, probs = _speech_with_gaps(0.001)
    up = M.upstream_processing(audio, probs, BS, M.noise_floor_db(RNG.normal(0, 0.001, SR * 10), SR))
    assert up.judged and up.clean, up.reasons


def test_upstream_flags_a_gate_that_silences_the_pauses():
    audio = speechlike(8.0).astype(np.float32)
    probs = probs_for(audio)
    up = M.upstream_processing(audio, probs, BS, room_floor_db=-60.0)
    assert up.judged and not up.clean
    assert any("quieter than the room" in r or "digital zero" in r for r in up.reasons)


def test_upstream_not_judged_without_pauses():
    audio = tone(150, 3.0, 0.1)
    up = M.upstream_processing(audio, np.ones(len(audio) // BS, np.float32), BS, -60.0)
    assert not up.judged and not up.clean


def test_upstream_flags_automatic_gain_recovering_in_pauses():
    parts, probs = [], []
    for _ in range(4):
        word = speechlike(1.0)
        parts.append(word)
        probs += [1.0] * (len(word) // BS)
        ramp = np.linspace(0.0005, 0.02, SR * 1) * RNG.normal(0, 1, SR)   # noise swells back after the word
        parts.append(ramp)
        probs += [0.0] * (SR // BS)
    audio = np.concatenate(parts).astype(np.float32)
    up = M.upstream_processing(audio, np.array(probs, np.float32), BS, room_floor_db=-50.0)
    assert any("automatic gain" in r for r in up.reasons), up.reasons


# profile store ---------------------------------------------------------------------------------------------
def test_profile_round_trip_and_active(tmp_path):
    store = ProfileStore(tmp_path)
    assert store.active() is None and store.all() == {}
    a = MicProfile("Steam Streaming Microphone", "Windows WASAPI", 48000, 2, snr_db=31.0, analysis_gain_db=6.5)
    b = MicProfile("Razer USB Sound Card", "MME", 44100, 2)
    key_a = store.save(a)
    key_b = store.save(b)
    assert store.active().device_name == "Steam Streaming Microphone"      # first saved becomes active
    assert store.set_active(key_b) and store.active().device_name == "Razer USB Sound Card"
    assert not store.set_active("nope")
    assert store.all()[key_a].analysis_gain_db == 6.5
    assert key_a == profile_key("Steam Streaming Microphone", "Windows WASAPI")


def test_profile_file_is_numbers_only_and_ignores_unknown_keys(tmp_path):
    store = ProfileStore(tmp_path)
    store.save(MicProfile("Mic", "MME", 48000, 1))
    data = json.loads(store.path.read_text(encoding="utf-8"))
    p = list(data["profiles"].values())[0]
    p["future_field"] = 1
    store.path.write_text(json.dumps(data), encoding="utf-8")
    assert store.active().device_name == "Mic"
    assert not any(isinstance(v, (bytes, bytearray)) for v in p.values())


def test_corrupt_profile_file_reads_as_empty(tmp_path):
    (tmp_path / "mic_profiles.json").write_text("{not json", encoding="utf-8")
    assert ProfileStore(tmp_path).all() == {}


# calibrator ------------------------------------------------------------------------------------------------
class FakeLab:
    def __init__(self, name="Steam Streaming Microphone", hostapi="Windows WASAPI", sr=SR, channels=2):
        self.block_listeners = []
        dev = SimpleNamespace(name=name, hostapi=hostapi, sample_rate=sr, channels=channels)
        self.capture = SimpleNamespace(device=dev, sample_rate=sr, block_samples=BS, status="recording")
        self.level_db, self.peak_hold_db, self.clipped_total = -50.0, -40.0, 0

    def feed(self, audio, probs):
        for i, p in enumerate(probs):
            for fn in self.block_listeners:
                fn(audio[i * BS:(i + 1) * BS], float(p))


def _record(cal, lab, step, audio, probs=None):
    cal.record(step)
    audio = np.concatenate([audio, np.zeros(BS * 2, np.float32)])      # a whole number of blocks past the target
    lab.feed(audio, probs_for(audio) if probs is None else probs)
    cal.wait()


def test_full_calibration_builds_and_saves_a_profile(tmp_path):
    lab = FakeLab()
    cal = Calibrator(lab, tmp_path)
    _record(cal, lab, "silence", RNG.normal(0, 0.001, SR * 10).astype(np.float32))
    _record(cal, lab, "loud", (speechlike(10.0, amp=0.5)).astype(np.float32))
    _record(cal, lab, "normal", (speechlike(45.0, amp=0.1) + RNG.normal(0, 0.001, SR * 45)).astype(np.float32))
    assert set(cal.takes) == {"silence", "loud", "normal"}
    assert cal.takes["silence"].result["noise_floor_db"] == pytest.approx(-60, abs=1.5)
    assert cal.takes["normal"].result["snr_db"] > 25
    cal.save_profile()
    p = cal.store.active()
    assert p.device_name == "Steam Streaming Microphone" and p.snr_rating == "good"
    assert p.analysis_gain_db == M.recommended_gain_db(p.speech_level_db)
    assert any("Steam Link" in n for n in p.notes)
    assert cal.recordings_on_disk()                      # takes are on disk until deleted
    cal.delete_recordings()
    assert cal.recordings_on_disk() == []
    assert cal.store.active() is not None                # the numbers stay


def test_loud_clipping_gives_advice(tmp_path):
    lab = FakeLab()
    cal = Calibrator(lab, tmp_path)
    _record(cal, lab, "silence", RNG.normal(0, 0.001, SR * 10).astype(np.float32))
    loud = np.clip(speechlike(10.0, amp=3.0), -1, 1).astype(np.float32)
    _record(cal, lab, "loud", loud)
    _record(cal, lab, "normal", (speechlike(45.0) + RNG.normal(0, 0.001, SR * 45)).astype(np.float32))
    p = cal.build_profile()
    assert p.clipped_samples > 0
    assert any("lower this mic's level" in a for a in p.advice)


def test_profile_needs_silence_and_normal(tmp_path):
    lab = FakeLab()
    cal = Calibrator(lab, tmp_path)
    assert cal.build_profile() is None and "Record at least" in cal.message


def test_switching_mic_clears_takes(tmp_path):
    lab = FakeLab()
    cal = Calibrator(lab, tmp_path)
    _record(cal, lab, "silence", RNG.normal(0, 0.001, SR * 10).astype(np.float32))
    assert "silence" in cal.takes
    lab.capture.device = SimpleNamespace(name="Razer USB Sound Card", hostapi="MME", sample_rate=SR, channels=2)
    cal.state()
    assert cal.takes == {}


def test_state_is_json_and_page_loads_nothing_external(tmp_path):
    lab = FakeLab()
    cal = Calibrator(lab, tmp_path)
    json.dumps(cal.state())
    assert "http://" not in CAL_PAGE.replace("http://127.0.0.1", "") and "https://" not in CAL_PAGE
    assert "src=" not in CAL_PAGE


def test_no_mic_selected(tmp_path):
    lab = FakeLab()
    lab.capture = None
    cal = Calibrator(lab, tmp_path)
    cal.record("silence")
    assert "Pick a microphone" in cal.message
    assert cal.state()["device"] is None


def test_calibration_routes_on_the_local_server(tmp_path):
    import urllib.request
    from tools.voice_lab.server import serve

    class Lab(FakeLab):
        def state(self):
            return {}

        def action(self, name, body):
            pass

    lab = Lab()
    cal = Calibrator(lab, tmp_path)
    server = serve(lab, 0, cal)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        page = urllib.request.urlopen(f"http://127.0.0.1:{port}/calibrate", timeout=5).read().decode()
        assert "Mic calibration" in page
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/cal/record", data=json.dumps({"step": "silence"}).encode(),
                                     method="POST", headers={"Content-Type": "application/json"})
        state = json.loads(urllib.request.urlopen(req, timeout=5).read())
        assert state["recording"]["step"] == "silence"
    finally:
        server.shutdown()
