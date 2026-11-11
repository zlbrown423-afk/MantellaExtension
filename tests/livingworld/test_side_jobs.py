"""Side jobs: Salonia scouts Whiterun, proposes marks, the player picks one, she steals or flirts for coin.

The whole flow runs through the real request path (HeadlessSession); the word matching is tested on its own.
"""
import json
import sqlite3

import pytest

from livingworld.harness import actors
from livingworld.harness.perception import KEY_EVENTS, actor_field, react
from livingworld.harness.scripted_llm import ScriptedLLM
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, KEY_HINTS, LivingWorld
from livingworld.social import side_jobs
from src.http.communication_constants import communication_constants as comm_consts

PLAYER = actors.player("Torvar")
SALONIA = actors.actor("Salonia Carvain", 0x000B8149, 0xFF001A01, 1, "Imperial", "FemaleCommoner")
PLAUTIS = actors.actor("Plautis Carvain", 0x000B8148, 0xFF00196B, 0, "Imperial", "MaleCommoner")
BELETHOR = actors.actor("Belethor", 0x00013B9F, 0x00013BA0, 0, "Breton", "MaleCommoner")
YSOLDA = actors.actor("Ysolda", 0x00013BAB, 0x00013BB0, 1, "Nord", "FemaleNord")


@pytest.fixture(autouse=True)
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.setenv("MANTELLA_LW_TASKS", "0")
    monkeypatch.setattr(LivingWorld, "start_workers", False)


def open_db(work_dir) -> sqlite3.Connection:
    conn = sqlite3.connect(work_dir / "data" / "livingworld" / "HeadlessTest.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def npc_id(db, name):
    return db.execute("SELECT npc_id FROM npc WHERE display_name = ?", (name,)).fetchone()[0]


def surrendered(day):
    rec = react(SALONIA, PLAYER, day, via="surrender", victim_json=PLAUTIS, location="Haafingar")
    return rec + "|killer=player"


class Talk:
    def __init__(self, prompts, lines, keys):
        self.prompts, self.lines, self.keys = prompts, lines, keys

    @property
    def hints(self):
        return [h for k in self.keys for h in (k.get(KEY_HINTS) or [])]

    @property
    def orders(self):
        return [o for k in self.keys for o in (k.get(side_jobs.KEY_JOB_DO) or [])]

    @property
    def actions(self):
        return [a for line in self.lines for a in line.actions]

    def prompt(self):
        return self.prompts[0]


def talk(work_dir, npcs, day, said=(), events=None, location="Whiterun"):
    llm = ScriptedLLM(replies=["Hm."])
    lines = []
    with HeadlessSession(str(work_dir), llm, settings={"disabled_actions": ""}) as s:
        s.start([PLAYER, *npcs], location=location, game_days=day, context_extra={KEY_EVENTS: events or []})
        lines += s.continue_until_player_turn()
        for text in said:
            lines += s.say(text)
        s.end(game_days=day + 0.001)
        keys = [r.get(comm_consts.KEY_REPLYTYPE_NPCTALK) or {} for _, r in s.exchanges]
        starts = [r for _, r in s.exchanges if side_jobs.KEY_JOBS_ON in r]
    t = Talk([c.system_prompt for c in llm.dialogue_calls], lines, keys)
    t.notes = " ".join(c.all_text for c in llm.dialogue_calls)
    t.start = starts[0] if starts else {}
    return t


def with_salonia(tmp_path):
    talk(tmp_path, [SALONIA], 3.5, events=[surrendered(3.4995)], location="Haafingar")


# ---- words ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("said, kind", [
    ("I want you to go steal some jewelry in Whiterun.", "theft"),
    ("Could you lift a few things worth 30 gold or more?", "theft"),
    ("Go flirt with the guards for coin.", "flirt"),
    ("Can you sweet talk some men out of their gold?", "flirt"),
    ("Scope out some marks and see what we can pinch.", "theft"),
])
def test_asking_for_a_job(said, kind):
    assert side_jobs.player_asks(said) == kind


@pytest.mark.parametrize("said", [
    "Did you steal that ring?", "Don't steal anything while we're here.", "Nice weather.",
    "The guards are flirting with Ysolda again.", "Who stole my sweetroll?",
])
def test_not_asking(said):
    assert side_jobs.player_asks(said) is None


def test_picking_a_mark():
    marks = side_jobs.marks_catalogue()["theft"][:3]
    assert side_jobs.player_picks("Let's do the first one.", marks) == 0
    assert side_jobs.player_picks("Go with Arcadia.", marks) == 1
    assert side_jobs.player_picks("Which one is safest?", marks) is None
    assert side_jobs.player_picks("No, forget it.", marks) == "drop"
    assert side_jobs.player_picks("I have no idea, tell me more.", marks) is None


# ---- the whole job ----------------------------------------------------------------------------------

