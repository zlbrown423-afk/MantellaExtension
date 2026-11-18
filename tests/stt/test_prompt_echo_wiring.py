"""The Transcriber drops Whisper's echo of the mic hint before anyone sees it (livingworld/audio/prompt_echo.py)."""
import numpy as np

from src.stt.stt import Transcriber

HINT = "This is a conversation with Hulda in The Bannered Mare."


def _transcriber(whisper_says: str) -> Transcriber:
    t = Transcriber.__new__(Transcriber)
    t.stt_service = 'whisper'
    t.prompt = HINT
    t.transcription_times = []
    t.proactive_mic_mode = False
    t.min_refresh_secs = 1.0
    t.loglevel = 27
    t._current_transcription = ''
    t._consecutive_empty_count = 0
    t.whisper_transcribe = lambda audio, prompt: whisper_says
    return t


def test_echo_on_silence_counts_as_nothing_heard():
    t = _transcriber(" This is a conversation with Hulda in The Bannered Mare.")
    assert t._transcribe(np.zeros(1600, dtype=np.float32)) == ''
    assert t._consecutive_empty_count == 1


def test_real_speech_after_echo_is_kept():
    t = _transcriber("This is a conversation with Hulda in The Bannered Mare. Got any mead?")
    assert t._transcribe(np.zeros(1600, dtype=np.float32)) == "Got any mead?"


def test_plain_speech_untouched():
    t = _transcriber(" Hello Hulda.")
    assert t._transcribe(np.zeros(1600, dtype=np.float32)) == " Hello Hulda."
