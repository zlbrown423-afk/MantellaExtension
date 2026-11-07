"""S1 "Word gets around Riverwood": seeded relationships, claims from what people saw, gossip that spreads
to people who were not there, reports from conversations, opinions in prompts, and children left out.

The game side is simulated with perception records built as the Living World Papyrus writes them;
everything after that is the real code path through Mantella's /mantella route.
"""
import sqlite3

import pytest

from livingworld.harness import actors
from livingworld.harness.perception import KEY_EVENTS, grab, seen
from livingworld.harness.scripted_llm import ScriptedExtraction, ScriptedLLM
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, KEY_LOAD_NONCE, KEY_LOADED_GAME_DAYS, KEY_TIMELINE_ID, LivingWorld
from livingworld.memory import jobs
from livingworld.social import acts, gossip
from livingworld.store.minors import looks_like_child

FOREARM = "NPC L Forearm [LLar]"
PLAYER = actors.player("Torvar")


def sigrid():
    return actors.actor("Sigrid", 0x00013476, 0x00013483, 1, "Nord", "FemaleEvenToned")


def camilla():
    return actors.actor("Camilla Valerius", 0x0001347B, 0x00013488, 1, "Imperial", "FemaleYoungEager")


def frodnar():
    # race and voice as an adult's on purpose: the child must be recognised from Mantella's character list
    return actors.actor("Frodnar", 0x0001347E, 0x0001348B, 0, "Nord", "MaleEvenToned")


@pytest.fixture(autouse=True)
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.setattr(LivingWorld, "start_workers", False)
    monkeypatch.setattr(jobs, "BACKOFF_SECONDS", (0, 0, 0, 0, 0))


