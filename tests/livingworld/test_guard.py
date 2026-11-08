"""G1 "Join the Whiterun Guard": the player asks Commander Caius to join, the guard's rules decide,
a yes starts a service record and hands over the uniform, guards recognise the player's rank, generic
guards have names of their own, and word of it gets around.

Everything runs through Mantella's real /mantella route with a scripted LLM. The game side sends each
participant's IsGuard(), crime faction and the player's bounty there (Mantella-Spell LIVINGWORLD PATCH).
"""
import json
import sqlite3

import pytest

from livingworld.guard import GUARD_ENV, dev, orgs as O, recruit as R, store as S
from livingworld.harness import actors
from livingworld.harness.scripted_llm import ScriptedLLM, when_player_says
from livingworld.harness.session import HeadlessSession
from livingworld.integration import (ENV_VAR, KEY_CRIME_FACTION, KEY_CRIME_PLUGIN, KEY_GUARD_ISSUE, KEY_IS_GUARD,
                                     KEY_LOAD_NONCE, KEY_LOADED_GAME_DAYS, KEY_PLAYER_BOUNTY, KEY_THANE_OF,
                                     KEY_TIMELINE_ID, KEY_WANTS_A_WORD, LivingWorld)
from livingworld.memory import jobs
from livingworld.store.world_store import WorldStore
from src.http.communication_constants import communication_constants as comm_consts

PLAYER = actors.player("Torvar")
DAY = 3.4
WHITERUN = O.by_key("whiterun_guard")
ENLIST = when_player_says("join the guard", "EnlistRecruit: Very well. Welcome to the Guard, recruit.")


@pytest.fixture(autouse=True)
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.delenv(GUARD_ENV, raising=False)
    monkeypatch.setattr(LivingWorld, "start_workers", False)
    monkeypatch.setattr(jobs, "BACKOFF_SECONDS", (0, 0, 0, 0, 0))


def guard_keys(a, is_guard=True, bounty=0):
    a[KEY_IS_GUARD] = is_guard
    a[KEY_CRIME_FACTION] = int(WHITERUN.crime_faction, 16)
    a[KEY_CRIME_PLUGIN] = "Skyrim.esm"
    a[KEY_PLAYER_BOUNTY] = bounty
    return a


def caius(bounty=0):
    return guard_keys(actors.actor("Commander Caius", 0x00038257, 0x00038258, 0, "Imperial", "MaleCommander"),
                      is_guard=False, bounty=bounty)


def whiterun_guard(ref=0x0001A6B2, female=False):
    return guard_keys(actors.actor("Whiterun Guard", 0x000A0DA4, ref, 1 if female else 0, "Nord",
                                   "FemaleCommander" if female else "MaleGuard"))


def talk(work_dir, day, say=None, npcs=None, extra=None, context_extra=None, location="Whiterun"):
    """One conversation (with Caius by default). Returns (llm, first NPC's first system prompt)."""
    llm = ScriptedLLM(rules=[ENLIST], replies=["Hm."])
    with HeadlessSession(str(work_dir), llm) as s:
        s.start([PLAYER, *(npcs or [caius()])], location=location, game_days=day, extra=extra,
                context_extra=context_extra)
        s.continue_until_player_turn()
        if say:
            s.say(say)
        s.end(game_days=day + 0.01)
        LivingWorld.latest.wait_for_jobs()
        talk.reply = s.exchanges[1][1]
        lines = [r[comm_consts.KEY_REPLYTYPE_NPCTALK] for _, r in s.exchanges
                 if KEY_GUARD_ISSUE in (r.get(comm_consts.KEY_REPLYTYPE_NPCTALK) or {})]
        talk.issued = [line[KEY_GUARD_ISSUE] for line in lines]
        talk.issued_ids = [line["mantella_lw_guard_issue_ids"] for line in lines]
        talk.issued_plugins = [line["mantella_lw_guard_issue_plugins"] for line in lines]
    prompt = llm.dialogue_calls[0].system_prompt if llm.dialogue_calls else ""
    return llm, prompt


def db_path(work_dir):
    return str(work_dir / "data" / "livingworld" / "HeadlessTest.sqlite")


def open_db(work_dir) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(work_dir))
    conn.row_factory = sqlite3.Row
    return conn


def npc_id(db, name):
    return db.execute("SELECT npc_id FROM npc WHERE display_name = ? ORDER BY npc_id LIMIT 1", (name,)).fetchone()[0]


