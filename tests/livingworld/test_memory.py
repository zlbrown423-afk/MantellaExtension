"""Milestone 1, PR B: per-NPC memory extraction in the background, and recall into prompts.

Follows the test plan's phases A to C (and D2 for a group conversation) with the scripted LLM
standing in for the extraction model.
"""
import sqlite3

import pytest

from livingworld.harness import actors
from livingworld.harness.scripted_llm import Fact, ScriptedExtraction, ScriptedLLM, when_player_says
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, LivingWorld
from livingworld.memory import jobs
from livingworld.memory.extraction import Extractor, parse_reply
from livingworld.memory.recall import time_ago


@pytest.fixture(autouse=True)
def living_world_inline(monkeypatch):
    """Store on, background thread off: each test runs jobs itself, so results are deterministic."""
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.setattr(LivingWorld, "start_workers", False)
    monkeypatch.setattr(jobs, "BACKOFF_SECONDS", (0, 0, 0, 0, 0))


def open_db(work_dir) -> sqlite3.Connection:
    conn = sqlite3.connect(work_dir / "data" / "livingworld" / "HeadlessTest.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def ids(db):
    rows = {r["display_name"]: r["npc_id"] for r in db.execute("SELECT npc_id, display_name FROM npc")}
    return rows.get("Gerdur"), rows.get("Hod"), rows.get("Torvar")


GERDUR_LEARNS = ScriptedExtraction(
    facts=[
        Fact("I'm 31", "Gerdur", "age", "31", "Gerdur"),
        Fact("Green", "Gerdur", "favorite color", "green", "Gerdur"),
        Fact("I'm Torvar", "Torvar", "name", "Torvar", "Torvar"),
        Fact("Falkreath", "Torvar", "hometown", "Falkreath", "Torvar"),
        Fact("spiders terrify me", "Torvar", "fear", "spiders", "Torvar"),
    ],
    # a fact nobody said (the sister question): must be dropped
    raw_facts=[{"about": "Torvar", "predicate": "sister_name", "value": "Ingrid", "said_by": "Torvar", "line": 1}],
    memories=["I met Torvar from Falkreath at the mill. He is afraid of spiders."],
)


def gerdur_llm(**kwargs) -> ScriptedLLM:
    return ScriptedLLM(
        replies=["Welcome to Riverwood, traveler."],
        rules=[when_player_says("how old", "I'm 31, not that it's any of your business."),
               when_player_says("favorite color", "Green. Like the pines around the mill."),
               when_player_says("sister", "I don't know your sister, friend."),
               when_player_says("remember", "Of course I remember you.")],
        extractions={"Gerdur": GERDUR_LEARNS},
        **kwargs,
    )


def phase_a(work_dir, llm=None):
    llm = llm or gerdur_llm()
    with HeadlessSession(str(work_dir), llm) as s:
        s.start([actors.player("Torvar"), actors.gerdur()], location="Riverwood", game_days=3.5)
        s.continue_until_player_turn()
        s.say("How old are you, if you don't mind me asking?")
        s.say("What's your favorite color?")
        s.say("I'm Torvar, I grew up in Falkreath.")
        s.say("Honestly, spiders terrify me.")
        s.say("What's my sister's name?")
        s.end(game_days=3.6)
    return s, llm


# ---- Phase A: extraction ----------------------------------------------------------------------

def test_ending_only_queues_the_extraction(tmp_path):
    s, llm = phase_a(tmp_path)
    db = open_db(tmp_path)
    assert [tuple(r) for r in db.execute("SELECT kind, status, attempts FROM job")] == \
        [("extract_conversation", "queued", 0)]
    assert not llm.extraction_calls   # nothing ran while the game was waiting


def test_gerdur_learns_her_own_facts_and_the_players(tmp_path):
    phase_a(tmp_path)
    assert LivingWorld.latest.wait_for_jobs() == 1
    db = open_db(tmp_path)
    gerdur, _, torvar = ids(db)

    # A3: Gerdur's own facts
    own = {(r["predicate"], r["value"], r["origin"]) for r in db.execute(
        "SELECT predicate, value, origin FROM knowledge WHERE holder_id = ? AND subject_id = ?", (gerdur, gerdur))}
    assert own == {("age", "31", "self"), ("favorite_color", "green", "self")}

    # A4: what she learned about the player, attributed to the player
    about_player = {(r["predicate"], r["value"], r["origin"], r["told_by_id"]) for r in db.execute(
        "SELECT predicate, value, origin, told_by_id FROM knowledge WHERE holder_id = ? AND subject_id = ?",
        (gerdur, torvar))}
    assert about_player == {("name", "Torvar", "told", torvar), ("hometown", "Falkreath", "told", torvar),
                            ("fear", "spiders", "told", torvar)}

    # A5: no invented facts
    assert db.execute("SELECT COUNT(*) FROM knowledge WHERE predicate LIKE '%sister%'").fetchone()[0] == 0

    # A6: the job finished
    assert [tuple(r) for r in db.execute("SELECT kind, status, attempts FROM job")] == \
        [("extract_conversation", "done", 1)]

    # memories are Gerdur's, first person, linked to the conversation
    mem = db.execute("SELECT owner_id, content, created_by FROM memory").fetchall()
    assert [(r["owner_id"], r["created_by"]) for r in mem] == [(gerdur, "extraction")]
    assert db.execute("SELECT COUNT(*) FROM memory_source").fetchone()[0] == 1


def test_the_player_gets_no_extraction(tmp_path):
    _, llm = phase_a(tmp_path)
    LivingWorld.latest.wait_for_jobs()
    assert len(llm.extraction_calls) == 1
    assert "Gerdur" in llm.extraction_calls[0].system_prompt


def test_extraction_survives_rate_limits(tmp_path):
    _, llm = phase_a(tmp_path)
    world = LivingWorld.latest
    real = world.complete
    failures = iter([RuntimeError("HTTP 429")] * 3)

    def flaky(system, user):
        error = next(failures, None)
        if error:
            raise error
        return real(system, user)
    world.complete = flaky

    for _ in range(4):
        world.wait_for_jobs()
    db = open_db(tmp_path)
    assert [tuple(r) for r in db.execute("SELECT status, attempts FROM job")] == [("done", 4)]
    assert db.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0] == 5


