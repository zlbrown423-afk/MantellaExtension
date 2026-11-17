"""AudioV2Session: what the speech-to-text code (src/stt/stt.py) talks to when MANTELLA_LW_AUDIO_V2=1.

It owns the AudioCapture, counts the 16 kHz blocks the transcriber consumes, remembers the speech-detector
probability of each, and on request cuts the finished utterance out of both paths and publishes it as an
UtteranceAudio. The transcriber only has to call five small methods, so its own logic stays as it was."""
from __future__ import annotations

import logging
from collections import deque
from concurrent.futures import Future
from typing import Any, Callable

import numpy as np

from livingworld.audio import handoff, metrics, settings
from livingworld.audio.capture import STT_BLOCK, AudioCapture
from livingworld.audio.handoff import MicSnapshot, UtteranceAudio
from livingworld.audio.paths import STT_RATE

log = logging.getLogger("livingworld.audio")

BLOCK_S = STT_BLOCK / STT_RATE


class AudioV2Session:
    def __init__(self, save_folder: str, max_utterance_s: float = 60.0, capture_cls: type = AudioCapture,
                 bus: handoff.UtteranceBus | None = None, **capture_kwargs: Any) -> None:
        self.settings = settings.load(save_folder)
        self.capture = capture_cls(self.settings.device_name, self.settings.hostapi, self.settings.gain_db,
                                   self.settings.stt_filters, **capture_kwargs)
        self.bus = bus or handoff.BUS
        self._probs: deque[float] = deque(maxlen=int((max_utterance_s + 5.0) / BLOCK_S) + 8)
        self.blocks_seen = 0

    # lifecycle --------------------------------------------------------------------------------------------
    def start(self, on_block: Callable[[np.ndarray], None]) -> bool:
        self.blocks_seen = 0
        self._probs.clear()
        ok = self.capture.start(on_block)
        if ok and self.settings.profile is None:
            log.warning("audio v2: no mic profile found; capturing %s at 0 dB with no filters and marking the analysis "
                        "signal as unverified. Run the calibration page (python -m tools.voice_lab) to make one.",
                        self.capture.device.name if self.capture.device else "the default mic")
        return ok

    def stop(self) -> None:
        self.capture.stop()

    # per block (called by the transcriber's processing thread) ------------------------------------------
    def on_chunk(self) -> None:
        self.blocks_seen += 1

    def on_vad_prob(self, p: float) -> None:
        self._probs.append(float(p))

    # per utterance ----------------------------------------------------------------------------------------
    def publish(self, stt_audio: np.ndarray, mode: str, timed_out: bool, pause_s: float | None) -> "Future[str] | None":
        """Cut the utterance the transcriber is about to finalize out of both paths and hand it to subscribers.
        ``stt_audio`` is exactly the buffer speech-to-text gets. Returns the transcript future the transcriber must
        resolve, or None when nobody is subscribed (then nothing is copied)."""
        if not self.bus.has_subscribers or len(stt_audio) == 0:
            return None
        try:
            nbuf = len(stt_audio) // STT_BLOCK
            end_blk = self.blocks_seen
            n0, n1 = self.capture.native_range_for_blocks(end_blk - nbuf, end_blk)
            analysis = self.capture.extract(n0, n1)
            probs = np.array(list(self._probs)[-nbuf:], dtype=np.float32) if mode != "ptt" else np.zeros(0, np.float32)
            if mode != "ptt" and len(probs) < nbuf:
                probs = np.concatenate([np.zeros(nbuf - len(probs), np.float32), probs])
            s, e = self.capture.wall_time(n0), self.capture.wall_time(n1)
            fut: "Future[str]" = Future()
            utt = UtteranceAudio(
                utterance_id=handoff.new_utterance_id(), speech_start_wall=s, speech_end_wall=e,
                start_sample=n0, end_sample=n1, analysis_audio=analysis, analysis_rate=self.capture.sample_rate,
                stt_audio=np.array(stt_audio, dtype=np.float32), stt_rate=STT_RATE, vad_probs=probs,
                vad_block_s=BLOCK_S, capture_mode=mode, hit_listen_timeout=timed_out, pause_threshold_s=pause_s,
                transcript=fut, npc_audio_overlap=handoff.npc_overlap(s, e), mic=self._snapshot(analysis))
            self.bus.publish(utt)
            return fut
        except Exception:
            log.exception("audio v2: could not build the utterance handoff")
            return None

    def _snapshot(self, analysis: np.ndarray) -> MicSnapshot:
        cap, prof = self.capture, self.settings.profile
        dev = cap.device
        return MicSnapshot(
            device_name=dev.name if dev else "", hostapi=dev.hostapi if dev else "", sample_rate=cap.sample_rate,
            gain_db=cap.gain_db, profile_found=prof is not None,
            noise_floor_db=prof.noise_floor_db if prof else None, snr_db=prof.snr_db if prof else None,
            clipped_samples=metrics.clipping(analysis).full_scale,
            analysis_clean=bool(prof and prof.analysis_clean and prof.clean_judged),
            stt_filters=bool(cap._flt is not None and cap._flt.active), dropped_blocks=cap.dropped_blocks)
