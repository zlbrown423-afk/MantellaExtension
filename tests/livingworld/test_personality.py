"""Personality profiles (livingworld/personality): eligibility, generation, precedence, change over time,
discovery, prompts, admin, persistence, migration and the romance bridge. Server only; nothing needs the game."""
import json
import sqlite3

import pytest

from livingworld.personality import admin, bridge, discovery, evolve, generate, prompt, schema
from livingworld.personality import profile as P
from livingworld.personality.config import Settings, SettingsFile, parse
from livingworld.personality.service import PersonalityService
from livingworld.store.minors import mark_children
from livingworld.store.world_store import WorldStore, _migration_files


@pytest.fixture(autouse=True)
def bridge_off():
    bridge.use_settings(None)
    yield
    bridge.use_settings(None)


def add(db, name, sex="female", race="Nord", bio="", base=None, player=False):
    base = base or "0x%08X" % (0x100000 + abs(hash(name)) % 0xFFFFF)
    npc = db.execute("INSERT INTO npc (plugin, base_form_id, display_name, sex, race, is_player, created_gt)"
                     " VALUES ('Skyrim.esm', ?, ?, ?, ?, ?, 0)", (base, name, sex, race, int(player))).lastrowid
    if bio:
        db.execute("INSERT INTO persona_version (npc_id, version, bio, traits_json, source, created_gt)"
                   " VALUES (?, 1, ?, '{}', 'mantella_csv', 0)", (npc, bio))
        from livingworld.social.traits import seed_traits_from_bio
        seed_traits_from_bio(db, npc, bio, 0)
    return npc


def rel(db, a, b, gt=0.5, event_type=None, **deltas):
    db.execute("INSERT OR IGNORE INTO relationship (from_id, to_id, status, updated_gt) VALUES (?,?,'acquaintance',0)",
               (a, b))
    event = None
    if event_type:
        event = db.execute("INSERT INTO world_event (timeline_id, type, actors_json, payload_json, source, game_time)"
                           " VALUES (1, ?, ?, '{}', 'game', ?)", (event_type, json.dumps([b, a]), gt)).lastrowid
    db.execute("INSERT INTO relationship_change (timeline_id, from_id, to_id, deltas_json, cause_event_id, reason,"
               " game_time) VALUES (1,?,?,?,?, 'test', ?)", (a, b, json.dumps(deltas), event, gt))


@pytest.fixture
def world():
    store = WorldStore(":memory:")
    with store.transaction() as db:
        ids = {
            "player": add(db, "Dragonborn", sex="male", player=True),
            "aela": add(db, "Aela", bio="A bold, proud warrior of the Companions. Confident and brash."),
            "dinya": add(db, "Dinya", race="Altmer", bio="A pious, devout and modest priestess of Mara. Quiet, private."),
            "ysolda": add(db, "Ysolda", bio="A friendly, cheerful merchant, warm and talkative."),
            "nobody": add(db, "Strange Woman", race=None, bio="Unknown."),
            "bandit": add(db, "Bandit Chief", sex="male"),
            "girl": add(db, "Lucia", race="NordRaceChild", bio="An orphan girl."),
        }
        mark_children(db)
    return store, ids


def settings(**npcs):
    return parse({"npcs": npcs})


# ---- schema and migration ----

def test_migration_017_applies_on_an_existing_world(tmp_path):
    path = str(tmp_path / "old.sqlite")
    conn = sqlite3.connect(path)
    for version, sql in _migration_files():
        if version < 17:
            conn.executescript(f"BEGIN;\n{sql}\nCOMMIT;")
    conn.execute("INSERT INTO npc (plugin, base_form_id, display_name, sex, race, created_gt)"
                 " VALUES ('Skyrim.esm', '0x00013BBD', 'Aela', 'female', 'Nord', 0)")
    conn.commit()
    conn.close()
    store = WorldStore(path)
    assert store.schema_version() == 17
    assert store.scalar("SELECT display_name FROM npc") == "Aela"      # existing data kept
    for table in ("personality_profile", "personality_change", "personality_knowledge", "personality_admin"):
        assert store.scalar("SELECT COUNT(*) FROM sqlite_master WHERE name = ?", (table,)) == 1
    store.close()


def test_schema_validation_and_json_schema():
    assert schema.validate_value("libido", 70) == 70.0
    for bad in (("libido", 101), ("libido", "high"), ("exclusivity", True), ("intimacy_pace", "rushed"),
                ("boundaries", ["no_such"]), ("nonsense", 1)):
        with pytest.raises(ValueError):
            schema.validate_value(*bad)
    assert schema.validate_value("boundaries", ["no_unasked_touch", "no_unasked_touch"]) == ["no_unasked_touch"]
    js = schema.json_schema()
    assert set(js["properties"]["npcs"]["additionalProperties"]["properties"]["values"]["properties"]) == set(schema.BY_KEY)


def test_settings_file_skips_bad_parts_and_keeps_the_rest():
    s = parse({"default_mode": "sideways", "baseline": {"libido": 10, "nope": 5},
               "npcs": {"Aela": {"values": {"libido": 80, "initiation": 500}, "mode": "manual"},
                        "Skyrim.esm|13bbd": {"enabled": False}}})
    assert s.default_mode == "hybrid" and s.baseline == {"libido": 10.0}
    assert s.npcs["Aela"].values == {"libido": 80.0} and s.npcs["Aela"].mode == "manual"
    assert len(s.problems) == 3
    assert s.for_npc("Someone", "Skyrim.esm", "0x00013BBD").enabled is False
    assert s.for_npc("aela", None, None).values == {"libido": 80.0}


# ---- eligibility ----

