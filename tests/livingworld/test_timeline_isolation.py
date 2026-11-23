"""Timeline isolation (remediation Stage 1, J0): what happens on a branch the player reloads away from never
reaches the loaded game, and nothing of it is deleted.

The 2026-11-23 audit found one memory (Gisli, memory 636) visible on the continuing timeline although its
conversation started after the loaded save: the conversation had been closed with the loaded save's earlier
clock, and extraction stamped the memory with that time. These tests pin the rule down: anything learnt from
a conversation carries a game time no earlier than the conversation's own lines.
"""
import sqlite3

import pytest

from livingworld.harness import actors
from livingworld.harness.scripted_llm import Fact, ScriptedExtraction, ScriptedLLM, when_player_says
from livingworld.harness.session import HeadlessSession
from livingworld.integration import (ENV_VAR, KEY_LOAD_NONCE, KEY_LOADED_GAME_DAYS, KEY_TIMELINE_ID,
                                     LivingWorld)
from livingworld.memory import jobs
from livingworld.memory.extraction import Extractor
from livingworld.store.conversation_log import ConversationLog
from livingworld.store.world_store import WorldStore


@pytest.fixture(autouse=True)
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.setenv("MANTELLA_LW_TASKS", "0")
    monkeypatch.setattr(LivingWorld, "start_workers", False)
    monkeypatch.setattr(jobs, "BACKOFF_SECONDS", (0, 0, 0, 0, 0))


