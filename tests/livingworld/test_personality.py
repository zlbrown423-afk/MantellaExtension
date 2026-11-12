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