def test_only_adult_women_whose_age_is_established(world):
    store, ids = world
    with store.transaction() as db:
        s = Settings()
        assert P.resolve(db, ids["aela"], s, 1).active
        assert P.resolve(db, ids["dinya"], s, 1).active                  # an elf is an adult race too
        assert not P.resolve(db, ids["bandit"], s, 1).eligible
        assert not P.resolve(db, ids["player"], s, 1).eligible
        assert P.resolve(db, ids["girl"], s, 1).why == "a child"
        unknown = P.resolve(db, ids["nobody"], s, 1)
        assert not unknown.eligible and "cannot be established" in unknown.why
        confirmed = settings(**{"Strange Woman": {"adult_confirmed": True}, "Lucia": {"adult_confirmed": True}})
        assert P.resolve(db, ids["nobody"], confirmed, 1).active
        assert not P.resolve(db, ids["girl"], confirmed, 1).eligible     # never for a child
        assert db.execute("SELECT COUNT(*) FROM personality_profile WHERE npc_id IN (?,?,?)",
                          (ids["girl"], ids["bandit"], ids["player"])).fetchone()[0] == 0


# ---- generation ----

def test_generation_is_individual_and_never_rerolls(world):
    store, ids = world
    with store.transaction() as db:
        a1 = generate.generate(db, ids["aela"]).values
        a2 = generate.generate(db, ids["aela"]).values
        d = generate.generate(db, ids["dinya"]).values
        y = generate.generate(db, ids["ysolda"]).values
    assert a1 == a2
    assert a1 != d != y
    assert a1["sexual_confidence"] > d["sexual_confidence"]            # bold warrior vs modest priestess
    assert d["intimacy_boundary"] == "married_only"                    # a priestess of Mara
    assert d["privacy"] > y["privacy"]
    for v in (a1, d, y):
        for key in schema.BY_KEY:
            schema.validate_value(key, v[key])
    with store.transaction() as db:
        first = P.resolve(db, ids["aela"], Settings(), 1).values
        db.execute("UPDATE persona_version SET bio = 'A shy, timid, modest woman.' WHERE npc_id = ?", (ids["aela"],))
        assert P.resolve(db, ids["aela"], Settings(), 2).values == first   # stored: a bio edit needs a regenerate


def test_romance_seed_maps_one_to_one():
    from livingworld.social.seed import seeds
    seed = seeds()[0]
    gerdur = next(p for p in seed["people"] if p["key"] == "gerdur")
    store = WorldStore(":memory:")
    with store.transaction() as db:
        npc = add(db, "Gerdur", base="0x" + gerdur["base_id"].upper().rjust(8, "0"))
        v = generate.generate(db, npc).values
    r = seed["romance"]["gerdur"]
    approx = pytest.approx
    assert v["libido"] == approx(r["drive"] * 100) and v["romantic_inclination"] == approx(r["romantic"] * 100)
    assert v["sexual_confidence"] == approx((1 - r["shy"]) * 100) and v["initiation"] == approx(r["flirtatious"] * 100)
    assert v["exclusivity"] == approx(r["monogamy"] * 100) and v["intimacy_boundary"] == r["intimacy_boundary"]
    assert v["attracted_to_sex"] == ["male"]


# ---- precedence ----

def test_hand_set_values_win_and_are_locked(world):
    store, ids = world
    s = settings(Aela={"values": {"libido": 12, "boundaries": ["no_public_affection"]}})
    with store.transaction() as db:
        p = P.resolve(db, ids["aela"], s, 1)
        assert p.values["libido"] == 12 and p.sources["libido"] == "manual" and p.locked("libido")
        assert p.values["boundaries"] == ["no_public_affection"]
        evolve.evaluate(db, ids["aela"], s, 1)
        rel(db, ids["aela"], ids["player"], gt=1.5, event_type="physical_interaction", trust=-0.6)
        rel(db, ids["aela"], ids["player"], gt=1.6, trust=0.5, intimacy=0.4)
        res = evolve.evaluate(db, ids["aela"], s, 3)
        assert all(c[0] not in ("libido", "boundaries") for c in res.changes)
        p = P.resolve(db, ids["aela"], s, 3)
        assert p.values["libido"] == 12 and p.values["boundaries"] == ["no_public_affection"]


def test_manual_mode_uses_only_hand_set_values_and_never_changes(world):
    store, ids = world
    s = settings(Ysolda={"mode": "manual", "values": {"libido": 40, "intimacy_boundary": "committed_only"}})
    with store.transaction() as db:
        p = P.resolve(db, ids["ysolda"], s, 1)
        assert p.values == {"libido": 40.0, "intimacy_boundary": "committed_only"}
        assert evolve.evaluate(db, ids["ysolda"], s, 1).skipped == "manual mode"
        assert db.execute("SELECT COUNT(*) FROM personality_profile WHERE npc_id = ?", (ids["ysolda"],)).fetchone()[0] == 0


def test_disabled_profile_and_module(world):
    store, ids = world
    with store.transaction() as db:
        assert not P.resolve(db, ids["aela"], settings(Aela={"enabled": False}), 1).active
        assert not P.resolve(db, ids["aela"], parse({"enabled": False}), 1).active


def test_baseline_shifts_generated_numbers(world):
    store, ids = world
    with store.transaction() as db:
        plain = P.resolve(db, ids["aela"], Settings(), 1).values["initiation"]
        shifted = P.resolve(db, ids["aela"], parse({"baseline": {"initiation": -10}}), 1).values["initiation"]
    assert shifted == round(max(0, plain - 10), 1)


# ---- change over time ----

