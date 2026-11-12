"""G2g recruit training (drill, sparring bout, law quiz, patrol beside a senior guard) and written reports."""
from livingworld.guard import training as TR
from livingworld.harness import actors
from livingworld.harness.perception import KEY_EVENTS, fight, watch
from livingworld.harness.scripted_llm import ScriptedLLM
from livingworld.harness.session import HeadlessSession
from livingworld.integration import LivingWorld
from src.http.communication_constants import communication_constants as comm_consts

from tests.livingworld.test_guard import DAY, PLAYER, caius, living_world_inline, open_db, service, whiterun_guard  # noqa: F401
from tests.livingworld.test_guard_duty import ENLIST, PAY, REPORT, duties, enlisted, talk

YSOLDA = actors.actor("Ysolda", 0x00013BAB, 0x00013BAC, 1, "Nord", "FemaleYoungEager")


def chat(work_dir, day, lines, npcs=None, events=None):
    """One conversation in which the player says several things. Returns (first prompt, hints)."""
    llm = ScriptedLLM(rules=[ENLIST, REPORT, PAY], replies=["Hm."])
    with HeadlessSession(str(work_dir), llm) as s:
        s.start([PLAYER, *(npcs or [caius()])], location="Whiterun", game_days=day,
                context_extra={KEY_EVENTS: events} if events else None)
        s.continue_until_player_turn()
        for i, line in enumerate(lines):
            s.say(line, game_days=day + 0.0005 * (i + 1))
        s.end(game_days=day + 0.002)
        LivingWorld.latest.wait_for_jobs()
        hints = [h for _, r in s.exchanges for h in ((r.get(comm_consts.KEY_REPLYTYPE_NPCTALK) or {})
                                                      .get("mantella_lw_hints") or [])]
    return (llm.dialogue_calls[0].system_prompt if llm.dialogue_calls else ""), hints


def events(tmp_path, kind):
    return [dict(r) for r in open_db(tmp_path).execute(
        "SELECT * FROM world_event WHERE type = ? ORDER BY event_id", (f"guard.{kind}",))]


def last(tmp_path):
    return duties(tmp_path)[-1]


def full_course(monkeypatch):
    """The old four-step course (drill, bout, quiz, patrol), to test the step kinds the shipped course retired."""
    import dataclasses
    c = TR.course("whiterun_guard")
    old = {s.key: s for s in c.retired + c.steps}
    full = dataclasses.replace(c, steps=tuple(old[k] for k in ("drill", "spar", "quiz", "patrol")), retired=())
    monkeypatch.setattr(TR, "course", lambda key: full)


def test_training_is_one_fist_bout_then_routine_duties(tmp_path):
    enlisted(tmp_path, trained=False)
    prompt = talk(tmp_path, DAY + 0.01)
    assert "Commander Caius is training Torvar, a new recruit (step 1 of 1): go a sparring bout" in prompt
    assert any(h.startswith("Training 1/1:") and "Ask any Whiterun guard for a fist bout, then tell Commander Caius"
               " you're reporting back." in h for h in talk.hints)
    # a fellow guard is told, and is the one to start the bout
    prompt = talk(tmp_path, DAY + 0.02, npcs=[whiterun_guard()])
    assert "training step now is a sparring bout, fists only" in prompt and "'Brawl:'" in prompt
    # reporting back before the bout: not filed, no reprimand; Caius is told what is missing
    llm = ScriptedLLM(replies=["Hm."])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([PLAYER, caius()], location="Whiterun", game_days=DAY + 0.03)
        s.continue_until_player_turn()
        s.say("Reporting back, sir.", game_days=DAY + 0.0305)
        s.end(game_days=DAY + 0.031)
        LivingWorld.latest.wait_for_jobs()
    assert any("no fist bout yet" in c.last_user_text for c in llm.dialogue_calls)
    assert last(tmp_path)["state"] == "accepted" and events(tmp_path, "reprimanded") == []
    talk(tmp_path, DAY + 0.04, npcs=[whiterun_guard()], events=[fight(PLAYER, 0.7, DAY + 0.04, location="Whiterun")])
    merit = service(open_db(tmp_path))["merit"]
    _, hints = chat(tmp_path, DAY + 0.05, ["Reporting back, Commander. The bout is done."])
    assert duties(tmp_path)[0]["state"] == "completed"
    assert "Training complete!" in hints
    assert len(events(tmp_path, "trained")) == 1
    svc = service(open_db(tmp_path))
    assert svc["status"] == "active" and svc["merit"] > merit + 2
    talk(tmp_path, DAY + 0.6)
    assert not last(tmp_path)["template"].startswith("duty:training:")


