"""AI cost logging (MANTELLA_LW_COST_LOG, on by default): one record per LLM call with tokens and cost,
stored in llm_request, and the summary (python -m livingworld.perf.costs)."""
import asyncio
import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
from openai import BadRequestError, RateLimitError

from livingworld.harness import actors
from livingworld.harness.scripted_llm import ScriptedLLM
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, LivingWorld
from livingworld.perf import costs, llm_usage
from livingworld.perf.llm_usage import LEDGER, UsageRecord
from livingworld.store.world_store import WorldStore


@pytest.fixture(autouse=True)
def fresh_ledger(monkeypatch):
    monkeypatch.delenv(llm_usage.ENV_VAR, raising=False)
    monkeypatch.delenv(llm_usage.PRICES_ENV, raising=False)
    monkeypatch.setattr(llm_usage, "_extra_prices", None)
    LEDGER.reset()
    yield
    LEDGER.reset()


def collected(fn):
    with llm_usage.collect() as records:
        fn()
    return records


# ---- pricing --------------------------------------------------------------------------------

def test_cost_prefers_what_the_service_reported():
    assert llm_usage.cost_for("openai/gpt-4o-mini", 1000, 100, reported=0.0042) == (0.0042, "reported")


def test_free_and_local_models_cost_nothing():
    assert llm_usage.cost_for("nvidia/nemotron-3.5-super-vl:free", 5000, 500) == (0.0, "free")
    assert llm_usage.cost_for("my-local-model", 5000, 500, local=True) == (0.0, "local")


def test_known_models_are_priced_from_the_table_with_or_without_provider_prefix():
    cost, source = llm_usage.cost_for("openai/gpt-4o-mini", 1_000_000, 1_000_000)
    assert source == "priced" and cost == pytest.approx(0.75)
    cost, source = llm_usage.cost_for("gpt-4o-mini", 2000, 0)
    assert source == "priced" and cost == pytest.approx(0.0003)


def test_unknown_models_keep_their_tokens_but_cost_is_unknown():
    assert llm_usage.cost_for("someone/new-model", 1000, 100) == (0.0, "unknown")


def test_a_price_file_adds_and_overrides_prices(tmp_path, monkeypatch):
    path = tmp_path / "prices.json"
    path.write_text(json.dumps({"someone/new-model": [1.0, 2.0], "openai/gpt-4o-mini": [0, 0]}))
    monkeypatch.setenv(llm_usage.PRICES_ENV, str(path))
    assert llm_usage.cost_for("someone/new-model", 1_000_000, 1_000_000) == (pytest.approx(3.0), "priced")
    assert llm_usage.cost_for("openai/gpt-4o-mini", 1000, 1000) == (0.0, "priced")


def test_a_broken_price_file_falls_back_to_the_built_in_table(tmp_path, monkeypatch):
    path = tmp_path / "prices.json"
    path.write_text("{not json")
    monkeypatch.setenv(llm_usage.PRICES_ENV, str(path))
    assert llm_usage.cost_for("openai/gpt-4o-mini", 1000, 0)[1] == "priced"


# ---- reading usage --------------------------------------------------------------------------

def test_usage_numbers_read_openai_objects_and_openrouter_extras():
    usage = SimpleNamespace(prompt_tokens=1200, completion_tokens=80,
                            prompt_tokens_details=SimpleNamespace(cached_tokens=1000),
                            model_extra={"cost": 0.00031})
    assert llm_usage.usage_numbers(usage) == {"prompt_tokens": 1200, "completion_tokens": 80,
                                              "cached_tokens": 1000, "reported_cost": pytest.approx(0.00031)}
    assert llm_usage.usage_numbers({"prompt_tokens": 5, "completion_tokens": 2})["prompt_tokens"] == 5
    assert llm_usage.usage_numbers(None) == {}