def test_change_is_gradual_capped_and_explained(world):
    store, ids = world
    s = Settings()
    with store.transaction() as db:
        start = P.resolve(db, ids["ysolda"], s, 1).values
        assert evolve.evaluate(db, ids["ysolda"], s, 1).skipped == "first checkpoint"
        for i in range(10):
            rel(db, ids["ysolda"], ids["player"], gt=1.1 + i * 0.01, trust=0.2, intimacy=0.2)
        res = evolve.evaluate(db, ids["ysolda"], s, 1.5)
        moved = {c[0]: c[2] for c in res.changes}
        assert 0 < moved["openness"] <= evolve.MAX_PER_DAY and 0 < moved["intimacy_comfort"] <= evolve.MAX_PER_DAY
        hist = admin.history(db, ids["ysolda"])
        assert any(h.get("attribute") == "openness" and "trust" in h["reason"] for h in hist)
        for day in range(2, 40):          # a long, warm relationship: never more than the attribute's drift
            rel(db, ids["ysolda"], ids["player"], gt=day + 0.5, trust=0.3, intimacy=0.3)
            evolve.evaluate(db, ids["ysolda"], s, day + 0.9)
        end = P.resolve(db, ids["ysolda"], s, 40).values
        assert end["openness"] - start["openness"] <= schema.BY_KEY["openness"].drift + 1e-6
        assert end["libido"] == start["libido"]                      # nothing pushed desire
        again = P.resolve(db, ids["ysolda"], s, 40).values
        assert again == end                                          # reading twice changes nothing


def test_unwanted_handling_adds_a_boundary_that_is_never_removed(world):
    store, ids = world
    s = Settings()
    with store.transaction() as db:
        evolve.evaluate(db, ids["ysolda"], s, 1)
        before = P.resolve(db, ids["ysolda"], s, 1).values
        for i in range(4):
            rel(db, ids["ysolda"], ids["player"], gt=1.2 + i * 0.01, event_type="physical_interaction", trust=-0.15)
        evolve.evaluate(db, ids["ysolda"], s, 2)
        after = P.resolve(db, ids["ysolda"], s, 2).values
        assert "no_unasked_touch" in after["boundaries"]
        assert after["selectiveness"] > before["selectiveness"]
        assert after["intimacy_comfort"] < before["intimacy_comfort"]
        P.add_change(db, ids["ysolda"], "boundaries", "list_remove", "test", 2.5, value="no_unasked_touch")
        assert "no_unasked_touch" in P.resolve(db, ids["ysolda"], s, 3).values["boundaries"]


def test_changes_roll_back_with_an_older_save(world):
    store, ids = world
    s = Settings()
    with store.transaction() as db:
        evolve.evaluate(db, ids["ysolda"], s, 1)
        rel(db, ids["ysolda"], ids["player"], gt=3.0, trust=0.5, intimacy=0.5)
        evolve.evaluate(db, ids["ysolda"], s, 3.5)
        changed = P.resolve(db, ids["ysolda"], s, 3.5).values["openness"]
        # load a save from game day 2: a new timeline branching before the change
        db.execute("UPDATE timeline SET is_active = 0")
        db.execute("INSERT INTO timeline (parent_id, branch_game_time, save_name, is_active) VALUES (1, 2.0, 'old', 1)")
        rolled = P.resolve(db, ids["ysolda"], s, 2.1).values["openness"]
    assert rolled < changed
    assert rolled == generate.generate(store._conn, ids["ysolda"]).values["openness"]


def test_reset_and_regenerate(world):
    store, ids = world
    s = Settings()
    with store.transaction() as db:
        evolve.evaluate(db, ids["ysolda"], s, 1)
        rel(db, ids["ysolda"], ids["player"], gt=1.5, trust=0.5)
        evolve.evaluate(db, ids["ysolda"], s, 2)
        assert P.resolve(db, ids["ysolda"], s, 2).drift
        admin.reset(db, None, ids["ysolda"], 2.1)
        p = P.resolve(db, ids["ysolda"], s, 2.2)
        assert not p.drift and p.values == p.generated
        admin.regenerate(db, ids["ysolda"], 2.3)
        actions = [h.get("admin") for h in admin.history(db, ids["ysolda"])]
    assert "reset" in actions and "regenerate" in actions


# ---- discovery and prompts ----

def test_private_preferences_are_not_disclosed_to_a_stranger(world):
    store, ids = world
    s = settings(Aela={"values": {"libido": 90}})
    with store.transaction() as db:
        p = P.resolve(db, ids["aela"], s, 1)
        assert discovery.known(db, ids["aela"], ids["player"]) == {}
        standing = "\n".join(prompt.standing_lines(db, p, ids["player"], "Dragonborn", "male"))
        topic = "\n".join(prompt.topic_lines(db, p, ids["player"], "Dragonborn", "Would you share a bed with me?"))
        assert "desire" not in standing and "very high" not in topic
        assert "keeps her desires and her intimate life private" in topic
        assert "Desire is never a yes" in topic
        g = discovery.gate(db, p, ids["player"])
        assert "private" not in g.tiers
        allowed = discovery.shareable(p, g)
        assert "libido" not in allowed
        # she says it anyway: not recorded, because it was not hers to share yet
        assert discovery.heard(db, p, ids["player"], "I have a great desire for you.", allowed, 1) == []
        assert prompt.topic_lines(db, p, ids["player"], "Dragonborn", "Nice weather for fishing.") == []


