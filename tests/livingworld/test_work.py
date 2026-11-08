"""W1 "NPC tasks and shared work": Gerdur's mill needs firewood, she offers the work, the player agrees or
not, the work is counted, she remembers how it went, and word gets around.

Everything runs through Mantella's real /mantella route with a scripted LLM. The game side of W2 (items
handed over) is simulated by writing the ``item_given`` event the game will report.
"""
import io
import json
import sqlite3

import pytest

from livingworld.harness import actors
from livingworld.harness.scripted_llm import ScriptedLLM, when_player_says
from livingworld.harness.session import HeadlessSession
from livingworld.integration import (ENV_VAR, KEY_LOAD_NONCE, KEY_LOADED_GAME_DAYS, KEY_TIMELINE_ID,
                                     KEY_WANTS_A_WORD, LivingWorld)
from livingworld.memory import jobs
from livingworld.social import gossip
from livingworld.store import world_store
from livingworld.store.world_store import WorldStore
from livingworld.work import TASKS_ENV, conditions, dev, tasks as T
from livingworld.work.templates import by_key
from src.http.communication_constants import communication_constants as comm_consts

PLAYER = actors.player("Torvar")
AUTUMN = 3.4          # 20 Last Seed, noon is set by the session (time=12)
AGREE = when_player_says("i'll help", "AgreeTask: Thank you. The block is by the mill.")
GIVE_UP = when_player_says("can't finish", "ReleaseTask: Fine. I'll manage.")
CHECK = when_player_says("all done", "CheckTask: Let me see.")


@pytest.fixture(autouse=True)
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.delenv(TASKS_ENV, raising=False)
    monkeypatch.setattr(LivingWorld, "start_workers", False)
    monkeypatch.setattr(jobs, "BACKOFF_SECONDS", (0, 0, 0, 0, 0))