def test_a_job_that_keeps_failing_ends_up_dead(tmp_path):
    phase_a(tmp_path)
    world = LivingWorld.latest
    world.complete = lambda system, user: "not json at all"
    for _ in range(jobs.MAX_ATTEMPTS + 2):
        world.wait_for_jobs()
    db = open_db(tmp_path)
    job = db.execute("SELECT status, attempts, last_error FROM job").fetchone()
    assert (job["status"], job["attempts"]) == ("dead", jobs.MAX_ATTEMPTS)
    assert "JSON" in job["last_error"]


def test_running_extraction_twice_adds_nothing(tmp_path):
    phase_a(tmp_path)
    world = LivingWorld.latest
    world.wait_for_jobs()
    db = open_db(tmp_path)
    before = [db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("knowledge", "memory")]
    Extractor(world.store, world.complete).extract_conversation(1)
    after = [db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("knowledge", "memory")]
    assert before == after
    assert not jobs.JobQueue(world.store).enqueue("extract_conversation", {"conv_id": 1}, "extract:conv:1")


def test_a_job_left_by_a_crash_runs_after_restart(tmp_path):
    phase_a(tmp_path)
    world = LivingWorld.latest
    with world.store.transaction() as db:   # simulate a crash mid-job
        db.execute("UPDATE job SET status = 'running'")
    # Mantella starts again and the next conversation opens the same world
    with HeadlessSession(str(tmp_path), gerdur_llm()) as s:
        s.start([actors.player("Torvar"), actors.hod()], location="Riverwood", game_days=4.0)
        LivingWorld.latest.wait_for_jobs()
    db = open_db(tmp_path)
    assert db.execute("SELECT status FROM job WHERE idempotency_key = 'extract:conv:1'").fetchone()[0] == "done"
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_a_corrected_fact_supersedes_the_old_one(tmp_path):
    phase_a(tmp_path)
    world = LivingWorld.latest
    world.wait_for_jobs()
    gerdur, _, torvar = ids(open_db(tmp_path))
    extractor = Extractor(world.store, world.complete)
    heard = extractor.lines(1)
    line = next(l for l in heard if "Falkreath" in l.text)
    extractor.apply(1, gerdur, {"facts": [{"about": "Torvar", "predicate": "hometown", "value": "Windhelm",
                                           "said_by": "Torvar", "line": line.seq}]},
                    heard, {gerdur: "Gerdur", torvar: "Torvar"}, "Riverwood", 3.7)
    db = open_db(tmp_path)
    rows = db.execute("SELECT value, superseded_by FROM knowledge WHERE holder_id = ? AND predicate = 'hometown'"
                      " ORDER BY know_id", (gerdur,)).fetchall()
    assert [r["value"] for r in rows] == ["Falkreath", "Windhelm"]
    assert rows[0]["superseded_by"] is not None and rows[1]["superseded_by"] is None


def test_reply_parsing_tolerates_code_fences():
    assert parse_reply('```json\n{"facts": [], "memories": []}\n```') == {"facts": [], "memories": [], "reports": []}
    with pytest.raises(ValueError):
        parse_reply("Sorry, I can't help with that.")


# ---- Phase C: recall a week later -------------------------------------------------------------

def test_gerdur_recalls_a_week_later(tmp_path):
    phase_a(tmp_path)
    LivingWorld.latest.wait_for_jobs()

    llm = gerdur_llm()
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([actors.player("Torvar"), actors.gerdur()], location="Riverwood", game_days=10.6)
        s.continue_until_player_turn()
        s.say("Do you remember me?")
        s.end(game_days=10.7)

    prompt = llm.dialogue_calls[0].system_prompt
    assert "My age is 31" in prompt
    assert "Torvar told me that Torvar's hometown is Falkreath" in prompt
    assert "Torvar told me that Torvar's fear is spiders" in prompt
    assert "I last spoke with Torvar about 7 days ago." in prompt
    assert "(about 7 days ago) I met Torvar from Falkreath" in prompt
    assert "sister" not in prompt.split("What Gerdur knows")[1]

    # C3: recalled memories were rehearsed once for this conversation, not once per turn
    db = open_db(tmp_path)
    assert [tuple(r) for r in db.execute("SELECT rehearsal_count, last_recalled_gt FROM memory")] == [(1, 10.6)]


