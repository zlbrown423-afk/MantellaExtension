"""Optional transcript for the text-based speaking rate, using the same engines as Mantella's STT.

``--stt none`` (default), ``--stt whisper:base.en`` (faster-whisper on the CPU) or ``--stt moonshine:base``.
A model not yet on disk is downloaded on first use; after that, transcription is local.
"""
from __future__ import annotations

import numpy as np


class Transcriber:
    def __init__(self, spec: str) -> None:
        engine, _, model = spec.partition(":")
        self.engine = engine.strip().lower()
        self.model_name = model.strip()
        self._model = None
        self._tokenizer = None
        if self.engine == "whisper":
            from faster_whisper import WhisperModel
            self._model = WhisperModel(self.model_name or "base.en", device="cpu", compute_type="int8")
        elif self.engine == "moonshine":
            from moonshine_onnx import MoonshineOnnxModel, load_tokenizer
            self._model = MoonshineOnnxModel(model_name=f"moonshine/{self.model_name or 'base'}")
            self._tokenizer = load_tokenizer()
        else:
            raise ValueError(f"unknown STT engine '{spec}' (use whisper:<model> or moonshine:<tiny|base>)")

    @property
    def name(self) -> str:
        return f"{self.engine}:{self.model_name or 'default'}"

    def transcribe(self, audio16k: np.ndarray) -> str:
        audio16k = np.asarray(audio16k, dtype=np.float32)
        if self.engine == "whisper":
            segments, _ = self._model.transcribe(audio16k, language="en", beam_size=1, vad_filter=False)
            return " ".join(s.text for s in segments).strip()
        tokens = self._model.generate(audio16k[np.newaxis, :])
        return self._tokenizer.decode_batch(tokens)[0].strip()