def test_usage_is_requested_only_from_services_known_to_take_it():
    assert llm_usage.request_extras("https://openrouter.ai/api/v1", True) == {
        "stream_options": {"include_usage": True}, "extra_body": {"usage": {"include": True}}}
    assert llm_usage.request_extras("https://openrouter.ai/api/v1", False) == {"extra_body": {"usage": {"include": True}}}
    assert llm_usage.request_extras("https://api.openai.com/v1", True) == {"stream_options": {"include_usage": True}}
    assert llm_usage.request_extras("https://api.openai.com/v1", False) == {}
    assert llm_usage.request_extras("http://localhost:5001/v1", True) == {}


def test_the_players_own_request_settings_win():
    merged = llm_usage.merge_extras({"extra_body": {"provider": {"sort": "price"}}, "stream_options": {"x": 1}},
                                    {"extra_body": {"usage": {"include": True}},
                                     "stream_options": {"include_usage": True}})
    assert merged == {"extra_body": {"usage": {"include": True}, "provider": {"sort": "price"}},
                      "stream_options": {"x": 1}}


def test_switched_off_nothing_is_requested_or_recorded(monkeypatch):
    monkeypatch.setenv(llm_usage.ENV_VAR, "0")
    assert llm_usage.request_extras("https://openrouter.ai/api/v1", True) == {}
    assert collected(lambda: llm_usage.record_call(task="dialogue", model="m", usage={"prompt_tokens": 1})) == []
    assert LEDGER.session["foreground"].calls == 0


# ---- the ledger -----------------------------------------------------------------------------

def test_records_wait_for_a_sink_then_are_handed_over():
    llm_usage.record_call(task="summary", model="m", usage={"prompt_tokens": 10, "completion_tokens": 2})
    got = []
    LEDGER.set_sink(got.extend)
    assert [r.task for r in got] == ["summary"]
    llm_usage.record_call(task="extract", model="m", usage={"prompt_tokens": 1, "completion_tokens": 1})
    assert [r.task for r in got] == ["summary", "extract"] and got[1].lane == "background"


def test_the_waiting_buffer_is_capped(monkeypatch):
    monkeypatch.setattr(llm_usage, "PENDING_MAX", 3)
    for _ in range(5):
        llm_usage.record_call(task="request", model="m")
    got = []
    LEDGER.set_sink(got.extend)
    assert len(got) == 3 and LEDGER.dropped == 2


def test_a_failing_sink_never_reaches_the_caller():
    def broken(records):
        raise sqlite3.OperationalError("database is locked")
    LEDGER.set_sink(broken)
    assert llm_usage.record_call(task="extract", model="m") is not None
    assert LEDGER.dropped == 1


def test_attribution_fills_task_and_ids():
    with llm_usage.attribute(task="extract", conv_id=7), llm_usage.attribute(npc_id=3):
        assert llm_usage.task_for("request") == "extract"
        records = collected(lambda: llm_usage.record_call(task=llm_usage.task_for("request"), model="m"))
    assert (records[0].task, records[0].conv_id, records[0].npc_id) == ("extract", 7, 3)
    assert llm_usage.task_for("tools") == "tools"


def test_tokens_are_counted_locally_when_the_service_sends_none():
    rec = llm_usage.record_call(task="dialogue", model="openai/gpt-4o-mini", estimate=lambda: (1000, 50))
    assert rec.estimated and (rec.prompt_tokens, rec.completion_tokens) == (1000, 50) and rec.cost_source == "priced"
    failed = llm_usage.record_call(task="dialogue", model="m", status="error", error="x", estimate=lambda: (1, 1))
    assert failed.prompt_tokens is None and not failed.estimated


def test_session_line_splits_foreground_and_background():
    llm_usage.record_call(task="dialogue", model="m", usage={"prompt_tokens": 1, "cost": 0.01})
    llm_usage.record_call(task="extract", model="m", usage={"prompt_tokens": 1, "cost": 0.002})
    llm_usage.record_call(task="extract", model="mystery", usage={"prompt_tokens": 1})
    line = LEDGER.session_line()
    assert "$0.0120" in line and "foreground $0.0100 in 1 calls" in line and "background $0.0020 in 2" in line
    assert "1 call(s) with no known price" in line


