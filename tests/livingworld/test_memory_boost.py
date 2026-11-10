"""Better memory when a conversation starts again (MANTELLA_LW_MEMORY_BOOST, on by default).

- extraction replies that were cut off or sloppy are mended instead of lost
- memories given as plain sentences are kept
- a wrong line number is matched to the line that really holds the fact
- recall keeps slots for what was said, so repeated grabs cannot push it all out
- the end of the last conversation is recapped word for word
- past conversations that taught nothing are extracted once more (backfill)
"""
import json
import sqlite3

import pytest

from livingworld.harness import actors
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, LivingWorld
from livingworld.memory import backfill, jobs
from livingworld.memory.extraction import Extractor, parse_reply
from livingworld.memory.recall import recall_for
from livingworld.memory.settings import BOOST_ENV, extract_max_tokens

from tests.livingworld.test_memory import gerdur_llm, ids, open_db, phase_a


@pytest.fixture(autouse=True)
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.delenv(BOOST_ENV, raising=False)
    monkeypatch.setattr(LivingWorld, "start_workers", False)
    monkeypatch.setattr(jobs, "BACKOFF_SECONDS", (0, 0, 0, 0, 0))


# ---- parsing ---------------------------------------------------------------------------------

def test_a_reply_cut_off_part_way_keeps_what_was_complete():
    reply = ('{"memories": [{"text": "Titus promised to bring timber tomorrow.", "importance": 0.7},'
             ' {"text": "He said \\"see you }\\" and left.", "importance": 0.3}],'
             ' "facts": [{"about": "Titus", "predicate": "hometown", "value": "Cyrodiil", "said_by": "Titus", "line": 3},'
             ' {"about": "Titus", "predic')
    data = parse_reply(reply)
    assert [m["text"] for m in data["memories"]] == ["Titus promised to bring timber tomorrow.",
                                                    'He said "see you }" and left.']
    assert [f["value"] for f in data["facts"]] == ["Cyrodiil"]
    assert data["reports"] == []


def test_trailing_commas_and_plain_sentence_memories_are_accepted():
    data = parse_reply('```json\n{"memories": ["I met Titus at the mill.",], "facts": [],}\n```')
    assert data["memories"] == [{"text": "I met Titus at the mill."}]


def test_nothing_usable_is_still_an_error():
    with pytest.raises(ValueError):
        parse_reply('{"memories": [{"text": "cut off before anything clo')


def test_extraction_reply_length_has_a_floor(monkeypatch):
    monkeypatch.setenv("MANTELLA_LW_EXTRACT_MAX_TOKENS", "50")
    assert extract_max_tokens() == 400
    monkeypatch.setenv("MANTELLA_LW_EXTRACT_MAX_TOKENS", "junk")
    assert extract_max_tokens() == 1500


# ---- extraction ------------------------------------------------------------------------------

def test_plain_sentence_memories_are_stored(tmp_path):
    phase_a(tmp_path)
    world = LivingWorld.latest
    world.complete = lambda system, user: json.dumps(
        {"memories": ["Torvar from Falkreath asked about my sister; I had no idea what he meant."], "facts": []})
    world.wait_for_jobs()
    db = open_db(tmp_path)
    assert [r[0] for r in db.execute("SELECT content FROM memory WHERE created_by = 'extraction'")] == \
        ["Torvar from Falkreath asked about my sister; I had no idea what he meant."]


def test_a_bad_reply_is_asked_again_straight_away(tmp_path):
    phase_a(tmp_path)
    world = LivingWorld.latest
    replies = iter(["Sure! Here is what Gerdur remembers.", '{"memories": ["Torvar came by the mill."]}'])
    asked = []

    def complete(system, user):
        asked.append(user)
        return next(replies)
    world.complete = complete
    world.wait_for_jobs()
    db = open_db(tmp_path)
    assert tuple(db.execute("SELECT status, attempts FROM job").fetchone()) == ("done", 1)
    assert "not valid JSON" in asked[1]
    assert db.execute("SELECT COUNT(*) FROM memory WHERE created_by = 'extraction'").fetchone()[0] == 1