def test_a_drill_already_in_hand_still_resolves_by_saying_reporting_back(tmp_path):
    """The drill was retired from the course; a recruit holding one can still report it, then gets the bout."""
    enlisted(tmp_path, trained=False)
    db = open_db(tmp_path)
    talk(tmp_path, DAY + 0.01)
    db.execute("UPDATE task SET template = 'duty:training:drill' WHERE template = 'duty:training:spar'")
    db.commit()
    talk(tmp_path, DAY + 0.02, npcs=[whiterun_guard()])                   # seen at the drill, in Whiterun
    prompt, _ = chat(tmp_path, DAY + 0.09, ["Reporting back, sir. Drill's done."])
    ds = duties(tmp_path)
    assert ds[0]["template"] == "duty:training:drill" and ds[0]["state"] == "completed"
    assert ds[1]["template"] == "duty:training:spar"


def test_a_new_recruit_trains_step_by_step_and_passes_probation_early(tmp_path, monkeypatch):
    full_course(monkeypatch)
    enlisted(tmp_path, trained=False)
    prompt = talk(tmp_path, DAY + 0.01)
    assert "Commander Caius is training Torvar, a new recruit (step 1 of 4): drill" in prompt
    assert any(h.startswith("Training 1/4:") for h in talk.hints)
    assert last(tmp_path)["template"] == "duty:training:drill"
    # drill: be at the place, back after half of it; the next step follows at once, no rest needed
    talk(tmp_path, DAY + 0.03, npcs=[whiterun_guard()])
    prompt = talk(tmp_path, DAY + 0.06)
    assert [d["template"] for d in duties(tmp_path)] == ["duty:training:drill", "duty:training:spar"]
    assert duties(tmp_path)[0]["state"] == "completed"
    assert "(step 2 of 4): go a sparring bout" in prompt
    # spar: the game's fight record counts
    talk(tmp_path, DAY + 0.07, npcs=[whiterun_guard()], events=[fight(PLAYER, 0.7, DAY + 0.07, location="Whiterun")])
    prompt = talk(tmp_path, DAY + 0.08)
    assert last(tmp_path)["template"] == "duty:training:quiz"
    assert "is testing Torvar on the law of Whiterun" in prompt and "ONE question at a time" in prompt
    # quiz: answers checked by key words
    _, hints = chat(tmp_path, DAY + 0.09, ["I'd arrest them and give the goods back to the owner.",
                                          "Commander Caius, sir.",
                                          "I refuse the coin and report the offer."])
    assert "Training: law and orders quiz passed." in hints
    # patrol: only counts with a senior guard in the conversation
    talk(tmp_path, DAY + 0.10)                                            # given the patrol
    assert last(tmp_path)["template"] == "duty:training:patrol"
    talk(tmp_path, DAY + 0.11, npcs=[YSOLDA])
    assert last(tmp_path)["state"] == "accepted"                          # nobody senior there: not counted
    talk(tmp_path, DAY + 0.12, npcs=[whiterun_guard()])
    assert last(tmp_path)["state"] == "in_progress"
    merit = service(open_db(tmp_path))["merit"]
    prompt = talk(tmp_path, DAY + 0.2)
    assert last(tmp_path)["state"] == "completed"
    assert "last step of Torvar's recruit training" in prompt
    assert "Training complete!" in talk.hints
    assert len(events(tmp_path, "trained")) == 1
    svc = service(open_db(tmp_path))
    assert svc["status"] == "active" and svc["merit"] > merit + 2
    # now routine duties (after a rest)
    talk(tmp_path, DAY + 0.6)
    assert not last(tmp_path)["template"].startswith("duty:training:")


def test_failing_the_quiz_is_no_reprimand_and_can_be_retaken(tmp_path, monkeypatch):
    full_course(monkeypatch)
    enlisted(tmp_path, trained=False)
    talk(tmp_path, DAY + 0.01, say="debug guard seen")                    # the drill...
    for verb in ("report", "duty", "seen", "report", "duty"):              # ...and the bout, by test commands
        talk(tmp_path, DAY + 0.2, say=f"debug guard {verb}", npcs=[whiterun_guard()])
    assert last(tmp_path)["template"] == "duty:training:quiz"
    _, hints = chat(tmp_path, DAY + 0.21, ["Pass.", "No idea.", "Dunno."])
    assert "Training: quiz failed. Ask to try it again." in hints
    assert last(tmp_path)["state"] == "failed" and events(tmp_path, "reprimanded") == []
    talk(tmp_path, DAY + 0.22)
    assert last(tmp_path)["template"] == "duty:training:quiz" and last(tmp_path)["state"] == "accepted"