def test_trust_opens_private_talk_and_disclosure_is_remembered(world):
    store, ids = world
    s = settings(Aela={"values": {"libido": 90}, "private": ["exclusivity"]})
    with store.transaction() as db:
        rel(db, ids["aela"], ids["player"], gt=0.5, trust=0.9, affinity=0.8, intimacy=0.6, familiarity=0.9)
        p = P.resolve(db, ids["aela"], s, 1)
        g = discovery.gate(db, p, ids["player"])
        assert "private" in g.tiers
        allowed = discovery.shareable(p, g)
        assert "libido" in allowed and "exclusivity" not in allowed     # hand-marked private: never
        assert "very high desire" in "\n".join(prompt.topic_lines(db, p, ids["player"], "D", "Do you love me?"))
        assert discovery.heard(db, p, ids["player"], "I'll admit my desire runs hot.", allowed, 1.1) == ["libido"]
        assert discovery.known(db, ids["aela"], ids["player"]) == {"libido": "known"}
        topic = "\n".join(prompt.topic_lines(db, p, ids["player"], "D", "kiss me"))
        assert "already knows" in topic
        # with others listening a private person keeps it to herself
        private_p = P.resolve(db, ids["aela"], settings(Aela={"values": {"privacy": 90}}), 1)
        assert "private" not in discovery.gate(db, private_p, ids["player"], others_listening=True).tiers


def test_stated_boundary_is_disclosed_not_consent(world):
    store, ids = world
    s = settings(Ysolda={"values": {"boundaries": ["no_unasked_touch"]}})
    with store.transaction() as db:
        p = P.resolve(db, ids["ysolda"], s, 1)
        topic = "\n".join(prompt.topic_lines(db, p, ids["player"], "D", "Can I hug you?"))
        assert "nobody touches her without asking first" in topic
        allowed = discovery.shareable(p, discovery.gate(db, p, ids["player"]))
        assert "boundaries:no_unasked_touch" in allowed
        discovery.heard(db, p, ids["player"], "Hands off unless you ask first.", allowed, 1)
        assert discovery.known(db, ids["ysolda"], ids["player"])["boundaries:no_unasked_touch"] == "disclosed"
        assert P.resolve(db, ids["ysolda"], s, 2).values["boundaries"] == ["no_unasked_touch"]


def test_seen_flirting_is_only_a_suspicion(world):
    store, ids = world
    with store.transaction() as db:
        p = P.resolve(db, ids["ysolda"], Settings(), 1)
        event = db.execute("INSERT INTO world_event (timeline_id, type, actors_json, payload_json, source, game_time)"
                           " VALUES (1, 'romance.flirt', ?, '{}', 'sim', 0.8)", (json.dumps([ids["ysolda"], ids["bandit"]]),)).lastrowid
        db.execute("INSERT INTO event_witness (event_id, npc_id, how) VALUES (?,?, 'saw')", (event, ids["player"]))
        assert discovery.observe(db, p, ids["player"], 1) == ["initiation"]
        assert discovery.known(db, ids["ysolda"], ids["player"]) == {"initiation": "suspected"}
        view = admin.player_view(db, Settings(), ids["ysolda"], ids["player"], 1)
        assert list(view["knows"]) == ["initiation"]          # the player view never shows the rest


# ---- admin, persistence ----

def test_export_import_round_trip_writes_hand_set_values(world, tmp_path):
    store, ids = world
    sfile = SettingsFile(str(tmp_path / "personality.json"))
    with store.transaction() as db:
        exported = admin.export(db, Settings(), [ids["aela"], ids["girl"]], 1)
        assert list(exported["profiles"]) == [admin.key_for(db, ids["aela"])]   # never a child
        problems = admin.import_profiles(sfile, exported, db, take="values")
    assert problems == []
    s = sfile.get()
    entry = s.for_npc("Aela", "Skyrim.esm", store.scalar("SELECT base_form_id FROM npc WHERE npc_id = ?", (ids["aela"],)))
    assert set(entry.values) == set(schema.BY_KEY)
    with store.transaction() as db:
        p = P.resolve(db, ids["aela"], s, 1)
        assert all(p.locked(k) for k in schema.BY_KEY)
        bad = admin.import_profiles(sfile, {"profiles": {"X|0x1": {"values": {"libido": 900}}}}, db)
    assert bad and "libido" in bad[0]


def test_profiles_survive_restarts(tmp_path):
    path = str(tmp_path / "w.sqlite")
    store = WorldStore(path)
    with store.transaction() as db:
        npc = add(db, "Camilla", bio="Kind and romantic.")
        player = add(db, "Dragonborn", sex="male", player=True)
        first = P.resolve(db, npc, Settings(), 1).values
        evolve.evaluate(db, npc, Settings(), 1)
        rel(db, npc, player, gt=1.5, trust=0.6, intimacy=0.5)
        evolve.evaluate(db, npc, Settings(), 2)
        rel(db, npc, player, gt=2.0, trust=0.9, affinity=0.9, intimacy=0.6, familiarity=0.9)
        p = P.resolve(db, npc, Settings(), 2)
        discovery.record(db, npc, player, "attracted_to:kindness", "known", "told", 2)
        evolved = p.values
    store.close()
    store = WorldStore(path)
    with store.transaction() as db:
        assert P.resolve(db, npc, Settings(), 2).values == evolved
        assert evolved != first
        assert discovery.known(db, npc, player) == {"attracted_to:kindness": "known"}
    store.close()


def test_settings_file_edits_apply_without_restart(tmp_path):
    path = tmp_path / "personality.json"
    sfile = SettingsFile(str(path))
    assert sfile.get().npcs == {}
    path.write_text(json.dumps({"npcs": {"Aela": {"values": {"libido": 33}}}}), encoding="utf-8")
    assert sfile.get().npcs["Aela"].values == {"libido": 33.0}
    path.write_text(json.dumps({"npcs": {"Aela": {"values": {"libido": 44, "padding": 1}}}}), encoding="utf-8")
    assert sfile.get().npcs["Aela"].values == {"libido": 44.0}


