"""Territory (livingworld/territory): the ownership mirror, garrison roster, who knows what, on a real in-memory DB.

Rule tests only. They cannot show the game side (LWTerritory.esp spawning, persistence across a real save)."""
import json

import pytest

from livingworld.store.identity import IdentityRegistry
from livingworld.store.world_store import WorldStore
from livingworld.territory import debug as TD, gossip as G, ingest as I, prompts as P, records as R, store as S
from livingworld.territory import territory_debug_enabled, territory_enabled
from livingworld.territory.sites import all_sites, by_form_id, by_key


@pytest.fixture
def store():
    return WorldStore(":memory:")


@pytest.fixture
def db(store):
    with store.transaction() as conn:
        yield conn


def actor(ref, base, name, player=0):
    return f"{ref}^{base}^Skyrim.esm^Skyrim.esm^{player}^{name}"


def wit(ref, base, name, dist=10.0, los=1):
    return f"w={dist}^{los}^0^0^{actor(ref, base, name)}"


def rec(key, via, site="halted_stream_camp", gt=10.0, **kw):
    parts = [f"v=1", f"key={key}", "type=territory", f"via={via}", f"gt={gt}", f"site={site}"]
    parts += [f"{k}={v}" for k, v in kw.items() if k != "w"]
    parts += kw.get("w", [])
    return "|".join(parts)


def npc(db, name, base, home=None, child=False):
    return db.execute("INSERT INTO npc (plugin, base_form_id, ref_form_id, display_name, is_player, home_location,"
                      " is_child, created_gt) VALUES ('Skyrim.esm', ?, ?, ?, 0, ?, ?, 0)",
                      (base, base + "r", name, home, int(child))).lastrowid


def test_registry():
    assert len(all_sites()) == 8
    assert by_key("halted_stream_camp").form_id == "0x00019193"
    assert by_form_id("0x19193").key == "halted_stream_camp"
    assert by_form_id("00019193", "Skyrim.esm").key == "halted_stream_camp"
    assert by_key("fort_greymoor") is None   # civil-war forts stay out on purpose


def test_flags(monkeypatch):
    monkeypatch.delenv("MANTELLA_LW_TERRITORY", raising=False)
    assert not territory_enabled() and not territory_debug_enabled()
    monkeypatch.setenv("MANTELLA_LW_TERRITORY", "1")
    assert territory_enabled() and territory_debug_enabled()
    monkeypatch.setenv("MANTELLA_LW_TERRITORY_DEBUG", "0")
    assert not territory_debug_enabled()


def test_parse_good_and_bad():
    r = R.parse(rec("k1", "occupied", owner="thalmor", prev="unoccupied", strength=5,
                    w=[wit(1, 0x13B9A, "Hod")]))
    assert (r.via, r.owner, r.strength, len(r.witnesses)) == ("occupied", "thalmor", 5, 1)
    for bad in (rec("k", "occupied", owner="elves"), rec("k", "occupied"), rec("k", "cleared", owner="unoccupied"),
                rec("k", "occupied", site="fort_greymoor", owner="thalmor"), rec("k", "dance", owner="thalmor"),
                rec("k", "assign", owner="thalmor"), "v=2|key=k|type=territory|via=occupied|gt=1|site=halted_stream_camp"):
        with pytest.raises(Exception):
            R.parse(bad)
    t = R.parse(rec("k", "transfer", npc=actor(5, 0x500, "Soldier"), faction="wardens", to="silent_moons_camp"))
    assert t.to == "silent_moons_camp"


def feed(store, *raw):
    return I.ingest(store, IdentityRegistry(store), list(raw))


def test_occupation_lifecycle_and_dates(store):
    r = feed(store, rec("a", "occupied", gt=10, owner="thalmor", prev="unoccupied", strength=5, since=10),
             rec("b", "reinforced", gt=12, owner="thalmor", prev="thalmor", strength=7),
             rec("c", "cleared", gt=20, owner="unoccupied", prev="thalmor", strength=0, cooldown=34))
    assert (r.applied, r.skipped) == (3, 0)
    with store.reading() as db:
        st = S.state_of(db, "halted_stream_camp")
        assert (st.owner, st.previous_owner, st.cooldown_gt) == ("unoccupied", "thalmor", 34.0)
        ch = S.changes(db, "halted_stream_camp")
        assert [c["kind"] for c in ch] == ["cleared", "reinforced", "occupied"]
        assert ch[1]["held_since"] == 10          # a reinforcement keeps the original occupation date
        ev = db.execute("SELECT type, payload_json FROM world_event WHERE type LIKE 'territory.%' ORDER BY event_id").fetchall()
        assert [e[0] for e in ev] == ["territory.occupied", "territory.reinforced", "territory.cleared"]
        p = json.loads(ev[0][1])
        for k in ("location_key", "owner_before", "owner_after", "strength", "contested", "source", "confidence"):
            assert k in p
        assert p["location_key"] == "halted_stream_camp" and p["owner_after"] == "thalmor"
        assert S.state_of(db, "silent_moons_camp").owner == "unoccupied"   # others untouched


