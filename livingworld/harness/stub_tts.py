"""A stand-in TTS service that writes short silent WAV files and logs which voice spoke each line.

The log is what voice-isolation tests check: every line an NPC says must be
synthesized with that NPC's own voice model.
"""
from __future__ import annotations

import wave
from dataclasses import dataclass

from src.config.config_loader import ConfigLoader
from src.tts.synthesization_options import SynthesizationOptions
from src.tts.ttsable import TTSable


@dataclass
class SynthesizedLine:
    voice: str
    text: str


class StubTTS(TTSable):
    SAMPLE_RATE = 22050
    SECONDS = 0.05

    def __init__(self, config: ConfigLoader) -> None:
        super().__init__(config)
        self.lines: list[SynthesizedLine] = []
        self.voice_changes: list[str] = []

    def change_voice(self, voice: str, in_game_voice: str | None = None, csv_in_game_voice: str | None = None, advanced_voice_model: str | None = None, voice_accent: str | None = None, voice_gender: int | None = None, voice_race: str | None = None):
        self._last_voice = voice
        self.voice_changes.append(voice)

    def _synthesize_voiceline(self, voiceline: str, final_voiceline_file: str, synth_options: SynthesizationOptions):
        self.lines.append(SynthesizedLine(str(self._last_voice), voiceline.strip()))
        with wave.open(final_voiceline_file, "w") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.SAMPLE_RATE)
            wf.writeframes(b"\x00\x00" * int(self.SAMPLE_RATE * self.SECONDS))
