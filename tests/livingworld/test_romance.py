"""R1 romance off screen: bonds, consent, privacy, flirting and jealousy, pace, timelines, and children left out.

These drive the server-side simulation directly on a fresh world database with the Riverwood seed;
nothing here needs the game.
"""
import json

import pytest

from livingworld.perception import intoxication
from livingworld.social import consent, gossip, processor, romance
from livingworld.social.seed import Seeder, seeds
from livingworld.store.minors import mark_children
from livingworld.store.world_store import WorldStore


@pytest.fixture(autouse=True)
def romance_on(monkeypatch):
    monkeypatch.setenv(romance.ROMANCE_ENV, "1")


def world(with_child=True) -> WorldStore:
    store = WorldStore(":memory:")
    seed = seeds()[0]
    with store.transaction() as db:
        for p in seed["people"]:
            db.execute("INSERT INTO npc (plugin, base_form_id, display_name, sex, created_gt) VALUES (?,?,?,?,0)",
                       (p["plugin"], "0x" + p["base_id"].upper().rjust(8, "0"), p["name"],
                        seed["romance"].get(p["key"], {}).get("sex")))
        if with_child:   # Frodnar, Hod and Gerdur's son
            db.execute("INSERT INTO npc (plugin, base_form_id, display_name, sex, created_gt)"
                       " VALUES ('Skyrim.esm', '0x0001347E', 'Frodnar', 'male', 0)")
        mark_children(db)
        Seeder().apply(db, 1.0)
        romance.catch_up(db, "riverwood", 1.0)   # first contact: seeded spouses start out married
    return store


def run(store, until, step=1.0, start=None):
    t = start if start is not None else 2.0
    while t <= until:
        with store.transaction() as db:
            gossip.catch_up(db, "riverwood", t)
            romance.catch_up(db, "riverwood", t)
            processor.process(db)
        t += step


def nid(store, name):
    return store.scalar("SELECT npc_id FROM npc WHERE display_name = ?", (name,))


def history(store):
    return [tuple(r) for r in store.query(
        "SELECT a_id, b_id, stage_from, stage_to, reason, round(game_time, 4) FROM romance_bond_change ORDER BY change_id")]


def push(store, a, b, deltas, gt=1.5):
    with store.transaction() as db:
        romance._delta(db, 1, a, b, deltas, "test", gt)


# ---- seed ----

def test_seeded_spouses_start_married_and_places_are_seeded():
    s = world()
    hod, gerdur = nid(s, "Hod"), nid(s, "Gerdur")
    with s.reading() as db:
        assert romance.bond(db, hod, gerdur)["stage"] == "married"
        assert romance.bond(db, nid(s, "Alvor"), nid(s, "Sigrid"))["stage"] == "married"
        t_hod, t_gerdur = romance.traits(db, hod), romance.traits(db, gerdur)
        assert t_hod["flirtatious"] > 0.8 and t_hod["jealous"] < 0.2      # Hod: curious, flirts, rarely jealous
        assert t_gerdur["monogamy"] < 0.4                                 # Gerdur: likeliest in Riverwood to stray
        mill = db.execute("SELECT * FROM place WHERE place_key = 'riverwood_mill_house'").fetchone()
        assert romance.place_privacy(db, mill, hod, gerdur) == pytest.approx(0.75)   # Frodnar lives there too
        assert romance.place_privacy(db, mill, nid(s, "Sven"), nid(s, "Camilla Valerius")) == 0.0   # not their home
        upstairs = db.execute("SELECT * FROM place WHERE place_key = 'riverwood_trader_upstairs'").fetchone()
        assert romance.place_privacy(db, upstairs, nid(s, "Camilla Valerius"), nid(s, "Faendal")) < romance.PRIVATE_ENOUGH


# ---- consent ----

def ask(store, who, other, kind="intimate", stage="married", privacy=0.9, now=2.0, **kw):
    with store.transaction() as db:
        return consent.decide(db, who, other, kind, "invite", romance.traits(db, who), stage, now,
                              place_privacy=privacy, **kw)