def test_combine_sums_a_turns_calls():
    recs = [UsageRecord("dialogue", "a", prompt_tokens=100, completion_tokens=10, cost_usd=0.1, cost_source="reported"),
            UsageRecord("dialogue", "a", prompt_tokens=50, completion_tokens=None, cost_usd=0.2,
                        cost_source="reported", estimated=True)]
    c = llm_usage.combine(recs)
    assert c["prompt_tokens"] == 150 and c["completion_tokens"] == 10 and c["cost_usd"] == pytest.approx(0.3)
    assert c["cost_source"] == "reported" and c["estimated"] == 1 and c["model"] == "a"
    recs.append(UsageRecord("dialogue", "b", cost_source="priced"))
    assert llm_usage.combine(recs)["cost_source"] == "mixed"


# ---- the Mantella clients -------------------------------------------------------------------

def _client(cls_name="ClientBase", base_url="https://openrouter.ai/api/v1", model="openai/gpt-4o-mini"):
    from src.llm.client_base import ClientBase
    from src.llm.function_client import FunctionClient
    cls = {"ClientBase": ClientBase, "FunctionClient": FunctionClient}[cls_name]
    client = object.__new__(cls)
    client._request_params = {"max_tokens": 250}
    client._model_name = model
    client._base_url = base_url
    client._is_local = False
    client._caching_enabled = False
    client._generation_lock = __import__("threading").Lock()
    return client


def _message(monkeypatch):
    from src.llm.messages import UserMessage
    monkeypatch.setattr(UserMessage, "get_openai_message", lambda self: {"role": "user", "content": "hi"})
    monkeypatch.setattr(UserMessage, "get_ingame_events_text", lambda self: "")
    return object.__new__(UserMessage)


def _completion(text="ok", usage=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason="stop")],
                           usage=usage)


def _sync(monkeypatch, client, create):
    from src.llm.client_base import ClientBase
    sent = []

    class Completions:
        def create(self, **kwargs):
            sent.append(kwargs)
            return create(**kwargs)
    fake = SimpleNamespace(chat=SimpleNamespace(completions=Completions()), close=lambda: None)
    monkeypatch.setattr(ClientBase, "model_name", property(lambda self: self._model_name))
    monkeypatch.setattr(type(client), "generate_sync_client", lambda self: fake)
    return sent


def test_a_one_shot_call_is_recorded_with_its_usage_and_asks_openrouter_for_cost(monkeypatch):
    client = _client()
    usage = SimpleNamespace(prompt_tokens=900, completion_tokens=120, prompt_tokens_details=None,
                            model_extra={"cost": 0.0002})
    sent = _sync(monkeypatch, client, lambda **kw: _completion("A summary.", usage))
    with llm_usage.attribute(task="summary"):
        records = collected(lambda: client.request_call(_message(monkeypatch)))
    assert sent[0]["extra_body"] == {"usage": {"include": True}} and "stream_options" not in sent[0]
    rec, = records
    assert (rec.task, rec.lane, rec.prompt_tokens, rec.completion_tokens) == ("summary", "background", 900, 120)
    assert rec.cost_usd == pytest.approx(0.0002) and rec.cost_source == "reported" and rec.status == "ok"


def test_a_tool_calling_client_records_its_calls_as_tools(monkeypatch):
    client = _client("FunctionClient", base_url="http://localhost:5001/v1")
    sent = _sync(monkeypatch, client, lambda **kw: _completion("x", {"prompt_tokens": 10, "completion_tokens": 1}))
    rec, = collected(lambda: client._request_call_full(_message(monkeypatch)))
    assert rec.task == "tools" and rec.lane == "foreground" and "extra_body" not in sent[0]


def test_a_rate_limited_call_is_recorded_as_such(monkeypatch):
    client = _client()
    monkeypatch.setattr("time.sleep", lambda s: None)

    def refuse(**kw):
        raise RateLimitError("slow down", response=httpx.Response(429, request=httpx.Request("POST", "http://x")),
                             body=None)
    _sync(monkeypatch, client, refuse)
    rec, = collected(lambda: client._request_call_full(_message(monkeypatch)))
    assert rec.status == "rate_limited" and rec.prompt_tokens is None


