"""The cutpurse duty is an actual inquiry: ask the people who saw it, follow the trail, catch or arrest the thief.

Before, any conversation in Whiterun counted as dealing with it, so reporting back "caught" a thief nobody had
looked for (user report 2026-11-17: "the cutpurse missions don't really work").
"""
import json

from livingworld.guard import incident as INC, inquiry as INQ, orgs as O, store as S
from livingworld.store.world_store import WorldStore

from tests.livingworld.test_guard import DAY, db_path, living_world_inline, npc_id, open_db  # noqa: F401
from tests.livingworld.test_guard_complainant import memories, the_incident, townsperson
from tests.livingworld.test_guard_duty import duties, enlisted, talk, whiterun_guard

WHITERUN = O.by_key("whiterun_guard")


def theft(work_dir, *, suspect=False):
    """Open a theft incident (with or without a named suspect) and have Caius send Torvar to it."""
    enlisted(work_dir)
    talk(work_dir, DAY + 0.005, say="debug guard incident theft", npcs=[whiterun_guard()], location="Riverwood")
    event_id, p = the_incident(work_dir)
    if bool(p["suspect"]) != suspect:
        k = next(k for k in INC.kinds("whiterun_guard") if k.key == "market_theft")
        store = WorldStore(db_path(work_dir))
        with store.transaction() as db:
            INC.close(db, INC.get(db, event_id), "went cold", DAY + 0.006)
            for n in range(1, 400):
                seed = INC._roll("test", n)
                if bool(((seed >> 48) % 1000) / 1000 < k.suspect_chance) == suspect:
                    INC._open(db, WHITERUN, k, int(DAY), DAY + 0.006, seed)
                    break
        event_id, p = the_incident(work_dir)
    talk(work_dir, DAY + 0.01)                                          # Caius hands it out
    assert duties(work_dir)[-1]["template"] == f"duty:incident:market_theft:{event_id}"
    return event_id, p


def report(work_dir, day):
    return talk(work_dir, day, say="Reporting back, I caught the thief.")


def test_market_theft_is_an_inquiry_with_witnesses_and_hideouts():
    k = next(k for k in INC.kinds("whiterun_guard") if k.key == "market_theft")
    assert k.need == "inquiry" and len(k.witnesses) >= 4 and k.hideouts


def test_the_complainant_is_never_the_suspect():
    k = next(k for k in INC.kinds("whiterun_guard") if k.key == "market_theft")
    assert set(k.complainants) & set(k.suspects)              # Brenuin is on both lists
    store_seen = 0
    for n in range(300):
        seed = INC._roll("whiterun_guard", n, k.key)
        comp = k.complainants[(seed >> 24) % len(k.complainants)]
        suspects = [x for x in k.suspects if x != comp]
        if suspects and ((seed >> 48) % 1000) / 1000 < k.suspect_chance:
            assert suspects[(seed >> 56) % len(suspects)] != comp
            store_seen += 1
    assert store_seen


def test_talking_to_anyone_in_whiterun_no_longer_catches_the_thief(tmp_path):
    theft(tmp_path)
    talk(tmp_path, DAY + 0.02, npcs=[whiterun_guard()])              # just chatting in town
    report(tmp_path, DAY + 0.03)
    assert duties(tmp_path)[-1]["state"] != "completed"               # nothing was done: a report of a catch is false
    db = open_db(tmp_path)
    assert db.execute("SELECT COUNT(*) FROM world_event WHERE type = 'guard.arrested'").fetchone()[0] == 0


def test_after_asking_around_an_early_report_is_not_a_lie(tmp_path):
    _, p = theft(tmp_path)
    talk(tmp_path, DAY + 0.02, npcs=[townsperson(p["witnesses"][0][0])])
    report(tmp_path, DAY + 0.03)
    assert duties(tmp_path)[-1]["state"] == "in_progress"
    db = open_db(tmp_path)
    assert db.execute("SELECT COUNT(*) FROM world_event WHERE type = 'guard.reprimanded'").fetchone()[0] == 0