def test_the_quiz_judges_answers_by_their_points():
    from livingworld.guard import orgs as O
    org = O.by_key("whiterun_guard")
    assert TR.judge(org, "Death, by the axe.", [], "Caius").key == "murder"
    assert TR.judge(org, "Back to my post on the walls!", [], "Caius").key == "alarm"
    assert TR.judge(org, "I'd take it, who'd know?", [], "Caius") is None


def test_a_written_report_is_kept_judged_and_remembered(tmp_path):
    enlisted(tmp_path)
    talk(tmp_path, DAY + 0.01)
    task = last(tmp_path)["task_id"]
    talk(tmp_path, DAY + 0.03, say="Can I help?", npcs=[YSOLDA],
         events=[watch(PLAYER, task, 0, DAY + 0.03, target_json=YSOLDA)])
    merit = service(open_db(tmp_path))["merit"]
    prompt, _ = chat(tmp_path, DAY + 0.2, ["Sir, my report: Ysolda came to me with a matter during the shift,",
                                          "and I dealt with it there and then. Otherwise all quiet at the post."])
    assert "asks Torvar for their report in their own words" in prompt
    rep = events(tmp_path, "written_report")
    assert len(rep) == 1 and '"verdict": "thorough"' in rep[0]["payload_json"]
    db = open_db(tmp_path)
    assert db.execute("SELECT 1 FROM memory WHERE content LIKE '%written report%Ysolda came to me%'").fetchone()
    assert service(db)["merit"] > merit + 1.0                              # duty + shift + a thorough report
    assert "1 written report" in talk(tmp_path, DAY + 0.21)
    assert "remembers Torvar's recent written reports" in talk(tmp_path, DAY + 0.22)


def test_a_report_with_made_up_claims_costs_merit(tmp_path):
    enlisted(tmp_path)
    talk(tmp_path, DAY + 0.01)
    talk(tmp_path, DAY + 0.03, npcs=[whiterun_guard()])
    chat(tmp_path, DAY + 0.2, ["Sir, a dragon attacked the gate and I killed three bandits myself before noon."])
    rep = events(tmp_path, "written_report")
    assert len(rep) == 1 and '"verdict": "made up"' in rep[0]["payload_json"]
    assert "dragon" in rep[0]["payload_json"] and "bandits" in rep[0]["payload_json"]


def test_first_orders_come_in_the_conversation_the_recruit_enlists_in(tmp_path):
    """The prompt is written before the player enlists, so without this Caius invents a duty of his own."""
    llm = ScriptedLLM(rules=[ENLIST, REPORT, PAY], replies=["Hm."])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([PLAYER, caius()], location="Whiterun", game_days=DAY)
        s.continue_until_player_turn()
        s.say("I want to join the guard.", game_days=DAY + 0.0005)
        s.say("Yes, sir. What are my orders?", game_days=DAY + 0.001)
        s.say("Understood.", game_days=DAY + 0.0015)
        s.end(game_days=DAY + 0.002)
        LivingWorld.latest.wait_for_jobs()
        hints = [h for _, r in s.exchanges for h in ((r.get(comm_consts.KEY_REPLYTYPE_NPCTALK) or {})
                                                      .get("mantella_lw_hints") or [])]
    assert "has a duty" not in llm.dialogue_calls[0].system_prompt          # not yet a member when it was written
    noted = [c.all_text for c in llm.dialogue_calls if "real first orders" in c.last_user_text]
    assert len(noted) == 1 and "(step 1 of 1): go a sparring bout" in noted[0]
    assert sum("real first orders" in c.last_user_text for c in llm.dialogue_calls) == 1   # once only
    assert [d["template"] for d in duties(tmp_path)] == ["duty:training:spar"]
    assert any(h.startswith("Training 1/1:") for h in hints)


def test_no_first_orders_when_the_superior_is_not_there(tmp_path):
    """Irileth takes the player on; Caius, who gives duties, is not in the conversation."""
    irileth = whiterun_guard()
    irileth.update(actors.actor("Irileth", 0x00013BB8, 0x00013BB9, 1, "DarkElf", "FemaleDarkElf"))
    irileth["mantella_lw_is_guard"] = False
    llm = ScriptedLLM(rules=[ENLIST], replies=["Hm."])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([PLAYER, irileth], location="Whiterun", game_days=DAY)
        s.continue_until_player_turn()
        s.say("I want to join the guard.", game_days=DAY + 0.0005)
        s.say("What now?", game_days=DAY + 0.001)
        s.end(game_days=DAY + 0.002)
        LivingWorld.latest.wait_for_jobs()
    assert service(open_db(tmp_path))["status"] == "probation"
    assert not any("real first orders" in c.last_user_text for c in llm.dialogue_calls)
    assert duties(tmp_path) == []
