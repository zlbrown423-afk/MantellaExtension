"""The townsperson named as having reported a theft or brawl to the guard remembers doing so.

Before, the complainant was only a name inside the incident's 'why': the guard sent to the cutpurse asked
Carlotta (or Fralia, Anoriath, Brenuin) about it and they knew nothing of any complaint.
"""
import json

from livingworld.guard import incident as INC, orgs as O, store as S
from livingworld.harness import actors
from livingworld.store.world_store import WorldStore

from tests.livingworld.test_guard import DAY, db_path, living_world_inline, npc_id, open_db  # noqa: F401
from tests.livingworld.test_guard_duty import duties, enlisted, talk, whiterun_guard

WHITERUN = O.by_key("whiterun_guard")


def townsperson(name):
    return actors.actor(name, 0x0001B000 + len(name), 0x0001C000 + len(name), 1, "Imperial", "FemaleEvenToned")


def the_incident(work_dir):
    row = open_db(work_dir).execute("SELECT event_id, payload_json FROM world_event WHERE type = 'guard.incident'"
                                    " ORDER BY event_id DESC LIMIT 1").fetchone()
    return row[0], json.loads(row[1])


def memories(work_dir, name):
    db = open_db(work_dir)
    return [r[0] for r in db.execute("SELECT content FROM memory WHERE owner_id = ?", (npc_id(db, name),))]


def test_the_theft_names_a_complainant_with_their_own_account():
    k = next(k for k in INC.kinds("whiterun_guard") if k.key == "market_theft")
    assert k.complainants and k.complaint and len(k.culprit_looks) == len(k.culprits) and k.losses


def test_the_complainant_remembers_reporting_the_theft_to_the_guard_sent(tmp_path):
    enlisted(tmp_path)
    talk(tmp_path, DAY + 0.005, say="debug guard incident theft", npcs=[whiterun_guard()], location="Riverwood")
    _, p = the_incident(tmp_path)
    who = p["complainant"]
    assert who in ("Carlotta Valentia", "Fralia Gray-Mane", "Anoriath", "Brenuin")
    assert p["complaint"].startswith(who) and "septims" in p["complaint"]
    talk(tmp_path, DAY + 0.01)                                            # Caius sends Torvar to it
    assert duties(tmp_path)[-1]["template"].startswith("duty:incident:market_theft:")
    prompt = talk(tmp_path, DAY + 0.02, npcs=[townsperson(who)])
    assert f"{who} went to the Whiterun Guard" in prompt
    assert "Torvar is the guard sent to deal with it" in prompt
    if p["look"]:
        assert p["look"] in prompt and "does not know their name" in prompt
    mems = memories(tmp_path, who)
    mine = [m for m in mems if "I went to the guard to get the thief caught" in m]
    assert len(mine) == 1 and who not in mine[0]                          # in their own words
    talk(tmp_path, DAY + 0.025, npcs=[townsperson(who)])                  # the memory is written once
    assert len([m for m in memories(tmp_path, who) if "I went to the guard" in m]) == 1


def test_someone_already_known_remembers_from_the_moment_it_is_reported(tmp_path):
    enlisted(tmp_path)
    for who in ("Carlotta Valentia", "Fralia Gray-Mane", "Anoriath", "Brenuin"):
        talk(tmp_path, DAY + 0.002, npcs=[townsperson(who)])             # all met before
    talk(tmp_path, DAY + 0.005, say="debug guard incident theft", npcs=[whiterun_guard()], location="Riverwood")
    event_id, p = the_incident(tmp_path)
    db = open_db(tmp_path)
    owner = npc_id(db, p["complainant"])
    assert db.execute("SELECT COUNT(*) FROM memory WHERE owner_id = ? AND event_id = ?", (owner, event_id)).fetchone()[0] == 1
    assert db.execute("SELECT how FROM event_witness WHERE event_id = ? AND npc_id = ?", (event_id, owner)).fetchone()[0] \
        == "participated"


def test_after_the_arrest_the_complainant_thanks_the_guard_who_did_it(tmp_path):
    enlisted(tmp_path)
    talk(tmp_path, DAY + 0.005, say="debug guard incident theft", npcs=[whiterun_guard()], location="Riverwood")
    _, p = the_incident(tmp_path)
    talk(tmp_path, DAY + 0.01)
    talk(tmp_path, DAY + 0.02, npcs=[whiterun_guard()])
    talk(tmp_path, DAY + 0.03, say="Reporting back, I caught the thief.")
    assert duties(tmp_path)[-1]["state"] == "completed"
    prompt = talk(tmp_path, DAY + 0.05, npcs=[townsperson(p["complainant"])])
    assert "the culprit was caught" in prompt and "it was Torvar who did it" in prompt


def test_nobody_else_gets_the_complaint(tmp_path):
    enlisted(tmp_path)
    talk(tmp_path, DAY + 0.005, say="debug guard incident theft", npcs=[whiterun_guard()], location="Riverwood")
    _, p = the_incident(tmp_path)
    other = next(n for n in ("Carlotta Valentia", "Anoriath") if n != p["complainant"])
    assert "went to the Whiterun Guard" not in talk(tmp_path, DAY + 0.02, npcs=[townsperson(other)])


def test_incidents_opened_before_this_still_know_their_complainant(tmp_path):
    enlisted(tmp_path)
    old = {"org": "whiterun_guard", "key": "market_theft", "title": "catch the cutpurse working the Plains District market",
           "why": "Anoriath had a purse snatched in the market and wants the thief caught", "instructions": "",
           "where": ["Whiterun"], "need": "seen", "outcome": "arrested", "place": "in the Plains District market",
           "shift_hours": 3, "min_rank": "recruit", "merit": 1.5, "culprit": "a Breton tinker named Jonnet",
           "expires_gt": DAY + 1, "day": int(DAY), "suspect": None, "charge": "theft"}     # no complainant field
    store = WorldStore(db_path(tmp_path))
    with store.transaction() as db:
        S.event(db, "incident", [], old, DAY + 0.004, "Whiterun", [])
    prompt = talk(tmp_path, DAY + 0.02, npcs=[townsperson("Anoriath")])
    assert "Anoriath went to the Whiterun Guard" in prompt and "septims" not in prompt.split("Anoriath went")[1][:300]
    assert any("cut away my purse, so I went to the guard" in m for m in memories(tmp_path, "Anoriath"))