def test_asking_a_witness_then_following_the_trail_catches_the_thief(tmp_path):
    event_id, p = theft(tmp_path)
    assert p["hideout"] and len(p["witnesses"]) == 2
    w, clue = p["witnesses"][1]
    assert p["hideout"] in clue
    prompt = talk(tmp_path, DAY + 0.02, npcs=[townsperson(w)])
    assert clue in prompt and "Torvar is the guard from the Whiterun Guard looking into it" in prompt
    assert any(clue in m for m in memories(tmp_path, w))
    task = duties(tmp_path)[-1]
    db = open_db(tmp_path)
    assert db.execute("SELECT COUNT(*) FROM v_task_change WHERE task_id = ? AND reason = ?",
                      (task["task_id"], "asked: " + w)).fetchone()[0] == 1
    prompt = talk(tmp_path, DAY + 0.03, npcs=[townsperson("Adrianne Avenicci")], location=p["hideout"])
    assert "cornered them here" in prompt
    report(tmp_path, DAY + 0.04)
    assert duties(tmp_path)[-1]["state"] == "completed"
    closed = open_db(tmp_path).execute("SELECT payload_json FROM world_event WHERE type = 'guard.incident_closed'"
                                       " ORDER BY event_id DESC LIMIT 1").fetchone()[0]
    assert json.loads(closed)["outcome"] == "arrested"


def test_going_to_the_hideout_without_asking_anyone_finds_nothing(tmp_path):
    _, p = theft(tmp_path)
    prompt = talk(tmp_path, DAY + 0.02, npcs=[townsperson("Adrianne Avenicci")], location=p["hideout"])
    assert "cornered them here" not in prompt
    assert duties(tmp_path)[-1]["state"] == "accepted"


def test_the_complainant_points_at_the_witnesses(tmp_path):
    _, p = theft(tmp_path)
    prompt = talk(tmp_path, DAY + 0.02, npcs=[townsperson(p["complainant"])])
    for w, _ in p["witnesses"]:
        assert w in prompt


def test_a_named_suspect_has_to_be_arrested(tmp_path):
    event_id, p = theft(tmp_path, suspect=True)
    assert p["suspect"] and not p["hideout"]
    assert p["suspect"] not in (p["complainant"], *[w for w, _ in p["witnesses"]])
    talk(tmp_path, DAY + 0.02, npcs=[townsperson(p["witnesses"][0][0])])
    report(tmp_path, DAY + 0.03)
    assert duties(tmp_path)[-1]["state"] == "in_progress"
    store = WorldStore(db_path(tmp_path))
    with store.transaction() as db:
        task = duties(tmp_path)[-1]
        from livingworld.work import tasks as T
        t = T.get(db, task["task_id"])
        d = __import__("livingworld.guard.duty", fromlist=["x"]).duty_for(db, t)
        assert INQ.done(db, t, d) == 0
        assert "has not been arrested" in INQ.missing(db, t, d, "Torvar")


def test_old_seen_incidents_still_work_as_before(tmp_path):
    enlisted(tmp_path)
    old = {"org": "whiterun_guard", "key": "market_theft", "title": "catch the cutpurse working the Plains District market",
           "why": "Anoriath had a purse snatched in the market and wants the thief caught", "instructions": "",
           "where": ["Whiterun"], "need": "seen", "outcome": "arrested", "place": "in the Plains District market",
           "shift_hours": 3, "min_rank": "recruit", "merit": 1.5, "culprit": "a Breton tinker named Jonnet",
           "expires_gt": DAY + 1, "day": int(DAY), "suspect": None, "charge": "theft", "complainant": "Anoriath"}
    store = WorldStore(db_path(tmp_path))
    with store.transaction() as db:
        S.event(db, "incident", [], old, DAY + 0.004, "Whiterun", [])
    talk(tmp_path, DAY + 0.01)
    talk(tmp_path, DAY + 0.02, npcs=[whiterun_guard()])
    report(tmp_path, DAY + 0.03)
    assert duties(tmp_path)[-1]["state"] == "completed"
