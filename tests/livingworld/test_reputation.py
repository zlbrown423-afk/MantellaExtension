"""S2 reputation layers: gifts, thefts and blows from the game; standing by domain; opinions that form at a
threshold; community and faction views; retellings that grow and witnesses who set them straight; traits
from bios; and hints the player overhears at the chosen information level.

The game side is simulated with perception records built as the Living World Papyrus writes them.
"""
import random
import sqlite3

import pytest

from livingworld.harness import actors
from livingworld.harness.perception import KEY_EVENTS, gift, hit, seen, theft
from livingworld.harness.scripted_llm import ScriptedLLM
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, KEY_HINTS, LivingWorld
from livingworld.memory import jobs
from livingworld.perception.records import parse_record
from livingworld.social import gossip, reputation, traits
from livingworld.store.world_store import WorldStore

PLAYER = actors.player("Torvar")


def sigrid():
    return actors.actor("Sigrid", 0x00013476, 0x00013483, 1, "Nord", "FemaleEvenToned")


def alvor():
    return actors.actor("Alvor", 0x00013475, 0x00013482, 0, "Nord", "MaleEvenToned")


def lucan():
    return actors.actor("Lucan Valerius", 0x0001347A, 0x00013487, 0, "Imperial", "MaleEvenToned")


def camilla():
    return actors.actor("Camilla Valerius", 0x0001347B, 0x00013488, 1, "Imperial", "FemaleYoungEager")


@pytest.fixture(autouse=True)
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.setattr(LivingWorld, "start_workers", False)
    monkeypatch.setattr(jobs, "BACKOFF_SECONDS", (0, 0, 0, 0, 0))