def test_a_wrong_line_number_is_matched_to_the_real_line(tmp_path):
    phase_a(tmp_path)
    world = LivingWorld.latest
    world.complete = lambda system, user: json.dumps({"facts": [
        {"about": "Torvar", "predicate": "hometown", "value": "Falkreath", "said_by": "Torvar", "line": 1},
        # never said by anyone: still dropped
        {"about": "Torvar", "predicate": "sister_name", "value": "Ingrid", "said_by": "Torvar", "line": 1}]})
    world.wait_for_jobs()
    db = open_db(tmp_path)
    assert [tuple(r) for r in db.execute("SELECT predicate, value FROM knowledge")] == [("hometown", "Falkreath")]


def test_with_the_boost_off_a_wrong_line_is_dropped(tmp_path, monkeypatch):
    monkeypatch.setenv(BOOST_ENV, "0")
    phase_a(tmp_path)
    world = LivingWorld.latest
    world.complete = lambda system, user: json.dumps({"facts": [
        {"about": "Torvar", "predicate": "hometown", "value": "Falkreath", "said_by": "Torvar", "line": 1}]})
    world.wait_for_jobs()
    assert open_db(tmp_path).execute("SELECT COUNT(*) FROM knowledge").fetchone()[0] == 0


def test_an_older_conversation_never_overrides_newer_news(tmp_path):
    phase_a(tmp_path)
    world = LivingWorld.latest
    world.wait_for_jobs()
    gerdur, _, torvar = ids(open_db(tmp_path))
    extractor = Extractor(world.store, world.complete)
    heard = extractor.lines(1)
    line = next(l for l in heard if "Falkreath" in l.text)
    # a late job from an earlier day (game day 2.0, before the hometown was learned on 3.6)
    extractor.apply(1, gerdur, {"facts": [{"about": "Torvar", "predicate": "hometown", "value": "Windhelm",
                                           "said_by": "Torvar", "line": line.seq}]},
                    heard, {gerdur: "Gerdur", torvar: "Torvar"}, "Riverwood", 2.0)
    rows = open_db(tmp_path).execute("SELECT value FROM knowledge WHERE predicate = 'hometown'").fetchall()
    assert [r[0] for r in rows] == ["Falkreath"]


# ---- recall ----------------------------------------------------------------------------------

def _add_memories(store, owner, rows):
    with store.transaction() as db:
        timeline = db.execute("SELECT timeline_id FROM timeline WHERE is_active = 1").fetchone()[0]
        for content, occurred, importance, source_kind, created_by in rows:
            db.execute("INSERT INTO memory (timeline_id, owner_id, kind, content, about_ids_json, location, occurred_gt,"
                       " importance, created_by, source_kind) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (timeline, owner, "episodic", content, "[]", "Riverwood", occurred, importance, created_by,
                        source_kind))


def test_grabs_cannot_push_out_what_was_said(tmp_path):
    phase_a(tmp_path)
    world = LivingWorld.latest
    world.wait_for_jobs()
    gerdur, _, torvar = ids(open_db(tmp_path))
    grabs = [("Torvar touched me on my shoulder in Riverwood.", 3.7 + i * 0.001, 0.5, "experienced", "perception")
             for i in range(30)]
    grabs += [(f"Torvar grabbed me by the arm for {i} seconds.", 3.75 + i * 0.001, 0.6, "experienced", "perception")
              for i in range(10)]
    _add_memories(world.store, gerdur, grabs)

    recall = recall_for(world.store, gerdur, 3.8, torvar, mark_rehearsed=False)
    text = recall.text()
    assert "I met Torvar from Falkreath" in text   # the conversation memory survives 40 newer grabs
    assert text.count("touched me on my shoulder") == 1
    assert "30 times, most recently" in text
    assert len(recall.memories) <= 12


def test_with_the_boost_off_only_the_newest_memories_show(tmp_path, monkeypatch):
    phase_a(tmp_path)
    world = LivingWorld.latest
    world.wait_for_jobs()
    gerdur, _, torvar = ids(open_db(tmp_path))
    _add_memories(world.store, gerdur, [(f"Torvar grabbed me {i}.", 3.7 + i * 0.001, 0.5, "experienced", "perception")
                                        for i in range(10)])
    monkeypatch.setenv(BOOST_ENV, "0")
    text = recall_for(world.store, gerdur, 3.8, torvar, mark_rehearsed=False).text()
    assert "I met Torvar" not in text
    assert "it ended like this" not in text