def test_example_profiles_file_is_valid():
    import os
    import livingworld.personality as pkg
    path = os.path.join(os.path.dirname(pkg.__file__), "data", "personality.example.json")
    with open(path, encoding="utf-8") as f:
        s = parse(json.load(f))
    assert s.problems == [] and len(s.npcs) >= 4


# ---- romance bridge, service ----

def test_romance_reads_profiles_and_seeded_people_keep_their_numbers(world, tmp_path):
    from livingworld.social import romance
    from livingworld.social.seed import seeds
    store, ids = world
    seed = seeds()[0]
    gerdur = next(p for p in seed["people"] if p["key"] == "gerdur")
    sfile = SettingsFile(str(tmp_path / "personality.json"))
    with store.transaction() as db:
        g = add(db, "Gerdur", base="0x" + gerdur["base_id"].upper().rjust(8, "0"))
        before = romance.traits(db, g)
        bridge.use_settings(sfile.get)
        after = romance.traits(db, g)
        for key in ("drive", "romantic", "shy", "flirtatious", "monogamy", "intimacy_boundary", "attracted_to"):
            assert after[key] == pytest.approx(before[key]) if isinstance(before[key], float) else after[key] == before[key]
        dinya = romance.traits(db, ids["dinya"])
        assert dinya["intimacy_boundary"] == "married_only" and dinya["drive"] != romance.DEFAULT_TRAITS["drive"]
        assert romance.traits(db, ids["girl"])["drive"] == romance.DEFAULT_TRAITS["drive"]
        (tmp_path / "personality.json").write_text(json.dumps({"npcs": {"Gerdur": {"values": {"intimacy_boundary": "never"}}}}))
        assert romance.traits(db, g)["intimacy_boundary"] == "never"     # a hand-set boundary is what consent applies


def test_service_flow(world, tmp_path):
    store, ids = world
    svc = PersonalityService(str(tmp_path), str(tmp_path / "personality.json"))
    present = {ids["aela"]: "Aela", ids["girl"]: "Lucia"}
    with store.transaction() as db:
        rel(db, ids["aela"], ids["player"], gt=0.5, trust=0.6, affinity=0.5, familiarity=0.5)
        svc.started(db, list(present), ids["player"], 1, None)
        assert svc.notes(db, "Lovely day.", present, ids["player"], "D", 1) == []
        notes = svc.notes(db, "What do you look for in a man?", present, ids["player"], "D", 1)
        assert len(notes) == 1 and notes[0].startswith("Aela's heart")      # the child gets nothing
        p = P.resolve(db, ids["aela"], svc.settings(), 1)
        liked = schema.ATTRACTION[p.values["attracted_to"][0]]
        assert svc.npc_said(db, ids["aela"], ids["player"], f"I admire {liked}, if you must know.", 1, None)
        assert svc.npc_said(db, ids["girl"], ids["player"], "I like kindness", 1, None) == []
        out = svc.debug(db, "debug profile", present, ids["player"], 1)
    assert out and "Aela" in out
    assert (tmp_path / "personality-inspect" / "Aela.json").exists()


# ---- dialogue personality ----

from livingworld.personality import dialogue as D   # noqa: E402


def _dialogue(store, npc, manual=None, s=None, traits=None):
    with store.transaction() as db:
        p = P.resolve(db, npc, s or Settings(), 1)
        from livingworld.social.traits import traits_of
        return D.resolve(p, traits if traits is not None else traits_of(db, npc), manual)


def test_dialogue_is_individual_and_stable(world):
    store, ids = world
    a1, a2 = _dialogue(store, ids["aela"]), _dialogue(store, ids["aela"])
    dinya = _dialogue(store, ids["dinya"])
    assert a1.toggles == a2.toggles and a1.sliders == a2.sliders and a1.vocabulary == a2.vocabulary
    assert a1.sliders != dinya.sliders
    assert a1.sliders["confidence"] > dinya.sliders["confidence"]
    assert not dinya.toggles["suggestive"]                     # a modest priestess
    assert set(a1.toggles) == set(D.TOGGLES) and set(a1.sliders) == set(D.SLIDERS)


def test_toggles_are_independent_and_hand_set_wins(world):
    store, ids = world
    base = _dialogue(store, ids["aela"])
    off = _dialogue(store, ids["aela"], {"toggles": {"teasing": False}})
    assert off.toggles["teasing"] is False and off.sources["toggles.teasing"] == "manual"
    assert {k: v for k, v in off.toggles.items() if k != "teasing"} == \
           {k: v for k, v in base.toggles.items() if k != "teasing"}
    slid = _dialogue(store, ids["aela"], {"sliders": {"flirtation": 5}})
    assert slid.sliders["flirtation"] == 5 and slid.sliders["playfulness"] == base.sliders["playfulness"]
    lines = "\n".join(D.style_lines(off, "Aela", "D", D.Moment(trust=0.8, attraction=0.6), topic=True))
    assert "She never uses:" in lines and "playful teasing" in lines.split("She never uses:")[1]


def test_vocabulary_merge_no_duplicates_and_forbidden_wins(world):
    store, ids = world
    manual, problems = D.validate({"vocabulary": {"terms_of_affection": ["My Heart", "my  heart", "pup"],
                                                  "forbidden": ["pup"]},
                                   "remove": {"expressions": ["by the Nine"]}, "toggles": {"nope": True}})
    assert problems and manual["vocabulary"]["terms_of_affection"] == ["My Heart", "pup"]
    d = _dialogue(store, ids["ysolda"], manual)
    terms = [D._norm(x) for x in d.vocabulary["terms_of_affection"]]
    assert terms.count("my heart") == 1 and "pup" not in terms
    assert "by the nine" not in [D._norm(x) for x in d.vocabulary.get("expressions", [])]
    lines = "\n".join(D.style_lines(d, "Ysolda", "D", D.Moment(trust=0.9, attraction=0.8), topic=True))
    assert '"pup"' in lines.split("She never says:")[1] and "not lines to repeat" in lines