def test_attraction_is_not_consent():
    s = world()
    camilla, faendal = nid(s, "Camilla Valerius"), nid(s, "Faendal")
    push(s, camilla, faendal, {"attraction": 1.0, "intimacy": 1.0, "trust": 1.0, "satisfaction": 1.0})
    answer = ask(s, camilla, faendal, stage="courting")       # Camilla's boundary is committed_only
    assert answer.answer == "no" and answer.reasons["why"] == "boundary"
    logged = s.query("SELECT answer, reasons_json FROM consent_decision")
    assert logged[-1][0] == "no" and json.loads(logged[-1][1])["why"] == "boundary"


def test_marriage_is_not_consent_and_a_no_costs_her_nothing():
    s = world()
    hod, gerdur = nid(s, "Hod"), nid(s, "Gerdur")
    with s.transaction() as db:
        db.execute("INSERT INTO emotion_state (npc_id, mood_json, baseline_json, updated_gt)"
                   " VALUES (?, '{\"label\": \"miserable\", \"score\": 1}', '{}', 2.0)", (gerdur,))
    before = s.scalar("SELECT COUNT(*) FROM relationship_change WHERE from_id = ?", (gerdur,))
    answer = ask(s, gerdur, hod)
    assert answer.answer == "no" and answer.reasons["why"] == "mood_too_low"
    assert s.scalar("SELECT COUNT(*) FROM relationship_change WHERE from_id = ?", (gerdur,)) == before


def test_drink_never_helps_and_heavy_drink_rules_it_out(monkeypatch):
    s = world()
    hod, gerdur = nid(s, "Hod"), nid(s, "Gerdur")
    wishes = {}
    for stage in (None, "merry", "tipsy", "drunk", "very drunk"):
        monkeypatch.setattr(intoxication, "drink_state", lambda db, npc, now, st=stage: intoxication.DrinkState(0, 0, st))
        answer = ask(s, gerdur, hod)
        wishes[stage] = answer
    assert wishes[None].yes
    assert wishes["merry"].reasons["wish"] == wishes["tipsy"].reasons["wish"] == wishes[None].reasons["wish"]
    assert wishes["drunk"].reasons["why"] == wishes["very drunk"].reasons["why"] == "too_drunk_to_consent"


def test_gifts_gold_and_debts_change_nothing():
    s = world()
    hod, gerdur = nid(s, "Hod"), nid(s, "Gerdur")
    plain = ask(s, gerdur, hod, log=False)
    with s.transaction() as db:
        db.execute("INSERT INTO obligation (kind, debtor_id, creditor_id, description, created_gt)"
                   " VALUES ('debt', ?, ?, 'owes him 500 gold', 1.0)", (gerdur, hod))
        db.execute("INSERT INTO world_event (type, actors_json, payload_json, source, game_time)"
                   " VALUES ('trade', ?, '{\"gift\": true, \"gold\": 500}', 'game', 1.5)", (json.dumps([hod, gerdur]),))
    pressured = ask(s, gerdur, hod, log=False)
    assert pressured.answer == plain.answer and pressured.reasons == plain.reasons


def test_not_private_enough_is_a_no():
    s = world()
    answer = ask(s, nid(s, "Gerdur"), nid(s, "Hod"), privacy=0.5)
    assert answer.reasons["why"] == "not_private"


# ---- children ----

def test_children_never_take_part():
    s = world()
    frodnar = nid(s, "Frodnar")
    run(s, 60)
    assert s.scalar("SELECT is_child FROM npc WHERE npc_id = ?", (frodnar,)) == 1
    for table, cols in (("romance_bond_change", ("a_id", "b_id")), ("encounter", ("a_id", "b_id")),
                        ("consent_decision", ("npc_id", "other_id"))):
        assert s.scalar(f"SELECT COUNT(*) FROM {table} WHERE {cols[0]} = ? OR {cols[1]} = ?", (frodnar, frodnar)) == 0
    assert s.scalar("SELECT COUNT(*) FROM world_event WHERE type LIKE 'romance.%' AND actors_json LIKE ?",
                    (f"%{frodnar}%",)) == 0
    with s.transaction() as db:
        answer = consent.decide(db, nid(s, "Hod"), frodnar, "date", "invite", romance.traits(db, nid(s, "Hod")), None, 60)
    assert answer.reasons["why"] == "child"