def service(db):
    return db.execute("SELECT * FROM v_service WHERE is_reset = 0 ORDER BY service_id DESC LIMIT 1").fetchone()


# ---- 1-3. asking to join, the requirements, the first rank ----

def test_caius_is_willing_and_says_how(tmp_path):
    _, prompt = talk(tmp_path, DAY)
    assert "The guard, and Commander Caius:" in prompt
    assert "willing to take them on as a Recruit on probation for 7 days" in prompt
    assert "begin the reply with 'EnlistRecruit:'" in prompt
    assert open_db(tmp_path).execute("SELECT COUNT(*) FROM service").fetchone()[0] == 0   # asking is not joining


def test_asking_to_join_starts_a_service_as_a_recruit(tmp_path):
    talk(tmp_path, DAY, say="I want to join the guard.")
    db = open_db(tmp_path)
    svc = service(db)
    assert svc["org_key"] == "whiterun_guard" and svc["rank_key"] == "recruit" and svc["status"] == "probation"
    assert svc["path"] == "applied" and svc["recruiter_id"] == npc_id(db, "Commander Caius")
    assert svc["member_id"] == npc_id(db, "Torvar")
    assert talk.issued == ["Whiterun Guard's Armor|Skyrim.esm|0x0002150D;Whiterun Guard's Helmet|Skyrim.esm|0x00021615;"
                           "Whiterun Guard's Shield|Skyrim.esm|0x000214E0"]
    assert talk.issued_ids == [[0x2150D, 0x21615, 0x214E0]] and talk.issued_plugins == [["Skyrim.esm"] * 3]


def test_a_bounty_in_the_hold_bars_joining_and_the_action_is_refused(tmp_path):
    _, prompt = talk(tmp_path, DAY, say="I want to join the guard.", npcs=[caius(bounty=120)])
    assert "will not take them on now (has a bounty of 120 gold in Whiterun)" in prompt
    assert "Do not use EnlistRecruit" in prompt
    assert open_db(tmp_path).execute("SELECT COUNT(*) FROM service").fetchone()[0] == 0   # the model cannot overrule
    assert talk.issued == []


def test_a_bad_name_in_whiterun_bars_joining(tmp_path):
    talk(tmp_path, DAY)
    store = WorldStore(db_path(tmp_path))
    with store.transaction() as db:
        c, p = npc_id(db, "Commander Caius"), npc_id(db, "Torvar")
        db.execute("INSERT OR IGNORE INTO relationship (from_id, to_id, status, updated_gt) VALUES (?,?,?,?)",
                   (c, p, "stranger", DAY))
        db.execute("INSERT INTO relationship_change (timeline_id, from_id, to_id, deltas_json, reason, game_time)"
                   " VALUES (1,?,?,?,?,?)", (c, p, json.dumps({"trust": -0.4, "respect": -0.3}), "test", DAY))
        d = R.decide(db, WHITERUN, c, p, DAY + 0.1)
        assert not d.ok and d.blocking[0].name == "standing"
    store.close()
    _, prompt = talk(tmp_path, DAY + 0.1, say="I want to join the guard.")
    assert "will not take them on now (standing -0.35" in prompt
    assert open_db(tmp_path).execute("SELECT COUNT(*) FROM service").fetchone()[0] == 0


def test_a_thane_is_invited_and_taken_on_at_the_jarls_word(tmp_path):
    _, prompt = talk(tmp_path, DAY, context_extra={KEY_THANE_OF: ["Whiterun"]})
    assert "As thane of Whiterun, Torvar is taken on at the Jarl's word" in prompt
    assert "Torvar is thane of Whiterun; if it fits the moment, Commander Caius may suggest" in prompt
    assert KEY_WANTS_A_WORD not in talk.reply              # he is already here
    talk(tmp_path, DAY + 0.05, npcs=[whiterun_guard()], context_extra={KEY_THANE_OF: ["Whiterun"]})
    assert KEY_WANTS_A_WORD not in talk.reply              # he raised it a moment ago; no nagging
    talk(tmp_path, DAY + 8, npcs=[whiterun_guard()], context_extra={KEY_THANE_OF: ["Whiterun"]})
    assert talk.reply.get(KEY_WANTS_A_WORD) == "Commander Caius|Skyrim.esm|0x00038257"
    talk(tmp_path, DAY + 8.1, say="I'd like to join the guard.", context_extra={KEY_THANE_OF: ["Whiterun"]})
    assert service(open_db(tmp_path))["path"] == "thane"


