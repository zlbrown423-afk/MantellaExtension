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