# ---- the simulation ----

def test_same_save_tells_the_same_story():
    a, b = world(), world()
    run(a, 45)
    run(b, 45)
    assert history(a) == history(b)
    assert [tuple(r) for r in a.query("SELECT a_id, b_id, kind, outcome, game_time FROM encounter ORDER BY encounter_id")] == \
           [tuple(r) for r in b.query("SELECT a_id, b_id, kind, outcome, game_time FROM encounter ORDER BY encounter_id")]


def test_off_screen_is_honest_and_encounters_never_raise_attraction_or_trust():
    s = world()
    run(s, 60)
    assert s.scalar("SELECT COUNT(*) FROM encounter WHERE kind = 'intimate' AND outcome = 'completed'") > 0
    assert s.query("SELECT DISTINCT mode FROM encounter")[0][0] == "offscreen"
    for (deltas,) in s.query("SELECT deltas_json FROM relationship_change WHERE reason IN"
                             " ('a night together', 'an evening together', 'interrupted')"):
        assert not {"attraction", "trust"} & set(json.loads(deltas))
    texts = [r[0] for r in s.query("SELECT content FROM memory")]
    assert texts and not any("animation" in t.lower() for t in texts)
    # every completed intimate encounter had a yes from both people at every phase
    for (enc, a, b) in s.query("SELECT encounter_id, a_id, b_id FROM encounter WHERE kind = 'intimate' AND outcome = 'completed'"):
        answers = {(r[0], r[1]): r[2] for r in s.query("SELECT npc_id, phase, answer FROM consent_decision"
                                                       " WHERE encounter_id = ?", (enc,))}
        assert all(answers[(p, ph)] == "yes" for p in (a, b) for ph in ("invite", "arrival", "start"))


def test_pace_stays_human():
    s = world()
    run(s, 150)
    changes = s.query("SELECT a_id, b_id, stage_to, game_time FROM romance_bond_change WHERE reason <> 'seed'"
                      " ORDER BY game_time")
    new = [c[3] for c in changes if c[2] in ("courting", "affair")]
    assert all(b - a >= romance.COMMUNITY_NEW_BOND_GAP - 1e-6 for a, b in zip(new, new[1:]))
    nights = s.query("SELECT a_id, b_id, CAST(game_time AS INTEGER) d, COUNT(*) FROM encounter"
                     " WHERE kind = 'intimate' AND outcome = 'completed' GROUP BY 1, 2, 3 HAVING COUNT(*) > 1")
    assert nights == []
    married = s.scalar("SELECT COUNT(*) FROM v_romance_bond WHERE stage = 'married'")
    assert married >= 1   # marriages do not fall apart over ordinary weeks


def test_hod_flirts_and_gerdur_seeing_it_hurts():
    s = world()
    run(s, 120)
    hod, gerdur = nid(s, "Hod"), nid(s, "Gerdur")
    flirts = s.query("SELECT event_id FROM world_event WHERE type = 'romance.flirt' AND json_extract(actors_json, '$[0]') = ?",
                     (hod,))
    assert flirts
    seen = s.scalar("SELECT COUNT(*) FROM world_event e JOIN event_witness w ON w.event_id = e.event_id"
                    " WHERE e.type = 'romance.flirt' AND json_extract(e.actors_json, '$[0]') = ? AND w.npc_id = ?",
                    (hod, gerdur))
    hurt = s.query("SELECT deltas_json FROM relationship_change WHERE from_id = ? AND to_id = ?"
                   " AND reason LIKE 'learned Hod flirted%'", (gerdur, hod))
    assert seen + s.scalar("SELECT COUNT(*) FROM knowledge k JOIN claim c ON c.claim_id = k.claim_id"
                           " WHERE k.holder_id = ? AND c.act = 'flirted' AND c.subject_id = ? AND k.origin = 'told'",
                           (gerdur, hod)) >= 1
    assert hurt and all(json.loads(d)["jealousy"] > 0 and json.loads(d)["satisfaction"] < 0 for (d,) in hurt)


