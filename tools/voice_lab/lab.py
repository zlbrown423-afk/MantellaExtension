"""Voice Lab state: mic, meters, protocol, analysis worker, baseline and validation. The web page only
reads ``state()`` and calls ``action()``."""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable

import numpy as np

from livingworld.voice import pitch as pitch_mod
from livingworld.voice.baseline import Baseline
from livingworld.voice.features import MEASURES, analyze
from livingworld.voice.validation import KEEP_FACTOR, PERFORMED, compare_baselines, separation_table
from tools.voice_lab.capture import Device, MicCapture, list_devices, find_device, resample
from tools.voice_lab.protocol import Protocol, Segmenter, Step, Utterance, default_steps
from tools.voice_lab.storage import Storage

log = logging.getLogger("voice_lab")

NOISE_SECONDS = 10.0
METER_DECAY_DB = 1.5     # per block, for the peak-hold display


def _r(v: float | None, nd: int = 2) -> float | None:
    return None if v is None else round(float(v), nd)


class VoiceLab:
    def __init__(self, storage: Storage, stt: Any = None, prefer_praat: bool = True, pause_s: float = 1.2,
                 steps: list[Step] | None = None, lister: Callable[[], list[Device]] = list_devices,
                 capture_cls: type = MicCapture) -> None:
        self.storage = storage
        self.stt = stt
        self.prefer_praat = prefer_praat
        self.pause_s = pause_s
        self.steps = steps
        self.lister = lister
        self.capture_cls = capture_cls
        self.lock = threading.RLock()
        self.block_listeners: list[Callable[[np.ndarray, float], None]] = []   # e.g. the calibration recorder
        self.capture: Any = None
        self.segmenter: Segmenter | None = None
        self.devices: list[Device] = []
        self.level_db = -120.0
        self.peak_hold_db = -120.0
        self.clipped_total = 0
        self.noise_floor_db: float | None = None
        self._noise_blocks: list[float] | None = None
        self._noise_target = 0
        self.protocol: Protocol | None = None
        self._pending_step: str | None = None
        self.baseline: Baseline | None = None
        self.previous_baseline: Baseline | None = None
        self.validation: list[Any] | None = None
        self.day_compare: list[Any] | None = None
        self.last: dict[str, Any] | None = None
        self.message = ""
        self.take_count = 0
        self._q: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._work, daemon=True, name="voice_lab_analysis")
        self._worker.start()

    # mic --------------------------------------------------------------------------------------------------
    def refresh_devices(self) -> list[Device]:
        try:
            self.devices = self.lister()
        except Exception as e:
            self.devices = []
            self.message = f"Could not list audio devices: {e}"
        return self.devices

    def select_device(self, name: str, hostapi: str | None = None) -> bool:
        dev = find_device(self.refresh_devices(), name, hostapi)
        if dev is None:
            self.message = f"No input device named '{name}'."
            return False
        with self.lock:
            if self.capture is not None:
                self.capture.stop()
            self.capture = self.capture_cls(dev, self._on_block)
            self.segmenter = Segmenter(dev.sample_rate, self.capture.block_samples, pause_s=self.pause_s)
            self.clipped_total = 0
            self.noise_floor_db = None
            self.baseline = Baseline.load(self.storage.baseline_path(dev.name))
            self.previous_baseline = Baseline.load(self.storage.previous_baseline_path(dev.name))
            self.message = f"Using {dev.name} ({dev.hostapi}, {dev.sample_rate} Hz)."
        self.storage.save_settings({"device": dev.name, "hostapi": dev.hostapi})
        self.capture.start()
        return True

    @property
    def mic_name(self) -> str:
        return self.capture.device.name if self.capture is not None else ""

    def _on_block(self, block: np.ndarray, prob: float, rms_db: float, peak_db: float, clipped: int) -> None:
        for listener in list(self.block_listeners):
            try:
                listener(block, prob)
            except Exception:
                log.exception("block listener failed")
        with self.lock:
            self.level_db = rms_db
            self.peak_hold_db = max(peak_db, self.peak_hold_db - METER_DECAY_DB)
            self.clipped_total += clipped
            if self._noise_blocks is not None:
                self._noise_blocks.append(rms_db)
                if len(self._noise_blocks) >= self._noise_target:
                    self._finish_noise()
                return
            if self.segmenter is None:
                return
            utt = self.segmenter.feed(block, prob)
            if utt is None:
                return
            step = self.protocol.current if (self.protocol and not self.protocol.done) else None
            if step is not None and step.kind == "take" and self._pending_step is None:
                self._pending_step = step.id
                self._q.put((utt, step))
            else:
                self._q.put((utt, None))

    def measure_noise(self) -> None:
        with self.lock:
            if self.capture is None:
                self.message = "Pick a microphone first."
                return
            self._noise_blocks = []
            self._noise_target = int(NOISE_SECONDS / (self.capture.block_samples / self.capture.sample_rate))
            self.message = "Measuring room noise: stay silent for 10 seconds."

    def _finish_noise(self) -> None:
        blocks = np.array(self._noise_blocks or [-120.0])
        self._noise_blocks = None
        self.noise_floor_db = float(np.median(blocks))
        if self.segmenter is not None:
            self.segmenter.reset()
        if self.protocol and self.protocol.current and self.protocol.current.kind == "silence":
            self.protocol.record_silence(self.noise_floor_db)
        self.message = f"Noise floor {self.noise_floor_db:.1f} dBFS."

    # protocol ---------------------------------------------------------------------------------------------
    def start_protocol(self) -> None:
        with self.lock:
            if self.capture is None:
                self.message = "Pick a microphone first."
                return
            self.protocol = Protocol(self.steps or default_steps())
            self.validation = None
            self.day_compare = None
            self._pending_step = None
            self.take_count = 0
            self.storage = Storage(self.storage.root)   # a new session folder
        self.measure_noise()

    def redo(self) -> None:
        with self.lock:
            if not self.protocol:
                return
            take = self.protocol.redo()
            if take and take.wav:
                try:
                    from pathlib import Path
                    Path(take.wav).unlink()
                except OSError:
                    pass
            if self.protocol.current and self.protocol.current.kind == "silence":
                self.protocol.index += 1   # the noise floor stays; redo the silence with "Measure noise"
            self._pending_step = None
            if self.segmenter:
                self.segmenter.reset()
            self.message = "Step repeated."

    def skip(self) -> None:
        with self.lock:
            if self.protocol:
                self.protocol.skip()
                self._pending_step = None
                if self.protocol.done:
                    self._finish_protocol()

    def stop_protocol(self) -> None:
        with self.lock:
            self.protocol = None
            self._pending_step = None
            self.message = "Protocol stopped. Free talk: each line is measured against your baseline, not saved."

    def _finish_protocol(self) -> None:
        p = self.protocol
        mic = self.mic_name
        neutral = p.features_for("neutral")
        if len(neutral) < 3:
            self.message = "Protocol finished, but fewer than 3 neutral takes: no baseline saved."
            return
        new = Baseline.from_features(neutral, mic, self.capture.sample_rate if self.capture else 0)
        old = Baseline.load(self.storage.baseline_path(mic))
        if old is not None:
            self.storage.archive_baseline(mic)
            self.previous_baseline = old
            self.day_compare = compare_baselines(old, new)
            self.storage.write_csv("day_compare.csv", [
                {"measurement": d.key, "first_median": _r(d.first, 3), "second_median": _r(d.second, 3),
                 "first_spread": _r(d.spread, 3), "held": int(d.held)} for d in self.day_compare])
        new.save(self.storage.baseline_path(mic))
        self.baseline = new
        self.validation = separation_table(neutral, p.performed())
        self.storage.write_csv("validation.csv", [self._validation_row(r) for r in self.validation])
        kept = [r.key for r in self.validation if r.verdict == "kept"]
        self.message = (f"Done. Baseline saved. Kept {len(kept)} measurement(s): {', '.join(kept) or 'none'}. "
                        f"Numbers are in {self.storage.session_dir}.")

    @staticmethod
    def _validation_row(r: Any) -> dict[str, Any]:
        row = {"measurement": r.key, "neutral_median": _r(r.neutral_median, 3), "natural_spread": _r(r.spread, 3)}
        row.update({f"sep_{label}": _r(r.separation.get(label), 2) for label in PERFORMED})
        row.update({"verdict": r.verdict, "because": r.because})
        return row

    # analysis ---------------------------------------------------------------------------------------------
    def _work(self) -> None:
        while True:
            utt, step = self._q.get()
            try:
                self._analyze(utt, step)
            except Exception:
                log.exception("analysis failed")
                with self.lock:
                    self._pending_step = None

    def _analyze(self, utt: Utterance, step: Step | None) -> None:
        transcript = ""
        if self.stt is not None:
            try:
                transcript = self.stt.transcribe(resample(utt.audio, utt.sample_rate))
            except Exception as e:
                self.message = f"Transcription failed: {e}"
        started = time.perf_counter()
        feats = analyze(utt.audio, utt.sample_rate, utt.vad_probs, utt.block_s, noise_floor_db=self.noise_floor_db,
                        transcript=transcript, prefer_praat=self.prefer_praat)
        elapsed_ms = (time.perf_counter() - started) * 1000
        with self.lock:
            saved_as = None
            if step is not None and self.protocol and self.protocol.current and self.protocol.current.id == step.id:
                self.take_count += 1
                name = f"{self.take_count:02d}_{step.id}"
                wav = str(self.storage.save_wav(name, utt.audio, utt.sample_rate))
                self.protocol.record(feats, wav, transcript)
                row = {"take": self.take_count, "step": step.id, "label": step.label,
                       "seconds_recorded": _r(len(utt.audio) / utt.sample_rate, 2),
                       "timed_out": int(utt.timed_out), **feats.to_row()}
                self.storage.write_csv("measurements.csv", [row], append=True)
                saved_as = step.label
                if self.protocol.done:
                    self._finish_protocol()
            if step is not None and self._pending_step == step.id:
                self._pending_step = None
            zs = self.baseline.zscores(feats) if self.baseline else {}
            self.last = {
                "source": saved_as or "free talk", "transcript": transcript, "timed_out": utt.timed_out,
                "analysis_ms": round(elapsed_ms), "pitch_backend": feats.pitch_backend,
                "measures": [{
                    "key": m.key, "label": m.label, "unit": m.unit,
                    "value": _r(feats.measures[m.key].value, 3), "reliable": feats.measures[m.key].reliable,
                    "note": feats.measures[m.key].note,
                    "base": _r(self.baseline.stats[m.key].median, 2) if (self.baseline and m.key in self.baseline.stats) else None,
                    "z": _r(zs.get(m.key), 2)} for m in MEASURES],
            }

    # deletion ---------------------------------------------------------------------------------------------
    def delete_recordings(self) -> None:
        removed = self.storage.delete_recordings()
        left = len(self.storage.recordings_on_disk())
        self.message = f"Deleted {removed} recording(s). {left} left on disk."

    # page -------------------------------------------------------------------------------------------------
    def action(self, name: str, body: dict[str, Any]) -> None:
        if name == "devices":
            self.refresh_devices()
        elif name == "select":
            self.select_device(str(body.get("name", "")), body.get("hostapi"))
        elif name == "noise":
            self.measure_noise()
        elif name == "start":
            self.start_protocol()
        elif name == "redo":
            self.redo()
        elif name == "skip":
            self.skip()
        elif name == "stop":
            self.stop_protocol()
        elif name == "reset_clip":
            with self.lock:
                self.clipped_total = 0
        elif name == "delete":
            self.delete_recordings()
        else:
            self.message = f"Unknown action {name}"

    def state(self) -> dict[str, Any]:
        with self.lock:
            p = self.protocol
            step = p.current if p else None
            noise_left = None
            if self._noise_blocks is not None and self.capture is not None:
                noise_left = round((self._noise_target - len(self._noise_blocks))
                                   * self.capture.block_samples / self.capture.sample_rate, 1)
            snr = None
            if self.noise_floor_db is not None and self.baseline and "loudness_db" in self.baseline.stats:
                snr = self.baseline.stats["loudness_db"].median - self.noise_floor_db
            return {
                "devices": [d.to_json() for d in self.devices],
                "device": ({**self.capture.device.to_json(), "status": self.capture.status}
                           if self.capture is not None else None),
                "level_db": _r(self.level_db, 1), "peak_db": _r(self.peak_hold_db, 1),
                "clipped": self.clipped_total, "noise_floor_db": _r(self.noise_floor_db, 1),
                "noise_left_s": noise_left, "speaking": bool(self.segmenter and self.segmenter.speaking),
                "snr_db": _r(snr, 1), "stt": getattr(self.stt, "name", None),
                "pitch_backend": pitch_mod.backend_name(self.prefer_praat),
                "protocol": None if p is None else {
                    "index": p.index, "total": len(p.steps), "done": p.done, "pending": self._pending_step,
                    "step": None if step is None else {"id": step.id, "kind": step.kind, "label": step.label,
                                                       "prompt": step.prompt, "line": step.line}},
                "last": self.last,
                "baseline": None if self.baseline is None else {
                    "mic": self.baseline.mic, "created": self.baseline.created,
                    "n": max((s.n for s in self.baseline.stats.values()), default=0)},
                "validation": None if self.validation is None else [self._validation_row(r) for r in self.validation],
                "keep_factor": KEEP_FACTOR,
                "day_compare": None if self.day_compare is None else [
                    {"measurement": d.key, "first": _r(d.first), "second": _r(d.second), "spread": _r(d.spread),
                     "held": d.held} for d in self.day_compare],
                "session_dir": str(self.storage.session_dir),
                "recordings": len(self.storage.recordings_on_disk()),
                "message": self.message,
            }
