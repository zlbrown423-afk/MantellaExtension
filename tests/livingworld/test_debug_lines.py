"""Lines starting with "debug" run their test command but are not speech (user 2026-11-17): the NPC never sees
the words, and they stay out of the conversation log (memories, facts, leads, diaries) and Mantella's summary."""
from livingworld.dialogue import debug_lines
from livingworld.harness.scripted_llm import ScriptedLLM
from livingworld.harness.session import HeadlessSession
from livingworld.integration import LivingWorld

from tests.livingworld.test_guard import DAY, PLAYER, caius, living_world_inline, npc_id, open_db, service  # noqa: F401
from tests.livingworld.test_guard_duty import enlisted, talk


def test_what_counts_as_a_debug_line(monkeypatch):
    assert debug_lines.is_debug("debug guard reinstate")
    assert debug_lines.is_debug("  Debug, max dirt on Hod")
    assert not debug_lines.is_debug("I need to debug my thinking")
    assert not debug_lines.is_debug("Debugging is hard")
    monkeypatch.setenv(debug_lines.ENV, "0")
    assert not debug_lines.is_debug("debug guard reinstate")


def test_the_command_runs_but_nobody_hears_or_remembers_it(tmp_path):
    enlisted(tmp_path)
    llm = ScriptedLLM(replies=["Hm."])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([PLAYER, caius()], location="Whiterun", game_days=DAY + 0.2)
        s.continue_until_player_turn()
        s.say("debug guard dismiss")
        s.say("Fine weather today.")
        s.end(game_days=DAY + 0.21)
        LivingWorld.latest.wait_for_jobs()
    db = open_db(tmp_path)
    assert service(db)["status"] == "dismissed"                    # the command still ran
    said = [r[0] for r in db.execute("SELECT text FROM utterance")]
    assert "Fine weather today." in said
    assert not any("debug" in (t or "").lower() for t in said)
    seen = " ".join(str(m.get("content")) for c in llm.calls for m in c.messages)
    assert "debug guard dismiss" not in seen and "Fine weather today." in seen


def test_switched_off_the_line_is_kept_as_before(tmp_path, monkeypatch):
    monkeypatch.setenv(debug_lines.ENV, "0")
    enlisted(tmp_path)
    talk(tmp_path, DAY + 0.2, say="debug guard dismiss")
    said = [r[0] for r in open_db(tmp_path).execute("SELECT text FROM utterance")]
    assert any("debug guard dismiss" in (t or "") for t in said)


# ---- misheard commands (log 2026-11-17: "The Bug Guard Reinstate", "Debug guard re-in-state") ----

def test_misheard_commands_are_read_as_typed():
    c = debug_lines.command_text
    assert c("The Bug Guard Reinstate") == "debug guard reinstate"
    assert c("Debug guard re-in-state") == "debug guard reinstate"
    assert c("debug guard re in state.") == "debug guard reinstate"
    assert c("De bug, guard promote") == "debug guard promote"
    assert c("Debugg guard status") == "debug guard status"
    assert c("the bug guards reinstate") == "debug guard reinstate"
    assert c("debug guard pro-mote") == "debug guard promote"
    assert c("the bug max dirt") == "debug max dirt"
    assert c("The bug face happy") == "debug face happy"
    assert c("debug guard incident camp valtheim") == "debug guard incident camp valtheim"
    assert debug_lines.is_debug("The Bug Guard Reinstate")


def test_ordinary_speech_is_left_alone():
    c = debug_lines.command_text
    for line in ("The bug bit me while I was on guard.", "The bug is clean now.", "the bug guard",
                 "The bug guard is a strange beast.", "Debugging is hard", "I need to debug my thinking",
                 "de bug me not", "Reinstate me, Commander.", "Let's re in state the old ways"):
        assert c(line) is None, line
        assert not debug_lines.is_debug(line), line


def test_typed_lines_are_kept_word_for_word():
    assert debug_lines.command_text("debug max dirt on Hod") == "debug max dirt on Hod"
    assert debug_lines.command_text("debug whatever") == "debug whatever"     # still hidden, as before
    assert debug_lines.is_debug("debug whatever")


def test_fuzzy_reading_switched_off(monkeypatch):
    monkeypatch.setenv(debug_lines.FUZZY_ENV, "0")
    assert debug_lines.command_text("The Bug Guard Reinstate") is None
    assert debug_lines.command_text("Debug guard re-in-state") == "Debug guard re-in-state"


def test_misheard_reinstate_runs_and_is_hidden(tmp_path):
    enlisted(tmp_path)
    llm = ScriptedLLM(replies=["Hm."])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([PLAYER, caius()], location="Whiterun", game_days=DAY + 0.2)
        s.continue_until_player_turn()
        s.say("Debug guard dismiss")
        s.say("The Bug Guard Re-in-state")
        s.end(game_days=DAY + 0.21)
        LivingWorld.latest.wait_for_jobs()
    db = open_db(tmp_path)
    assert service(db)["status"] == "active"                       # dismissed, then reinstated by the misheard line
    said = " ".join(r[0] or "" for r in db.execute("SELECT text FROM utterance")).lower()
    assert "bug" not in said and "state" not in said
    seen = " ".join(str(m.get("content")) for c in llm.calls for m in c.messages)
    assert "Re-in-state" not in seen and "The Bug" not in seen


def test_misheard_thane_is_read_as_thane():
    """User 2026-11-18: "thane" never registers; it is heard as "bain" or "fien" (and similar)."""
    for heard in ("debug guard bain whiterun", "Debug guard fien whiterun.", "debug guard bane white run",
                  "the bug guard fine whiterun", "Debug guard, thain, Whiterun", "debug guard fain",
                  "de bug guard thein white run", "debug guard bain", "debug guard spain whiterun",
                  "Debug guard Spain, White Run.", "debug guard fain whiterun"):
        got = debug_lines.command_text(heard)
        assert got is not None and got.startswith("debug guard thane"), (heard, got)
    assert debug_lines.command_text("debug guard bain whiterun") == "debug guard thane whiterun"
    assert debug_lines.command_text("debug guard thane whiterun") == "debug guard thane whiterun"


def test_thane_sounds_only_count_right_after_debug_guard():
    assert debug_lines.command_text("Bain is a fine name for a dog") is None
    assert debug_lines.command_text("the bug bit me, it was fine") is None
    assert debug_lines.command_text("the guard was fine in Whiterun") is None
    # a real verb later in the line wins: "then" is just a word here
    assert debug_lines.command_text("debug guard then report") == "debug guard then report"
    assert debug_lines.command_text("the bug guard fine report") == "debug guard fine report"


def test_thane_sounds_switched_off(monkeypatch):
    monkeypatch.setenv(debug_lines.FUZZY_ENV, "0")
    assert debug_lines.command_text("debug guard bain whiterun") == "debug guard bain whiterun"
    assert debug_lines.command_text("the bug guard bain whiterun") is None


def test_heard_bain_makes_the_player_thane(tmp_path):
    enlisted(tmp_path)
    llm = ScriptedLLM(replies=["Hm."])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([PLAYER, caius()], location="Whiterun", game_days=DAY + 0.2)
        s.continue_until_player_turn()
        s.say("Debug guard bain white run.")
        s.end(game_days=DAY + 0.21)
        LivingWorld.latest.wait_for_jobs()
    db = open_db(tmp_path)
    from livingworld.guard import store as GS
    assert GS.last_event_gt(db, "thane", npc_id(db, "Torvar"), "whiterun_guard") is not None