def test_background_calls_record_once_even_when_json_mode_is_refused(monkeypatch):
    client = _client()

    def create(**kw):
        if "response_format" in kw:
            raise BadRequestError("no json mode", response=httpx.Response(400, request=httpx.Request("POST", "http://x")),
                                  body=None)
        return _completion('{"memories": []}', {"prompt_tokens": 3000, "completion_tokens": 200})
    sent = _sync(monkeypatch, client, create)
    with llm_usage.attribute(task="extract", conv_id=4):
        records = collected(lambda: client.background_request_call(_message(monkeypatch), {"max_tokens": 1500},
                                                                   json_mode=True))
    assert len(sent) == 2 and all(k["extra_body"] == {"usage": {"include": True}} for k in sent)
    rec, = records
    assert (rec.task, rec.conv_id, rec.prompt_tokens, rec.cost_source) == ("extract", 4, 3000, "priced")


def test_a_failed_background_call_is_recorded_and_still_raises(monkeypatch):
    client = _client()

    def create(**kw):
        raise ValueError("boom")
    _sync(monkeypatch, client, create)
    with llm_usage.collect() as records, pytest.raises(ValueError):
        client.background_request_call(_message(monkeypatch))
    assert records[0].status == "error" and records[0].error == "boom"


class _Stream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


def _chunk(text=None, usage=None):
    choices = [SimpleNamespace(delta=SimpleNamespace(content=text, tool_calls=None))] if text is not None else []
    return SimpleNamespace(choices=choices, usage=usage)


def _streaming(monkeypatch, client, chunks):
    sent = []

    class Completions:
        async def create(self, **kwargs):
            sent.append(kwargs)
            return _Stream(chunks)

    async def close():
        return None
    fake = SimpleNamespace(chat=SimpleNamespace(completions=Completions()), close=close)
    from src.llm.client_base import ClientBase
    monkeypatch.setattr(ClientBase, "model_name", property(lambda self: self._model_name))
    monkeypatch.setattr(ClientBase, "max_tokens_param", property(lambda self: 250))
    monkeypatch.setattr(ClientBase, "_should_enable_vision", lambda self: False)
    client._startup_async_client = fake
    client._function_client = None
    client._image_client = None
    return sent


def _run_stream(client, messages, stop_after=None):
    async def go():
        out = []
        gen = client.streaming_call(messages, False)
        async for kind, value in gen:
            out.append(value)
            if stop_after and len(out) >= stop_after:
                await gen.aclose()
                break
        return out
    return asyncio.run(go())


def test_a_streamed_reply_takes_its_usage_from_the_last_chunk(monkeypatch):
    client = _client()
    usage = SimpleNamespace(prompt_tokens=4000, completion_tokens=30,
                            prompt_tokens_details=SimpleNamespace(cached_tokens=3500), model_extra={"cost": 0.0007})
    sent = _streaming(monkeypatch, client, [_chunk("Welcome "), _chunk("to Riverwood."), _chunk(usage=usage)])
    with llm_usage.collect() as records:
        text = _run_stream(client, _message(monkeypatch))
    assert "".join(text) == "Welcome to Riverwood."
    assert sent[0]["stream_options"] == {"include_usage": True}
    rec, = records
    assert (rec.task, rec.lane, rec.prompt_tokens, rec.cached_tokens) == ("dialogue", "foreground", 4000, 3500)
    assert rec.cost_usd == pytest.approx(0.0007) and not rec.estimated


def test_a_stream_without_usage_is_estimated_even_when_cut_short(monkeypatch):
    client = _client(base_url="https://example.com/v1", model="openai/gpt-4o-mini")
    monkeypatch.setattr(type(client), "get_count_tokens", lambda self, m: 7 if isinstance(m, str) else 1200)
    sent = _streaming(monkeypatch, client, [_chunk("One. "), _chunk("Two. "), _chunk("Three.")])
    with llm_usage.collect() as records:
        _run_stream(client, _message(monkeypatch), stop_after=1)
    assert "stream_options" not in sent[0] and "extra_body" not in sent[0]
    rec, = records
    assert rec.estimated and (rec.prompt_tokens, rec.completion_tokens) == (1200, 7) and rec.cost_source == "priced"