def test_a_hurt_gerdur_may_stray_but_only_with_both_yes():
    s = world()
    hod, gerdur, faendal = nid(s, "Hod"), nid(s, "Gerdur"), nid(s, "Faendal")
    # Gerdur has watched Hod flirt for weeks; she and Faendal are drawn to each other
    push(s, gerdur, hod, {"jealousy": 0.9, "satisfaction": -0.4})
    push(s, gerdur, faendal, {"attraction": 0.8})
    push(s, faendal, gerdur, {"attraction": 0.8})
    with s.reading() as db:
        ts = {n: romance.traits(db, n) for n in (gerdur, hod, faendal)}
        assert romance.interest(db, gerdur, faendal, ts[gerdur], ts[faendal]) >= romance.AFFAIR_INTEREST
        # Hod, unhurt and committed, does not feel free the same way
        assert romance.freedom(db, hod, ts[hod], faendal) < romance.freedom(db, gerdur, ts[gerdur], faendal)
    with s.transaction() as db:
        romance._ask(db, 1, None, gerdur, faendal, {gerdur: ts[gerdur], faendal: ts[faendal]}, 2.0, affair=True)
        b = romance.bond(db, gerdur, faendal)
    assert b["stage"] == "affair" and b["secret"] == 1
    assert s.scalar("SELECT status FROM relationship WHERE from_id = ? AND to_id = ?", (hod, gerdur)) == "spouse"
    # Faendal not interested: he says no, and nothing starts
    s2 = world()
    g2, f2 = nid(s2, "Gerdur"), nid(s2, "Faendal")
    push(s2, g2, nid(s2, "Hod"), {"jealousy": 0.9, "satisfaction": -0.4})
    push(s2, g2, f2, {"attraction": 0.8})
    with s2.transaction() as db:
        ts2 = {n: romance.traits(db, n) for n in (g2, f2)}
        romance._ask(db, 1, None, g2, f2, ts2, 2.0, affair=True)
        assert romance.bond(db, g2, f2) is None
    assert s2.scalar("SELECT COUNT(*) FROM world_event WHERE type = 'romance.declined'") == 1


def test_a_night_at_the_inn_can_be_noticed_and_gossiped():
    s = world()
    gerdur, faendal, hod = nid(s, "Gerdur"), nid(s, "Faendal"), nid(s, "Hod")
    with s.transaction() as db:
        romance.set_bond(db, 1, gerdur, faendal, "affair", "affair", 2.0, "Riverwood", secret=True)
        inn = db.execute("SELECT * FROM place WHERE place_key = 'sleeping_giant_room'").fetchone()
        event_id = romance._event(db, 1, "romance.encounter", "Riverwood", [gerdur, faendal],
                                  {"kind": "intimate", "place": inn["place_key"], "place_name": inn["name"],
                                   "mode": "offscreen", "secret": True},
                                  2.9, {gerdur: "participated", faendal: "participated", nid(s, "Orgnar"): "saw"})
        processor.process(db)
        claim = db.execute("SELECT * FROM claim WHERE truth_event_id = ?", (event_id,)).fetchone()
    assert claim["act"] == "spent_night" and claim["sensitivity"] == "private"
    holders = {r[0]: r[1] for r in s.query("SELECT holder_id, will_share FROM knowledge WHERE claim_id = ?", (claim["claim_id"],))}
    assert holders[nid(s, "Orgnar")] == "yes" and holders[faendal] == "no" and gerdur not in holders
    # when Hod hears it, it hits him: jealousy, trust and satisfaction
    with s.transaction() as db:
        row = dict(db.execute("SELECT * FROM v_claim_knowledge WHERE claim_id = ? AND holder_id = ?",
                              (claim["claim_id"], nid(s, "Orgnar"))).fetchone())
        from livingworld.social import claims
        claims.hear(db, 1, hod, nid(s, "Orgnar"), row, row["know_id"], 0.9, 0.6, 3.5)
    r = s.query("SELECT jealousy, trust, satisfaction FROM v_relationship WHERE from_id = ? AND to_id = ?", (hod, gerdur))[0]
    assert r[0] > 0 and r[1] < 0.8 and r[2] < 0.3