def test_talking_again_picks_up_where_it_stopped(tmp_path):
    phase_a(tmp_path)
    # no extraction has run yet: the recap comes straight from the log
    llm = gerdur_llm()
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([actors.player("Torvar"), actors.gerdur()], location="Riverwood", game_days=3.65)
        s.continue_until_player_turn()
        s.say("Where were we?")
        s.end(game_days=3.7)
    prompt = llm.dialogue_calls[0].system_prompt
    assert "The last time I talked with Torvar" in prompt
    assert "Torvar: What's my sister's name?" in prompt
    assert "Gerdur (me): I don't know your sister, friend." in prompt
    # the conversation going on now is not recapped as if it were the last one
    assert "Where were we?" not in prompt.split("it ended like this")[1].split("\n\n")[0]


def test_no_recap_after_a_couple_of_days(tmp_path):
    phase_a(tmp_path)
    llm = gerdur_llm()
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([actors.player("Torvar"), actors.gerdur()], location="Riverwood", game_days=6.0)
        s.continue_until_player_turn()
        s.say("Hello again.")
        s.end(game_days=6.1)
    assert "it ended like this" not in llm.dialogue_calls[0].system_prompt


# ---- backfill --------------------------------------------------------------------------------

def test_conversations_that_taught_nothing_are_extracted_once_more(tmp_path):
    phase_a(tmp_path)
    world = LivingWorld.latest
    world.complete = lambda system, user: '{"facts": [], "memories": []}'   # the old, empty result
    world.wait_for_jobs()
    store = world.store
    assert backfill.queue_backfill(store) == 1
    assert backfill.queue_backfill(store) == 0   # each conversation only once

    world.complete = lambda system, user: json.dumps({
        "memories": ["Torvar told me he is scared of spiders."],
        "reports": [{"about": "Torvar", "did": "fought a bear", "valence": 0.5, "said_by": "Torvar", "line": 2}]})
    world.wait_for_jobs()
    db = open_db(tmp_path)
    assert [r[0] for r in db.execute("SELECT content FROM memory WHERE created_by = 'extraction'")] == \
        ["Torvar told me he is scared of spiders."]
    assert db.execute("SELECT COUNT(*) FROM claim WHERE act = 'reported'").fetchone()[0] == 0   # no old gossip


def test_backfill_can_be_switched_off(tmp_path, monkeypatch):
    phase_a(tmp_path)
    world = LivingWorld.latest
    world.complete = lambda system, user: '{"facts": [], "memories": []}'
    world.wait_for_jobs()
    monkeypatch.setenv(backfill.BACKFILL_ENV, "0")
    assert backfill.queue_backfill(world.store) == 0


# ---- the background LLM call -----------------------------------------------------------------

def test_background_calls_get_their_own_reply_length_and_skip_the_spoken_cap(monkeypatch):
    import httpx
    from openai import BadRequestError
    from types import SimpleNamespace
    from src.llm.client_base import ClientBase
    from src.llm.messages import UserMessage

    sent = []

    class FakeCompletions:
        def create(self, **kwargs):
            sent.append(kwargs)
            if "response_format" in kwargs:   # a service without JSON mode
                raise BadRequestError("no json mode", response=httpx.Response(400, request=httpx.Request("POST", "http://x")),
                                      body=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"memories": []}'),
                                                            finish_reason="stop")])

    fake = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()), close=lambda: None)
    client = object.__new__(ClientBase)
    client._request_params = {"max_tokens": 250, "stop": ["#"], "temperature": 1.0}
    client._model_name = "test-model"
    monkeypatch.setattr(ClientBase, "model_name", property(lambda self: self._model_name))
    monkeypatch.setattr(ClientBase, "generate_sync_client", lambda self: fake)
    message = object.__new__(UserMessage)
    monkeypatch.setattr(UserMessage, "get_openai_message", lambda self: {"role": "user", "content": "hi"})

    reply = client.background_request_call(message, {"max_tokens": 1500, "temperature": 0.2}, json_mode=True)
    assert reply == '{"memories": []}'
    assert [("response_format" in k) for k in sent] == [True, False]
    assert all(k["max_tokens"] == 1500 and "stop" not in k and k["temperature"] == 0.2 for k in sent)