def open_db(work_dir) -> sqlite3.Connection:
    conn = sqlite3.connect(work_dir / "data" / "livingworld" / "HeadlessTest.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def load(timeline, nonce, day):
    return {KEY_TIMELINE_ID: timeline, KEY_LOAD_NONCE: nonce, KEY_LOADED_GAME_DAYS: day}


CANON_MEMORY = "Torvar told me he grew up in Falkreath."
BRANCH_MEMORY = "Torvar confessed he burned down the Sleeping Giant's stable."


def talk(work_dir, *, say, memory, fact, day, end_day, extra):
    llm = ScriptedLLM(replies=["Go on."], rules=[when_player_says("falkreath", "Falkreath, is it?"),
                                                 when_player_says("stable", "You did WHAT?")],
                      extractions={"Gerdur": ScriptedExtraction(facts=[fact], memories=[memory])})
    with HeadlessSession(str(work_dir), llm) as s:
        reply = s.start([actors.player("Torvar"), actors.gerdur()], game_days=day, extra=extra)
        s.continue_until_player_turn()
        s.say(say, game_days=day + 0.001)
        s.end(game_days=end_day)
        LivingWorld.latest.wait_for_jobs()
    return reply, llm


def gerdur_prompt(work_dir, day, extra):
    llm = ScriptedLLM(replies=["Hm."])
    with HeadlessSession(str(work_dir), llm) as s:
        reply = s.start([actors.player("Torvar"), actors.gerdur()], game_days=day, extra=extra)
        s.continue_until_player_turn()
        s.end(game_days=day + 0.001)
        LivingWorld.latest.wait_for_jobs()
    return llm.dialogue_calls[0].system_prompt, reply


def counts(db):
    return {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("conversation", "utterance", "memory", "knowledge")}


# ---- the remediation prompt's 7-step scenario ----
def test_a_branch_reloaded_away_from_never_reaches_the_loaded_game(tmp_path):
    # 1. canonical interaction (day 3.5), 2. the player saves at day 3.6
    reply, _ = talk(tmp_path, say="I grew up in Falkreath.", memory=CANON_MEMORY,
                    fact=Fact("Falkreath", "Torvar", "hometown", "Falkreath", "Torvar"),
                    day=3.5, end_day=3.51, extra=load(0, 101, 3.4))
    canon = reply[KEY_TIMELINE_ID]
    # 3 + 4. a branch after the save, with a memory and a fact of its own
    talk(tmp_path, say="I burned down the stable.", memory=BRANCH_MEMORY,
         fact=Fact("stable", "Torvar", "crime", "burned the stable", "Torvar"),
         day=4.0, end_day=4.01, extra=load(canon, 102, 3.6))
    before = counts(open_db(tmp_path))
    # 5. reload the canonical save
    prompt, _ = gerdur_prompt(tmp_path, 3.7, load(canon, 103, 3.6))
    # 6. the branch is not in the prompt, the visible memories or the visible knowledge; the canon is
    assert "Falkreath" in prompt
    assert "stable" not in prompt.lower()
    db = open_db(tmp_path)
    visible = [r[0] for r in db.execute("SELECT content FROM v_visible_memory")]
    assert CANON_MEMORY in visible and BRANCH_MEMORY not in visible
    facts = [r[0] for r in db.execute("SELECT value FROM v_known_facts")]
    assert "burned the stable" not in facts
    # 7. the abandoned rows are all still on disk (the reload conversation only adds its own)
    after = counts(db)
    assert all(after[t] >= before[t] for t in before)
    assert db.execute("SELECT COUNT(*) FROM memory WHERE content = ?", (BRANCH_MEMORY,)).fetchone()[0] == 1


def test_a_branch_closed_with_the_loaded_saves_earlier_clock_stays_on_its_branch(tmp_path):
    """The memory 636 case: the end request carries a game time from before the save, so the conversation
    would end before it started and its memory would be stamped inside the loaded save's past."""
    reply, _ = talk(tmp_path, say="I grew up in Falkreath.", memory=CANON_MEMORY,
                    fact=Fact("Falkreath", "Torvar", "hometown", "Falkreath", "Torvar"),
                    day=3.5, end_day=3.51, extra=load(0, 101, 3.4))
    canon = reply[KEY_TIMELINE_ID]
    talk(tmp_path, say="I burned down the stable.", memory=BRANCH_MEMORY,
         fact=Fact("stable", "Torvar", "crime", "burned the stable", "Torvar"),
         day=4.0, end_day=3.55, extra=load(canon, 102, 3.6))   # closed with an earlier clock than its lines
    db = open_db(tmp_path)
    conv = db.execute("SELECT started_gt, ended_gt FROM conversation ORDER BY conv_id DESC LIMIT 1").fetchone()
    assert conv["ended_gt"] >= conv["started_gt"]
    (stamped,) = db.execute("SELECT occurred_gt FROM memory WHERE content = ?", (BRANCH_MEMORY,)).fetchone()
    assert stamped >= 4.0
    prompt, _ = gerdur_prompt(tmp_path, 3.7, load(canon, 103, 3.6))
    assert "stable" not in prompt.lower()
    visible = [r[0] for r in open_db(tmp_path).execute("SELECT content FROM v_visible_memory")]
    assert BRANCH_MEMORY not in visible and CANON_MEMORY in visible


# ---- the two rules on their own ----
def test_close_never_moves_the_end_before_the_conversation():
    store = WorldStore(":memory:")
    log = ConversationLog(store)
    conv = log.open("Riverwood", "player", 4.0)
    with store.transaction() as db:
        db.execute("INSERT INTO npc (plugin, base_form_id, display_name, sex, created_gt)"
                   " VALUES ('Skyrim.esm', '0x0001A694', 'Gerdur', 'female', 4.0)")
        npc = db.execute("SELECT npc_id FROM npc WHERE display_name = 'Gerdur'").fetchone()
    log.add_participant(conv, npc[0])
    log.append(conv, npc[0], "Hello.", None, 4.02)
    log.close(conv, 3.6)   # the loaded save's clock
    with store.transaction() as db:
        assert db.execute("SELECT ended_gt FROM conversation WHERE conv_id = ?", (conv,)).fetchone()[0] == 4.02
    conv2 = log.open("Riverwood", "player", 5.0)
    log.close(conv2, 5.1)   # a normal close keeps its own time
    with store.transaction() as db:
        assert db.execute("SELECT ended_gt FROM conversation WHERE conv_id = ?", (conv2,)).fetchone()[0] == 5.1


def test_extraction_stamps_no_earlier_than_the_last_line():
    store = WorldStore(":memory:")
    log = ConversationLog(store)
    conv = log.open("Riverwood", "player", 4.0)
    with store.transaction() as db:
        db.execute("INSERT INTO npc (plugin, base_form_id, display_name, sex, created_gt)"
                   " VALUES ('Skyrim.esm', '0x0001A694', 'Gerdur', 'female', 4.0)")
        npc = db.execute("SELECT npc_id FROM npc WHERE display_name = 'Gerdur'").fetchone()[0]
    log.add_participant(conv, npc)
    log.append(conv, npc, "Hello.", None, 4.03)
    with store.transaction() as db:   # a row closed by an older build, before close() kept the order
        db.execute("UPDATE conversation SET ended_gt = 3.6 WHERE conv_id = ?", (conv,))
    extractor = Extractor(store, lambda *a, **k: "{}")
    assert extractor.source_time(conv, 3.6) == 4.03
    assert extractor.source_time(conv, 4.5) == 4.5