def test_dialogue_follows_change_over_time_but_not_randomly(world):
    store, ids = world
    s = Settings()
    before = _dialogue(store, ids["ysolda"], s=s)
    with store.transaction() as db:
        P.add_change(db, ids["ysolda"], "initiation", "drift", "test", 1.5, delta=10)
    after = _dialogue(store, ids["ysolda"], s=s)
    assert after.sliders["initiative"] == min(100, before.sliders["initiative"] + 10)
    assert after.sliders["playfulness"] == before.sliders["playfulness"]
    assert after.vocabulary == before.vocabulary


def test_dialogue_off_switches_and_manual_mode(world):
    store, ids = world
    assert not _dialogue(store, ids["aela"], {"enabled": False}).enabled
    with store.transaction() as db:
        p = P.resolve(db, ids["aela"], Settings(), 1)
        assert not D.resolve(p, {}, None, module_on=False).enabled
        manual = P.resolve(db, ids["ysolda"], settings(Ysolda={"mode": "manual", "values": {"libido": 40}}), 1)
        d = D.resolve(manual, {}, {"toggles": {"teasing": True}})
    assert d.toggles["teasing"] and not d.toggles["suggestive"] and d.sliders == {}


def test_how_far_depends_on_trust_company_and_mood(world):
    store, ids = world
    d = _dialogue(store, ids["aela"], {"sliders": {"flirtation": 80, "suggestiveness": 80, "privacy": 90}})
    close = D.license_for(d, D.Moment(trust=0.9, attraction=0.8))
    crowd = D.license_for(d, D.Moment(trust=0.9, attraction=0.8, others_listening=True))
    stranger = D.license_for(d, D.Moment())
    sad = D.license_for(d, D.Moment(trust=0.9, attraction=0.8, mood_score=1))
    assert close > crowd and close > stranger and close > sad
    assert D.style_lines(d, "Aela", "D", D.Moment(), topic=False) == []        # no interest, nothing said


def test_dialogue_editor_round_trip(world, tmp_path):
    store, ids = world
    sfile = SettingsFile(str(tmp_path / "personality.json"))
    key = "Skyrim.esm|" + store.scalar("SELECT base_form_id FROM npc WHERE npc_id = ?", (ids["aela"],))
    admin.set_dialogue(sfile, key, "toggles", "suggestive", False)
    admin.set_dialogue(sfile, key, "sliders", "flirtation", 77)
    admin.phrase(sfile, key, "add", "terms_of_affection", "shield-sibling")
    admin.phrase(sfile, key, "add", "terms_of_affection", "Shield-Sibling")
    admin.phrase(sfile, key, "remove", "expressions", "by the Nine")
    with pytest.raises(ValueError):
        admin.set_dialogue(sfile, key, "sliders", "flirtation", 300)
    entry = sfile.get().npcs[key]
    assert entry.dialogue["toggles"] == {"suggestive": False} and entry.dialogue["sliders"] == {"flirtation": 77.0}
    assert entry.dialogue["vocabulary"]["terms_of_affection"] == ["Shield-Sibling"]
    with store.transaction() as db:
        shown = admin.inspect(db, sfile.get(), ids["aela"], 1)["dialogue"]
    assert shown["toggles"]["suggestive"] is False and shown["sliders"]["flirtation"] == 77.0
    admin.reset_dialogue(sfile, key)
    assert sfile.get().npcs[key].dialogue == {}


def test_service_notes_carry_dialogue_style(world, tmp_path):
    store, ids = world
    (tmp_path / "personality.json").write_text(json.dumps({"npcs": {"Aela": {"dialogue": {
        "toggles": {"double_entendres": False}, "vocabulary": {"forbidden": ["wench"]}}}}}))
    svc = PersonalityService(str(tmp_path), str(tmp_path / "personality.json"))
    with store.transaction() as db:
        rel(db, ids["aela"], ids["player"], gt=0.5, trust=0.7, attraction=0.6, familiarity=0.5)
        notes = svc.notes(db, "You're beautiful, you know.", {ids["aela"]: "Aela"}, ids["player"], "D", 1)
        standing = svc.standing(db, ids["aela"], ids["player"], "D", 1)
    assert "double entendres" in notes[0].split("She never uses:")[1] and '"wench"' in notes[0]
    assert any(line.startswith("Her way with flirting") for line in standing)


# ---- intimacy preferences and the OStim adapter ----

import os   # noqa: E402

from livingworld.personality import intimacy as I   # noqa: E402
from livingworld.personality.frameworks import base as FW   # noqa: E402
from livingworld.personality.frameworks import ostim as OS   # noqa: E402

OSTIM = os.path.join(os.path.dirname(__file__), "fixtures", "ostim751b")   # real 7.5.1b files, unchanged


def _prefs(store, npc, manual=None, s=None, gt=1):
    with store.transaction() as db:
        p = P.resolve(db, npc, s or Settings(), gt)
        return I.resolve(db, p, manual)