def test_a_failed_stream_is_recorded_as_an_error(monkeypatch):
    client = _client()
    _streaming(monkeypatch, client, [])

    class Broken:
        async def create(self, **kwargs):
            raise RuntimeError("connection reset")
    client._startup_async_client.chat = SimpleNamespace(completions=Broken())
    monkeypatch.setattr("src.utils.play_error_sound", lambda: None)
    with llm_usage.collect() as records:
        _run_stream(client, _message(monkeypatch))
    assert records[0].status == "error" and "connection reset" in records[0].error


# ---- the Living World store -----------------------------------------------------------------

class CostedLLM(ScriptedLLM):
    """A scripted model that reports usage the way the real clients do."""

    async def streaming_call(self, messages, is_multi_npc, tools=None):
        async for part in super().streaming_call(messages, is_multi_npc, tools):
            yield part
        llm_usage.record_call(task="dialogue", model="openai/gpt-4o-mini",
                              usage={"prompt_tokens": 2000, "completion_tokens": 20, "cost": 0.0004})

    def request_call(self, messages):
        reply = super().request_call(messages)
        llm_usage.record_call(task=llm_usage.task_for("request"), model="nvidia/nemotron:free",
                              usage={"prompt_tokens": 3000, "completion_tokens": 300})
        return reply


@pytest.fixture
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.setattr(LivingWorld, "start_workers", False)


def _db(work_dir):
    db = sqlite3.connect(work_dir / "data" / "livingworld" / "HeadlessTest.sqlite")
    db.row_factory = sqlite3.Row
    return db


def test_a_conversation_stores_every_call_with_tokens_and_cost(tmp_path, living_world_inline, caplog):
    caplog.set_level(1, logger="Mantella")
    with HeadlessSession(str(tmp_path), CostedLLM(replies=["Welcome to Riverwood."])) as s:
        s.start([actors.player("Torvar"), actors.gerdur()], location="Riverwood", game_days=3.5)
        s.continue_until_player_turn()
        s.say("How is the mill?")
        s.end(game_days=3.6)
        LivingWorld.latest.wait_for_jobs()
    db = _db(tmp_path)
    rows = db.execute("SELECT * FROM llm_request ORDER BY req_id").fetchall()
    dialogue = [r for r in rows if r["task"] == "dialogue"]
    assert len(dialogue) == 2
    for r in dialogue:
        assert (r["lane"], r["prompt_tokens"], r["completion_tokens"], r["cost_source"]) == ("foreground", 2000, 20,
                                                                                             "reported")
        assert r["cost_usd"] == pytest.approx(0.0004) and r["npc_id"] is not None and r["trace_id"] is not None
        assert r["model"] == "openai/gpt-4o-mini"
    extract = [r for r in rows if r["task"] == "extract"]
    assert extract and all(r["lane"] == "background" and r["cost_source"] == "free" and r["conv_id"] == dialogue[0]["conv_id"]
                           for r in extract)
    assert any("AI cost $0.0008 (2 calls" in m for m in caplog.messages)


def test_switched_off_dialogue_rows_carry_no_cost(tmp_path, living_world_inline, monkeypatch):
    monkeypatch.setenv(llm_usage.ENV_VAR, "0")
    with HeadlessSession(str(tmp_path), CostedLLM(replies=["Hello."])) as s:
        s.start([actors.player("Torvar"), actors.gerdur()], location="Riverwood", game_days=3.5)
        s.continue_until_player_turn()
        s.end(game_days=3.6)
        LivingWorld.latest.wait_for_jobs()
    rows = _db(tmp_path).execute("SELECT task, prompt_tokens, cost_usd FROM llm_request").fetchall()
    assert rows and all(r["task"] == "dialogue" and r["prompt_tokens"] is None and r["cost_usd"] == 0 for r in rows)


