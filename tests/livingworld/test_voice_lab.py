"""Voice Lab 0.1: measurements, baseline, validation, protocol and the lab driven end to end with a fake mic.
Synthetic "speech" is a harmonic tone with a syllable-rate amplitude envelope, so every expected value is known."""
from __future__ import annotations

import ast
import json
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np
import pytest

from livingworld.voice import features as F
from livingworld.voice import pitch as P
from livingworld.voice.baseline import Baseline
from livingworld.voice.validation import compare_baselines, separation_table
from tools.voice_lab.capture import Device
from tools.voice_lab.protocol import Protocol, Segmenter, default_steps

SR = 16000
RNG = np.random.default_rng(7)


def voice(f0=150.0, dur=2.0, amp=0.2, syl=4.0, sr=SR):
    t = np.arange(int(dur * sr)) / sr
    sig = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 8))
    env = 0.5 - 0.5 * np.cos(2 * np.pi * syl * t) if syl else np.full(len(t), 0.5)
    return amp * sig * env / 2


def silence(dur, level=0.001, sr=SR):
    return RNG.normal(0, level, int(dur * sr))


def utterance(f0=150.0, amp=0.2, syl=4.0, gaps=(), sr=SR):
    parts = [silence(0.3, sr=sr), voice(f0, 1.6, amp, syl, sr)]
    for g in gaps:
        parts += [silence(g, sr=sr), voice(f0, 0.8, amp, syl, sr)]
    parts += [voice(f0, 0.8, amp, syl, sr), silence(1.2, sr=sr)]
    return np.concatenate(parts)


# --- pitch ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("prefer_praat", [False, True])
@pytest.mark.parametrize("f0", [110.0, 150.0, 220.0])
def test_pitch_trackers_find_f0(prefer_praat, f0):
    times, track, backend = P.track(voice(f0, 1.0, syl=None), SR, prefer_praat)
    voiced = P.clean_track(track)
    assert len(voiced) > 50
    assert abs(np.median(voiced) - f0) < f0 * 0.02
    if not prefer_praat:
        assert backend == "yin"


# --- measurements ---------------------------------------------------------------------------------------
def test_measures_known_signal():
    feats = F.analyze(utterance(gaps=(0.6,)), SR, transcript="I need to speak with the Jarl about the dragon")
    m = feats.measures
    assert m["pause_count"].value == 1 and abs(m["longest_pause_s"].value - 0.6) < 0.1
    assert 3.3 < m["duration_s"].value < 4.0
    assert abs(m["pitch_median_st"].value - P.semitones(150.0)) < 0.3
    assert m["pitch_range_st"].value < 0.5
    assert 3.2 < m["rate_syl_s"].value < 5.0
    assert m["text_rate_syl_s"].value is not None
    assert m["snr_db"].value > 25 and m["clipped_fraction"].value == 0
    assert all(m[k].reliable for k in ("duration_s", "loudness_db", "pitch_median_st", "tilt_db", "rate_syl_s"))