def open_db(work_dir) -> sqlite3.Connection:
    conn = sqlite3.connect(work_dir / "data" / "livingworld" / "HeadlessTest.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


class Spy:
    """Collects the extra keys the server sends the game with NPC lines."""
    def __init__(self):
        self.keys = []


def talk(work_dir, npcs, day, records=None, llm=None, spy=None):
    llm = llm or ScriptedLLM(replies=["Hm."])
    with HeadlessSession(str(work_dir), llm) as s:
        s.start([PLAYER, *npcs], game_days=day, context_extra={KEY_EVENTS: records or []})
        lw = LivingWorld.latest
        if spy is not None:
            original = lw.line_reply_keys

            def capture(sentence):
                keys = original(sentence)
                spy.keys.append(dict(keys))
                return keys
            lw.line_reply_keys = capture
        s.continue_until_player_turn()
        s.end(game_days=day + 0.01)
        LivingWorld.latest.wait_for_jobs()
    return llm


def npc_id(db, name):
    return db.execute("SELECT npc_id FROM npc WHERE display_name = ?", (name,)).fetchone()[0]


def standing(db, holder, subject, domain):
    row = db.execute("SELECT value FROM v_standing WHERE holder_id = ? AND subject_id = ? AND domain = ?",
                     (npc_id(db, holder), npc_id(db, subject), domain)).fetchone()
    return row[0] if row else 0.0


def meet_everyone(work_dir, day=3.0):
    talk(work_dir, [actors.gerdur(), actors.hod(), sigrid(), alvor()], day)


# ---- records ----

def test_the_new_game_records_parse():
    g = parse_record(gift(PLAYER, sigrid(), "Sweetroll", 3.0, quantity=2, gold=16))
    assert (g.type, g.item, g.quantity, g.gold, g.via) == ("gift", "Sweetroll", 2, 16, "gift_menu")
    t = parse_record(theft(PLAYER, "Silver Goblet", 3.0, owner="Lucan Valerius", gold=30))
    assert (t.type, t.owner, t.target, t.via) == ("theft", "Lucan Valerius", None, "take")
    h = parse_record(hit(PLAYER, actors.hod(), 3.0, weapon="Iron Sword"))
    assert (h.type, h.item, h.target.name) == ("hit", "Iron Sword", "Hod")


# ---- standing and beliefs ----

def test_a_gift_makes_the_receiver_and_onlookers_think_the_player_generous(tmp_path):
    meet_everyone(tmp_path)
    rec = gift(PLAYER, sigrid(), "Gold Ring", 3.5, gold=150, witnesses=[seen(alvor(), 3.0)])
    talk(tmp_path, [sigrid()], 3.5, records=[rec])
    db = open_db(tmp_path)
    assert standing(db, "Sigrid", "Torvar", "generosity") > standing(db, "Alvor", "Torvar", "generosity") > 0
    claim = db.execute("SELECT * FROM claim WHERE act = 'gifted'").fetchone()
    assert claim["detail"] == " (Gold Ring)" and claim["object_id"] == npc_id(db, "Sigrid")
    beliefs = db.execute("SELECT statement FROM v_belief WHERE holder_id = ? AND about_id = ?",
                         (npc_id(db, "Sigrid"), npc_id(db, "Torvar"))).fetchall()
    assert [b[0] for b in beliefs] == ["Torvar is generous"]


def test_a_theft_seen_by_the_owner_makes_them_think_the_player_a_thief(tmp_path):
    talk(tmp_path, [lucan(), camilla()], 3.0)
    rec = theft(PLAYER, "Silver Goblet", 3.5, owner="Lucan Valerius", gold=60, location="Riverwood Trader",
                witnesses=[seen(lucan(), 2.0), seen(camilla(), 5.0)])
    llm = talk(tmp_path, [lucan()], 3.5, records=[rec])
    db = open_db(tmp_path)
    lucan_id, torvar = npc_id(db, "Lucan Valerius"), npc_id(db, "Torvar")
    claim = db.execute("SELECT * FROM claim WHERE act = 'stole'").fetchone()
    assert claim["object_id"] == lucan_id                            # the owner is named from the game's owner field
    assert standing(db, "Lucan Valerius", "Torvar", "honesty") <= -0.3       # his own things: taken hard
    assert standing(db, "Camilla Valerius", "Torvar", "honesty") < 0          # his sister saw it too
    assert standing(db, "Lucan Valerius", "Torvar", "lawfulness") < 0
    mem = db.execute("SELECT content FROM memory WHERE owner_id = ? AND event_id IS NOT NULL", (lucan_id,)).fetchone()
    assert "steal my Silver Goblet" in mem[0]
    statements = {r[0] for r in db.execute("SELECT statement FROM v_belief WHERE holder_id = ? AND about_id = ?",
                                           (lucan_id, torvar))}
    assert any("thief" in s or "trusted around other people's things" in s for s in statements)
    prompt = talk(tmp_path, [lucan()], 3.6).dialogue_calls[0].system_prompt
    assert "What I've come to think: Torvar" in prompt and "(I've seen it myself)" in prompt


def test_a_picked_pocket_goes_unnoticed_by_the_victim(tmp_path):
    talk(tmp_path, [actors.hod(), sigrid()], 3.0)
    rec = theft(PLAYER, "Gold", 3.5, victim_json=actors.hod(), via="pickpocket", gold=20,
                witnesses=[seen(actors.hod(), 0.5), seen(sigrid(), 12.0)])
    talk(tmp_path, [actors.hod()], 3.5, records=[rec])
    db = open_db(tmp_path)
    event = db.execute("SELECT event_id FROM world_event WHERE type = 'theft'").fetchone()[0]
    perceivers = {r[0] for r in db.execute("SELECT npc_id FROM event_witness WHERE event_id = ?", (event,))}
    assert npc_id(db, "Hod") not in perceivers          # he never notices
    assert npc_id(db, "Sigrid") not in perceivers       # too far away to see a quiet hand
    assert standing(db, "Hod", "Torvar", "honesty") == 0


def test_a_hit_frightens_the_victim_and_brands_the_player_violent(tmp_path):
    meet_everyone(tmp_path)
    rec = hit(PLAYER, actors.hod(), 3.5, weapon="Iron Sword", witnesses=[seen(actors.gerdur(), 4.0, rank_to_target=4)])
    talk(tmp_path, [actors.hod()], 3.5, records=[rec])
    db = open_db(tmp_path)
    hod, gerdur, torvar = npc_id(db, "Hod"), npc_id(db, "Gerdur"), npc_id(db, "Torvar")
    view = db.execute("SELECT fear, trust FROM v_relationship WHERE from_id = ? AND to_id = ?", (hod, torvar)).fetchone()
    assert view["fear"] > 0 and view["trust"] < 0
    assert standing(db, "Hod", "Torvar", "gentleness") < -0.2
    assert standing(db, "Gerdur", "Torvar", "gentleness") < 0     # her husband: taken hard too
    assert db.execute("SELECT detail FROM claim WHERE act = 'hit'").fetchone()[0] == " with Iron Sword"


def test_opinions_form_at_a_threshold_and_drop_back(tmp_path):
    store = WorldStore(str(tmp_path / "w.sqlite"))
    with store.transaction() as db:
        for i, name in ((1, "A"), (2, "B")):
            db.execute("INSERT INTO npc (npc_id, display_name, plugin, base_form_id, is_player, created_gt)"
                       " VALUES (?,?,?,?,0,0)", (i + 100, name, "Test.esp", f"0x{i:08X}"))
        claim = {"claim_id": None, "subject_id": 102, "object_id": None, "act": "gifted", "valence": 0.4,
                 "sensitivity": "public"}
        reputation.record(db, 1, 101, claim, 0.3, 0.9, True, False, 1.0)
        assert db.execute("SELECT COUNT(*) FROM v_belief").fetchone()[0] == 0       # one small kindness is not enough
        reputation.record(db, 1, 101, claim, 0.7, 0.9, True, False, 1.1)
        assert db.execute("SELECT statement FROM v_belief").fetchone()[0] == "B is generous"
        reputation.adjust(db, 1, 101, 102, {"generosity": -0.4}, 1.2, None, None, "test")
        assert db.execute("SELECT COUNT(*) FROM v_belief").fetchone()[0] == 0       # fell back: no longer held


# ---- community and faction views ----

def test_a_community_view_is_the_members_own_views_weighted_by_influence(tmp_path):
    meet_everyone(tmp_path)
    rec = gift(PLAYER, actors.gerdur(), "Gold Ring", 3.5, gold=200, witnesses=[seen(actors.hod(), 3.0)])
    talk(tmp_path, [actors.gerdur()], 3.5, records=[rec])
    db = open_db(tmp_path)
    torvar = npc_id(db, "Torvar")
    with_cid = db.execute("SELECT r.* FROM v_community_reputation r JOIN community c USING (community_id)"
                          " WHERE c.key = 'riverwood' AND r.subject_id = ? AND r.domain = 'generosity'", (torvar,)).fetchone()
    assert with_cid["value"] > 0 and 0 < with_cid["reach"] < 1 and with_cid["holders"] == 2
    # nothing is stored for the community: it is computed from what Gerdur and Hod think
    assert db.execute("SELECT COUNT(*) FROM standing_change WHERE holder_id NOT IN (?, ?)",
                      (npc_id(db, "Gerdur"), npc_id(db, "Hod"))).fetchone()[0] == 0
    store = WorldStore(str(tmp_path / "data" / "livingworld" / "HeadlessTest.sqlite"))
    with store.reading() as rdb:
        assert "known to be generous" in (reputation.summary(rdb, "riverwood", torvar) or "")


def test_guards_weigh_lawfulness_most(tmp_path):
    store = WorldStore(str(tmp_path / "w.sqlite"))
    with store.transaction() as db:
        for i, name, player in ((1, "Guard", 0), (2, "Torvar", 1)):
            db.execute("INSERT INTO npc (npc_id, display_name, plugin, base_form_id, is_player, created_gt)"
                       " VALUES (?,?,?,?,?,0)", (i, name, "Test.esp", f"0x{i:08X}", player))
        db.execute("INSERT INTO guard_profile (npc_id, org_key, given_name, first_seen_gt) VALUES (1, 'whiterun_guard', 'Brond', 0)")
        assert reputation.sync_guard_factions(db) >= 1
        stolen = {"claim_id": None, "subject_id": 2, "object_id": None, "act": "stole", "valence": -0.7, "sensitivity": "public"}
        reputation.record(db, 1, 1, stolen, 0.5, 0.9, True, False, 1.0)
        gave = {"claim_id": None, "subject_id": 2, "object_id": None, "act": "gifted", "valence": 0.4, "sensitivity": "public"}
        reputation.record(db, 1, 1, gave, 0.5, 0.9, True, False, 1.0)
        score, reach = reputation.faction_score(db, "whiterun_guard", 2)
        assert score < 0 and reach == 1.0      # a gift does not make up for theft in a guard's eyes


# ---- retelling and setting it straight ----

def test_dishonest_tellers_make_it_bigger_and_honest_ones_do_not(tmp_path):
    store = WorldStore(str(tmp_path / "w.sqlite"))
    with store.transaction() as db:
        for i, honest in ((1, 0.1), (2, 0.9)):
            db.execute("INSERT INTO npc (npc_id, display_name, plugin, base_form_id, is_player, created_gt)"
                       " VALUES (?,?,?,?,0,0)", (i, f"N{i}", "Test.esp", f"0x{i:08X}"))
            db.execute("INSERT INTO persona_version (npc_id, version, bio, traits_json, source, created_gt)"
                       " VALUES (?, 1, '', ?, 'admin', 0)", (i, f'{{"honest": {honest}}}'))
        rng = random.Random(7)
        liar = [gossip.distort(db, 1, 0.4, rng, 1.0) for _ in range(200)]
        honest = [gossip.distort(db, 2, 0.4, rng, 1.0) for _ in range(200)]
    assert sum(m > 0.4 for m in liar) > 40
    assert sum(m > 0.4 for m in honest) < 15            # rarely
    assert any(m < 0.4 for m in honest)                 # and sometimes played down


def test_a_witness_sets_an_exaggerated_story_straight(tmp_path, monkeypatch):
    monkeypatch.setattr(gossip, "EXAGGERATE_BASE", 1.0)          # every retelling grows
    monkeypatch.setattr(gossip, "CORRECT_RATE", 50.0)            # and witnesses always talk
    meet_everyone(tmp_path)
    rec = hit(PLAYER, actors.hod(), 3.5, witnesses=[seen(actors.gerdur(), 4.0, rank_to_target=4)])
    talk(tmp_path, [actors.hod()], 3.5, records=[rec])
    talk(tmp_path, [sigrid()], 9.0)
    db = open_db(tmp_path)
    assert db.execute("SELECT COUNT(*) FROM world_event WHERE type = 'gossip_exchange'"
                      " AND json_extract(payload_json, '$.distorted') > 0").fetchone()[0] >= 1
    corrections = db.execute("SELECT COUNT(*) FROM world_event WHERE type = 'gossip_correction'").fetchone()[0]
    assert corrections >= 1
    truth = db.execute("SELECT magnitude FROM claim WHERE act = 'hit'").fetchone()[0]
    corrected = db.execute("SELECT k.heard_magnitude FROM v_claim_knowledge k JOIN world_event e"
                           " ON e.event_id = k.source_event_id WHERE e.type = 'gossip_correction'").fetchall()
    assert corrected and all(abs(r[0] - truth) < 1e-6 for r in corrected)
    reasons = [r[0] for r in db.execute("SELECT reason FROM standing_change WHERE reason = 'set straight by a witness'")]
    assert reasons


# ---- traits ----

def test_traits_come_from_the_bio_and_the_seed_file_wins():
    t = traits.from_bio("A notorious gossip and a liar who works as a smuggler.", "x")
    assert t["gossipy"] > 0.6 and t["honest"] < 0.35 and t["law"] < 0.35
    plain = traits.from_bio("", "x")
    assert all(0.4 <= v <= 0.6 for v in plain.values())


def test_seed_file_gossipiness_survives_bio_traits(tmp_path):
    talk(tmp_path, [sigrid()], 1.0)
    db = open_db(tmp_path)
    import json
    row = db.execute("SELECT traits_json FROM persona_version WHERE npc_id = ?", (npc_id(db, "Sigrid"),)).fetchone()
    data = json.loads(row[0])
    assert data["gossipy"] == 0.6           # riverwood.json
    assert "honest" in data                 # from her Mantella bio


# ---- hints ----

def test_balanced_hint_is_hearsay_and_shown_once(tmp_path):
    meet_everyone(tmp_path)
    rec = gift(PLAYER, actors.gerdur(), "Gold Ring", 3.5, gold=200, witnesses=[seen(actors.hod(), 3.0)])
    talk(tmp_path, [actors.gerdur()], 3.5, records=[rec])
    spy = Spy()
    talk(tmp_path, [sigrid()], 9.0, spy=spy)
    sent = [k[KEY_HINTS] for k in spy.keys if KEY_HINTS in k]
    # Torvar saw his own gift, so that claim is never hinted back to him; Sigrid may have heard it, which is
    # still the same claim. Nothing else about him exists, so no hint at all.
    assert sent == []


def test_informative_hints_name_who_has_heard_what(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTELLA_LW_INFO_LEVEL", "informative")
    meet_everyone(tmp_path)
    # someone else hits Hod in front of Gerdur; Torvar was not there
    other = actors.actor("Faendal", 0x00013480, 0x0001348D, 0, "WoodElf", "MaleYoungEager")
    talk(tmp_path, [other], 3.2)
    rec = hit(other, actors.hod(), 3.5, witnesses=[seen(actors.gerdur(), 4.0, rank_to_target=4)])
    talk(tmp_path, [actors.hod()], 3.5, records=[rec])
    spy = Spy()
    for day in (4.0, 4.5, 5.0):   # Torvar gets to know Hod well
        talk(tmp_path, [actors.hod()], day, spy=spy)
    talk(tmp_path, [sigrid()], 9.0, spy=spy)
    sent = [h for k in spy.keys if KEY_HINTS in k for h in k[KEY_HINTS]]
    assert any("Faendal hit Hod" in h for h in sent), sent
    db = open_db(tmp_path)
    assert db.execute("SELECT COUNT(*) FROM v_player_notice").fetchone()[0] == len(sent)
    spy2 = Spy()
    talk(tmp_path, [sigrid()], 9.5, spy=spy2)
    assert not any("Faendal hit Hod" in h for k in spy2.keys if KEY_HINTS in k for h in k[KEY_HINTS])


def test_balanced_hint_names_who_was_talking_so_the_player_can_ask_them(tmp_path):
    meet_everyone(tmp_path)
    other = actors.actor("Faendal", 0x00013480, 0x0001348D, 0, "WoodElf", "MaleYoungEager")
    talk(tmp_path, [other], 3.2)
    rec = hit(other, actors.hod(), 3.5, witnesses=[seen(actors.gerdur(), 4.0, rank_to_target=4)])
    talk(tmp_path, [actors.hod()], 3.5, records=[rec])
    spy = Spy()
    for day in (4.0, 4.5, 5.0):   # Torvar gets to know Hod well
        talk(tmp_path, [actors.hod()], day, spy=spy)
    talk(tmp_path, [sigrid()], 9.0, spy=spy)
    sent = [h for k in spy.keys if KEY_HINTS in k for h in k[KEY_HINTS]]
    # Hod was hit by Faendal: Hod is the one talking, and the gist is still the player's to find out
    assert sent == ["You overhear Hod talking about Faendal."], sent


def test_minimal_level_shows_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTELLA_LW_INFO_LEVEL", "minimal")
    meet_everyone(tmp_path)
    other = actors.actor("Faendal", 0x00013480, 0x0001348D, 0, "WoodElf", "MaleYoungEager")
    talk(tmp_path, [other], 3.2)
    talk(tmp_path, [actors.hod()], 3.5, records=[hit(other, actors.hod(), 3.5)])
    spy = Spy()
    for day in (4.0, 4.5, 5.0):
        talk(tmp_path, [actors.hod()], day, spy=spy)
    talk(tmp_path, [sigrid()], 9.0, spy=spy)
    assert not any(KEY_HINTS in k for k in spy.keys)


# ---- switches and children ----

def test_reputation_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTELLA_LW_REPUTATION", "0")
    meet_everyone(tmp_path)
    talk(tmp_path, [actors.hod()], 3.5, records=[hit(PLAYER, actors.hod(), 3.5)])
    db = open_db(tmp_path)
    assert db.execute("SELECT COUNT(*) FROM standing_change").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM claim WHERE act = 'hit'").fetchone()[0] == 1   # S1 gossip still works


def test_children_never_hold_or_get_a_reputation(tmp_path):
    frodnar = actors.actor("Frodnar", 0x0001347E, 0x0001348B, 0, "Nord", "MaleEvenToned")
    talk(tmp_path, [actors.hod(), frodnar], 3.0)
    talk(tmp_path, [actors.hod()], 3.5, records=[hit(PLAYER, frodnar, 3.5, witnesses=[seen(actors.hod(), 2.0)]),
                                                 gift(PLAYER, actors.hod(), "Apple", 3.5, witnesses=[seen(frodnar, 1.0)])])
    db = open_db(tmp_path)
    child = npc_id(db, "Frodnar")
    assert db.execute("SELECT COUNT(*) FROM world_event WHERE type = 'violence'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM standing_change WHERE holder_id = ? OR subject_id = ?",
                      (child, child)).fetchone()[0] == 0


def test_a_harmless_spell_is_not_an_attack(tmp_path):
    meet_everyone(tmp_path)
    recs = [hit(PLAYER, actors.hod(), 3.5, weapon=w, via="conversation", witnesses=[seen(actors.gerdur(), 4.0)])
            for w in ("Anim: Kneel", "Face: Gasp", "Debug Bath (Target)")]
    talk(tmp_path, [actors.hod()], 3.5, records=recs)
    db = open_db(tmp_path)
    assert db.execute("SELECT COUNT(*) FROM world_event WHERE type = 'violence'").fetchone()[0] == 0
    assert standing(db, "Hod", "Torvar", "gentleness") == 0


def test_the_cleanup_undoes_harmless_spells_already_stored(tmp_path, monkeypatch):
    import os
    from livingworld.perception import pipeline
    meet_everyone(tmp_path)
    # stored the way an older build did, before harmless spells were skipped
    monkeypatch.setattr(pipeline, "harmless_spell", lambda name: False)
    rec = hit(PLAYER, actors.hod(), 3.5, weapon="Anim: Kneel", witnesses=[seen(actors.gerdur(), 4.0, rank_to_target=4)])
    talk(tmp_path, [actors.hod()], 3.5, records=[rec])
    db = open_db(tmp_path)
    hod, torvar = npc_id(db, "Hod"), npc_id(db, "Torvar")
    before = db.execute("SELECT trust FROM v_relationship WHERE from_id = ? AND to_id = ?", (hod, torvar)).fetchone()[0]
    assert before < 0 and standing(db, "Hod", "Torvar", "gentleness") < 0
    folder = os.path.join(os.path.dirname(reputation.__file__), "..", "store", "migrations")
    sql = open(os.path.join(folder, "010_harmless_spells.sql"), encoding="utf-8").read()
    db.executescript(sql.split("INSERT INTO schema_version")[0])
    after = db.execute("SELECT trust FROM v_relationship WHERE from_id = ? AND to_id = ?", (hod, torvar)).fetchone()[0]
    assert after > before
    assert standing(db, "Hod", "Torvar", "gentleness") == 0
    event = db.execute("SELECT event_id FROM world_event WHERE type = 'violence'").fetchone()[0]
    assert db.execute("SELECT COUNT(*) FROM memory WHERE event_id = ? AND superseded_by IS NULL", (event,)).fetchone()[0] == 0