def test_one_hold_at_a_time(tmp_path):
    talk(tmp_path, DAY, say="I want to join the guard.")
    _, prompt = talk(tmp_path, DAY + 0.2)
    assert "Torvar serves in the Whiterun Guard as a Recruit, still on probation" in prompt
    assert "willing to take them on" not in prompt
    store = WorldStore(db_path(tmp_path))
    with store.transaction() as db:
        d = R.decide(db, WHITERUN, npc_id(db, "Commander Caius"), npc_id(db, "Torvar"), DAY + 0.2)
        assert [c.name for c in d.blocking] == ["serving"]
    store.close()


# ---- 7. recognition, 9. guards as people, gossip ----

def test_generic_guards_get_names_of_their_own(tmp_path):
    a, b = whiterun_guard(0x0001A6B2), whiterun_guard(0x0001A6B3, female=True)
    _, prompt = talk(tmp_path, DAY, npcs=[a])
    db = open_db(tmp_path)
    names = [r[0] for r in db.execute("SELECT given_name FROM guard_profile WHERE given_name IS NOT NULL")]
    assert len(names) == 1 and f"this guard's own name is {names[0]}" in prompt
    talk(tmp_path, DAY + 0.1, npcs=[b])
    talk(tmp_path, DAY + 0.2, npcs=[a])
    db = open_db(tmp_path)
    rows = db.execute("SELECT given_name FROM guard_profile WHERE given_name IS NOT NULL ORDER BY npc_id").fetchall()
    assert len(rows) == 2 and rows[0][0] == names[0] and rows[1][0] != names[0]     # stable and distinct
    assert {r[0] for r in db.execute("SELECT org_key FROM guard_profile")} == {"whiterun_guard"}


def test_a_guard_sends_the_player_to_caius(tmp_path):
    _, prompt = talk(tmp_path, DAY, npcs=[whiterun_guard()])
    assert "cannot take anyone on; that is for Commander Caius or Irileth" in prompt


def test_guards_recognise_the_new_recruit_and_their_rank(tmp_path):
    talk(tmp_path, DAY, say="I want to join the guard.", npcs=[caius(), whiterun_guard()])
    _, prompt = talk(tmp_path, DAY + 0.5, npcs=[whiterun_guard()])
    assert "Torvar serves in the Whiterun Guard as a Recruit" in prompt
    assert "taken on by Commander Caius" in prompt
    assert "outranks Torvar" in prompt


def test_word_of_the_enlistment_gets_around(tmp_path):
    talk(tmp_path, DAY, say="I want to join the guard.", npcs=[caius(), whiterun_guard()])
    db = open_db(tmp_path)
    claim = db.execute("SELECT * FROM claim WHERE act = 'enlisted'").fetchone()
    assert claim["subject_id"] == npc_id(db, "Torvar") and claim["object_id"] == npc_id(db, "Commander Caius")
    assert "as a Recruit in the Whiterun Guard" in claim["detail"]
    holders = {r[0] for r in db.execute("SELECT holder_id FROM knowledge WHERE claim_id = ?", (claim["claim_id"],))}
    assert npc_id(db, "Whiterun Guard") in holders                       # the guard saw it happen
    from livingworld.social import claims
    assert claims.sentence(db, dict(claim), npc_id(db, "Whiterun Guard")) == \
        "Torvar was taken on by Commander Caius as a Recruit in the Whiterun Guard in Whiterun"


# ---- debug commands, dismissal, saves, switches ----

def test_in_game_test_commands(tmp_path):
    talk(tmp_path, DAY, say="Debug guard enlist whiterun.")
    db = open_db(tmp_path)
    assert service(db)["path"] == "admin" and service(db)["rank_key"] == "recruit"
    talk(tmp_path, DAY + 0.1, say="debug guard rank optio")
    _, prompt = talk(tmp_path, DAY + 0.2, npcs=[whiterun_guard()])
    assert "as an Optio" in prompt and "not the Imperial Legion" in prompt and "Torvar outranks Whiterun Guard" in prompt
    db = open_db(tmp_path)
    assert db.execute("SELECT COUNT(*) FROM claim WHERE act = 'promoted'").fetchone()[0] == 1
    talk(tmp_path, DAY + 0.3, say="debug guard dismiss")
    _, prompt = talk(tmp_path, DAY + 0.4)
    assert "was dismissed from the Whiterun Guard 0 days ago" in prompt
    talk(tmp_path, DAY + 0.5, say="debug guard reinstate")
    assert service(open_db(tmp_path))["status"] == "active"