def _probs(x, start=0.3, end=2.7):
    probs = np.full(len(x) // int(F.VAD_BLOCK_S * SR), 0.05)
    probs[int(start / F.VAD_BLOCK_S):int(end / F.VAD_BLOCK_S)] = 0.9
    return probs


def test_louder_is_six_db_and_rate_follows_syllables():
    x1, x2 = utterance(amp=0.1), utterance(amp=0.2)
    quiet = F.analyze(x1, SR, vad_probs=_probs(x1))
    loud = F.analyze(x2, SR, vad_probs=_probs(x2))
    assert abs((loud.value("loudness_db") - quiet.value("loudness_db")) - 6.02) < 0.3
    fast = F.analyze(utterance(syl=6.0), SR)
    slow = F.analyze(utterance(syl=2.5), SR)
    assert fast.value("rate_syl_s") > loud.value("rate_syl_s") > slow.value("rate_syl_s")


def test_clipping_makes_loudness_unreliable():
    feats = F.analyze(np.clip(utterance(amp=3.0), -1, 1), SR)
    assert feats.value("clipped_fraction") > 0.001
    assert not feats.measures["loudness_db"].reliable and feats.measures["loudness_db"].note == "clipped"


def test_short_and_empty_input():
    short = F.analyze(np.concatenate([silence(0.3), voice(dur=0.3), silence(0.5)]), SR)
    assert not short.measures["duration_s"].reliable and not short.measures["rate_syl_s"].reliable
    nothing = F.analyze(silence(1.0), SR)
    assert nothing.value("loudness_db") is None and "no speech found" in nothing.notes
    assert F.analyze(np.zeros(0), SR).value("pitch_median_st") is None


def test_vad_probabilities_drive_the_speech_span():
    x = utterance()
    feats = F.analyze(x, SR, vad_probs=_probs(x))
    assert abs(feats.value("duration_s") - 2.4) < 0.1 and feats.value("pause_count") == 0


def test_syllable_count():
    assert F.count_syllables("I need to speak with the Jarl") == 7
    assert F.count_syllables("watchtower dragon") == 5


def test_native_rate_48k():
    feats = F.analyze(utterance(sr=48000), 48000)
    assert abs(feats.value("pitch_median_st") - P.semitones(150.0)) < 0.3


# --- baseline and validation ----------------------------------------------------------------------------
def _takes(n, **kw):
    out = []
    for i in range(n):
        amp = kw.get("amp", 0.15) * (1 + RNG.normal(0, 0.03))
        f0 = kw.get("f0", 150.0) * (1 + RNG.normal(0, 0.005))
        out.append(F.analyze(utterance(f0=f0, amp=amp, syl=kw.get("syl", 4.0), gaps=kw.get("gaps", ())), SR,
                             prefer_praat=False))
    return out


def test_baseline_zscores_refine_and_roundtrip(tmp_path):
    neutral = _takes(5)
    base = Baseline.from_features(neutral, "Headset Microphone (Steam Streaming Microphone)", SR)
    assert {"loudness_db", "pitch_median_st", "rate_syl_s"} <= set(base.stats)
    loud = F.analyze(utterance(amp=0.6), SR, prefer_praat=False)
    assert base.z("loudness_db", loud.value("loudness_db")) > 5
    assert not base.refine(loud)                      # a shout never teaches the baseline
    before = base.stats["loudness_db"].median
    assert base.refine(neutral[0]) and base.stats["loudness_db"].median != before
    path = tmp_path / "b.json"
    base.save(path)
    again = Baseline.load(path)
    assert again.stats["loudness_db"].median == pytest.approx(base.stats["loudness_db"].median)
    assert Baseline.load(tmp_path / "missing.json") is None


def test_validation_keeps_real_changes_and_drops_weak_ones():
    neutral = _takes(5)
    performed = {"loud": _takes(2, amp=0.45), "quiet": _takes(2, amp=0.05), "high": _takes(2, f0=200.0),
                 "low": _takes(2, f0=152.0), "fast": _takes(2, syl=6.5), "slow": _takes(2, syl=2.0),
                 "hesitant": _takes(2, gaps=(0.7, 0.7))}
    rows = {r.key: r for r in separation_table(neutral, performed)}
    assert rows["loudness_db"].verdict == "kept"
    assert rows["pitch_median_st"].verdict == "kept" and "high" in rows["pitch_median_st"].because
    assert rows["pause_count"].verdict == "kept"
    assert rows["snr_db"].verdict == "quality"
    assert rows["pitch_range_st"].verdict == "not tested"
    weak = {r.key: r for r in separation_table(neutral, {"high": _takes(2, f0=151.0)})}
    assert weak["pitch_median_st"].verdict == "dropped"


def test_day_to_day_compare():
    a = Baseline.from_features(_takes(5), "mic")
    b = Baseline.from_features(_takes(5), "mic")
    c = Baseline.from_features(_takes(5, amp=0.5), "mic")
    held = {d.key: d.held for d in compare_baselines(a, b)}
    moved = {d.key: d.held for d in compare_baselines(a, c)}
    assert held["pitch_median_st"] and not moved["loudness_db"]


# --- protocol and segmenter -----------------------------------------------------------------------------
def test_protocol_steps_and_redo():
    p = Protocol()
    assert [s.label for s in p.steps[:3]] == ["silence", "loud_read", "neutral"]
    assert len(p.steps) == 2 + 5 + 7 * 2
    p.record_silence(-60.0)
    take = p.record(F.VocalFeatures())
    assert take.label == "loud_read" and p.index == 2
    assert p.redo() is take and p.index == 1 and not p.takes
    p.skip()
    assert p.current.label == "neutral"


def test_segmenter_matches_mantella():
    seg = Segmenter(SR, 512, threshold=0.4, pause_s=1.2)
    probs = [0.0] * 10 + [0.9] * 30 + [0.1] * 38 + [0.1] * 5
    out = [u for i, p in enumerate(probs) if (u := seg.feed(np.full(512, i, np.float32), p, now=i * 0.032))]
    assert len(out) == 1
    u = out[0]
    assert len(u.vad_probs) == 5 + 30 + 38   # 5 blocks of lookback, speech, then just over 1.2 s of silence
    assert u.audio[0] == 5 and not u.timed_out


# --- the lab end to end with a fake mic ------------------------------------------------------------------
class FakeCapture:
    def __init__(self, device, on_block):
        self.device, self.on_block = device, on_block
        self.sample_rate, self.block_samples, self.status = device.sample_rate, 512, "recording"

    def start(self):
        pass

    def stop(self):
        pass


def _feed(lab, audio, speech_from=None, speech_to=None):
    n = 512
    for i in range(len(audio) // n):
        block = audio[i * n:(i + 1) * n].astype(np.float32)
        t = i * n / SR
        prob = 0.9 if speech_from is not None and speech_from <= t < speech_to else 0.05
        lab._on_block(block, prob, float(10 * np.log10(np.mean(block.astype(float) ** 2) + 1e-12)),
                      float(20 * np.log10(np.max(np.abs(block)) + 1e-12)), int(np.sum(np.abs(block) >= 0.999)))


def _wait(cond, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_lab_runs_the_protocol_and_deletes_recordings(tmp_path):
    from tools.voice_lab.lab import VoiceLab
    from tools.voice_lab.storage import Storage
    dev = Device(3, "Headset Microphone (Steam Streaming Microphone)", "Windows WASAPI", SR, 1, True)
    lab = VoiceLab(Storage(tmp_path), prefer_praat=False, lister=lambda: [dev], capture_cls=FakeCapture)
    assert lab.select_device("Steam Streaming")             # found by part of its name
    lab.start_protocol()
    _feed(lab, silence(10.2))
    assert lab.noise_floor_db is not None and lab.protocol.current.label == "loud_read"
    plan = {"loud_read": dict(amp=0.4), "neutral": {}, "loud": dict(amp=0.45), "quiet": dict(amp=0.05),
            "fast": dict(syl=6.5), "slow": dict(syl=2.0), "hesitant": dict(gaps=(0.7, 0.7)),
            "high": dict(f0=200.0), "low": dict(f0=110.0)}
    while not lab.protocol.done:
        step = lab.protocol.current
        kw = dict(plan[step.label])
        amp = kw.pop("amp", 0.15) * (1 + RNG.normal(0, 0.03))
        x = utterance(amp=amp, **kw)
        speech_end = len(x) / SR - 1.2
        _feed(lab, np.concatenate([x, silence(0.3)]), 0.3, speech_end)
        assert _wait(lambda: lab._pending_step is None and lab._q.empty()), step.id
    st = lab.state()
    verdicts = {r["measurement"]: r["verdict"] for r in st["validation"]}
    assert verdicts["loudness_db"] == "kept" and verdicts["pitch_median_st"] == "kept"
    assert lab.storage.baseline_path(dev.name).exists()
    session = Path(st["session_dir"])
    rows = (session / "measurements.csv").read_text().splitlines()
    assert len(rows) == 1 + 1 + 5 + 14 and "transcript" not in rows[0]
    assert (session / "validation.csv").exists() and st["recordings"] == 20
    lab.action("delete", {})
    assert not list(tmp_path.rglob("*.wav")) and lab.state()["recordings"] == 0
    # free talk after the protocol: measured against the baseline, nothing saved
    lab.stop_protocol()
    _feed(lab, np.concatenate([utterance(amp=0.6), silence(0.3)]), 0.3, 2.7)
    assert _wait(lambda: lab.last and lab.last["source"] == "free talk")
    z = {m["key"]: m["z"] for m in lab.last["measures"]}
    assert z["loudness_db"] > 3 and not list(tmp_path.rglob("*.wav"))
    # a second run on another day compares with the first baseline (V6)
    lab.start_protocol()
    _feed(lab, silence(10.2))
    while not lab.protocol.done:
        step = lab.protocol.current
        if step.label != "neutral":
            lab.skip()
            continue
        _feed(lab, np.concatenate([utterance(amp=0.15 * (1 + RNG.normal(0, 0.03))), silence(0.3)]), 0.3, 2.7)
        assert _wait(lambda: lab._pending_step is None and lab._q.empty())
    assert lab.day_compare and lab.storage.previous_baseline_path(dev.name).exists()


def test_page_is_local_only(tmp_path):
    from tools.voice_lab.lab import VoiceLab
    from tools.voice_lab.server import PAGE, serve
    from tools.voice_lab.storage import Storage
    assert "http://" not in PAGE and "https://" not in PAGE      # loads nothing from the internet
    lab = VoiceLab(Storage(tmp_path), lister=lambda: [], capture_cls=FakeCapture)
    server = serve(lab, 0)
    host, port = server.server_address[:2]
    assert host == "127.0.0.1"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        html = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read().decode()
        assert "<title>Voice Lab</title>" in html
        state = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=5).read())
        assert state["device"] is None and state["protocol"] is None
    finally:
        server.shutdown()


# --- consent isolation (plan test X1) -------------------------------------------------------------------
def test_consent_code_never_imports_voice():
    root = Path(__file__).resolve().parents[2] / "livingworld"
    files = [root / "social" / "consent.py", root / "personality" / "intimacy.py", *(root / "touch").glob("*.py")]
    for f in [f for f in files if f.exists()]:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        names = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        names += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        assert not [n for n in names if "voice" in n], f
