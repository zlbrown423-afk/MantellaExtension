"""Milestone 1, PR A: the SQLite store, stable NPC identity and the raw conversation log.

Query names follow the test plan's checkpoints (A1, A2, A7, C1) with the schema's column names.
"""
import sqlite3

import pytest

from livingworld.harness import actors
from livingworld.harness.scripted_llm import ScriptedLLM, when_player_says
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR
from livingworld.store.conversation_log import ConversationLog
from livingworld.store.identity import ActorIdentity, IdentityRegistry, VoiceBindingConflict, parse_form_id
from livingworld.store.world_store import WorldStore


@pytest.fixture
def living_world_on(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")


def db_path(work_dir):
    return work_dir / "data" / "livingworld" / "HeadlessTest.sqlite"


def open_db(work_dir) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(work_dir))
    conn.row_factory = sqlite3.Row
    return conn


def gerdur_llm() -> ScriptedLLM:
    return ScriptedLLM(
        replies=["Welcome to Riverwood, traveler."],
        rules=[when_player_says("how old", "I'm 31, not that it's any of your business."),
               when_player_says("from", "Falkreath? Long way from home.")],
    )


def talk_to_gerdur(work_dir, start_day=3.5, end_day=3.6):
    with HeadlessSession(str(work_dir), gerdur_llm()) as s:
        s.start([actors.player(), actors.gerdur()], location="Riverwood", game_days=start_day)
        s.continue_until_player_turn()
        s.say("How old are you, Gerdur?")
        s.say("I'm Torvar, I grew up in Falkreath.")
        s.end(game_days=end_day)
    return s


# ---- schema and store ----------------------------------------------------------------------

def test_store_creates_schema_once(tmp_path):
    path = str(tmp_path / "w.sqlite")
    store = WorldStore(path)
    assert store.schema_version() == 4
    assert store.scalar("PRAGMA journal_mode") == "wal"
    assert store.active_timeline_id() == 1
    store.close()
    again = WorldStore(path)  # reopening does not re-run the migration
    assert again.scalar("SELECT COUNT(*) FROM schema_version") == 4
    assert again.scalar("PRAGMA integrity_check") == "ok"
    again.close()


@pytest.mark.parametrize("raw, plugin, local_id, mantella_id", [
    (0x0001347C, "Skyrim.esm", "0x0001347C", "01347C"),
    (0x0501ABCD, "load_index:05", "0x0001ABCD", "01ABCD"),
    (0xFE012ABC, "light:012", "0x00000ABC", "000ABC"),
    (0xFE012ABC - 2**32, "light:012", "0x00000ABC", "000ABC"),   # Papyrus sends FE.. IDs as negative ints
])
def test_form_ids_keep_plugin_and_full_id(raw, plugin, local_id, mantella_id):
    form = parse_form_id(raw)
    assert (form.plugin, form.local_id, form.mantella_id) == (plugin, local_id, mantella_id)


def test_plugin_name_from_the_game_wins_over_the_load_index():
    assert parse_form_id(0x0501ABCD, "MyFollower.esp").plugin == "MyFollower.esp"


# ---- a real Mantella conversation goes into SQLite ------------------------------------------

def test_store_is_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    talk_to_gerdur(tmp_path)
    assert not db_path(tmp_path).exists()