def test_group_talk_stores_each_speakers_cost(tmp_path, living_world_inline):
    llm = CostedLLM(replies=["Hod, this is Torvar.", "Well met."])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([actors.player("Torvar"), actors.gerdur(), actors.hod()], location="Riverwood", game_days=11.0)
        s.continue_until_player_turn()
        s.end(game_days=11.1)
    rows = _db(tmp_path).execute("SELECT npc_id, prompt_tokens FROM llm_request WHERE task = 'dialogue'").fetchall()
    assert len({r["npc_id"] for r in rows}) == 2 and all(r["prompt_tokens"] == 2000 for r in rows)


def test_the_schema_has_the_cost_columns(tmp_path):
    store = WorldStore(str(tmp_path / "w.sqlite"))
    cols = {r[1] for r in store._conn.execute("PRAGMA table_info(llm_request)")}
    assert {"lane", "cached_tokens", "estimated", "cost_source", "prompt_tokens", "cost_usd"} <= cols
    assert store.schema_version() >= 24


# ---- the summary ----------------------------------------------------------------------------

def _seeded(tmp_path) -> str:
    path = str(tmp_path / "World1.sqlite")
    store = WorldStore(path)
    with store.transaction() as db:
        npc = db.execute("INSERT INTO npc (plugin, base_form_id, display_name, created_gt) VALUES ('Skyrim.esm','0x1','Gerdur', 1.0)"
                         ).lastrowid
        db.execute("INSERT INTO llm_request (task, model, status) VALUES ('dialogue', 'old', 'ok')")   # before logging
        for task, model, cost, source, prompt in [("dialogue", "openai/gpt-4o-mini", 0.004, "reported", 4000),
                                                  ("dialogue", "openai/gpt-4o-mini", 0.006, "reported", 6000),
                                                  ("extract", "nvidia/x:free", 0.0, "free", 3000),
                                                  ("summary", "mystery/model", 0.0, "unknown", 1000)]:
            llm_usage.insert_usage(db, 1, task, model, npc if task == "dialogue" else None, None, None, 900, "ok",
                                   None, {"prompt_tokens": prompt, "completion_tokens": 100, "cost_usd": cost,
                                          "cost_source": source, "cached_tokens": 500 if task == "dialogue" else None})
    store.close()
    return path


def test_summary_totals_lanes_models_and_npcs(tmp_path):
    db = costs.connect(_seeded(tmp_path))
    s = costs.summarize(db)
    assert s["total"].calls == 4 and s["total"].cost == pytest.approx(0.010) and s["total"].prompt == 14000
    lanes = {l.label: l for l in s["lanes"]}
    assert lanes["foreground"].cost == pytest.approx(0.010) and lanes["background"].calls == 2
    assert s["models"][0].label == "openai/gpt-4o-mini"
    assert [(l.label, l.calls) for l in s["npcs"]] == [("Gerdur", 2)]
    assert s["unknown"] == [("mystery/model", 1, 1100)] and s["before_logging"] == 1
    text = costs.render("World1.sqlite", s, None)
    assert "Total: $0.0100 in 4 calls" in text and "mystery/model: 1 calls" in text
    assert "1 older row(s) from before cost logging" in text


def test_summary_last_days_leaves_out_older_calls(tmp_path):
    path = _seeded(tmp_path)
    store = WorldStore(path)
    with store.transaction() as db:
        db.execute("UPDATE llm_request SET at = '2020-01-01T00:00:00.000Z' WHERE task = 'summary'")
    store.close()
    s = costs.summarize(costs.connect(path), days=7)
    assert s["total"].calls == 3 and s["unknown"] == []


def test_summary_command_reads_a_folder_of_worlds(tmp_path, capsys):
    _seeded(tmp_path)
    assert costs.main([str(tmp_path), "--days", "30"]) == 0
    out = capsys.readouterr().out
    assert "AI cost: World1.sqlite (last 30 days)" in out and "By model:" in out


def test_summary_of_a_database_from_before_cost_logging(tmp_path):
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE llm_request (req_id INTEGER PRIMARY KEY, task TEXT, model TEXT)")
    db.commit()
    db.close()
    s = costs.summarize(costs.connect(str(path)))
    assert s == {"schema_ready": False}
    assert "no cost columns yet" in costs.render("old.sqlite", s, None)
