"""G2f arrests: the player, serving in the guard, arrests someone; rules decide whether they go quietly, argue, run
or fight; the game escorts them to the cell and lets them out; the superior books them (or reprimands)."""
import json

from livingworld.guard import arrest as AR, incident as INC, orgs as O, store as S
from livingworld.harness import actors
from livingworld.harness.perception import KEY_EVENTS, jail
from livingworld.harness.scripted_llm import ScriptedLLM
from livingworld.harness.session import HeadlessSession
from livingworld.integration import LivingWorld
from livingworld.store.world_store import WorldStore
from src.http.communication_constants import communication_constants as comm_consts

from tests.livingworld.test_guard import DAY, PLAYER, caius, db_path, living_world_inline, npc_id, open_db, service, whiterun_guard  # noqa: F401
from tests.livingworld.test_guard_duty import ENLIST, PAY, REPORT, enlisted, talk

BRENUIN = actors.actor("Brenuin", 0x00013B9C, 0x00013B9D, 1, "Nord", "MaleCoarse")
YSOLDA = actors.actor("Ysolda", 0x00013BAB, 0x00013BAC, 1, "Nord", "FemaleYoungEager")


def say(work_dir, day, lines, npcs, events=None):
    """One conversation; returns (the user text the AI got after each line, the reply keys sent)."""
    llm = ScriptedLLM(rules=[ENLIST, REPORT, PAY], replies=["Hm."])
    with HeadlessSession(str(work_dir), llm) as s:
        s.start([PLAYER, *npcs], location="Whiterun", game_days=day,
                context_extra={KEY_EVENTS: events} if events else None)
        s.continue_until_player_turn()
        for i, line in enumerate(lines):
            s.say(line, game_days=day + 0.0005 * (i + 1))
        s.end(game_days=day + 0.002)
        LivingWorld.latest.wait_for_jobs()
        keys = [r.get(comm_consts.KEY_REPLYTYPE_NPCTALK) or {} for _, r in s.exchanges]
    texts = [c.last_user_text for c in llm.dialogue_calls]
    say.hints = [h for k in keys for h in (k.get("mantella_lw_hints") or [])]
    say.prompt = llm.dialogue_calls[0].system_prompt if llm.dialogue_calls else ""
    return "\n".join(texts), keys


def arrests(work_dir):
    return [dict(r) | json.loads(r["payload_json"]) for r in open_db(work_dir).execute(
        "SELECT * FROM world_event WHERE type = 'guard.arrest' ORDER BY event_id")]


def suspect(work_dir, name, day):
    store = WorldStore(db_path(work_dir))
    with store.transaction() as db:
        from livingworld.guard.service import _suspect_incident
        return _suspect_incident(db, O.by_key("whiterun_guard"), name, day).event_id


def sent(keys, key):
    return [k[key] for k in keys if key in k]


def test_no_evidence_means_an_argument_and_insisting_is_a_wrongful_arrest(tmp_path):
    enlisted(tmp_path)
    text, keys = say(tmp_path, DAY + 0.01, ["Ysolda, you're under arrest."], [YSOLDA])
    assert "nothing in the guard's record ties Ysolda to any crime" in text
    assert any("nothing on record against Ysolda" in h for h in say.hints)
    assert sent(keys, "mantella_lw_guard_arrest_name") == []
    assert arrests(tmp_path)[-1]["response"] == "argue"
    # said again in the same conversation: they go, wrongfully
    text, keys = say(tmp_path, DAY + 0.02, ["You're under arrest, Ysolda.", "I said you're under arrest."], [YSOLDA])
    a = arrests(tmp_path)[-1]
    assert a["wrongful"] is True and a["response"] == "submit"
    assert "this arrest is wrongful" in text
    assert sent(keys, "mantella_lw_guard_arrest_mode") == ["submit"]
    assert sent(keys, "mantella_lw_guard_jail_id") == [0x000267E4]
    # the game locks her up; Caius books it as a reprimand and lets her out
    say(tmp_path, DAY + 0.05, ["..."], [whiterun_guard()],
        events=[jail(PLAYER, YSOLDA, a["event_id"], "jailed", DAY + 0.05)])
    merit = service(open_db(tmp_path))["merit"]
    _, keys = say(tmp_path, DAY + 0.1, ["Reporting in."], [caius()])
    assert sent(keys, "mantella_lw_guard_release") == [[a["event_id"]]]
    assert any("Wrongful arrest" in h for h in say.hints)
    assert service(open_db(tmp_path))["merit"] < merit
    db = open_db(tmp_path)
    assert db.execute("SELECT 1 FROM world_event WHERE type = 'guard.reprimanded'").fetchone()
    assert db.execute("SELECT 1 FROM memory WHERE owner_id = ? AND content LIKE '%arrested me with no charge%'",
                      (npc_id(db, "Ysolda"),)).fetchone()