def test_gerdur_conversation_is_logged(tmp_path, living_world_on):
    s = talk_to_gerdur(tmp_path)
    db = open_db(tmp_path)

    gerdur = db.execute("SELECT * FROM npc WHERE display_name = 'Gerdur'").fetchone()
    player = db.execute("SELECT * FROM npc WHERE is_player = 1").fetchone()
    assert (gerdur["plugin"], gerdur["base_form_id"], gerdur["ref_form_id"]) == \
        ("Skyrim.esm", "0x0001347C", "Skyrim.esm|0x00013489")
    assert gerdur["sex"] == "female" and gerdur["race"] == "Nord"

    # A1: one conversation, two participants
    convs = db.execute("SELECT c.*, COUNT(p.npc_id) n FROM conversation c JOIN participant p USING (conv_id)"
                       " GROUP BY c.conv_id").fetchall()
    assert len(convs) == 1
    assert (convs[0]["n"], convs[0]["kind"], convs[0]["location"]) == (2, "player", "Riverwood")
    assert (convs[0]["started_gt"], convs[0]["ended_gt"]) == (3.5, 3.6)

    # A2: every NPC line is Gerdur's, in her bound voice, in the order the game played them
    rows = db.execute("SELECT seq, speaker_id, text, voice_model FROM utterance ORDER BY seq").fetchall()
    npc_rows = [r for r in rows if r["speaker_id"] != player["npc_id"]]
    assert {r["speaker_id"] for r in npc_rows} == {gerdur["npc_id"]}
    assert [r["text"] for r in npc_rows] == [line.text for line in s.lines]
    assert {r["voice_model"] for r in npc_rows} == {"FemaleNord"}
    assert db.execute("SELECT voice_model FROM voice_binding WHERE npc_id = ?",
                      (gerdur["npc_id"],)).fetchone()[0] == "FemaleNord"
    player_lines = [r["text"] for r in rows if r["speaker_id"] == player["npc_id"]]
    assert player_lines == ["How old are you, Gerdur?", "I'm Torvar, I grew up in Falkreath."]
    assert all(r["voice_model"] is None for r in rows if r["speaker_id"] == player["npc_id"])
    assert [r["seq"] for r in rows] == list(range(1, len(rows) + 1))
    # Mantella's own memory still works next to the store
    assert list((tmp_path / "data" / "Skyrim" / "conversations").rglob("Gerdur.json"))


def test_identity_and_rows_survive_a_restart(tmp_path, living_world_on):
    talk_to_gerdur(tmp_path, 3.5, 3.6)
    db = open_db(tmp_path)
    first_ids = db.execute("SELECT npc_id, display_name FROM npc ORDER BY npc_id").fetchall()
    first_count = db.execute("SELECT COUNT(*) FROM utterance").fetchone()[0]
    db.close()

    talk_to_gerdur(tmp_path, 10.6, 10.7)  # a new Mantella process a week later
    db = open_db(tmp_path)
    assert [tuple(r) for r in db.execute("SELECT npc_id, display_name FROM npc ORDER BY npc_id")] == \
        [tuple(r) for r in first_ids]
    assert db.execute("SELECT COUNT(*) FROM utterance").fetchone()[0] == 2 * first_count
    # C1: the game-time gap between the two conversations is recorded
    starts = [r[0] for r in db.execute("SELECT started_gt FROM conversation ORDER BY started_gt")]
    assert starts[1] - starts[0] >= 7.0


def test_logged_lines_cannot_be_changed(tmp_path, living_world_on):
    talk_to_gerdur(tmp_path)
    db = open_db(tmp_path)
    # A7: the raw log is append-only
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("UPDATE utterance SET text = 'x' WHERE utt_id = 1")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("DELETE FROM utterance WHERE utt_id = 1")
    with pytest.raises(sqlite3.DatabaseError, match="closed once"):
        db.execute("UPDATE conversation SET ended_gt = 99 WHERE conv_id = 1")


def test_group_conversation_keeps_each_speaker_and_voice(tmp_path, living_world_on):
    # with the store on, each NPC gets their own AI call (PR C), so each reply is one speaker's
    llm = ScriptedLLM(replies=["Hod, come meet our guest.", "Well met, friend."])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([actors.player(), actors.gerdur(), actors.hod()], location="Riverwood", game_days=5.0)
        s.continue_until_player_turn()
        s.end(game_days=5.1)

    db = open_db(tmp_path)
    rows = db.execute("SELECT n.display_name, u.text, u.voice_model, v.voice_model bound FROM utterance u"
                      " JOIN npc n ON n.npc_id = u.speaker_id JOIN voice_binding v ON v.npc_id = u.speaker_id"
                      " ORDER BY u.seq").fetchall()
    assert [(r["display_name"], r["text"]) for r in rows] == \
        [("Gerdur", "Hod, come meet our guest."), ("Hod", "Well met, friend.")]
    assert all(r["voice_model"] == r["bound"] for r in rows)
    assert {r["display_name"]: r["voice_model"] for r in rows} == {"Gerdur": "FemaleNord", "Hod": "MaleNord"}
    assert db.execute("SELECT COUNT(*) FROM participant").fetchone()[0] == 3