def test_loading_an_older_save_rolls_romance_back():
    s = world()
    run(s, 60)
    cut = 20.0
    assert s.scalar("SELECT COUNT(*) FROM encounter WHERE game_time > ?", (cut,)) > 0
    with s.transaction() as db:
        db.execute("UPDATE timeline SET is_active = 0")
        db.execute("INSERT INTO timeline (timeline_id, parent_id, branch_game_time, save_name, is_active)"
                   " VALUES (2, 1, ?, 'older', 1)", (cut,))
    assert s.scalar("SELECT COUNT(*) FROM v_encounter WHERE game_time > ?", (cut,)) == 0
    with s.reading() as db:
        visible = db.execute("SELECT COUNT(*) FROM v_romance_bond WHERE since_gt > ?", (cut,)).fetchone()[0]
    assert visible == 0


def test_whoever_was_there_can_tell_the_player_about_a_flirt():
    """2026-11-08: the player was told Riverwood was talking about Hod; Hilde, the only one who knew (Hod had
    flirted with her), had nothing about it in her prompt, so asking her led nowhere."""
    from livingworld.memory.recall import recall_for
    s = world()
    hod, hilde, sven = nid(s, "Hod"), nid(s, "Hilde"), nid(s, "Sven")
    with s.transaction() as db:
        romance._event(db, 1, "romance.flirt", "Riverwood", [hod, hilde], {"returned": False}, 13.8,
                       {hod: "participated", hilde: "participated", sven: "saw"})
        processor.process(db)
    said = recall_for(s, hilde, 14.8, mark_rehearsed=False).text()
    assert "(yesterday, it happened to me) Hod flirted with me in Riverwood. I did not return it." in said, said
    assert "(yesterday, I saw it myself) Hod flirted with Hilde in Riverwood." in \
        recall_for(s, sven, 14.8, mark_rehearsed=False).text()
    # a month and more later it has faded from the prompt
    assert "flirted" not in recall_for(s, hilde, 50.0, mark_rehearsed=False).text()


def test_recall_lines_speak_in_their_own_words():
    s = world()
    gerdur, hod, faendal = nid(s, "Gerdur"), nid(s, "Hod"), nid(s, "Faendal")
    push(s, gerdur, hod, {"jealousy": 0.5})
    with s.transaction() as db:
        romance.set_bond(db, 1, gerdur, faendal, "affair", "affair", 2.0, "Riverwood", secret=True)
        lines = romance.recall_lines(db, gerdur, 3.0)
    assert any(line.startswith("Hod is my spouse") and "jealous" in line for line in lines)
    assert any("secretly seeing Faendal" in line for line in lines)
    with s.reading() as db:
        assert not any("Faendal" in line for line in romance.recall_lines(db, hod, 3.0))


def test_romance_is_off_unless_switched_on(monkeypatch):
    monkeypatch.delenv(romance.ROMANCE_ENV, raising=False)
    assert not romance.romance_enabled()


def test_a_day_of_catch_up_fits_the_conversation_budget():
    s = world()
    run(s, 30)
    with s.transaction() as db:
        result = romance.catch_up(db, "riverwood", 31.0, budget_ms=150.0)
    assert result.steps >= 1 and result.ms < 1000   # generous for slow CI; in practice tens of milliseconds


def test_in_conversation_gerdur_knows_she_is_married(tmp_path, monkeypatch):
    """Through Mantella's real conversation path: the romance catch-up runs and her own bond reaches her prompt."""
    from livingworld.harness import actors
    from livingworld.integration import ENV_VAR, LivingWorld
    from livingworld.memory import jobs
    from tests.livingworld import test_social
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.setattr(LivingWorld, "start_workers", False)
    monkeypatch.setattr(jobs, "BACKOFF_SECONDS", (0, 0, 0, 0, 0))
    test_social.talk(tmp_path, [actors.gerdur(), actors.hod()], 1.0)
    llm = test_social.talk(tmp_path, [actors.gerdur()], 3.0)
    assert "Hod is my spouse." in llm.dialogue_calls[0].system_prompt
    db = test_social.open_db(tmp_path)
    assert db.execute("SELECT COUNT(*) FROM romance_bond_change WHERE stage_to = 'married'").fetchone()[0] == 1
