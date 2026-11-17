"""Microphone capture for Voice Lab: device list, capture at the device's native rate, Silero VAD on a 16 kHz
copy (the same VAD Mantella uses), level meter and clipping counter, and reopening the mic by name if it
drops (a headset reconnect)."""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable

import numpy as np

log = logging.getLogger("voice_lab")

VAD_RATE = 16000
VAD_BLOCK = 512                    # 32 ms at 16 kHz
BLOCK_S = VAD_BLOCK / VAD_RATE
CLIP_LEVEL = 0.999


from livingworld.audio.devices import Device, find_device, list_devices   # noqa: F401  (re-exported for Voice Lab and its tests)


def to_vad_rate(block: np.ndarray, sample_rate: int) -> np.ndarray:
    """One capture block resampled to a 512-sample 16 kHz VAD block."""
    if sample_rate != VAD_RATE:
        try:
            from scipy.signal import resample_poly
            from math import gcd
            g = gcd(VAD_RATE, sample_rate)
            block = resample_poly(block, VAD_RATE // g, sample_rate // g)
        except ImportError:
            block = np.interp(np.linspace(0, len(block) - 1, VAD_BLOCK), np.arange(len(block)), block)
    block = np.asarray(block, dtype=np.float32)
    if len(block) < VAD_BLOCK:
        block = np.pad(block, (0, VAD_BLOCK - len(block)))
    return block[:VAD_BLOCK]


def resample(audio: np.ndarray, sample_rate: int, target: int = VAD_RATE) -> np.ndarray:
    if sample_rate == target:
        return np.asarray(audio, dtype=np.float32)
    try:
        from scipy.signal import resample_poly
        from math import gcd
        g = gcd(target, sample_rate)
        return resample_poly(audio, target // g, sample_rate // g).astype(np.float32)
    except ImportError:
        n = int(len(audio) * target / sample_rate)
        return np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)


BlockHandler = Callable[[np.ndarray, float, float, float, int], None]   # block, vad prob, rms dB, peak dB, clipped


class MicCapture:
    """Captures one device by name. Calls ``on_block`` on a worker thread for every 32 ms block."""

    def __init__(self, device: Device, on_block: BlockHandler) -> None:
        self.device = device
        self.sample_rate = device.sample_rate
        self.block_samples = int(round(BLOCK_S * self.sample_rate))
        self.on_block = on_block
        self.status = "stopped"
        self._q: queue.Queue = queue.Queue()
        self._stream = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._failed = threading.Event()

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="voice_lab_capture")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._close()
        if self._thread:
            self._thread.join(timeout=2)

    def _open(self) -> bool:
        import sounddevice as sd
        dev = find_device(list_devices(), self.device.name, self.device.hostapi)
        if dev is None:
            self.status = "not found (unplugged?)"
            return False
        self.device = dev
        self._failed.clear()

        def callback(indata, frames, t, status):   # an overflow only drops a block; a dead device ends the stream
            self._q.put(indata[:, 0].copy())

        try:
            self._stream = sd.InputStream(device=dev.index, samplerate=self.sample_rate, channels=1,
                                          blocksize=self.block_samples, dtype="float32", callback=callback,
                                          finished_callback=self._failed.set)
            self._stream.start()
        except Exception as e:
            self.status = f"could not open: {e}"
            self._stream = None
            return False
        self.status = "recording"
        return True

    def _close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def _run(self) -> None:
        from silero_vad_lite import SileroVAD
        vad = SileroVAD(VAD_RATE)
        while self._running:
            if self._stream is None and not self._open():
                time.sleep(2.0)
                continue
            try:
                block = self._q.get(timeout=0.5)
            except queue.Empty:
                if self._failed.is_set() or (self._stream is not None and not self._stream.active):
                    log.warning("mic stream stopped; reopening %s", self.device.name)
                    self.status = "reconnecting"
                    self._close()
                continue
            block = block.astype(np.float32)
            prob = float(vad.process(to_vad_rate(block, self.sample_rate)))
            rms_db = float(10 * np.log10(np.mean(block.astype(np.float64) ** 2) + 1e-12))
            peak_db = float(20 * np.log10(np.max(np.abs(block)) + 1e-12))
            clipped = int(np.sum(np.abs(block) >= CLIP_LEVEL))
            try:
                self.on_block(block, prob, rms_db, peak_db, clipped)
            except Exception:
                log.exception("block handler failed")