def test_a_named_suspect_is_arrested_jailed_booked_and_let_out(tmp_path, monkeypatch):
    enlisted(tmp_path)
    say(tmp_path, DAY + 0.005, ["Hello."], [BRENUIN])       # the world knows Brenuin
    suspect(tmp_path, "Brenuin", DAY + 0.006)
    monkeypatch.setattr(AR, "_roll", lambda *a: 0.0)        # the roll says: goes quietly
    text, keys = say(tmp_path, DAY + 0.01, ["Brenuin, you're under arrest for the theft in the market."],
                     [BRENUIN, whiterun_guard()])
    a = arrests(tmp_path)[-1]
    assert a["response"] == "submit" and a["charge"] == "theft" and a["wrongful"] is False
    assert "named as the suspect" in text and "goes quietly" in text
    assert sent(keys, "mantella_lw_guard_arrest_name") == ["Brenuin"]
    assert any("Brenuin goes quietly" in h for h in say.hints)
    # while escorted, Brenuin knows it; then the game reports the cell
    prompt = say(tmp_path, DAY + 0.02, ["Keep walking."], [BRENUIN]) and say.prompt
    assert "is under arrest for theft" in prompt
    say(tmp_path, DAY + 0.03, ["In you go."], [BRENUIN],
        events=[jail(PLAYER, BRENUIN, a["event_id"], "jailed", DAY + 0.03)])
    say(tmp_path, DAY + 0.04, ["How is it in there?"], [BRENUIN])
    assert "locked in a cell in Dragonsreach Dungeon" in say.prompt
    merit = service(open_db(tmp_path))["merit"]
    say(tmp_path, DAY + 0.1, ["Reporting in."], [caius()])
    assert "has just brought in and booked: Brenuin for theft" in say.prompt
    assert "In the cells of Dragonsreach Dungeon right now: Brenuin (theft)" in say.prompt
    assert any(h == "Booked: Brenuin for theft." for h in say.hints)
    assert service(open_db(tmp_path))["merit"] >= merit + 1.75 - 1e-9
    db = open_db(tmp_path)
    news = db.execute("SELECT payload_json FROM world_event WHERE type = 'guard.arrested'").fetchone()
    assert news and json.loads(news[0])["culprit"] == "Brenuin"
    # sentence served: the game lets him out, and he is free again
    say(tmp_path, DAY + 1.2, ["Free to go."], [BRENUIN],
        events=[jail(PLAYER, BRENUIN, a["event_id"], "released", DAY + 1.2)])
    with WorldStore(db_path(tmp_path)).transaction() as db2:
        assert AR.held(db2, npc_id(db2, "Brenuin"), DAY + 1.3) is None


def test_running_or_fighting_and_being_caught_again(tmp_path, monkeypatch):
    enlisted(tmp_path)
    say(tmp_path, DAY + 0.005, ["Hello."], [BRENUIN])
    suspect(tmp_path, "Brenuin", DAY + 0.006)
    monkeypatch.setattr(AR, "_roll", lambda *a: 0.999)      # the roll says: will not be taken
    text, keys = say(tmp_path, DAY + 0.01, ["You're under arrest."], [BRENUIN])
    a = arrests(tmp_path)[-1]
    assert a["response"] in ("flee", "fight")
    assert sent(keys, "mantella_lw_guard_arrest_mode") == [a["response"]]
    assert "will not be taken" in text
    # caught again soon after: much likelier to go quietly
    monkeypatch.setattr(AR, "_roll", lambda *a: 0.9)
    say(tmp_path, DAY + 0.05, ["You're under arrest, and this time you come quietly."], [BRENUIN, whiterun_guard()])
    b = arrests(tmp_path)[-1]
    assert b["caught"] is True and b["p_submit"] > a["p_submit"] and b["response"] == "submit"


def test_an_escort_never_brought_in_slips_away(tmp_path, monkeypatch):
    enlisted(tmp_path)
    say(tmp_path, DAY + 0.005, ["Hello."], [BRENUIN])
    suspect(tmp_path, "Brenuin", DAY + 0.006)
    monkeypatch.setattr(AR, "_roll", lambda *a: 0.0)
    say(tmp_path, DAY + 0.01, ["You're under arrest."], [BRENUIN])
    say(tmp_path, DAY + 1.5, ["Hm."], [YSOLDA])
    db = open_db(tmp_path)
    rel = db.execute("SELECT payload_json FROM world_event WHERE type = 'guard.released'").fetchone()
    assert rel and json.loads(rel[0])["why"] == "escaped"


def test_arrest_words():
    assert AR.wants_arrest("Brenuin, you're under arrest!")
    assert AR.wants_arrest("I'm arresting you for theft.")
    assert not AR.wants_arrest("I was arrested once in Markarth.")
    assert not AR.wants_arrest("Nice weather today.")


def test_incidents_can_name_a_real_suspect():
    org = O.by_key("whiterun_guard")
    k = next(k for k in INC.kinds(org.key) if k.key == "market_theft")
    assert k.suspects and k.charge == "theft" and 0 < k.suspect_chance < 1
    assert org.jail is not None and org.jail.form_id == "0x000267E4"


def test_switching_arrests_off(tmp_path, monkeypatch):
    monkeypatch.setenv(AR.ENV, "0")
    enlisted(tmp_path)
    say(tmp_path, DAY + 0.01, ["Ysolda, you're under arrest."], [YSOLDA])
    assert arrests(tmp_path) == []