def open_db(work_dir) -> sqlite3.Connection:
    conn = sqlite3.connect(work_dir / "data" / "livingworld" / "HeadlessTest.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def talk(work_dir, day, say=None, npcs=None, time=12, rules=(), extra=None, context_extra=None):
    """One conversation with Gerdur (by default). Returns (llm, Gerdur's first system prompt)."""
    llm = ScriptedLLM(rules=[AGREE, GIVE_UP, CHECK, *rules], replies=["Hm."])
    with HeadlessSession(str(work_dir), llm) as s:
        s.start([PLAYER, *(npcs or [actors.gerdur()])], game_days=day, time=time, extra=extra,
                context_extra=context_extra)
        s.continue_until_player_turn()
        if say:
            s.say(say)
        s.end(game_days=day + 0.01)
        LivingWorld.latest.wait_for_jobs()
        talk.reply = s.exchanges[1][1]
    prompt = llm.dialogue_calls[0].system_prompt if llm.dialogue_calls else ""
    return llm, prompt


def npc_id(db, name):
    return db.execute("SELECT npc_id FROM npc WHERE display_name = ?", (name,)).fetchone()[0]


def the_task(db, template="mill_firewood"):
    return db.execute("SELECT * FROM v_task WHERE template = ? ORDER BY task_id DESC LIMIT 1", (template,)).fetchone()


def hand_over(db_path, count, day, item="Firewood"):
    """What the game side (W2) will report: the player handed items to Gerdur."""
    db = sqlite3.connect(db_path)
    player = db.execute("SELECT npc_id FROM npc WHERE is_player = 1").fetchone()[0]
    gerdur = db.execute("SELECT npc_id FROM npc WHERE display_name = 'Gerdur'").fetchone()[0]
    db.execute("INSERT INTO world_event (timeline_id, type, location, actors_json, payload_json, source, game_time)"
               " VALUES ((SELECT timeline_id FROM timeline WHERE is_active = 1), 'item_given', 'Riverwood', ?, ?,"
               " 'game', ?)", (json.dumps([player, gerdur]), json.dumps({"item": item, "count": count}), day))
    db.commit()
    db.close()


def db_path(work_dir):
    return str(work_dir / "data" / "livingworld" / "HeadlessTest.sqlite")


def deltas_to_player(db):
    rows = db.execute("SELECT deltas_json, reason FROM relationship_change WHERE from_id = ? AND to_id = ?"
                      " AND reason LIKE 'task %'", (npc_id(db, "Gerdur"), npc_id(db, "Torvar"))).fetchall()
    return [(json.loads(r[0]), r[1]) for r in rows]


# ---- 1. a real need, offered at a sensible time ----

def test_gerdur_needs_firewood_before_winter_and_offers_the_work(tmp_path):
    _, prompt = talk(tmp_path, AUTUMN)
    assert "Work on Gerdur's mind:" in prompt
    assert "Hod and Faendal cannot keep up" in prompt                 # the reason, from her occupation
    assert "split and stack firewood for the mill (10 pieces of firewood)" in prompt
    assert "free to say no" in prompt and "50 gold" in prompt
    db = open_db(tmp_path)
    task = the_task(db)
    assert task["giver_id"] == npc_id(db, "Gerdur") and task["state"] == "available"   # nobody took it up
    states = [h["state"] for h in T.history(db, task["task_id"])]
    assert states == ["available", "offered", "available"]
    assert db.execute("SELECT COUNT(*) FROM world_event WHERE type = 'task.offered'").fetchone()[0] == 1


def test_no_offer_at_night_and_no_need_in_midsummer(tmp_path):
    _, prompt = talk(tmp_path, AUTUMN, time=23)
    assert "Work on Gerdur's mind" not in prompt
    db = open_db(tmp_path)
    assert the_task(db)["state"] == "available"        # the need is real, but she won't ask at 11 at night
    assert conditions.month(AUTUMN) == "Last Seed"
    assert conditions.month(300.0) == "Midyear"
    other = tmp_path / "summer"
    other.mkdir()
    _, prompt = talk(other, 300.0)
    assert "Work on Gerdur's mind" not in prompt
    assert the_task(open_db(other)) is None


def test_offer_waits_out_bad_weather(tmp_path):
    rain = {comm_consts.KEY_CONTEXT_WEATHER: {"mantella_weather_classification": 2}}
    _, prompt = talk(tmp_path, AUTUMN, context_extra=rain)
    assert "Work on Gerdur's mind" not in prompt


def test_a_refusal_is_fine_and_she_does_not_nag(tmp_path):
    talk(tmp_path, AUTUMN, say="No thanks, I'm busy.")
    _, prompt = talk(tmp_path, AUTUMN + 0.2)
    assert "Work on Gerdur's mind" not in prompt       # asked a few hours ago
    _, prompt = talk(tmp_path, AUTUMN + 1.1)
    assert "split and stack firewood" in prompt        # a day later she may ask again
    db = open_db(tmp_path)
    assert deltas_to_player(db) == []                  # saying no costs nothing


# ---- 2. agreeing, working, and her acknowledging it ----

def test_agreeing_records_a_promise_and_a_deadline(tmp_path):
    talk(tmp_path, AUTUMN, say="Sure, I'll help.")
    db = open_db(tmp_path)
    task = the_task(db)
    assert task["state"] == "accepted" and task["assignee_id"] == npc_id(db, "Torvar")
    assert task["deadline_gt"] == pytest.approx(AUTUMN + 3)
    promise = db.execute("SELECT * FROM obligation WHERE kind = 'promise'").fetchone()
    assert (promise["debtor_id"], promise["creditor_id"]) == (npc_id(db, "Torvar"), npc_id(db, "Gerdur"))
    _, prompt = talk(tmp_path, AUTUMN + 0.5)
    assert "Torvar agreed to help Gerdur split and stack firewood for the mill. So far 0 of 10" in prompt
    assert "promised 50 gold" in prompt


def test_finished_work_is_acknowledged_remembered_and_talked_about(tmp_path):
    talk(tmp_path, AUTUMN, say="Sure, I'll help.")
    hand_over(db_path(tmp_path), 6, AUTUMN + 0.2)
    hand_over(db_path(tmp_path), 5, AUTUMN + 0.3)
    _, prompt = talk(tmp_path, AUTUMN + 0.4)
    db = open_db(tmp_path)
    task = the_task(db)
    assert task["state"] == "completed" and task["progress"] == 11
    assert "Torvar finished helping Gerdur split and stack firewood for the mill" in prompt
    assert "on time" in prompt and "did more than asked" in prompt
    (deltas, _), = deltas_to_player(db)
    assert deltas["trust"] > 0 and deltas["affinity"] > 0 and deltas["familiarity"] > 0
    assert "admiration" in deltas                      # on time and more than asked
    assert "attraction" not in deltas                  # work never feeds romance directly
    memory = db.execute("SELECT content FROM memory WHERE owner_id = ? AND content LIKE '%firewood%'",
                        (npc_id(db, "Gerdur"),)).fetchone()
    assert memory[0].startswith("Torvar helped me split and stack firewood for the mill (on time")
    assert db.execute("SELECT COUNT(*) FROM obligation WHERE kind = 'favor'").fetchone()[0] == 1
    claim = db.execute("SELECT * FROM claim WHERE act = 'helped'").fetchone()
    assert claim["subject_id"] == npc_id(db, "Torvar") and claim["object_id"] == npc_id(db, "Gerdur")
    held = {r[0] for r in db.execute("SELECT holder_id FROM v_claim_knowledge WHERE claim_id = ?", (claim["claim_id"],))}
    assert npc_id(db, "Gerdur") in held


def test_word_of_the_help_reaches_hod(tmp_path):
    talk(tmp_path, AUTUMN, npcs=[actors.gerdur(), actors.hod()])   # both met, so both are in Riverwood's seed
    talk(tmp_path, AUTUMN + 0.05, say="Sure, I'll help.")
    hand_over(db_path(tmp_path), 10, AUTUMN + 0.2)
    talk(tmp_path, AUTUMN + 0.3)
    db = sqlite3.connect(db_path(tmp_path))
    with WorldStore(db_path(tmp_path)).transaction() as conn:
        gossip.catch_up(conn, "riverwood", AUTUMN + 20)
    claim_id = db.execute("SELECT claim_id FROM claim WHERE act = 'helped'").fetchone()[0]
    holders = {r[0] for r in db.execute("SELECT holder_id FROM v_claim_knowledge WHERE claim_id = ?", (claim_id,))}
    assert db.execute("SELECT npc_id FROM npc WHERE display_name = 'Hod'").fetchone()[0] in holders


def test_check_task_completes_only_what_was_really_done(tmp_path):
    talk(tmp_path, AUTUMN, say="Sure, I'll help.")
    hand_over(db_path(tmp_path), 4, AUTUMN + 0.1)
    talk(tmp_path, AUTUMN + 0.2, say="All done!")
    db = open_db(tmp_path)
    assert the_task(db)["state"] == "in_progress"       # she looked: 4 of 10 is not done
    assert any("checked: 4 of 10" in h["reason"] for h in T.history(db, the_task(db)["task_id"]))


# ---- 3. letting her down ----

def test_missing_the_deadline_costs_trust_and_spreads(tmp_path):
    talk(tmp_path, AUTUMN, say="Sure, I'll help.")
    _, prompt = talk(tmp_path, AUTUMN + 4)
    db = open_db(tmp_path)
    assert the_task(db)["state"] == "expired"
    assert "agreed to help Gerdur split and stack firewood for the mill but never finished it" in prompt
    (deltas, _), = deltas_to_player(db)
    assert deltas["trust"] < 0 and deltas["respect"] < 0
    assert db.execute("SELECT COUNT(*) FROM claim WHERE act = 'let_down'").fetchone()[0] == 1


def test_backing_out_costs_less_than_vanishing(tmp_path):
    talk(tmp_path, AUTUMN, say="Sure, I'll help.")
    talk(tmp_path, AUTUMN + 0.5, say="Sorry, I can't finish it.")
    db = open_db(tmp_path)
    assert the_task(db)["state"] == "failed"
    (withdrew, _), = deltas_to_player(db)
    other = tmp_path / "vanish"
    other.mkdir()
    talk(other, AUTUMN, say="Sure, I'll help.")
    talk(other, AUTUMN + 4)
    (vanished, _), = deltas_to_player(open_db(other))
    assert withdrew["trust"] < 0 and abs(withdrew["trust"]) < abs(vanished["trust"])


def test_after_repeated_let_downs_she_stops_asking(tmp_path):
    day = AUTUMN
    for _ in range(2):
        talk(tmp_path, day, say="Sure, I'll help.")
        day += 4                                       # deadline missed
        talk(tmp_path, day)
        day += 2.5                                     # past the cooldown
    _, prompt = talk(tmp_path, day)
    assert "no longer asks Torvar for help with work" in prompt
    assert "split and stack firewood for the mill (10" not in prompt
    db = open_db(tmp_path)
    changes = deltas_to_player(db)
    assert len(changes) == 2 and changes[1][0]["trust"] < changes[0][0]["trust"]   # the second hurts more


# ---- 4. saves, switches, children ----

def load(timeline, nonce, day):
    return {KEY_TIMELINE_ID: timeline, KEY_LOAD_NONCE: nonce, KEY_LOADED_GAME_DAYS: day}


def test_loading_an_older_save_rolls_the_task_back(tmp_path):
    talk(tmp_path, AUTUMN, extra=load(0, 101, AUTUMN - 0.1))
    timeline = talk.reply[KEY_TIMELINE_ID]
    talk(tmp_path, AUTUMN + 0.1, say="Sure, I'll help.", extra=load(timeline, 101, AUTUMN - 0.1))
    hand_over(db_path(tmp_path), 10, AUTUMN + 0.2)
    talk(tmp_path, AUTUMN + 0.3, extra=load(timeline, 101, AUTUMN - 0.1))
    assert the_task(open_db(tmp_path))["state"] == "completed"
    # the player loads the save made just after Gerdur first asked, before agreeing
    talk(tmp_path, AUTUMN + 0.06, extra=load(timeline, 102, AUTUMN + 0.05), time=23)
    db = open_db(tmp_path)
    assert the_task(db)["state"] == "available" and the_task(db)["assignee_id"] is None
    assert deltas_to_player(db) != []                    # raw rows stay (append-only)...
    gerdur, torvar = npc_id(db, "Gerdur"), npc_id(db, "Torvar")
    visible = db.execute("SELECT COUNT(*) FROM relationship_change c JOIN v_timeline_scope s"
                         " ON s.timeline_id = c.timeline_id AND c.game_time <= s.cutoff_gt"
                         " WHERE from_id = ? AND to_id = ? AND reason LIKE 'task %'", (gerdur, torvar)).fetchone()[0]
    assert visible == 0                                  # ...but none are visible on the new timeline


def test_switching_tasks_off(tmp_path, monkeypatch):
    monkeypatch.setenv(TASKS_ENV, "0")
    _, prompt = talk(tmp_path, AUTUMN)
    assert "Work on Gerdur's mind" not in prompt
    assert open_db(tmp_path).execute("SELECT COUNT(*) FROM task").fetchone()[0] == 0


def test_children_never_give_or_take_work(tmp_path):
    talk(tmp_path, AUTUMN, npcs=[actors.gerdur(), actors.hod()], time=23)
    store = WorldStore(db_path(tmp_path))
    with store.transaction() as db:
        task = T.find(db)[0]
        hod = npc_id(db, "Hod")
        db.execute("UPDATE npc SET is_child = 1 WHERE npc_id = ?", (hod,))   # pretend Hod were a child
        with pytest.raises(ValueError):
            T.change(db, task, "accepted", "test", AUTUMN, assignee_id=hod)
        with pytest.raises(ValueError):
            T.create(db, by_key("mill_firewood"), hod, AUTUMN)
    store.close()


def test_start_reply_names_who_wants_a_word(tmp_path):
    talk(tmp_path, AUTUMN, npcs=[actors.hod()])          # meet Hod; Gerdur not met yet, so no need yet
    assert KEY_WANTS_A_WORD not in talk.reply
    talk(tmp_path, AUTUMN + 0.1, say="No thanks.")      # Gerdur's need opens
    talk(tmp_path, AUTUMN + 1.2, npcs=[actors.hod()])
    assert talk.reply[KEY_WANTS_A_WORD] == "Gerdur|Skyrim.esm|0x0001347C"


def test_task_lifecycle_rejects_impossible_steps(tmp_path):
    talk(tmp_path, AUTUMN, time=23)
    store = WorldStore(db_path(tmp_path))
    with store.transaction() as db:
        task = T.find(db)[0]
        with pytest.raises(T.BadTransition):
            T.change(db, task, "completed", "skip ahead", AUTUMN)
        assert set(T.STATES) == {"available", "offered", "accepted", "in_progress", "paused", "completed",
                                 "failed", "expired"}
    store.close()


def test_outdoor_work_pauses_in_the_rain_and_resumes(tmp_path):
    talk(tmp_path, AUTUMN, say="Sure, I'll help.")
    hand_over(db_path(tmp_path), 2, AUTUMN + 0.1)
    rain = {comm_consts.KEY_CONTEXT_WEATHER: {"mantella_weather_classification": 2}}
    _, prompt = talk(tmp_path, AUTUMN + 0.2, context_extra=rain)
    assert the_task(open_db(tmp_path))["state"] == "paused"
    assert "on hold for now: too much rain" in prompt
    talk(tmp_path, AUTUMN + 0.3)
    assert the_task(open_db(tmp_path))["state"] == "in_progress"


# ---- 5. developer tool ----

def run_dev(tmp_path, *argv):
    lines = []
    code = dev.run(["--db", db_path(tmp_path), *argv], out=lines.append)
    return code, "\n".join(lines)


def test_developer_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(dev, "server_running", lambda port=4999: False)
    talk(tmp_path, AUTUMN, npcs=[actors.gerdur(), actors.hod()], time=23)
    code, text = run_dev(tmp_path, "explain", "Gerdur", "--hour", "23")
    assert "open  NO  free" in text and "offer NO  hour" in text
    code, text = run_dev(tmp_path, "create", "mill_firewood", "--giver", "Gerdur", "--assignee", "Hod")
    assert "[accepted] giver Gerdur, worker Hod" in text
    task_id = int(text.split()[0][1:])
    code, text = run_dev(tmp_path, "give", str(task_id), "10")
    assert "[completed]" in text
    code, text = run_dev(tmp_path, "show", str(task_id))
    assert "gave 10 Firewood" in text
    db = open_db(tmp_path)
    assert db.execute("SELECT COUNT(*) FROM claim WHERE act = 'helped'").fetchone()[0] == 1   # Hod helped Gerdur
    code, text = run_dev(tmp_path, "reset", "--npc", "Gerdur")
    assert code == 1 and "--yes" in text
    code, text = run_dev(tmp_path, "reset", "--npc", "Gerdur", "--yes")
    assert "reset 1 task(s)" in text
    code, text = run_dev(tmp_path, "list")
    assert text.startswith("0 task(s)")


def test_developer_tool_forces_an_offer_and_deadlines(tmp_path, monkeypatch):
    monkeypatch.setattr(dev, "server_running", lambda port=4999: False)
    talk(tmp_path, AUTUMN, time=23)
    run_dev(tmp_path, "offer", "mill_firewood", "--giver", "Gerdur")
    _, prompt = talk(tmp_path, AUTUMN + 0.01, time=23, say="Sure, I'll help.")
    assert "split and stack firewood" in prompt          # forced, even at night
    task_id = the_task(open_db(tmp_path))["task_id"]
    run_dev(tmp_path, "deadline", str(task_id), "--in", "0.1")
    code, text = run_dev(tmp_path, "advance", "--days", "0.5")
    assert f"expired [{task_id}]" in text


def test_developer_tool_refuses_while_the_server_runs(tmp_path, monkeypatch):
    talk(tmp_path, AUTUMN)
    monkeypatch.setattr(dev, "server_running", lambda port=4999: True)
    code, text = run_dev(tmp_path, "complete", "1")
    assert code == 2 and "Stop it first" in text
    code, _ = run_dev(tmp_path, "list")                  # reading is fine
    assert code == 0


# ---- 6. migrations developed side by side ----

def test_a_lower_numbered_migration_arriving_later_is_still_applied(tmp_path, monkeypatch):
    files = world_store._migration_files()
    without_5 = [f for f in files if f[0] != 5]
    monkeypatch.setattr(world_store, "_migration_files", lambda: without_5)
    store = WorldStore(str(tmp_path / "w.sqlite"))
    assert 6 in store.applied_versions()
    fake_5 = (5, "CREATE TABLE late_feature (x INTEGER) STRICT;"
                 " INSERT INTO schema_version(version, note) VALUES (5, 'late');")
    monkeypatch.setattr(world_store, "_migration_files", lambda: sorted(without_5 + [fake_5]))
    store.migrate()
    assert 5 in store.applied_versions()
    assert store.scalar("SELECT COUNT(*) FROM late_feature") == 0
    store.close()
