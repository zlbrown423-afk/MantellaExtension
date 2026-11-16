"""Background context preparation for group talk (livingworld/dialogue/context_prep.py).

What must hold: the prompt built from a prepared snapshot is the same as one built fresh; one NPC's rows
never reach another NPC; any write that could change recall makes the snapshot unusable; the background
read writes nothing; cancel and close stop the work; the off switch turns it all off."""
import sqlite3
import threading

import pytest

from livingworld.dialogue import context_prep as cp
from livingworld.harness import actors
from livingworld.harness.scripted_llm import ScriptedLLM
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, LivingWorld
from livingworld.store.world_store import WorldStore
from tests.livingworld.test_group import GERDUR_BIO, HOD_BIO, gerdur_meets_torvar, open_db


@pytest.fixture(autouse=True)
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.delenv(cp.ENV_VAR, raising=False)
    monkeypatch.setattr(LivingWorld, "start_workers", False)


# ---- which statements count as writes ---------------------------------------------------------

WATCHED = frozenset({"memory", "knowledge", "belief", "relationship", "utterance", "world_event"})


@pytest.mark.parametrize("sql, dirty", [
    ("SELECT * FROM memory", False),
    ("BEGIN IMMEDIATE", False),
    ("COMMIT", False),
    ("PRAGMA busy_timeout = 5000", False),
    ("INSERT INTO memory (owner_id) VALUES (1)", True),
    ("insert or ignore into knowledge values (1)", True),
    ("UPDATE memory SET rehearsal_count = 1", True),
    ("DELETE FROM belief WHERE 1", True),
    ("REPLACE INTO relationship VALUES (1)", True),
    ("INSERT INTO utterance (timeline_id, conv_id, seq) VALUES (3, 7, 12)", False),  # this conversation's lines
    ("INSERT INTO utterance (timeline_id, conv_id, seq) VALUES (3, 6, 12)", True),   # another conversation's
    ("INSERT OR IGNORE INTO participant (conv_id, npc_id, joined_seq) VALUES (7,2,3)", False),
    ("UPDATE participant SET left_seq = NULL WHERE conv_id = 7 AND npc_id = 2", False),
    ("UPDATE participant SET npc_id = 9 WHERE npc_id = 2", True),                     # no conversation named
    ("INSERT INTO task_change (task_id) VALUES (1)", False),   # recall never reads it
    ("INSERT INTO world_event (type) VALUES ('speech')", False),  # append-only, reached only through watched rows
    ("UPDATE world_event SET type = 'x'", True),
    ("INSERT INTO llm_request (conv_id) VALUES (1)", False),
    ("CREATE TABLE x (a)", True),
    ("WITH q AS (SELECT 1) INSERT INTO memory SELECT * FROM q", True),
    ("something unexpected", True),
])
def test_statement_classification(sql, dirty):
    assert cp.statement_dirties(sql, WATCHED | {"participant"}, current_conv=7) is dirty


def test_a_trigger_counts_only_if_its_body_writes_a_watched_table():
    writing = frozenset({"memory_fts_ai"})
    assert cp.statement_dirties("-- TRIGGER memory_fts_ai", WATCHED, writing)
    assert not cp.statement_dirties("-- TRIGGER utterance_no_update", WATCHED, writing)


def test_the_watched_set_covers_every_table_recall_reads(tmp_path, monkeypatch):
    """Run real recalls (memory boost on and off, romance on) with SQLite reporting every table read."""
    import os
    from livingworld.memory.recall import recall_for
    gerdur_meets_torvar(tmp_path)
    store = WorldStore(str(tmp_path / "data" / "livingworld" / "HeadlessTest.sqlite"))
    prep = cp.ContextPrep(store, lambda k: None)
    read = set()

    def auth(action, a1, *_):
        if action == sqlite3.SQLITE_READ and a1:
            read.add(a1.lower())
        return sqlite3.SQLITE_OK

    store._conn.set_authorizer(auth)
    monkeypatch.setenv("MANTELLA_LW_ROMANCE", "1")
    ids = [r[0] for r in store.query("SELECT npc_id FROM npc")]
    for boost in ("1", "0"):
        monkeypatch.setenv("MANTELLA_LW_MEMORY_BOOST", boost)
        for npc in ids:
            for conv in (None, 1):
                recall_for(store, npc, 20.0, ids[0], mark_rehearsed=False, about_ids=ids, current_conv_id=conv)
    store._conn.set_authorizer(None)
    read.discard("sqlite_master")
    assert read and read <= prep.watched, sorted(read - prep.watched)
    prep.close()
    store.close()