def test_repeated_record_changes_nothing(store):
    line = rec("a", "occupied", owner="thalmor", prev="unoccupied", strength=4)
    assert feed(store, line).applied == 1
    r = feed(store, line)
    assert (r.applied, r.duplicate) == (0, 1)
    with store.reading() as db:
        assert len(S.changes(db, "halted_stream_camp")) == 1


def test_bad_record_does_not_stop_the_rest(store):
    r = feed(store, "garbage", rec("x", "occupied", owner="thalmor", prev="unoccupied", strength=3))
    assert (r.applied, r.skipped) == (1, 1)


def test_witness_knows_non_witness_does_not(store):
    feed(store, rec("a", "occupied", owner="thalmor", prev="unoccupied", strength=5,
                    w=[wit(1, 0x13B9A, "Hod", dist=8), wit(2, 0x13B9B, "FarAway", dist=90), wit(3, 0x13B9C, "Blind", los=0)]))
    with store.reading() as db:
        names = {r[0]: r[1] for r in db.execute("SELECT n.display_name, k.origin FROM v_territory_knowledge k"
                                                  " JOIN npc n ON n.npc_id = k.holder_id")}
        assert names == {"Hod": "witnessed"}       # too far and no line of sight: not told by the game, not omniscient
        hod = db.execute("SELECT npc_id FROM npc WHERE display_name = 'Hod'").fetchone()[0]
        lines = P.lines(db, hod, "Hod", 11.0)
        assert any("saw for themselves" in l and "the Thalmor hold Halted Stream Camp" in l for l in lines)
        far = db.execute("SELECT npc_id FROM npc WHERE display_name = 'FarAway'").fetchone()
        assert far is None or P.lines(db, far[0], "FarAway", 11.0) == []
        assert db.execute("SELECT COUNT(*) FROM event_witness").fetchone()[0] == 1


def test_children_never_know_or_serve(store):
    with store.transaction() as db:
        kid = npc(db, "Child", "0000AA01", child=True)
        a = S.timeline(db)
        assert not S.learn(db, kid, "halted_stream_camp", "thalmor", 3, False, "witnessed", 1.0, 1, 1, None)
        assert S.assign(db, "halted_stream_camp", kid, "thalmor", 1.0) is None


def test_garrison_no_duplicates_and_transfer(store):
    with store.transaction() as db:
        a = npc(db, "Soldier A", "00000A01")
        b = npc(db, "Soldier B", "00000A02")
        assert S.assign(db, "halted_stream_camp", a, "wardens", 5, rank="commander", squad="1", duty="gate") is not None
        assert S.assign(db, "halted_stream_camp", a, "wardens", 6, rank="commander", squad="1", duty="gate") is None  # same post
        assert len(S.roster(db, "halted_stream_camp")) == 1
        S.assign(db, "silent_moons_camp", a, "wardens", 7)      # moved: the old post ends, only one live post
        assert S.post_of(db, a).site_key == "silent_moons_camp"
        assert S.roster(db, "halted_stream_camp") == []
        S.assign(db, "halted_stream_camp", a, "wardens", 8, rank="commander")
        S.assign(db, "halted_stream_camp", b, "wardens", 9, rank="commander")   # one commander per holding
        assert S.commander(db, "halted_stream_camp").npc_id == b
        assert {p.npc_id: p.rank for p in S.roster(db, "halted_stream_camp")} == {a: "soldier", b: "commander"}
        assert db.execute("SELECT COUNT(*) FROM v_garrison WHERE npc_id = ?", (a,)).fetchone()[0] == 1


