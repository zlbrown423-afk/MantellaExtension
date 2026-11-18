"""Live voice readings in game: each finished player utterance (from the capture layer, MANTELLA_LW_AUDIO_V2=1)
is measured on a worker thread and scored against the player's own in-game baseline.

- The baseline is a rolling window of the player's recent in-game lines on this mic (robust median and spread
  per measurement), so it follows whatever Windows level and gain are in use now. Loudness only ever means
  something against it (the Steam Link mic has a noise gate and Windows gain control).
- Until the window holds enough lines, the Voice Lab baseline for this mic stands in for the level-free
  measurements (pitch, pauses, speaking rate), never for loudness: Voice Lab ran at a different gain.
- Measurements Voice Lab validation marked "dropped" for this player are not used.

Numbers only: no audio is kept or written. The window is saved to
Documents\\My Games\\Mantella\\data\\audio\\baseline\\<mic>.ingame.json so the next session starts warm.
"""
from __future__ import annotations

import csv
import json
import logging
import math
import queue
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from livingworld.voice import features as feat_mod
from livingworld.voice.baseline import SPREAD_FLOOR, Baseline
from livingworld.voice.tone import Cue, cues_from

log = logging.getLogger("livingworld.voice")

# measurements the tone judge uses
KEYS = ("loudness_db", "pitch_median_st", "pitch_range_st", "longest_pause_s", "voiced_ratio", "text_rate_syl_s")
LEVEL_KEYS = ("loudness_db",)       # depend on gain: only ever from the in-game window
WINDOW = 60                         # recent lines kept per measurement
MIN_LINES = 6                       # lines before the in-game window is used for a measurement
SAVE_EVERY = 5
READINGS_KEPT = 12
GAIN_RESET_DB = 1.0                 # a gain change bigger than this clears the loudness window
MATCH_WINDOW_S = 30.0               # a reading older than this is not paired with a player line