def test_hod_recalls_nothing_of_gerdurs(tmp_path):
    phase_a(tmp_path)
    LivingWorld.latest.wait_for_jobs()

    llm = ScriptedLLM(replies=["Hello there."])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([actors.player("Torvar"), actors.hod()], location="Riverwood", game_days=10.6)
        s.continue_until_player_turn()
        s.say("Am I afraid of anything?")
        s.end(game_days=10.7)

    for call in llm.dialogue_calls:
        assert "Falkreath" not in call.system_prompt
        assert "spiders" not in call.system_prompt
        assert "is 31" not in call.system_prompt


# ---- Group talk: Gerdur passes facts to Hod, with attribution (test plan D2) ----------------

def test_hod_learns_from_gerdur_with_attribution(tmp_path):
    phase_a(tmp_path)
    LivingWorld.latest.wait_for_jobs()

    hod_learns = ScriptedExtraction(facts=[
        Fact("I'm 31 years old", "Gerdur", "age", "31", "Gerdur"),
        Fact("he grew up in Falkreath", "Torvar", "hometown", "Falkreath", "Gerdur"),
    ])
    llm = ScriptedLLM(
        replies=["Gerdur: Hod, this is Torvar."],
        rules=[when_player_says("tell hod", "Gerdur: Hod, I'm 31 years old and I love green. "
                                            "Gerdur: And he grew up in Falkreath.")],
        extractions={"Gerdur": ScriptedExtraction(), "Hod": hod_learns},
    )
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([actors.player("Torvar"), actors.gerdur(), actors.hod()], location="Riverwood", game_days=11.0)
        s.continue_until_player_turn()
        s.say("Gerdur, tell Hod about yourself and where I'm from.")
        s.end(game_days=11.1)
    LivingWorld.latest.wait_for_jobs()

    db = open_db(tmp_path)
    gerdur, hod, torvar = ids(db)
    rows = db.execute(
        "SELECT k.subject_id, k.predicate, k.value, k.origin, k.told_by_id, p.holder_id parent_holder, p.origin parent_origin"
        " FROM knowledge k LEFT JOIN knowledge p ON p.know_id = k.parent_know_id WHERE k.holder_id = ?"
        " ORDER BY k.know_id", (hod,)).fetchall()
    assert [tuple(r) for r in rows] == [
        (gerdur, "age", "31", "told", gerdur, None, None),                 # Gerdur about herself: first-hand
        (torvar, "hometown", "Falkreath", "told", gerdur, gerdur, "told"),  # second-hand: chained to Gerdur's copy
    ]
    # E1/E2: nobody holds another's facts as their own
    assert db.execute("SELECT COUNT(*) FROM knowledge WHERE origin = 'self' AND holder_id <> subject_id").fetchone()[0] == 0
    # E3: the fear Gerdur never shared stays hers
    assert db.execute("SELECT COUNT(*) FROM knowledge WHERE holder_id = ? AND predicate = 'fear'", (hod,)).fetchone()[0] == 0
    # Gerdur's rows did not change when she shared
    assert db.execute("SELECT COUNT(*) FROM knowledge WHERE holder_id = ?", (gerdur,)).fetchone()[0] == 5

    with HeadlessSession(str(tmp_path), ScriptedLLM(replies=["Aye."])) as s:
        s.start([actors.player("Torvar"), actors.hod()], location="Riverwood", game_days=12.0)
        s.continue_until_player_turn()
        prompt = s.llm.dialogue_calls[0].system_prompt
    assert "Gerdur told me that Gerdur's age is 31" in prompt
    assert "Gerdur told me that Torvar's hometown is Falkreath" in prompt
    assert "spiders" not in prompt


def test_time_ago_wording():
    assert time_ago(0.01) == "just now"
    assert time_ago(0.25) == "about 6 hours ago"
    assert time_ago(1.0) == "yesterday"
    assert time_ago(7.0) == "about 7 days ago"
    assert time_ago(21) == "about 3 weeks ago"


def test_the_background_worker_runs_jobs_on_its_own(tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(LivingWorld, "start_workers", True)
    llm = gerdur_llm()
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([actors.player("Torvar"), actors.gerdur()], location="Riverwood", game_days=3.5)
        s.continue_until_player_turn()
        s.say("How old are you?")
        s.end(game_days=3.6)
        deadline = time.time() + 10
        while time.time() < deadline:
            if open_db(tmp_path).execute("SELECT status FROM job").fetchone()[0] == "done":
                break
            time.sleep(0.05)
    assert open_db(tmp_path).execute("SELECT status FROM job").fetchone()[0] == "done"
    assert len(llm.extraction_calls) == 1