def test_garrison_records_and_site_clearing_ends_posts(store):
    soldier = actor(7, 0x700, "Thalmor Soldier")
    feed(store, rec("o", "occupied", gt=10, owner="thalmor", prev="unoccupied", strength=2),
         rec("g1", "assign", gt=10, npc=soldier, faction="thalmor", rank="soldier", squad="A", duty="patrol",
             equip="Elven Sword;Elven Shield", hp="1.0"))
    with store.reading() as db:
        post = S.roster(db, "halted_stream_camp")[0]
        assert (post.faction, post.squad, post.duty, post.equipment) == ("thalmor", "A", "patrol", ("Elven Sword", "Elven Shield"))
        who = post.npc_id
        assert any("serves in the garrison the Thalmor keep at Halted Stream Camp" in l or "the Thalmor keep" in l
                   for l in P.lines(db, who, "Thalmor Soldier", 11))
    feed(store, rec("c", "cleared", gt=15, owner="unoccupied", prev="thalmor", cooldown=29))
    with store.reading() as db:
        assert S.roster(db, "halted_stream_camp") == []        # the garrison is gone with the site
        assert S.post_of(db, who) is None


def test_recall_and_dead_records(store):
    soldier = actor(7, 0x700, "Warden Rider")
    feed(store, rec("e", "established", gt=10, owner="wardens", prev="unoccupied", strength=1),
         rec("g1", "assign", gt=10, npc=soldier, faction="wardens"), rec("g2", "recall", gt=12, npc=soldier),
         rec("g3", "recall", gt=12.5, npc=soldier))   # a repeated recall is harmless
    with store.reading() as db:
        assert S.roster(db, "halted_stream_camp") == []
        assert db.execute("SELECT status FROM v_garrison_status ORDER BY game_time DESC LIMIT 1").fetchone()[0] == "recalled"


def test_commander_hears_reports_of_their_own_site(store):
    cmd = actor(9, 0x900, "Captain Wolf")
    feed(store, rec("e", "established", gt=10, owner="wardens", prev="unoccupied", strength=1),
         rec("g1", "assign", gt=10, npc=cmd, faction="wardens", rank="commander"),
         rec("r", "reinforced", gt=11, owner="wardens", prev="wardens", strength=4))
    with store.reading() as db:
        k = db.execute("SELECT origin, confidence, strength FROM v_territory_knowledge").fetchall()
        assert [(x[0], x[2]) for x in k] == [("reported", 4)] and k[0][1] < 1.0


def test_gossip_spreads_thinly_with_source_and_never_twice(store):
    feed(store, rec("a", "occupied", gt=10, owner="thalmor", prev="unoccupied", strength=5,
                    w=[wit(1, 0x13B9A, "Hod", dist=5)]))
    with store.transaction() as db:
        for i in range(30):
            npc(db, f"Whiterunner{i}", f"0000B{i:03d}", home="Whiterun")
        outsider = npc(db, "Far Traveller", "0000C001", home="Solitude")
        assert G.spread(db, 10.5) == 0                       # too soon: nobody has talked yet
        first = G.spread(db, 17.0)                           # past the 6-day reach: everyone who was going to hear has
        assert 8 <= first <= 25                              # some, not everyone
        assert G.spread(db, 17.0) == 0                       # one copy each, no repeats
        rows = db.execute("SELECT * FROM v_territory_knowledge WHERE origin = 'told'").fetchall()
        assert all(r["told_by_id"] is not None and r["confidence"] <= 0.5 for r in rows)
        assert not any(r["holder_id"] == outsider for r in rows)
        # seeded: a second world run gives the same people the same news
    s2 = WorldStore(":memory:")
    feed(s2, rec("a", "occupied", gt=10, owner="thalmor", prev="unoccupied", strength=5, w=[wit(1, 0x13B9A, "Hod", dist=5)]))
    with s2.transaction() as d2:
        for i in range(30):
            npc(d2, f"Whiterunner{i}", f"0000B{i:03d}", home="Whiterun")
        assert G.spread(d2, 17.0) == first


def test_nothing_spreads_if_nobody_saw_it(store):
    feed(store, rec("a", "occupied", gt=10, owner="thalmor", prev="unoccupied", strength=5))
    with store.transaction() as db:
        npc(db, "Someone", "0000B001", home="Whiterun")
        assert G.spread(db, 14.0) == 0