def scene_event(db, npc, partner, acts, gt, valence, forced=False):
    event = db.execute("INSERT INTO world_event (timeline_id, type, actors_json, payload_json, source, game_time)"
                       " VALUES (1, ?, ?, ?, 'game', ?)",
                       ("intimacy.forced" if forced else "romance.scene", json.dumps([partner, npc]),
                        json.dumps({"acts": acts}), gt)).lastrowid
    db.execute("INSERT INTO event_witness (event_id, npc_id, how) VALUES (?,?, 'participated')", (event, npc))
    mem = db.execute("INSERT INTO memory (owner_id, kind, content, occurred_gt, importance, valence, created_by)"
                     " VALUES (?, 'episodic', 'a night together', ?, 0.6, ?, 'extraction')", (npc, gt, valence)).lastrowid
    db.execute("INSERT INTO memory_source (mem_id, event_id) VALUES (?,?)", (mem, event))
    return event


def test_intimacy_prefs_are_individual_stable_and_independent(world):
    store, ids = world
    a1, a2, y = _prefs(store, ids["aela"]), _prefs(store, ids["aela"]), _prefs(store, ids["ysolda"])
    assert a1.interest == a2.interest and a1.boundaries == a2.boundaries
    assert a1.interest != y.interest and set(a1.interest) == set(I.GROUPS)
    one = _prefs(store, ids["aela"], {"ratings": {"oral": "strong"}})
    assert one.rating("oral") == "strong" and one.sources["oral"] == "manual"
    assert {g: v for g, v in one.interest.items() if g != "oral"} == {g: v for g, v in a1.interest.items() if g != "oral"}


def test_hand_set_rating_is_the_only_thing_that_lifts_a_boundary(world):
    store, ids = world
    s = settings(Ysolda={"values": {"boundaries": ["no_rough_handling"]}})
    assert _prefs(store, ids["ysolda"], s=s).rating("rough") == "boundary"
    lifted = _prefs(store, ids["ysolda"], {"ratings": {"rough": "neutral"}}, s=s)
    assert lifted.rating("rough") == "neutral"
    with store.transaction() as db:
        P.add_change(db, ids["ysolda"], "intimacy.kissing", "list_add", "test", 1.2, value="kissing")
        P.add_change(db, ids["ysolda"], "intimacy.kissing", "drift", "test", 1.3, delta=40)
    p = _prefs(store, ids["ysolda"], s=s, gt=2)
    assert p.rating("kissing") == "boundary"                           # drift never warms a boundary away
    manual, problems = I.validate({"ratings": {"kissing": "adores", "nope": "strong"}, "categories": {"oral": "x"}})
    assert manual == {} and len(problems) == 3


def test_no_intimacy_means_every_sexual_group_is_a_boundary(world):
    store, ids = world
    p = _prefs(store, ids["dinya"], s=settings(Dinya={"values": {"intimacy_boundary": "never"}}))
    assert I.SEXUAL <= p.boundaries and "hand_holding" not in p.boundaries


def test_switched_off_categories_and_module(world):
    store, ids = world
    p = _prefs(store, ids["aela"], {"categories": {"tail": False}})
    assert "tail" in p.off and "tail" not in sum(p.by_rating().values(), [])
    assert not _prefs(store, ids["aela"], {"enabled": False}).enabled
    with store.transaction() as db:
        assert not I.resolve(db, P.resolve(db, ids["aela"], Settings(), 1), None, module_on=False).enabled


def test_scenes_she_enjoyed_warm_only_what_was_in_them(world):
    store, ids = world
    s = Settings()
    with store.transaction() as db:
        evolve.evaluate(db, ids["ysolda"], s, 1)
        before = I.resolve(db, P.resolve(db, ids["ysolda"], s, 1), None)
        scene_event(db, ids["ysolda"], ids["player"], ["kiss", "hug"], 1.2, 0.9)   # OStim aliases
        res = evolve.evaluate(db, ids["ysolda"], s, 1.5)
        after = I.resolve(db, P.resolve(db, ids["ysolda"], s, 1.5), None)
    moved = {c[0] for c in res.changes if c[0].startswith("intimacy.")}
    assert moved == {"intimacy.kissing", "intimacy.affection"}
    assert 0 < after.interest["kissing"] - before.interest["kissing"] <= I.STEP
    assert after.interest["oral"] == before.interest["oral"]


def test_forced_scene_never_warms_and_a_bad_repeat_becomes_a_boundary(world):
    store, ids = world
    s = Settings()
    manual = {"ratings": {"rough": "dislikes"}}
    with store.transaction() as db:
        evolve.evaluate(db, ids["ysolda"], s, 1)
        before = I.resolve(db, P.resolve(db, ids["ysolda"], s, 1), None)
        scene_event(db, ids["ysolda"], ids["player"], ["vaginalsex"], 1.2, 0.8, forced=True)
        evolve.evaluate(db, ids["ysolda"], s, 1.5)
        after = I.resolve(db, P.resolve(db, ids["ysolda"], s, 1.5), None)
        assert after.interest["intercourse"] < before.interest["intercourse"]
        # a group she dislikes (not hand-locked) that goes badly again becomes a boundary, for good
        p = P.resolve(db, ids["ysolda"], s, 2)
        prefs = I.resolve(db, p, None)
        prefs.interest["anal"] = 20.0
        I.after_scene(db, p, prefs, {"anal"}, -0.8, 2, "test", set())
        assert I.resolve(db, p, None).rating("anal") == "boundary"
        # a hand-set rating is locked: scenes never move it
        assert I.after_scene(db, p, I.resolve(db, p, manual), {"rough"}, -0.9, 2.1, "t", {"rough"}) == []


def test_manual_mode_prefs_never_change(world):
    store, ids = world
    s = settings(Ysolda={"mode": "manual", "values": {"libido": 40}})
    with store.transaction() as db:
        p = P.resolve(db, ids["ysolda"], s, 1)
        prefs = I.resolve(db, p, None)
        assert set(prefs.interest.values()) == {50.0} and not prefs.boundaries
        assert I.after_scene(db, p, prefs, {"kissing"}, 1.0, 1, "t", set()) == []