def mic_slug(name: str) -> str:     # same file names as tools/voice_lab/storage.py
    return (re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_") or "mic")[:60]


def _norm_text(t: str) -> str:
    return " ".join(re.findall(r"[a-z0-9']+", (t or "").lower()))


@dataclass
class Reading:
    utterance_id: str
    end_wall: float
    transcript: str | None = None
    values: dict[str, float | None] = field(default_factory=dict)
    z: dict[str, float | None] = field(default_factory=dict)
    cues: list[Cue] | None = None          # None: not judged (see why)
    why: str = ""
    speech_s: float | None = None
    done: threading.Event = field(default_factory=threading.Event)


class RollingBaseline:
    """Recent in-game values per measurement; z-scores against their median and spread."""

    def __init__(self, mic: str, gain_db: float, fallback: Baseline | None = None,
                 dropped: set[str] | None = None) -> None:
        self.mic = mic
        self.gain_db = gain_db
        self.fallback = fallback
        self.dropped = set(dropped or ())
        self.values: dict[str, deque] = {k: deque(maxlen=WINDOW) for k in KEYS}
        self.added = 0

    def stat(self, key: str) -> tuple[float, float] | None:
        vals = self.values.get(key)
        if vals is not None and len(vals) >= MIN_LINES:
            arr = np.asarray(vals, dtype=np.float64)
            med = float(np.median(arr))
            mad = float(np.median(np.abs(arr - med))) * 1.4826
            return med, max(mad, SPREAD_FLOOR.get(key, 0.0))
        if self.fallback is not None and key not in LEVEL_KEYS:
            s = self.fallback.stats.get(key)
            if s is not None:
                return s.median, max(s.spread, SPREAD_FLOOR.get(key, 0.0))
        return None

    def z(self, values: dict[str, float | None]) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        for key in KEYS:
            v = values.get(key)
            st = None if (v is None or key in self.dropped) else self.stat(key)
            out[key] = None if st is None else (float(v) - st[0]) / st[1]
        return out

    def add(self, values: dict[str, float | None]) -> None:
        for key in KEYS:
            v = values.get(key)
            if v is not None and math.isfinite(v):
                self.values[key].append(float(v))
        self.added += 1

    def to_json(self) -> dict:
        return {"version": 1, "mic": self.mic, "gain_db": self.gain_db, "updated": time.time(),
                "values": {k: [round(x, 4) for x in v] for k, v in self.values.items()}}

    def load_json(self, data: dict) -> None:
        same_gain = abs(float(data.get("gain_db", self.gain_db)) - self.gain_db) <= GAIN_RESET_DB
        for k, vals in (data.get("values") or {}).items():
            if k in self.values and (same_gain or k not in LEVEL_KEYS):
                self.values[k].extend(float(x) for x in vals[-WINDOW:])


def latest_dropped(audio_root: Path) -> set[str]:
    """Measurements the newest Voice Lab validation.csv marked 'dropped' (did not separate deliberate changes)."""
    try:
        files = sorted((audio_root / "voice_lab").glob("*/validation.csv"))
        if not files:
            return set()
        with files[-1].open(encoding="utf-8", newline="") as fh:
            return {r["measurement"] for r in csv.DictReader(fh) if (r.get("verdict") or "").strip() == "dropped"}
    except Exception:
        return set()


def measure(utt) -> tuple[dict[str, float | None], float | None, str]:
    """Measure one UtteranceAudio. Returns (reliable values by key, phonation seconds, why-not-judged or '')."""
    if getattr(utt, "capture_mode", "") == "ptt" or not len(getattr(utt, "vad_probs", ())):
        audio, rate, probs = utt.analysis_audio, utt.analysis_rate, None
    elif getattr(utt.mic, "stt_filters", False):
        audio, rate, probs = utt.analysis_audio, utt.analysis_rate, None   # filtered STT path: use the raw one
    else:
        audio, rate, probs = utt.stt_audio, utt.stt_rate, utt.vad_probs    # 16 kHz, VAD blocks line up exactly
    # the calibrated noise floor lets a block at the floor count as a pause even while Silero still holds speech
    f = feat_mod.analyze(audio, rate, vad_probs=probs, vad_block_s=utt.vad_block_s, noise_floor_db=utt.mic.noise_floor_db)
    values = {k: f.reliable_value(k) for k in KEYS if k != "text_rate_syl_s"}
    dur, ratio = f.reliable_value("duration_s"), f.reliable_value("voiced_ratio")
    phonation = dur * ratio if (dur is not None and ratio is not None) else None
    why = ""
    if values.get("loudness_db") is None and values.get("pitch_median_st") is None:
        why = "too short or no clear speech"
    elif utt.npc_audio_overlap:
        why = "an NPC was talking at the same time"
        values["loudness_db"] = None
    if utt.mic.clipped_samples > 0.001 * max(1, len(utt.analysis_audio)):
        values["loudness_db"] = None
    return values, phonation, why


class LiveVoice:
    """Subscribes to the utterance handoff and keeps the latest readings."""

    def __init__(self, audio_root: Path, subscribe: Callable | None = None, start_thread: bool = True,
                 measure_fn: Callable = measure) -> None:
        self.audio_root = Path(audio_root)
        self._measure = measure_fn
        self._q: "queue.Queue" = queue.Queue(maxsize=8)
        self._lock = threading.Lock()
        self._readings: deque[Reading] = deque(maxlen=READINGS_KEPT)
        self._baselines: dict[str, RollingBaseline] = {}
        self._unsub = None
        if subscribe is None:
            from livingworld.audio.handoff import subscribe as subscribe
        self._unsub = subscribe(self._on_utterance)
        if start_thread:
            threading.Thread(target=self._run, daemon=True, name="voice_tone").start()

    # handoff side: copy-and-enqueue, never block --------------------------------------------------------
    def _on_utterance(self, utt) -> None:
        r = Reading(utt.utterance_id, utt.speech_end_wall)
        with self._lock:
            self._readings.append(r)
        try:
            self._q.put_nowait((utt, r))
        except queue.Full:
            r.why = "too many lines waiting"
            r.done.set()

    def _run(self) -> None:
        while True:
            utt, r = self._q.get()
            self.process(utt, r)

    def process(self, utt, r: Reading) -> None:
        try:
            values, phonation, why = self._measure(utt)
            r.speech_s = phonation
            try:
                text = utt.transcript.result(timeout=20)
            except Exception:
                text = ""
            r.transcript = text or ""
            if text and phonation and phonation >= 1.5:
                values["text_rate_syl_s"] = feat_mod.count_syllables(text) / phonation
            r.values = values
            base = self._baseline_for(utt.mic)
            r.z = base.z(values)
            if why:
                r.why = why
            else:
                lp = values.get("longest_pause_s")
                r.cues = cues_from(r.z, lp, phonation)
            if text and not why:
                base.add(values)
                if base.added % SAVE_EVERY == 0:
                    self._save(base)
        except Exception:
            log.exception("voice tone: could not measure an utterance")
            r.why = r.why or "measurement failed"
        finally:
            r.done.set()

    # baseline -------------------------------------------------------------------------------------------
    def _path(self, mic: str) -> Path:
        return self.audio_root / "baseline" / f"{mic_slug(mic)}.ingame.json"

    def _baseline_for(self, mic) -> RollingBaseline:
        name = mic.device_name or "default"
        base = self._baselines.get(name)
        if base is None or abs(base.gain_db - mic.gain_db) > GAIN_RESET_DB:
            fallback = Baseline.load(self.audio_root / "baseline" / f"{mic_slug(name)}.json")
            base = RollingBaseline(name, float(mic.gain_db), fallback, latest_dropped(self.audio_root))
            try:
                data = json.loads(self._path(name).read_text(encoding="utf-8"))
                base.load_json(data)
            except (OSError, ValueError):
                pass
            self._baselines[name] = base
            log.info("voice tone: baseline for %s (%s in-game lines, Voice Lab fallback %s, dropped %s)", name,
                     max((len(v) for v in base.values.values()), default=0), "yes" if fallback else "no",
                     sorted(base.dropped) or "none")
        return base

    def _save(self, base: RollingBaseline) -> None:
        try:
            path = self._path(base.mic)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(base.to_json()), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            log.warning("voice tone: could not save the in-game baseline")

    def flush(self) -> None:
        for base in list(self._baselines.values()):
            if base.added:
                self._save(base)

    # conversation side ----------------------------------------------------------------------------------
    def reading_for(self, text: str, wait_s: float = 1.5, now: float | None = None) -> Reading | None:
        """The reading of the utterance that produced ``text``: the newest one whose transcript matches, else the
        newest recent one. Waits up to ``wait_s`` for its measurement to finish."""
        now = time.time() if now is None else now
        with self._lock:
            recent = [r for r in self._readings if now - r.end_wall <= MATCH_WINDOW_S]
        if not recent:
            return None
        want = _norm_text(text)
        deadline = time.time() + wait_s
        for r in reversed(recent):
            r.done.wait(max(0.0, deadline - time.time()))
            got = _norm_text(r.transcript or "")
            if got and want and (got == want or got in want or want in got):
                self._consume(r)
                return r
        newest = recent[-1]
        if newest.done.is_set() and not newest.transcript:
            return None
        self._consume(newest)
        return newest

    def _consume(self, r: Reading) -> None:
        with self._lock:
            try:
                self._readings.remove(r)
            except ValueError:
                pass

    def close(self) -> None:
        self.flush()
        if self._unsub:
            self._unsub()


class ToneState:
    """The tone the player picked on the wheel. Stays until changed, cleared by 'neutral' or when a
    conversation ends; a pick made outside a conversation waits for the next one (up to ``max_idle_s``)."""

    def __init__(self, max_idle_s: float = 600.0) -> None:
        self.tone: str | None = None
        self.speech_skill: float | None = None
        self.set_at = 0.0
        self.max_idle_s = max_idle_s
        self._lock = threading.Lock()

    def set(self, tone: str | None, speech_skill: float | None = None, now: float | None = None) -> None:
        with self._lock:
            self.tone = None if tone in (None, "neutral") else tone
            if speech_skill is not None:
                self.speech_skill = speech_skill
            self.set_at = time.time() if now is None else now

    def touch(self, now: float | None = None) -> None:
        with self._lock:
            self.set_at = time.time() if now is None else now

    def current(self, now: float | None = None) -> str | None:
        now = time.time() if now is None else now
        with self._lock:
            if self.tone and now - self.set_at > self.max_idle_s:
                self.tone = None
            return self.tone

    def clear(self) -> None:
        with self._lock:
            self.tone = None