def open_db(work_dir) -> sqlite3.Connection:
    conn = sqlite3.connect(work_dir / "data" / "livingworld" / "HeadlessTest.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def talk(work_dir, npcs, day, records=None, llm=None, extra=None, say=None):
    llm = llm or ScriptedLLM(replies=["Hm."])
    with HeadlessSession(str(work_dir), llm) as s:
        s.start([PLAYER, *npcs], game_days=day, extra=extra, context_extra={KEY_EVENTS: records or []})
        s.continue_until_player_turn()
        if say:
            s.say(say)
        s.end(game_days=day + 0.01)
        LivingWorld.latest.wait_for_jobs()
    return llm


def npc_id(db, name):
    return db.execute("SELECT npc_id FROM npc WHERE display_name = ?", (name,)).fetchone()[0]


def grab_gerdur(day=3.5, key="g1"):
    """Torvar grabs Gerdur's left forearm for 4.2 s while her husband Hod watches from 4 m."""
    return grab(PLAYER, actors.gerdur(), FOREARM, 4.2, day, key=key, witnesses=[seen(actors.hod(), 4.0, rank_to_target=4)])


def meet_everyone(work_dir, day=3.0):
    talk(work_dir, [actors.gerdur(), actors.hod(), sigrid()], day)


# ---- seed ----

def test_riverwood_seed_links_hod_and_gerdur_and_their_village(tmp_path):
    talk(tmp_path, [actors.gerdur(), actors.hod()], 1.0)
    db = open_db(tmp_path)
    hod, gerdur = npc_id(db, "Hod"), npc_id(db, "Gerdur")
    row = db.execute("SELECT kinship, status, trust FROM v_relationship WHERE from_id = ? AND to_id = ?", (hod, gerdur)).fetchone()
    assert (row["kinship"], row["status"]) == ("spouse", "spouse") and row["trust"] > 0.5
    households = db.execute("SELECT c.key FROM membership m JOIN community c USING (community_id) WHERE m.npc_id = ?"
                            " ORDER BY c.key", (hod,)).fetchall()
    assert [r[0] for r in households] == ["riverwood", "riverwood_mill"]


# ---- claims and gossip ----

def test_people_who_saw_a_grab_hold_a_claim_they_can_pass_on(tmp_path):
    meet_everyone(tmp_path)
    talk(tmp_path, [actors.gerdur()], 3.5, records=[grab_gerdur()])
    db = open_db(tmp_path)
    claim = db.execute("SELECT * FROM claim").fetchone()
    assert (claim["act"], claim["detail"], claim["subject_id"]) == ("grabbed", " by the left forearm", npc_id(db, "Torvar"))
    holders = {r["holder_id"]: r["origin"] for r in db.execute("SELECT * FROM v_claim_knowledge")}
    assert holders == {npc_id(db, "Gerdur"): "witnessed", npc_id(db, "Hod"): "witnessed"}


def test_news_reaches_someone_who_was_not_there(tmp_path):
    meet_everyone(tmp_path)
    talk(tmp_path, [actors.gerdur()], 3.5, records=[grab_gerdur()])
    llm = talk(tmp_path, [sigrid()], 14.0)
    db = open_db(tmp_path)
    sigrid_id, torvar = npc_id(db, "Sigrid"), npc_id(db, "Torvar")
    heard = db.execute("SELECT * FROM v_claim_knowledge WHERE holder_id = ?", (sigrid_id,)).fetchone()
    assert heard is not None and heard["origin"] == "told"
    assert heard["told_by_id"] in (npc_id(db, "Gerdur"), npc_id(db, "Hod"))
    assert 0 < heard["believed"] <= 0.9                          # hearsay, never as sure as an eyewitness
    assert db.execute("SELECT COUNT(*) FROM world_event WHERE type = 'gossip_exchange'").fetchone()[0] >= 1
    prompt = llm.dialogue_calls[0].system_prompt
    assert "told me that Torvar grabbed Gerdur by the left forearm in Riverwood" in prompt
    assert "I did not see it myself" in prompt
    view = db.execute("SELECT trust, suspicion FROM v_relationship WHERE from_id = ? AND to_id = ?", (sigrid_id, torvar)).fetchone()
    assert view["trust"] < 0 and view["suspicion"] > 0
    assert "How I feel about Torvar right now" in prompt


def test_no_gossip_without_time_passing(tmp_path):
    meet_everyone(tmp_path)
    talk(tmp_path, [actors.gerdur()], 3.5, records=[grab_gerdur()])
    talk(tmp_path, [sigrid()], 3.6)
    db = open_db(tmp_path)
    assert db.execute("SELECT COUNT(*) FROM v_claim_knowledge WHERE holder_id = ?", (npc_id(db, "Sigrid"),)).fetchone()[0] == 0


def test_gossip_is_reproducible(tmp_path):
    for run in ("a", "b"):
        meet_everyone(tmp_path / run)
        talk(tmp_path / run, [actors.gerdur()], 3.5, records=[grab_gerdur()])
        talk(tmp_path / run, [sigrid()], 9.0)
    rows = [open_db(tmp_path / run).execute("SELECT game_time, actors_json FROM world_event WHERE type = 'gossip_exchange'"
                                            " ORDER BY event_id").fetchall() for run in ("a", "b")]
    assert [tuple(r) for r in rows[0]] == [tuple(r) for r in rows[1]]


def test_gossip_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTELLA_LW_GOSSIP", "0")
    meet_everyone(tmp_path)
    talk(tmp_path, [actors.gerdur()], 3.5, records=[grab_gerdur()])
    talk(tmp_path, [sigrid()], 14.0)
    assert open_db(tmp_path).execute("SELECT COUNT(*) FROM world_event WHERE type = 'gossip_exchange'").fetchone()[0] == 0


def test_loading_an_older_save_takes_back_the_gossip(tmp_path):
    meet_everyone(tmp_path)
    talk(tmp_path, [actors.gerdur()], 3.5, records=[grab_gerdur()], extra={KEY_TIMELINE_ID: 0, KEY_LOAD_NONCE: 1,
                                                                          KEY_LOADED_GAME_DAYS: 3.4})
    talk(tmp_path, [sigrid()], 14.0, extra={KEY_TIMELINE_ID: 1, KEY_LOAD_NONCE: 2, KEY_LOADED_GAME_DAYS: 13.9})
    db = open_db(tmp_path)
    sigrid_id = npc_id(db, "Sigrid")
    assert db.execute("SELECT COUNT(*) FROM v_claim_knowledge WHERE holder_id = ?", (sigrid_id,)).fetchone()[0] == 1
    # back to a save from day 3.45, before the grab: nobody knows anything about it any more
    talk(tmp_path, [sigrid()], 3.45, extra={KEY_TIMELINE_ID: 1, KEY_LOAD_NONCE: 3, KEY_LOADED_GAME_DAYS: 3.45})
    db = open_db(tmp_path)
    assert db.execute("SELECT COUNT(*) FROM v_claim_knowledge").fetchone()[0] == 0
    view = db.execute("SELECT trust FROM v_relationship WHERE from_id = ? AND to_id = ?",
                      (sigrid_id, npc_id(db, "Torvar"))).fetchone()
    assert view is None or view["trust"] == 0


# ---- reports in conversation ----

def test_what_the_player_tells_hod_becomes_news_hod_can_repeat(tmp_path):
    talk(tmp_path, [actors.gerdur(), actors.hod()], 1.0)
    report = {"about": "Torvar", "did": "helped Gerdur fix the mill wheel", "valence": 0.6, "phrase": "mill wheel"}
    llm = ScriptedLLM(replies=["Is that so?"], extractions={"Hod": ScriptedExtraction(reports=[report])})
    talk(tmp_path, [actors.hod()], 2.0, llm=llm, say="I helped Gerdur fix the mill wheel yesterday.")
    db = open_db(tmp_path)
    hod, torvar = npc_id(db, "Hod"), npc_id(db, "Torvar")
    copy = db.execute("SELECT * FROM v_claim_knowledge WHERE holder_id = ?", (hod,)).fetchone()
    assert (copy["act"], copy["told_by_id"], copy["subject_id"]) == ("reported", torvar, torvar)
    assert copy["source_utt_id"] is not None
    view = db.execute("SELECT affinity, admiration FROM v_relationship WHERE from_id = ? AND to_id = ?", (hod, torvar)).fetchone()
    assert view["affinity"] > 0 and view["admiration"] > 0
    prompt = talk(tmp_path, [actors.hod()], 2.5).dialogue_calls[0].system_prompt
    assert "Torvar told me that they helped Gerdur fix the mill wheel" in prompt


def test_a_report_on_a_line_nobody_said_is_dropped(tmp_path):
    talk(tmp_path, [actors.hod()], 1.0)
    report = {"about": "Torvar", "did": "stole a horse", "valence": -0.8, "said_by": "Torvar", "line": 99}
    llm = ScriptedLLM(replies=["Hm."], extractions={"Hod": ScriptedExtraction(reports=[report])})
    talk(tmp_path, [actors.hod()], 2.0, llm=llm, say="Nice day.")
    assert open_db(tmp_path).execute("SELECT COUNT(*) FROM claim").fetchone()[0] == 0


# ---- children are left out ----

def test_children_are_recognised_from_mantellas_character_list():
    assert looks_like_child("0x0001347E")                   # Frodnar
    assert looks_like_child(race="NordRaceChild")
    assert looks_like_child(voice="FemaleChild")
    assert not looks_like_child("0x0001347D")               # Hod


def test_a_child_is_never_grabbed_witness_seeded_or_told_anything(tmp_path):
    talk(tmp_path, [actors.gerdur(), actors.hod(), frodnar()], 3.0)
    child_grab = grab(PLAYER, frodnar(), FOREARM, 2.0, 3.5, key="c1", witnesses=[seen(actors.gerdur(), 3.0)])
    child_watching = grab(PLAYER, actors.gerdur(), FOREARM, 2.0, 3.6, key="c2", witnesses=[seen(frodnar(), 2.0),
                                                                                         seen(actors.hod(), 3.0)])
    talk(tmp_path, [actors.gerdur()], 3.7, records=[child_grab, child_watching])
    talk(tmp_path, [actors.hod()], 20.0)
    db = open_db(tmp_path)
    child = npc_id(db, "Frodnar")
    assert db.execute("SELECT is_child FROM npc WHERE npc_id = ?", (child,)).fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM world_event WHERE game_key = 'c1'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM event_witness WHERE npc_id = ?", (child,)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM membership WHERE npc_id = ?", (child,)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM knowledge WHERE holder_id = ? OR subject_id = ?", (child, child)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM memory WHERE owner_id = ?", (child,)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM relationship WHERE from_id = ? OR to_id = ?", (child, child)).fetchone()[0] == 0


# ---- rules ----

def test_hearing_about_harm_to_someone_close_hits_harder():
    far = acts.listener_deltas("grabbed", -0.6, 0.4, 0.8, close_to_object=False)
    near = acts.listener_deltas("grabbed", -0.6, 0.4, 0.8, close_to_object=True)
    assert near["trust"] < far["trust"] < 0


def test_long_absence_is_covered_in_a_bounded_number_of_steps(tmp_path):
    meet_everyone(tmp_path)
    talk(tmp_path, [actors.gerdur()], 3.5, records=[grab_gerdur()])
    from livingworld.store.world_store import WorldStore
    store = WorldStore(str(tmp_path / "data" / "livingworld" / "HeadlessTest.sqlite"))
    with store.transaction() as db:
        result = gossip.catch_up(db, "riverwood", 400.0)
    assert result.steps <= gossip.MAX_STEPS


def test_riverwood_seed_data_is_consistent():
    """Every name and community exists, no child is seeded, each pair has both directions once, and
    kinship read from each side matches (Hilde is Sven's parent, Sven is Hilde's child)."""
    from livingworld.social.seed import seeds, _local_id
    from livingworld.store.minors import looks_like_child
    inverse = {"spouse": {"spouse"}, "sibling": {"sibling"}, "parent": {"child"}, "child": {"parent"},
               "nephew": {"uncle", "aunt"}, "uncle": {"nephew"}, "aunt": {"nephew"}}
    for seed in seeds():
        communities = {c["key"] for c in seed["communities"]}
        people = {p["key"] for p in seed["people"]}
        for p in seed["people"]:
            assert not looks_like_child(_local_id(p["base_id"])), p["key"]
            assert set(p["member_of"]) <= communities, p["key"]
        rows = {}
        for r in seed["relationships"]:
            assert r["a"] in people and r["b"] in people
            back = r.get("back") or {}
            directions = [(r["a"], r["b"], r.get("kinship"))]
            if not r.get("one_way"):
                directions.append((r["b"], r["a"], back.get("kinship", r.get("kinship"))))
            for a, b, kinship in directions:
                assert (a, b) not in rows, f"{a}->{b} written twice"
                rows[(a, b)] = kinship
        for (a, b), kinship in rows.items():
            assert (b, a) in rows, f"{a}->{b} has no {b}->{a}"
            if kinship:
                assert rows[(b, a)] in inverse[kinship], (a, b, kinship, rows[(b, a)])
