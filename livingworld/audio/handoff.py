"""The UtteranceAudio handoff: what a finished player utterance looks like to anything that wants its audio
(the vocal perception code, a debug display). Consumers get a copy on their own thread, so nothing they do can
hold up speech-to-text. Nothing is written to disk and arrays are dropped when the last consumer returns.

    from livingworld.audio.handoff import subscribe
    subscribe(lambda utt: my_queue.put(utt))      # copy-and-enqueue; analysis runs on your own worker
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

log = logging.getLogger("livingworld.audio")

MAX_QUEUE = 20


@dataclass
class MicSnapshot:
    """What was true of the microphone when the utterance was captured."""
    device_name: str
    hostapi: str
    sample_rate: int                 # native capture rate of analysis_audio
    gain_db: float                   # fixed gain applied to both paths
    profile_found: bool              # False: no calibration profile for this mic, so the values below are unknown
    noise_floor_db: float | None
    snr_db: float | None
    clipped_samples: int             # clipped samples counted in this utterance's analysis audio
    analysis_clean: bool             # False when processing before Windows can't be ruled out (or wasn't checked)
    stt_filters: bool                # True when the STT path used any filter
    dropped_blocks: int              # blocks PortAudio reported as lost since capture started


@dataclass
class UtteranceAudio:
    utterance_id: str
    speech_start_wall: float         # wall clock (time.time()) of the first sample, lookback included
    speech_end_wall: float
    start_sample: int                # positions on the capture clock (native samples since capture started)
    end_sample: int
    analysis_audio: np.ndarray       # float32 at analysis_rate: DC removed and fixed gain only
    analysis_rate: int
    stt_audio: np.ndarray            # float32 at 16 kHz: exactly what speech-to-text received
    stt_rate: int
    vad_probs: np.ndarray            # Silero probability per 32 ms block of stt_audio (empty in push-to-talk mode)
    vad_block_s: float
    capture_mode: str                # "vad", "proactive" or "ptt"
    hit_listen_timeout: bool
    pause_threshold_s: float | None
    transcript: "Future[str]"        # resolves to the transcript ("" if none); analysis can start before it does
    npc_audio_overlap: bool | None   # True if an NPC voice line played during the span; None = not known
    mic: MicSnapshot
    created: float = field(default_factory=time.time)

    @property
    def duration_s(self) -> float:
        return len(self.analysis_audio) / self.analysis_rate if self.analysis_rate else 0.0


Consumer = Callable[[UtteranceAudio], None]


class UtteranceBus:
    """Delivers each utterance to every subscriber on one dispatcher thread."""

    def __init__(self) -> None:
        self._subs: list[Consumer] = []
        self._q: "queue.Queue[UtteranceAudio | None]" = queue.Queue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.dropped = 0

    @property
    def has_subscribers(self) -> bool:
        return bool(self._subs)

    def subscribe(self, fn: Consumer) -> Callable[[], None]:
        with self._lock:
            self._subs.append(fn)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True, name="utterance_bus")
                self._thread.start()

        def unsubscribe() -> None:
            with self._lock:
                if fn in self._subs:
                    self._subs.remove(fn)
        return unsubscribe

    def publish(self, utt: UtteranceAudio) -> bool:
        """Never blocks. If consumers fall behind, the oldest waiting utterance is dropped."""
        if not self._subs:
            return False
        while self._q.qsize() >= MAX_QUEUE:
            try:
                self._q.get_nowait()
                self.dropped += 1
                log.warning("utterance bus is backed up; dropped the oldest utterance")
            except queue.Empty:
                break
        self._q.put(utt)
        return True

    def _run(self) -> None:
        while True:
            utt = self._q.get()
            if utt is None:
                return
            for fn in list(self._subs):
                try:
                    fn(utt)
                except Exception:
                    log.exception("utterance consumer failed")

    def close(self) -> None:
        self._q.put(None)


BUS = UtteranceBus()


def subscribe(fn: Consumer) -> Callable[[], None]:
    return BUS.subscribe(fn)


def new_utterance_id() -> str:
    return uuid.uuid4().hex[:12]


# NPC speech, for the overlap flag ------------------------------------------------------------------------------
_NPC_LINES: deque = deque(maxlen=200)


def note_npc_speech(duration_s: float, start_wall: float | None = None) -> None:
    """Called when an NPC voice line is sent to the game: it will play from ``start_wall`` for ``duration_s``."""
    start = time.time() if start_wall is None else start_wall
    _NPC_LINES.append((start, start + max(0.0, float(duration_s))))


def npc_overlap(start_wall: float, end_wall: float, tail_s: float = 0.3) -> bool | None:
    """True if any noted NPC line overlaps [start_wall, end_wall] (the line's echo tail counts); None if no NPC
    line has ever been noted, because then there is nothing to compare against."""
    if not _NPC_LINES:
        return None
    return any(s < end_wall and e + tail_s > start_wall for s, e in _NPC_LINES)