def test_hearsay_never_overrides_what_they_saw(store):
    feed(store, rec("a", "occupied", gt=10, owner="thalmor", prev="unoccupied", strength=5,
                    w=[wit(1, 0x13B9A, "Hod", dist=5)]),
         rec("c", "cleared", gt=20, owner="unoccupied", prev="thalmor", cooldown=30))
    with store.transaction() as db:
        hod = db.execute("SELECT npc_id FROM npc WHERE display_name = 'Hod'").fetchone()[0]
        # Hod saw it occupied at 10; a rumour of the older state cannot replace a newer fact he holds
        S.learn(db, hod, "halted_stream_camp", "bandit", 3, False, "rumor", 0.3, 4.0, 22.0, None)
        row = S.known(db, hod)[0]
        assert row["owner"] == "thalmor" and row["origin"] == "witnessed"
        line = [l for l in P.lines(db, hod, "Hod", 23.0) if "Halted" in l][0]
        assert "saw for themselves" in line and "the Thalmor hold" in line


def test_rolls_back_with_an_older_save(store):
    feed(store, rec("a", "occupied", gt=10, owner="thalmor", prev="unoccupied", strength=5, w=[wit(1, 0x13B9A, "Hod", dist=5)]),
         rec("b", "cleared", gt=20, owner="unoccupied", prev="thalmor", cooldown=34))
    with store.transaction() as db:
        old = db.execute("SELECT timeline_id FROM timeline WHERE is_active = 1").fetchone()[0]
        db.execute("UPDATE timeline SET is_active = 0 WHERE timeline_id = ?", (old,))
        db.execute("INSERT INTO timeline (parent_id, branch_game_time, save_name, is_active) VALUES (?,?,'s',1)", (old, 15.0))
    with store.reading() as db:
        st = S.state_of(db, "halted_stream_camp")
        assert st.owner == "thalmor" and st.cooldown_gt is None       # the older save: occupied, not yet cleared
    with store.transaction() as db:
        db.execute("UPDATE timeline SET is_active = 0 WHERE parent_id IS NOT NULL")
        db.execute("INSERT INTO timeline (parent_id, branch_game_time, save_name, is_active) VALUES (1, 5.0, 's', 1)")
    with store.reading() as db:
        assert S.state_of(db, "halted_stream_camp").owner == "unoccupied"   # before it all
        assert S.known(db, db.execute("SELECT npc_id FROM npc WHERE display_name='Hod'").fetchone()[0]) == []


def test_sighted_is_news_only(store):
    feed(store, rec("e", "established", gt=10, owner="wardens", prev="unoccupied", strength=2),
         rec("s", "sighted", site="silent_moons_camp", gt=12, owner="thalmor", strength=4, w=[wit(1, 0x13B9A, "Warden Scout", dist=30)]))
    with store.reading() as db:
        assert S.state_of(db, "silent_moons_camp").owner == "unoccupied"    # a sighting does not change the mirror
        assert db.execute("SELECT COUNT(*) FROM v_visible_event WHERE type = 'territory.sighted'").fetchone()[0] == 1
        scout = db.execute("SELECT npc_id FROM npc WHERE display_name = 'Warden Scout'").fetchone()[0]
        assert S.known(db, scout)[0]["owner"] == "thalmor"


def test_debug_commands(store):
    with store.transaction() as db:
        hod = npc(db, "Hod", "0000D001")
        assert TD.parse("debug territory occupy halted stream camp") == ("occupy", "halted_stream_camp")
        assert TD.parse("Debug territory status") == ("status", None)
        assert TD.parse("hello there") is None
        out = TD.run(db, "debug territory occupy Redoran's Retreat", hod, 30.0)
        assert "redoran" in out.lower() and "thalmor" in out
        assert S.state_of(db, "redorans_retreat").owner == "thalmor"
        TD.run(db, "debug territory clear redorans retreat", hod, 31.0)
        st = S.state_of(db, "redorans_retreat")
        assert st.owner == "unoccupied" and st.cooldown_gt == pytest.approx(45.0)
        assert "unoccupied" in TD.run(db, "debug territory status", hod, 31.0)
        assert db.execute("SELECT source FROM v_territory_change ORDER BY change_id DESC").fetchone()[0] == "admin"


def test_prompt_without_knowledge_is_empty(store):
    with store.transaction() as db:
        n = npc(db, "Hulda", "0000D002")
        assert P.lines(db, n, "Hulda", 5.0) == []