def test_ostim_adapter_reads_real_installed_ids_and_aliases():
    o = OS.OStim(OSTIM)
    assert o.available() and o.actions() == {"hugging", "kissing", "vaginalsex", "vulvaleating", "pullinghair",
                                              "holdingleg", "holdingthigh"}
    assert o.canonical("cunnilingus") == "vulvaleating" and o.group_of("cunnilingus") == "oral"
    assert o.group_of("hug") == "affection" and o.group_of("someonesnewaction") == FW.UNKNOWN
    assert o.unmapped() == []
    scenes = o.scenes()
    assert set(scenes) == {"OStim2PStandingKissEmbraceMF", "OStim2PStandingHugMF", "OStim2PLyingCunnilingusMF",
                           "OStim2PMissionaryMF", "OStim2PDoggyStylePullingHairMF"}
    for sc in scenes.values():
        assert sc.actors == ("male", "female")
        assert all(o.group_of(a.action) != FW.UNKNOWN for a in sc.actions)
    # without the folder, the shipped 7.5.1b alias table still resolves what the game sends
    nowhere = OS.OStim("/does/not/exist")
    assert not nowhere.available() and nowhere.group_of("cunnilingus") == "oral" and nowhere.scenes() == {}
    # the table maps every alias onto an action the adapter knows
    assert all(a in OS.ACTION_GROUPS for a in OS.known_aliases().values())


def test_scene_compatibility_respects_boundaries_and_unknown_actions(world):
    store, ids = world
    o = OS.OStim(OSTIM)
    prefs = _prefs(store, ids["aela"], {"ratings": {"rough": "boundary", "kissing": "strong", "oral": "interested",
                                                    "intercourse": "neutral", "affection": "interested",
                                                    "caressing": "neutral"}})
    hair = FW.judge(o, prefs, o.scenes()["OStim2PDoggyStylePullingHairMF"])
    assert not hair.ok and any("boundary" in r for r in hair.reasons) and hair.groups["rough"] == "receiving"
    lick = FW.judge(o, prefs, o.scenes()["OStim2PLyingCunnilingusMF"])
    assert lick.ok and lick.groups["oral"] == "receiving"
    best = FW.compatible(o, prefs)
    assert [v.scene_id for v in best][0] == "OStim2PStandingKissEmbraceMF"
    assert "OStim2PDoggyStylePullingHairMF" not in [v.scene_id for v in best]
    weird = FW.SceneInfo("X", ("male", "female"), (FW.SceneAction("brandnewact", 0, 1),))
    assert not FW.judge(o, prefs, weird).ok                         # never assumed to be fine
    mapped = OS.OStim(OSTIM, {"brandnewact": "kissing"})
    assert FW.judge(mapped, prefs, weird).ok                        # unless mapped in the settings file
    off = _prefs(store, ids["aela"], {"categories": {"oral": False}})
    assert not FW.judge(o, off, o.scenes()["OStim2PLyingCunnilingusMF"]).ok


def test_in_scene_notes_object_to_a_boundary_and_private_likes_stay_private(world, tmp_path):
    store, ids = world
    (tmp_path / "personality.json").write_text(json.dumps({"npcs": {"Aela": {"intimacy": {
        "ratings": {"rough": "boundary", "kissing": "strong"}}}}}))
    svc = PersonalityService(str(tmp_path), str(tmp_path / "personality.json"))
    with store.transaction() as db:
        notes = svc.notes(db, "Hold still.", {ids["aela"]: "Aela"}, ids["player"], "D", 1,
                          scenes=[({ids["aela"], ids["player"]}, ["pullinghair", "vaginalsex"])])
        assert notes and "does not want this" in notes[0] and "rough" in notes[0]
        stranger = "\n".join(svc.notes(db, "What do you like in bed?", {ids["aela"]: "Aela"}, ids["player"], "D", 1))
        assert "What she enjoys" not in stranger
        assert "will not do" in stranger and "rough" in stranger.split("will not do")[1]
        rel(db, ids["aela"], ids["player"], gt=1.0, trust=0.9, affinity=0.8, intimacy=0.8, familiarity=0.8)
        db.execute("UPDATE relationship SET status = 'partner' WHERE from_id = ? AND to_id = ?",
                   (ids["aela"], ids["player"]))
        close = "\n".join(svc.notes(db, "What do you like in bed?", {ids["aela"]: "Aela"}, ids["player"], "D", 2))
    assert "What she enjoys" in close and "kissing" in close.split("What she enjoys")[1].split("\n")[0]


def test_intimacy_editor_round_trip(world, tmp_path):
    store, ids = world
    sfile = SettingsFile(str(tmp_path / "personality.json"))
    key = "Skyrim.esm|" + store.scalar("SELECT base_form_id FROM npc WHERE npc_id = ?", (ids["aela"],))
    admin.set_rating(sfile, key, "anal", "boundary")
    admin.set_rating(sfile, key, "kissing", "strong")
    admin.set_category(sfile, key, "vampire", False)
    with pytest.raises(ValueError):
        admin.set_rating(sfile, key, "kissing", "adores")
    assert sfile.get().npcs[key].intimacy == {"ratings": {"anal": "boundary", "kissing": "strong"},
                                              "categories": {"vampire": False}}
    with store.transaction() as db:
        shown = admin.inspect(db, sfile.get(), ids["aela"], 1)["intimacy"]
    assert shown["ratings"]["anal"] == "boundary" and shown["ratings"]["kissing"] == "strong"
    assert "vampire" in shown["off"]
    admin.set_rating(sfile, key, "anal", None)
    assert sfile.get().npcs[key].intimacy["ratings"] == {"kissing": "strong"}
