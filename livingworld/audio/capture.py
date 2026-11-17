"""AudioCapture: one microphone stream, opened by name at the device's native rate, feeding two paths on one
sample clock (see paths.py). Used by the speech-to-text code when MANTELLA_LW_AUDIO_V2=1.

Samples are counted from the first block the device delivers ("native samples"). The analysis signal is kept in a
ring buffer, so any stretch of time can be cut out afterwards by sample number. The speech-to-text path comes out as
512-sample, 16 kHz blocks, the same shape Mantella's own capture produced, and goes to ``on_stt_block``."""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable

import numpy as np

from livingworld.audio.devices import Device, default_device, find_device, list_devices
from livingworld.audio.paths import (STT_RATE, DCBlocker, Rechunker, StreamFilters, StreamResampler, db_to_lin,
                                     native_block_samples, soft_limit)

log = logging.getLogger("livingworld.audio")

STT_BLOCK = 512
RING_SECONDS = 70.0       # longer than the 30 s listen timeout plus lookback, with room to spare


class AudioCapture:
    def __init__(self, device_name: str | None = None, hostapi: str | None = None, gain_db: float = 0.0,
                 stt_filters: dict[str, Any] | None = None, ring_seconds: float = RING_SECONDS,
                 lister: Callable[[], list[Device]] = list_devices, stream_factory: Callable[..., Any] | None = None):
        self.device_name = device_name
        self.hostapi = hostapi
        self.gain_db = float(gain_db or 0.0)
        self.stt_filters_cfg = stt_filters or {}
        self.ring_seconds = ring_seconds
        self._lister = lister
        self._stream_factory = stream_factory
        self.device: Device | None = None
        self.sample_rate = 0
        self.status = "stopped"
        self.dropped_blocks = 0
        self.total_native = 0                       # samples delivered so far: the capture clock
        self.t0_wall: float | None = None           # wall time of native sample 0
        self._ring = np.zeros(0, dtype=np.float32)
        self._lock = threading.Lock()
        self._q: "queue.Queue[np.ndarray | None]" = queue.Queue()
        self._stream: Any = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._on_stt_block: Callable[[np.ndarray], None] | None = None
        self._dc: DCBlocker | None = None
        self._res: StreamResampler | None = None
        self._flt: StreamFilters | None = None
        self._rechunk = Rechunker(STT_BLOCK)
        self._gain = db_to_lin(self.gain_db)
        self.stt_blocks_emitted = 0

    # setup ------------------------------------------------------------------------------------------------
    def _pick_device(self) -> Device | None:
        devices = self._lister()
        if self.device_name:
            dev = find_device(devices, self.device_name, self.hostapi)
            if dev is None:
                log.warning("audio v2: input device '%s' not found; using the Windows default instead", self.device_name)
            else:
                return dev
        return default_device(devices)

    def start(self, on_stt_block: Callable[[np.ndarray], None]) -> bool:
        """Open the device and start both paths. Returns False (and leaves nothing running) if it can't."""
        try:
            dev = self._pick_device()
            if dev is None:
                self.status = "no input device"
                return False
            self.device = dev
            self.sample_rate = int(dev.sample_rate)
            self._on_stt_block = on_stt_block
            self._dc = DCBlocker(self.sample_rate)
            self._res = StreamResampler(self.sample_rate, STT_RATE)
            self._flt = StreamFilters(STT_RATE, self.stt_filters_cfg)
            self._rechunk = Rechunker(STT_BLOCK)
            self._ring = np.zeros(int(self.ring_seconds * self.sample_rate), dtype=np.float32)
            self.total_native = 0
            self.t0_wall = None
            self.stt_blocks_emitted = 0
            self.dropped_blocks = 0
            self._running = True
            self._thread = threading.Thread(target=self._work, daemon=True, name="audio_v2_paths")
            self._thread.start()
            factory = self._stream_factory
            if factory is None:
                import sounddevice as sd
                factory = sd.InputStream
            self._stream = factory(device=dev.index, samplerate=self.sample_rate, channels=1,
                                   blocksize=native_block_samples(self.sample_rate), dtype="float32",
                                   callback=self._callback, latency="low")
            self._stream.start()
            self.status = "recording"
            log.info("audio v2: capturing '%s' (%s) at %d Hz, fixed gain %+.1f dB, STT filters %s", dev.name,
                     dev.hostapi, self.sample_rate, self.gain_db, "on" if self._flt.active else "none")
            return True
        except Exception as e:
            log.warning("audio v2: could not start capture: %s", e)
            self.stop()
            self.status = f"could not open: {e}"
            return False

    def stop(self) -> None:
        self._running = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._thread is not None:
            self._q.put(None)
            self._thread.join(timeout=2)
            self._thread = None
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        self.status = "stopped"

    # stream callback (PortAudio thread: copy and hand over, nothing else) -----------------------------------
    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            self.dropped_blocks += 1
        if self.t0_wall is None:
            latency = getattr(self._stream, "latency", 0.0) or 0.0
            self.t0_wall = time.time() - frames / max(1, self.sample_rate) - float(latency)
        self._q.put(np.array(indata[:, 0], dtype=np.float32))

    # worker -----------------------------------------------------------------------------------------------
    def process_block(self, raw: np.ndarray) -> None:
        """Run one native block through both paths. Public so tests (and a file replay) can drive it directly."""
        analysis = (self._dc.process(raw) * np.float32(self._gain)).astype(np.float32)
        with self._lock:
            n, size = len(analysis), len(self._ring)
            pos = self.total_native % size
            first = min(n, size - pos)
            self._ring[pos:pos + first] = analysis[:first]
            if first < n:
                self._ring[:n - first] = analysis[first:]
            self.total_native += n
        stt = soft_limit(self._flt.process(self._res.process(analysis)))
        for block in self._rechunk.push(stt):
            self.stt_blocks_emitted += 1
            if self._on_stt_block is not None:
                self._on_stt_block(block)

    def _work(self) -> None:
        while self._running:
            raw = self._q.get()
            if raw is None:
                break
            try:
                self.process_block(raw)
            except Exception:
                log.exception("audio v2: block processing failed")

    # clock and cutting ------------------------------------------------------------------------------------
    def native_range_for_blocks(self, first_block: int, end_block: int) -> tuple[int, int]:
        """Native sample range covered by speech-to-text blocks [first_block, end_block)."""
        k = self.sample_rate / STT_RATE
        return int(round(first_block * STT_BLOCK * k)), int(round(end_block * STT_BLOCK * k))

    def wall_time(self, native_index: int) -> float:
        return (self.t0_wall or time.time()) + native_index / max(1, self.sample_rate)

    def extract(self, start: int, end: int) -> np.ndarray:
        """Analysis-path samples [start, end) from the ring buffer (a copy). Older than the ring: truncated."""
        with self._lock:
            size, total = len(self._ring), self.total_native
            end = min(end, total)
            start = max(start, total - size, 0)
            if end <= start:
                return np.zeros(0, dtype=np.float32)
            idx = np.arange(start, end) % size
            return self._ring[idx].copy()