def test_theft_scout_propose_pick_cover_and_report(tmp_path):
    with_salonia(tmp_path)
    asked = talk(tmp_path, [SALONIA], 4.0, said=["Salonia, I want you to go steal some jewelry for me."])
    assert asked.start[side_jobs.KEY_JOBS_ON] is True
    assert "Salonia will scout Whiterun for marks. Talk to Salonia alone in a few hours." in asked.hints
    assert "first Salonia Carvain will scout" in asked.notes
    assert {"identifier": side_jobs.TRAVEL_TO, "arguments": {"source": ["Salonia Carvain"],
                                                              "location": "Whiterun Marketplace"}} in asked.actions
    db = open_db(tmp_path)
    job = side_jobs.current(db, npc_id(db, "Salonia Carvain"), 4.01)
    assert job.kind == "theft" and len(job.marks) == 3 and job.stage == "scouting"

    early = talk(tmp_path, [SALONIA], 4.1)
    assert "still making connections" in early.prompt() and not any("marks:" in h for h in early.hints)

    mark = job.marks[0]
    picked = talk(tmp_path, [SALONIA], 4.3, said=["The first one. I'll keep them talking."])
    assert "has finished scouting Whiterun" in picked.prompt() and f"1) {mark['owner']} at {mark['place']}" in picked.prompt()
    assert "Salonia's marks: say one, two or three, or no:" in picked.hints
    assert any(h.startswith(f"1: {mark['owner']}") for h in picked.hints)
    assert any(h.startswith(f"Salonia goes after {mark['owner']}") for h in picked.hints)
    assert {"identifier": side_jobs.TRAVEL_TO, "arguments": {"source": ["Salonia Carvain"],
                                                              "location": mark["place"]}} in picked.actions
    job = side_jobs.current(open_db(tmp_path), job.npc_id, 4.31)
    assert job.stage == "approved" and job.cover

    owner = actors.actor(mark["owner"], 0x00013B9F, 0x00013BA0, 0, "Breton", "MaleCommoner")
    covered = talk(tmp_path, [owner], 4.35)
    assert len(covered.orders) == 1
    order = covered.orders[0].split("|")
    assert order[0] == str(job.job_id) and order[1] == "theft" and order[3] == "Salonia Carvain"
    assert order[5] == mark["owner"] and order[7] == "30"
    db = open_db(tmp_path)
    done = side_jobs.current(db, job.npc_id, 4.36)
    assert done.stage == "done" and done.outcome["covered"] is True

    if done.outcome["result"] == "ok":
        loot = (f"v=1|key=jobloot1|type=job|gt=4.4|loc=Whiterun|actor={actor_field(SALONIA)}|via=loot"
                f"|task={job.job_id}|loot=Gold Ring:75;Silver Necklace:60|gold=0")
        report = talk(tmp_path, [SALONIA], 4.6, events=[loot])
        assert "Gold Ring (75 gold)" in report.prompt() and "use Inventory" in report.prompt()
    else:
        report = talk(tmp_path, [SALONIA], 4.6)
        assert "caught" in report.prompt()
    assert side_jobs.current(open_db(tmp_path), job.npc_id, 4.7) is None   # told: the job is over

    again = talk(tmp_path, [SALONIA], 5.0)
    assert "news for Torvar" not in again.prompt()


def test_flirting_job_on_her_own(tmp_path):
    with_salonia(tmp_path)
    talk(tmp_path, [SALONIA], 4.0, said=["Go flirt with the men and guards for coin."])
    talk(tmp_path, [SALONIA], 4.3, said=["Number two."])
    db = open_db(tmp_path)
    job = side_jobs.current(db, npc_id(db, "Salonia Carvain"), 4.31)
    assert job.kind == "flirt" and job.stage == "approved"
    report = talk(tmp_path, [SALONIA], 4.7)
    assert len(report.orders) == 1 and "|flirt|" in report.orders[0]
    assert "news for Torvar" in report.prompt()
    mem = open_db(tmp_path).execute("SELECT content FROM memory WHERE owner_id = ? ORDER BY mem_id DESC",
                                    (job.npc_id,)).fetchone()[0]
    assert "flirt" in mem.lower()


def test_she_keeps_quiet_in_public(tmp_path):
    with_salonia(tmp_path)
    talk(tmp_path, [SALONIA], 4.0, said=["I want you to go steal some jewelry."])
    talk(tmp_path, [YSOLDA], 4.05)
    public = talk(tmp_path, [SALONIA, YSOLDA], 4.3)
    salonia_prompt = next(p for p in public.prompts if "secret side job" in p)
    assert "never mentions the job in front of them" in salonia_prompt
    assert "has finished scouting" not in salonia_prompt and not any("marks:" in h for h in public.hints)


def test_calling_it_off(tmp_path):
    with_salonia(tmp_path)
    talk(tmp_path, [SALONIA], 4.0, said=["Go steal something worth 30 gold."])
    off = talk(tmp_path, [SALONIA], 4.3, said=["No, forget it."])
    assert "Salonia's plan is called off." in off.hints
    db = open_db(tmp_path)
    assert side_jobs.current(db, npc_id(db, "Salonia Carvain"), 4.4) is None