def test_the_watched_set_covers_the_sql_in_recall_and_its_helpers(tmp_path):
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / "livingworld"
    sql_names = set(re.findall(r"\b(?:FROM|JOIN)\s+(v_\w+|[a-z_]+)\b", (root / "memory" / "recall.py").read_text()))
    sql_names = {n for n in sql_names if "_" in n or n in {"npc", "memory", "belief", "claim", "utterance"}}
    store = WorldStore(str(tmp_path / "w.sqlite"))
    prep = cp.ContextPrep(store, lambda k: None)
    tables = {r[0].lower() for r in store.query("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
    missing = {n for n in sql_names if n in tables} - prep.watched
    assert not missing, sorted(missing)
    prep.close()
    store.close()


# ---- the snapshot store on its own ------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    s = WorldStore(str(tmp_path / "w.sqlite"))
    yield s
    s.close()


def key(npc, now=5.0, present=(1, 2, 3)):
    return cp.PrepKey(1, 1, 7, npc, 1, present, now)


class Tagged:
    def __init__(self, npc_id):
        self.npc_id = npc_id


def make(store, compute=None):
    return cp.ContextPrep(store, compute or (lambda k: Tagged(k.npc_id)))


def test_a_prepared_recall_is_handed_over_once_and_only_for_its_own_npc(store):
    prep = make(store)
    prep.begin([key(2), key(3)])
    assert prep.wait_idle()
    got = prep.take(key(2))
    assert got is not None and got.npc_id == 2
    assert prep.take(key(2)) is None                      # used once
    assert prep.take(key(3)).npc_id == 3
    assert prep.stats.hits == 2
    prep.close()


def test_a_different_key_is_refused(store):
    prep = make(store)
    prep.begin([key(2), key(3)])
    assert prep.wait_idle()
    assert prep.take(key(2, now=5.01)) is None             # game time moved
    assert prep.take(key(3, present=(1, 3))) is None       # someone left
    assert prep.stats.misses == {"key changed": 2}
    prep.close()


def test_a_write_that_may_change_recall_spoils_the_snapshot(store):
    prep = make(store)
    prep.begin([key(2), key(3)])
    assert prep.wait_idle()
    with store.transaction() as db:
        db.execute("UPDATE timeline SET is_active = is_active")
    assert prep.take(key(2)) is None and prep.take(key(3)) is None
    assert prep.stats.misses == {"store written": 2}
    prep.close()


def test_writes_that_cannot_change_recall_leave_it_usable(store):
    prep = make(store)
    prep.begin([key(2)])
    assert prep.wait_idle()
    store.query("SELECT 1")                                   # a real read through the watched connection
    prep._on_statement("INSERT INTO utterance (timeline_id, conv_id, seq) VALUES (1, 7, 4)")
    prep._on_statement("INSERT INTO llm_request (conv_id) VALUES (1)")
    prep._on_statement("-- TRIGGER utterance_speaker_is_participant")
    assert prep.take(key(2)).npc_id == 2
    prep.close()


def test_our_own_rehearsal_write_does_not_spoil_the_others(store):
    prep = make(store)
    prep.begin([key(2), key(3)])
    assert prep.wait_idle()
    prep.quiet_write(lambda: store.query("SELECT 1") and None)
    prep.quiet_write(lambda: prep._on_statement("UPDATE memory SET rehearsal_count = 1"))
    assert prep.take(key(3)).npc_id == 3
    prep.close()


def test_cancel_drops_snapshots_and_a_running_job_stores_nothing(store):
    started, release = threading.Event(), threading.Event()

    def slow(k):
        started.set()
        release.wait(5)
        return Tagged(k.npc_id)

    prep = make(store, slow)
    prep.begin([key(2), key(3)])
    assert started.wait(5)
    prep.cancel()
    release.set()
    assert prep.wait_idle()
    assert prep.take(key(2)) is None and prep.take(key(3)) is None
    assert prep.stats.prepared == 0
    prep.close()


def test_a_new_turn_replaces_the_old_one(store):
    prep = make(store)
    prep.begin([key(2, now=5.0)])
    assert prep.wait_idle()
    prep.begin([key(2, now=6.0)])
    assert prep.wait_idle()
    assert prep.take(key(2, now=5.0)) is None
    prep.begin([key(2, now=6.0)])
    assert prep.wait_idle()
    assert prep.take(key(2, now=6.0)).npc_id == 2
    prep.close()


def test_close_stops_the_thread_and_the_write_watch(store):
    prep = make(store)
    prep.begin([key(2)])
    assert prep.wait_idle()
    thread = prep._thread
    prep.close()
    thread.join(2)
    assert not thread.is_alive()
    prep.begin([key(3)])                                  # ignored once closed
    assert prep.take(key(3)) is None


def test_a_failing_read_is_skipped(store):
    def boom(k):
        if k.npc_id == 2:
            raise RuntimeError("disk")
        return Tagged(k.npc_id)

    prep = make(store, boom)
    prep.begin([key(2), key(3)])
    assert prep.wait_idle()
    assert prep.take(key(2)) is None and prep.take(key(3)).npc_id == 3
    prep.close()


def test_many_threads_never_mix_npcs(store):
    prep = make(store)
    errors, barrier = [], threading.Barrier(8)

    def worker(i):
        try:
            barrier.wait()
            for _ in range(300):
                if i % 2:
                    prep.begin([key(n) for n in (2, 3, 4)])
                else:
                    for n in (2, 3, 4):
                        got = prep.take(key(n))
                        assert got is None or got.npc_id == n
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    prep.close()


# ---- through the server -----------------------------------------------------------------------

def group_turn(work_dir, prepare: bool, plant=None, line="How is everyone today?"):
    """A group talk; before the player's line, optionally prepare in the background as the mic would."""
    llm = ScriptedLLM(replies=["Hod, this is Torvar.", "Well met, Torvar.", "Aye.", "Indeed."])
    with HeadlessSession(str(work_dir), llm) as s:
        s.start([actors.player("Torvar"), actors.gerdur(), actors.hod()], location="Riverwood", game_days=11.0)
        s.continue_until_player_turn()
        lw = LivingWorld.latest
        calls_before = len(llm.dialogue_calls)
        prep = None
        if prepare:
            lw.player_turn_started(lw.context)
            prep = lw._context_prep
            assert prep is not None and prep.wait_idle()
        if plant:
            plant(lw)
        s.say(line)
        stats = prep.stats if prep is not None else None
        s.end(game_days=11.1)
    return [c.system_prompt for c in llm.dialogue_calls[calls_before:]], stats


def test_prepared_prompts_are_identical_to_fresh_ones(tmp_path):
    gerdur_meets_torvar(tmp_path / "a")
    gerdur_meets_torvar(tmp_path / "b")
    fresh, _ = group_turn(tmp_path / "a", prepare=False)
    prepared, stats = group_turn(tmp_path / "b", prepare=True)
    assert prepared == fresh
    assert stats.hits >= 1 and stats.prepared == 2


def test_no_npc_gets_another_npcs_rows(tmp_path):
    gerdur_meets_torvar(tmp_path)
    prompts, stats = group_turn(tmp_path, prepare=True)
    assert stats.hits >= 1
    for p in prompts:
        if "You are Hod" in p:
            assert GERDUR_BIO not in p and "spiders" not in p and "Falkreath" not in p
        if "You are Gerdur" in p:
            assert HOD_BIO not in p


def test_a_memory_stored_while_the_player_speaks_is_in_the_prompt(tmp_path):
    gerdur_meets_torvar(tmp_path)

    def plant(lw):
        gerdur = next(i for c, i in lw._npc_ids.items() if i and lw.store.scalar(
            "SELECT display_name FROM npc WHERE npc_id = ?", (i,)) == "Gerdur")
        with lw.store.transaction() as db:
            db.execute("INSERT INTO memory (timeline_id, owner_id, kind, content, about_ids_json, occurred_gt,"
                       " importance, valence, intensity, created_by, source_kind)"
                       " VALUES (?, ?, 'episodic', 'A wolf took one of our goats last night.', '[]', 10.99,"
                       " 0.9, -0.5, 0.6, 'admin', 'experienced')", (lw.store.active_timeline_id(), gerdur))

    prompts, stats = group_turn(tmp_path, prepare=True, plant=plant)
    gerdur_prompts = [p for p in prompts if "You are Gerdur" in p]
    assert gerdur_prompts and all("wolf took one of our goats" in p for p in gerdur_prompts)
    assert stats.hits == 0 and stats.misses.get("store written", 0) >= 1


def test_the_background_read_writes_nothing(tmp_path):
    gerdur_meets_torvar(tmp_path)
    llm = ScriptedLLM(replies=["Hello.", "Aye."])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([actors.player("Torvar"), actors.gerdur(), actors.hod()], location="Riverwood", game_days=11.0)
        s.continue_until_player_turn()
        lw = LivingWorld.latest
        before = lw.store._conn.total_changes
        lw.player_turn_started(lw.context)
        assert lw._context_prep.wait_idle()
        assert lw.store._conn.total_changes == before
        s.end(game_days=11.1)
    assert lw._context_prep is None   # the end of the conversation closed it


def test_rehearsal_counts_match_the_fresh_path(tmp_path):
    def counts(work_dir, prepare):
        gerdur_meets_torvar(work_dir)
        group_turn(work_dir, prepare=prepare)
        db = open_db(work_dir)
        return [tuple(r) for r in db.execute("SELECT mem_id, rehearsal_count FROM memory ORDER BY mem_id")]

    assert counts(tmp_path / "a", False) == counts(tmp_path / "b", True)


def test_one_to_one_talk_prepares_nothing(tmp_path):
    llm = ScriptedLLM(replies=["Hello."])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([actors.player("Torvar"), actors.gerdur()], location="Riverwood", game_days=3.0)
        s.continue_until_player_turn()
        lw = LivingWorld.latest
        lw.player_turn_started(lw.context)
        assert lw._context_prep is None
        s.end(game_days=3.1)


def test_off_switch(tmp_path, monkeypatch):
    monkeypatch.setenv(cp.ENV_VAR, "0")
    gerdur_meets_torvar(tmp_path)
    _, stats = group_turn(tmp_path, prepare=False)
    llm = ScriptedLLM(replies=["Hello.", "Aye."])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([actors.player("Torvar"), actors.gerdur(), actors.hod()], location="Riverwood", game_days=12.0)
        s.continue_until_player_turn()
        lw = LivingWorld.latest
        lw.player_turn_started(lw.context)
        assert lw._context_prep is None
        s.end(game_days=12.1)