def test_switching_the_test_commands_off(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTELLA_LW_GUARD_DEBUG", "0")
    talk(tmp_path, DAY, say="debug guard enlist")
    assert open_db(tmp_path).execute("SELECT COUNT(*) FROM service").fetchone()[0] == 0


def load(timeline, nonce, day):
    return {KEY_TIMELINE_ID: timeline, KEY_LOAD_NONCE: nonce, KEY_LOADED_GAME_DAYS: day}


def test_loading_an_older_save_rolls_the_service_back(tmp_path):
    talk(tmp_path, DAY, extra=load(0, 201, DAY - 0.1))
    timeline = talk.reply[KEY_TIMELINE_ID]
    talk(tmp_path, DAY + 0.1, say="I want to join the guard.", extra=load(timeline, 201, DAY - 0.1))
    talk(tmp_path, DAY + 0.2, say="debug guard rank legionnaire", extra=load(timeline, 201, DAY - 0.1))
    assert service(open_db(tmp_path))["rank_key"] == "legionnaire"
    talk(tmp_path, DAY + 0.16, extra=load(timeline, 202, DAY + 0.15))     # saved as a recruit
    assert service(open_db(tmp_path))["rank_key"] == "recruit"
    _, prompt = talk(tmp_path, DAY + 0.06, extra=load(timeline, 203, DAY + 0.05))   # saved before joining
    assert service(open_db(tmp_path)) is None
    assert "willing to take them on" in prompt


def test_switching_guard_careers_off(tmp_path, monkeypatch):
    monkeypatch.setenv(GUARD_ENV, "0")
    _, prompt = talk(tmp_path, DAY, say="I want to join the guard.")
    assert "The guard, and" not in prompt
    assert open_db(tmp_path).execute("SELECT COUNT(*) FROM service").fetchone()[0] == 0


def test_children_never_serve(tmp_path):
    talk(tmp_path, DAY)
    store = WorldStore(db_path(tmp_path))
    with store.transaction() as db:
        p = npc_id(db, "Torvar")
        c = npc_id(db, "Commander Caius")
        db.execute("UPDATE npc SET is_child = 1 WHERE npc_id = ?", (c,))   # pretend the recruit were a child
        with pytest.raises(ValueError):
            S.enlist(db, "whiterun_guard", c, "applied", DAY, rank_key="recruit", recruiter_id=p)
        assert not R.decide(db, WHITERUN, p, c, DAY).ok
    store.close()


# ---- developer tool ----

def run_dev(tmp_path, *argv):
    lines = []
    code = dev.run(["--db", db_path(tmp_path), *argv], out=lines.append)
    return code, lines


def test_developer_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(dev, "server_running", lambda: False)
    talk(tmp_path, DAY)
    code, lines = run_dev(tmp_path, "explain-recruit", "whiterun", "--bounty", "40")
    assert code == 0 and lines[0].startswith("the Whiterun Guard: would not take them on")
    assert any("NO  bounty: has a bounty of 40 gold in Whiterun" in l for l in lines)
    code, lines = run_dev(tmp_path, "enlist", "whiterun")
    assert code == 0 and "Recruit" in lines[-1]
    assert run_dev(tmp_path, "promote")[1][-1] == "rank now Legionnaire"
    assert run_dev(tmp_path, "set-rank", "centurion")[1][-1] == "rank now Centurion"
    code, lines = run_dev(tmp_path, "status")
    assert "Centurion, active" in lines[0]
    assert run_dev(tmp_path, "dismiss")[1][-1] == "dismissed from the Whiterun Guard"
    code, lines = run_dev(tmp_path, "explain-recruit", "whiterun")
    assert any("NO  dismissed" in l for l in lines)
    assert run_dev(tmp_path, "reset", "whiterun")[0] == 1
    assert run_dev(tmp_path, "reset", "whiterun", "--yes")[1][-1] == "reset 1 service record(s)"
    code, lines = run_dev(tmp_path, "roster", "whiterun")
    assert lines == ["Commander Caius: Prefect"]


def test_developer_tool_refuses_while_the_server_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(dev, "server_running", lambda: True)
    talk(tmp_path, DAY)
    code, lines = run_dev(tmp_path, "enlist")
    assert code == 2 and "seems to be running" in lines[0]