def test_only_in_whiterun(tmp_path):
    with_salonia(tmp_path)
    away = talk(tmp_path, [SALONIA], 4.0, said=["Go steal some jewelry for me."], location="Riverwood")
    assert "until they are back in Whiterun" in away.notes
    db = open_db(tmp_path)
    assert side_jobs.current(db, npc_id(db, "Salonia Carvain"), 4.1) is None


def test_the_dialogue_choice_asks_and_picks(tmp_path):
    with_salonia(tmp_path)
    base = f"type=job|loc=Whiterun|actor={actor_field(SALONIA)}|target={actor_field(PLAYER)}"
    talk(tmp_path, [SALONIA], 4.0, events=[f"v=1|key=ask1|{base}|gt=3.99|via=ask|task=flirt"])
    db = open_db(tmp_path)
    job = side_jobs.current(db, npc_id(db, "Salonia Carvain"), 4.0)
    assert job.kind == "flirt"
    talk(tmp_path, [YSOLDA], 4.3, events=[f"v=1|key=pick1|{base}|gt=4.29|via=approve|task=3"])
    job = side_jobs.current(open_db(tmp_path), job.npc_id, 4.31)
    assert job.stage == "approved" and job.pick == 2


def test_nobody_else_takes_jobs(tmp_path):
    talk(tmp_path, [YSOLDA], 4.0, said=["I want you to go steal some jewelry."])
    db = open_db(tmp_path)
    assert side_jobs.current(db, npc_id(db, "Ysolda"), 4.1) is None


def test_switch_off(tmp_path, monkeypatch):
    monkeypatch.setenv(side_jobs.ENV, "0")
    with_salonia(tmp_path)
    t = talk(tmp_path, [SALONIA], 4.0, said=["I want you to go steal some jewelry."])
    assert t.start[side_jobs.KEY_JOBS_ON] is False and not any("scout" in h for h in t.hints)
    assert open_db(tmp_path).execute("SELECT COUNT(*) FROM world_event WHERE type = 'side_job'").fetchone()[0] == 0


def test_the_same_roll_every_time(tmp_path):
    with_salonia(tmp_path)
    talk(tmp_path, [SALONIA], 4.0, said=["Go steal some jewelry."])
    talk(tmp_path, [SALONIA], 4.3, said=["The second one."])
    db = open_db(tmp_path)
    job = side_jobs.current(db, npc_id(db, "Salonia Carvain"), 4.31)
    import random
    first = random.Random(f"side-job:{job.job_id}").random()
    assert first == random.Random(f"side-job:{job.job_id}").random()


class _Unlucky:
    """random.Random stand-in whose rolls always fail (0.99) and whose picks are the first ones."""
    def __init__(self, *a, **k):
        pass

    def random(self):
        return 0.99

    def randint(self, a, b):
        return a

    def sample(self, pool, n):
        return list(pool)[:n]


def test_caught_while_the_player_covers(tmp_path):
    with_salonia(tmp_path)
    talk(tmp_path, [SALONIA], 4.0, said=["Go steal some jewelry."])
    db = open_db(tmp_path)
    job = side_jobs.current(db, npc_id(db, "Salonia Carvain"), 4.01)
    mark = job.marks[0]
    talk(tmp_path, [SALONIA], 4.3, said=[f"Let's go with {mark['owner'].split(' ')[0]}."])
    owner = actors.actor(mark["owner"], 0x00013B9F, 0x00013BA0, 0, "Breton", "MaleCommoner")
    real = side_jobs.random.Random
    side_jobs.random.Random = _Unlucky          # only for the roll as the job happens
    try:
        covered = talk(tmp_path, [owner], 4.35)
    finally:
        side_jobs.random.Random = real
    order = covered.orders[0].split("|")
    assert order[2] == "caught" and order[-1] == "40" and order[-3] == "0"
    db = open_db(tmp_path)
    owner_id = npc_id(db, mark["owner"])
    mems = [r[0] for r in db.execute("SELECT content FROM memory WHERE owner_id = ?", (owner_id,))]
    assert any("caught Salonia Carvain" in m for m in mems) and any("kept me talking" in m for m in mems)
    trust = db.execute("SELECT trust FROM v_relationship WHERE from_id = ? AND to_id = ?",
                       (owner_id, npc_id(db, "Torvar"))).fetchone()[0]
    assert trust < 0
    report = talk(tmp_path, [SALONIA], 4.6)
    assert "caught Salonia Carvain in the act" in report.prompt() and "may suspect both of them" in report.prompt()
    talk(tmp_path, [SALONIA], 5.0, said=["Go steal some jewelry."])
    db = open_db(tmp_path)
    nxt = side_jobs.current(db, npc_id(db, "Salonia Carvain"), 5.01)
    assert mark["key"] not in [m["key"] for m in nxt.marks]       # she will not go back there
