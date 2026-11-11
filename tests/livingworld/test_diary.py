"""Diaries: NPCs the player talks with a lot keep a private diary written from their own memories, which the
game shows as a book the player can find, read, and be caught reading."""
import sqlite3

import pytest

from livingworld import diary
from livingworld.diary import service as diaries
from livingworld.harness import actors
from livingworld.harness.scripted_llm import ScriptedExtraction, ScriptedLLM
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, LivingWorld
from livingworld.memory import jobs

PLAYER = actors.player("Torvar")
GERDUR_REF = 0x00013489
HOD_REF = 0x0001348A


@pytest.fixture(autouse=True)
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.setattr(LivingWorld, "start_workers", False)
    monkeypatch.setattr(jobs, "BACKOFF_SECONDS", (0, 0, 0, 0, 0))


def open_db(work_dir) -> sqlite3.Connection:
    conn = sqlite3.connect(work_dir / "data" / "livingworld" / "HeadlessTest.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def llm_for_gerdur() -> ScriptedLLM:
    return ScriptedLLM(
        replies=["Back again?"] * 20,
        extractions={"Gerdur": ScriptedExtraction(memories=["Torvar helped me stack firewood by the mill."])},
        diary_entries={"Gerdur": "Torvar came by the mill again. Hod pretends not to notice how often. "
                                 "I don't trust easy smiles, but this one carried the wood without being asked."})


def talk(s: HeadlessSession, game_days: float, people=None) -> dict:
    reply = s.start([PLAYER, *(people or [actors.gerdur()])], game_days=game_days)
    s.continue_until_player_turn()
    s.say("Need a hand?")
    s.end(game_days=game_days + 0.01)
    LivingWorld.latest.wait_for_jobs()
    return reply


def read_request(owner: int, seen: bool = False, witnesses=(), names=()) -> dict:
    return {"mantella_request_type": diary.REQUEST_READ, diary.KEY_WORLD_ID: "HeadlessTest",
            diary.KEY_OWNER_NPC_ID: owner, diary.KEY_SEEN_BY_OWNER: seen,
            diary.KEY_WITNESS_REF_IDS: list(witnesses), diary.KEY_WITNESS_NAMES: list(names),
            diary.KEY_LOCATION: "Gerdur's House", diary.KEY_GAME_DAYS: 9.5}


# ---- dates and text ----

def test_dates():
    assert diaries.date_text(1) == "Sundas, 17th of Last Seed"     # the game sends GameDaysPassed + 1
    assert diaries.date_text(2) == "Morndas, 18th of Last Seed"
    assert diaries.date_text(15) == "Sundas, 31st of Last Seed"
    assert diaries.date_text(16) == "Morndas, 1st of Hearthfire"


def test_parse_entry():
    assert diaries.parse_entry('{"entry": "The mill wheel squeaked all day. Hod will not fix it."}') \
        == "The mill wheel squeaked all day. Hod will not fix it."
    assert diaries.parse_entry('```json\n{"entry": "A long day at the mill, and the river high."}\n```') \
        == "A long day at the mill, and the river high."
    assert diaries.parse_entry('{"entry": ""}') is None
    assert diaries.parse_entry(None) is None


def test_long_entries_are_split_into_pages(tmp_path):
    text = " ".join(["The river was high and the mill wheel groaned all day long."] * 30)
    pages = diaries._split(text, 300)
    assert len(pages) > 1 and all(len(p) <= 300 for p in pages)
    assert " ".join(pages).split() == text.split()


# ---- in a conversation ----

def test_a_diary_after_five_conversations_written_from_her_memories(tmp_path):
    llm = llm_for_gerdur()
    with HeadlessSession(str(tmp_path), llm) as s:
        for day in range(4):
            reply = talk(s, 2.0 + day)
            assert diary.KEY_DIARY_REF_IDS not in reply
        assert reply[diary.KEY_DIARY_ON] is True
        assert not [c for c in llm.background_calls if diaries.HEADER.split("{")[0] in c.system_prompt]
        reply = talk(s, 6.4)   # the fifth
        assert reply[diary.KEY_DIARY_REF_IDS] == [GERDUR_REF]
        assert reply[diary.KEY_DIARY_TITLES] == ["Gerdur's Diary"]
        owner = reply[diary.KEY_DIARY_NPC_IDS][0]
        written = [c for c in llm.background_calls if "private diary of Gerdur" in c.system_prompt]
        assert len(written) == 1
        notes = written[0].last_user_text
        assert "Torvar helped me stack firewood" in notes
        assert "Fredas, 22nd of Last Seed" in notes             # day 6
        assert "first entry" in notes                           # looks back on how Torvar came along
        # she knows she keeps one
        s.start([PLAYER, actors.gerdur()], game_days=7.0)
        s.continue_until_player_turn()
        assert "Gerdur keeps a private diary" in llm.dialogue_calls[-1].system_prompt
        s.end(game_days=7.01)
        LivingWorld.latest.wait_for_jobs()
        page = s.post(read_request(owner))
    assert page["mantella_reply_type"] == diary.REPLY_READ
    assert page[diary.KEY_TITLE] == "Gerdur's Diary"
    assert page[diary.KEY_PAGES][0].startswith("Loredas, 23rd of Last Seed")   # newest first
    assert any("carried the wood" in p for p in page[diary.KEY_PAGES])
    db = open_db(tmp_path)
    assert db.execute("SELECT COUNT(*) FROM diary_read").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM memory WHERE content LIKE '%diary%'").fetchone()[0] == 0   # unseen


def test_a_day_is_rewritten_only_with_news_and_at_most_three_times(tmp_path, monkeypatch):
    monkeypatch.setenv(diary.MIN_TALKS_ENV, "1")
    llm = llm_for_gerdur()
    with HeadlessSession(str(tmp_path), llm) as s:
        for i in range(5):
            talk(s, 3.1 + i * 0.1)
    db = open_db(tmp_path)
    versions = db.execute("SELECT versions FROM v_diary_entry").fetchone()[0]
    assert versions == 3


def test_caught_reading_it(tmp_path, monkeypatch):
    monkeypatch.setenv(diary.MIN_TALKS_ENV, "1")
    llm = llm_for_gerdur()
    with HeadlessSession(str(tmp_path), llm) as s:
        reply = talk(s, 2.0, [actors.gerdur(), actors.hod()])
        owner = reply.get(diary.KEY_DIARY_NPC_IDS, [None])[0] or open_db(tmp_path).execute(
            "SELECT npc_id FROM npc WHERE display_name = 'Gerdur'").fetchone()[0]
        page = s.post(read_request(owner, seen=True, witnesses=[HOD_REF], names=["Hod"]))
        assert page[diary.KEY_PAGES]
        s.start([PLAYER, actors.gerdur()], game_days=9.6)
        s.continue_until_player_turn()
        assert "I caught Torvar reading my diary" in llm.dialogue_calls[-1].system_prompt
        s.end(game_days=9.61)
    db = open_db(tmp_path)
    mems = {r["display_name"]: r["content"] for r in db.execute(
        "SELECT n.display_name, m.content FROM memory m JOIN npc n ON n.npc_id = m.owner_id WHERE m.content LIKE '%diary%'")}
    assert mems["Gerdur"].startswith("I caught Torvar reading my diary")
    assert mems["Hod"] == "I saw Torvar reading Gerdur's diary."
    trust = db.execute("SELECT r.trust FROM v_relationship r JOIN npc a ON a.npc_id = r.from_id JOIN npc b"
                       " ON b.npc_id = r.to_id WHERE a.display_name = 'Gerdur' AND b.display_name = 'Torvar'"
                       ).fetchone()[0]
    assert trust < 0


def test_children_never_keep_one(tmp_path, monkeypatch):
    monkeypatch.setenv(diary.MIN_TALKS_ENV, "1")
    dorthe = actors.actor("Dorthe", 0x0001347F, 0x0001348C, 1, "NordChild", "FemaleChild")
    with HeadlessSession(str(tmp_path), llm_for_gerdur()) as s:
        reply = talk(s, 2.0, [dorthe])
        assert diary.KEY_DIARY_REF_IDS not in reply


def test_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv(diary.MIN_TALKS_ENV, "1")
    monkeypatch.setenv(diary.ENV, "0")
    llm = llm_for_gerdur()
    with HeadlessSession(str(tmp_path), llm) as s:
        reply = talk(s, 2.0)
        assert reply[diary.KEY_DIARY_ON] is False and diary.KEY_DIARY_REF_IDS not in reply
        page = s.post(read_request(1))
        assert page[diary.KEY_PAGES] == ["The pages are blank."]
    assert not [c for c in llm.background_calls if "private diary" in c.system_prompt]
