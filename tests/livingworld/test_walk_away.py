"""Ending a conversation while the server waits for the player to speak (the player walked away)."""
import threading
import time

import pytest

from src.http.communication_constants import communication_constants as comm_consts
from src.http.routes.mantella_route import mantella_route
from src.stt.stt import ListenAborted, Transcriber


def waiting_transcriber() -> Transcriber:
    stt = object.__new__(Transcriber)   # no mic, no models: only the waiting part is under test
    stt._lock = threading.Lock()
    stt._transcription_ready = threading.Event()
    stt._speech_detected = False
    stt._current_transcription = ""
    stt._has_unconsumed_transcription = False
    stt._temporary_pause_override = None
    stt._waiting = False
    stt._abort_wait = False
    stt.loglevel = 27
    stt.play_cough_sound = False
    return stt


def test_abort_wakes_a_waiting_listener():
    stt = waiting_transcriber()
    outcome = {}

    def listen():
        try:
            outcome["text"] = stt.get_latest_transcription()
        except ListenAborted:
            outcome["aborted"] = True

    t = threading.Thread(target=listen)
    t.start()
    for _ in range(50):
        if stt._waiting:
            break
        time.sleep(0.01)
    assert stt.abort_wait() is True
    t.join(timeout=2)
    assert not t.is_alive() and outcome == {"aborted": True}
    assert stt._abort_wait is False and stt._waiting is False


def test_abort_with_nobody_waiting_does_nothing():
    stt = waiting_transcriber()
    assert stt.abort_wait() is False
    stt._current_transcription = "hello"
    stt._transcription_ready.set()
    assert stt.get_latest_transcription() == "hello"   # a later wait is not aborted by mistake


class FakeGame:
    """Player input blocks like a real one waiting for speech, until the end request nudges it."""

    def __init__(self):
        self.stt = waiting_transcriber()
        self.ended = False

    def player_input(self, _json):
        try:
            self.stt.get_latest_transcription()
        except ListenAborted:
            return {comm_consts.KEY_REPLYTYPE: "mantella_lw_listen_aborted"}
        return {comm_consts.KEY_REPLYTYPE: "npc_talk"}

    def abort_player_wait(self):
        return self.stt.abort_wait()

    def end_conversation(self, _json):
        self.ended = True
        return {comm_consts.KEY_REPLYTYPE: comm_consts.KEY_REPLYTYPE_ENDCONVERSATION}


def test_an_end_request_is_not_stuck_behind_a_player_waiting_to_speak():
    route = object.__new__(mantella_route)
    route._mantella_route__game = FakeGame()
    route._mantella_route__request_lock = threading.Lock()
    handle = route._mantella_route__handle_request
    replies = {}
    waiting = threading.Thread(target=lambda: replies.update(
        input=handle(comm_consts.KEY_REQUESTTYPE_PLAYERINPUT, {})))
    waiting.start()
    time.sleep(0.05)
    started = time.monotonic()
    replies["end"] = handle(comm_consts.KEY_REQUESTTYPE_ENDCONVERSATION, {})
    waiting.join(timeout=2)
    assert time.monotonic() - started < 2
    assert replies["input"][comm_consts.KEY_REPLYTYPE] == "mantella_lw_listen_aborted"
    assert replies["end"][comm_consts.KEY_REPLYTYPE] == comm_consts.KEY_REPLYTYPE_ENDCONVERSATION
    assert route._mantella_route__game.ended