def test_npcs_with_the_same_name_get_separate_identities(tmp_path, living_world_on):
    def guard(ref_id):
        return actors.actor("Whiterun Guard", 0x000A2C94, ref_id, 0, "Nord", "MaleGuard")

    for ref_id, day in ((0x000B1001, 2.0), (0x000B1002, 3.0)):
        with HeadlessSession(str(tmp_path), ScriptedLLM(replies=["Move along."])) as s:
            s.start([actors.player(), guard(ref_id)], location="Whiterun", game_days=day)
            s.continue_until_player_turn()
            s.end(game_days=day + 0.1)

    db = open_db(tmp_path)
    guards = db.execute("SELECT npc_id, ref_form_id FROM npc WHERE display_name = 'Whiterun Guard'").fetchall()
    assert len(guards) == 2
    speakers = db.execute("SELECT conv_id, speaker_id FROM utterance u JOIN npc n ON n.npc_id = u.speaker_id"
                          " WHERE n.display_name = 'Whiterun Guard' ORDER BY conv_id").fetchall()
    assert [r["speaker_id"] for r in speakers] == [g["npc_id"] for g in guards]


def test_a_store_failure_never_breaks_the_conversation(tmp_path, living_world_on, monkeypatch):
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(ConversationLog, "append", broken)
    s = talk_to_gerdur(tmp_path)
    assert "31" in " ".join(line.text for line in s.lines)


# ---- voice binding rules --------------------------------------------------------------------

def test_a_bound_voice_cannot_be_swapped(tmp_path):
    store = WorldStore(str(tmp_path / "w.sqlite"))
    registry = IdentityRegistry(store)
    gerdur = registry.ensure_npc(ActorIdentity.from_actor_json(actors.gerdur()), 1.0)
    player = registry.ensure_npc(ActorIdentity.from_actor_json(actors.player()), 1.0)
    assert registry.bind_voice(gerdur, "piper", "FemaleNord", None, 1.0) == "FemaleNord"
    with pytest.raises(VoiceBindingConflict):
        registry.bind_voice(gerdur, "piper", "MaleNord", None, 1.0)
    with pytest.raises(sqlite3.DatabaseError, match="locked"):
        with store.transaction() as db:
            db.execute("UPDATE voice_binding SET voice_model = 'MaleNord' WHERE npc_id = ?", (gerdur,))

    log = ConversationLog(store)
    conv = log.open("Riverwood", "player", 1.0)
    log.add_participant(conv, gerdur)
    log.add_participant(conv, player)
    log.append(conv, gerdur, "Hello.", "FemaleNord", 1.0)
    with pytest.raises(sqlite3.IntegrityError, match="voice does not match"):
        log.append(conv, gerdur, "Hello in the wrong voice.", "MaleNord", 1.0)
    # the player is the same row however often they are seen
    assert registry.ensure_npc(ActorIdentity.from_actor_json(actors.player("Torvar")), 2.0) == player


def test_an_existing_database_gets_new_migrations(tmp_path):
    path = str(tmp_path / "w.sqlite")
    conn = sqlite3.connect(path)
    from importlib import resources
    conn.executescript(resources.files("livingworld.store.migrations").joinpath("001_initial.sql").read_text())
    conn.close()
    store = WorldStore(path)  # a version 1 file, as PR A created it
    assert store.schema_version() == 4
    assert store.scalar("SELECT COUNT(*) FROM game_load") == 0
