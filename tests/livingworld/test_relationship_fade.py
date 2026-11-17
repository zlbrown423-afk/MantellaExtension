"""Relationships and reputations fade (architecture audit M4, migration 026).

Each relationship or standing change counts less as it ages, down to a lasting mark held within a cap, so a
feeling left without contact drifts back toward where the relationship started. Kin, spouses and partners do
not fade; MANTELLA_LW_FADE=0 shows the plain sums; loading an older save shows feelings as they were then.
"""
import json

import pytest

from livingworld.store import fading
from livingworld.store.timeline import resolve_load
from livingworld.store.world_store import WorldStore

GERDUR, PLAYER, HOD = 1, 2, 3


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv(fading.ENV_VAR, raising=False)
    s = WorldStore(str(tmp_path / "w.sqlite"))
    with s.transaction() as db:
        for npc_id, name, player in ((GERDUR, "Gerdur", 0), (PLAYER, "Player", 1), (HOD, "Hod", 0)):
            db.execute("INSERT INTO npc (npc_id, plugin, base_form_id, display_name, is_player, created_gt)"
                       " VALUES (?, 'Skyrim.esm', ?, ?, ?, 1)", (npc_id, f"0x{npc_id}", name, player))
    yield s
    s.close()


def relate(store, a, b, kinship=None, status="acquaintance", **base):
    cols = ", ".join(["from_id", "to_id", "kinship", "status", "updated_gt", *base])
    marks = ", ".join("?" * (5 + len(base)))
    with store.transaction() as db:
        db.execute(f"INSERT INTO relationship ({cols}) VALUES ({marks})", (a, b, kinship, status, 1.0, *base.values()))


def change(store, a, b, gt, **deltas):
    with store.transaction() as db:
        db.execute("INSERT INTO relationship_change (from_id, to_id, deltas_json, reason, game_time)"
                   " VALUES (?,?,?,?,?)", (a, b, json.dumps(deltas), "test", gt))


def standing(store, holder, subject, domain, delta, gt, firsthand=1):
    with store.transaction() as db:
        db.execute("INSERT INTO standing_change (holder_id, subject_id, domain, delta, firsthand, game_time)"
                   " VALUES (?,?,?,?,?,?)", (holder, subject, domain, delta, firsthand, gt))


def talk_at(store, gt):
    """A conversation starting at game day ``gt``: this is what moves "now" forward."""
    with store.transaction() as db:
        db.execute("INSERT INTO conversation (timeline_id, location, kind, started_gt) VALUES (1, 'Riverwood', 'player', ?)",
                   (gt,))


def feel(store, a, b, col):
    return store.scalar(f"SELECT {col} FROM v_relationship WHERE from_id = ? AND to_id = ?", (a, b))


def raw(store, a, b, col):
    return store.scalar(f"SELECT {col} FROM v_relationship_raw WHERE from_id = ? AND to_id = ?", (a, b))


def view(store, holder, subject, domain, name="v_standing"):
    return store.scalar(f"SELECT value FROM {name} WHERE holder_id = ? AND subject_id = ? AND domain = ?",
                        (holder, subject, domain))


def test_nothing_fades_within_three_days(store):
    relate(store, GERDUR, PLAYER)
    change(store, GERDUR, PLAYER, 10.0, trust=-0.6, affinity=0.3)
    talk_at(store, 12.9)
    assert feel(store, GERDUR, PLAYER, "trust") == pytest.approx(-0.6)
    assert feel(store, GERDUR, PLAYER, "affinity") == pytest.approx(0.3)


def test_a_bad_evening_fades_to_a_lasting_mark(store):
    relate(store, GERDUR, PLAYER)
    change(store, GERDUR, PLAYER, 10.0, trust=-0.6)
    talk_at(store, 10.0 + 3 + 30)   # half of the fading part is gone after 3 + 30 days
    assert feel(store, GERDUR, PLAYER, "trust") == pytest.approx(0.4 * -0.6 + 0.6 * -0.6 * 0.5)
    talk_at(store, 10.0 + 3 + 3000)   # years later only the lasting part is left
    assert feel(store, GERDUR, PLAYER, "trust") == pytest.approx(0.4 * -0.6, abs=0.01)
    assert raw(store, GERDUR, PLAYER, "trust") == pytest.approx(-0.6)   # the plain sum is still there


def test_a_hundred_grabs_no_longer_set_a_permanent_ceiling(store):
    relate(store, GERDUR, PLAYER)
    for i in range(100):
        change(store, GERDUR, PLAYER, 10.0 + i * 0.001, trust=-0.16, fear=0.05)
    talk_at(store, 11.0)
    assert feel(store, GERDUR, PLAYER, "trust") == -1.0
    talk_at(store, 5000.0)
    trust = feel(store, GERDUR, PLAYER, "trust")
    assert -0.5 < trust < -0.4    # the lasting mark is capped at -0.4, plus what is still fading
    talk_at(store, 100000.0)
    assert feel(store, GERDUR, PLAYER, "trust") == pytest.approx(-0.4, abs=0.01)
    assert feel(store, GERDUR, PLAYER, "fear") == pytest.approx(0.2, abs=0.01)   # fear's cap
    # and something kind now moves her again
    change(store, GERDUR, PLAYER, 100000.0, trust=0.5)
    assert feel(store, GERDUR, PLAYER, "trust") == pytest.approx(-0.4 + 0.6 * 0.5, abs=0.01)


def test_feelings_fade_toward_the_seeded_base_not_zero(store):
    relate(store, GERDUR, HOD, status="friend", affinity=0.5, familiarity=0.6)
    change(store, GERDUR, HOD, 5.0, affinity=0.4)
    talk_at(store, 5000.0)
    assert feel(store, GERDUR, HOD, "affinity") == pytest.approx(0.5 + 0.35 * 0.4, abs=0.01)
    assert feel(store, GERDUR, HOD, "familiarity") == pytest.approx(0.6)


def test_fresh_contact_adds_without_bringing_old_feelings_back(store):
    relate(store, GERDUR, PLAYER)
    change(store, GERDUR, PLAYER, 10.0, affinity=0.8)
    talk_at(store, 1000.0)
    faded = feel(store, GERDUR, PLAYER, "affinity")
    change(store, GERDUR, PLAYER, 1000.0, affinity=0.05)
    assert feel(store, GERDUR, PLAYER, "affinity") == pytest.approx(faded + 0.05, abs=0.001)


def test_quick_feelings_fade_faster_than_slow_ones(store):
    relate(store, GERDUR, PLAYER)
    change(store, GERDUR, PLAYER, 0.0, fear=0.6, familiarity=0.6, respect=0.6)
    talk_at(store, 20.0)
    fear = feel(store, GERDUR, PLAYER, "fear")
    fam = feel(store, GERDUR, PLAYER, "familiarity")
    respect = feel(store, GERDUR, PLAYER, "respect")
    assert fear < respect < fam <= 0.6
    assert fam > 0.55   # you do not forget who someone is in three weeks


@pytest.mark.parametrize("kinship,status", [("sibling", "acquaintance"), (None, "spouse"), (None, "partner")])
def test_kin_spouses_and_partners_do_not_fade(store, kinship, status):
    relate(store, GERDUR, HOD, kinship=kinship, status=status)
    change(store, GERDUR, HOD, 0.0, affinity=0.7, trust=0.6)
    talk_at(store, 900.0)
    assert feel(store, GERDUR, HOD, "affinity") == pytest.approx(0.7)
    assert feel(store, GERDUR, HOD, "trust") == pytest.approx(0.6)


def test_standing_fades_and_hearsay_fades_faster(store):
    standing(store, GERDUR, PLAYER, "decency", -0.3, 0.0, firsthand=1)
    standing(store, HOD, PLAYER, "decency", -0.3, 0.0, firsthand=0)
    talk_at(store, 2.0)
    assert view(store, GERDUR, PLAYER, "decency") == pytest.approx(-0.3)
    talk_at(store, 3.0 + 15.0)
    seen = view(store, GERDUR, PLAYER, "decency")
    heard = view(store, HOD, PLAYER, "decency")
    assert heard == pytest.approx(0.4 * -0.3 + 0.6 * -0.3 * 0.5)
    assert heard > seen > -0.3
    assert view(store, HOD, PLAYER, "decency", "v_standing_raw") == pytest.approx(-0.3)


def test_standing_sixteen_deep_comes_back_to_the_cap(store):
    for i in range(80):
        standing(store, GERDUR, PLAYER, "decency", -0.2, 10.0 + i * 0.001)
    talk_at(store, 10.5)
    assert view(store, GERDUR, PLAYER, "decency") == -1.0
    talk_at(store, 100000.0)
    assert view(store, GERDUR, PLAYER, "decency") == pytest.approx(-0.4, abs=0.01)


def test_loading_an_older_save_shows_feelings_as_they_were_then(store):
    relate(store, GERDUR, PLAYER)
    change(store, GERDUR, PLAYER, 10.0, trust=-0.6)
    talk_at(store, 200.0)
    later = feel(store, GERDUR, PLAYER, "trust")
    assert later > -0.5
    resolve_load(store, 1234, 1, 11.0, "older save")
    assert store.scalar("SELECT now_gt FROM fade_clock") == pytest.approx(11.0)
    assert feel(store, GERDUR, PLAYER, "trust") == pytest.approx(-0.6)


def test_the_off_switch_shows_the_plain_sums(tmp_path, monkeypatch):
    path = str(tmp_path / "off.sqlite")
    s = WorldStore(path)
    with s.transaction() as db:
        for npc_id in (GERDUR, PLAYER):
            db.execute("INSERT INTO npc (npc_id, plugin, base_form_id, display_name, created_gt)"
                       " VALUES (?, 'Skyrim.esm', ?, 'x', 1)", (npc_id, f"0x{npc_id}"))
    relate(s, GERDUR, PLAYER)
    change(s, GERDUR, PLAYER, 0.0, trust=-0.6)
    standing(s, GERDUR, PLAYER, "honesty", -0.5, 0.0)
    talk_at(s, 500.0)
    assert feel(s, GERDUR, PLAYER, "trust") > -0.4
    s.close()

    monkeypatch.setenv(fading.ENV_VAR, "0")
    s = WorldStore(path)
    assert feel(s, GERDUR, PLAYER, "trust") == pytest.approx(-0.6)
    assert view(s, GERDUR, PLAYER, "honesty") == pytest.approx(-0.5)
    s.close()

    monkeypatch.delenv(fading.ENV_VAR)
    s = WorldStore(path)
    assert feel(s, GERDUR, PLAYER, "trust") > -0.4
    s.close()


def test_no_clock_yet_means_no_fading(store):
    relate(store, GERDUR, PLAYER)
    change(store, GERDUR, PLAYER, 0.0, trust=-0.6)
    assert store.scalar("SELECT now_gt FROM fade_clock") is None
    assert feel(store, GERDUR, PLAYER, "trust") == pytest.approx(-0.6)


def test_a_conversation_on_another_timeline_does_not_move_the_clock(store):
    talk_at(store, 50.0)
    with store.transaction() as db:
        db.execute("INSERT INTO timeline (timeline_id, parent_id, branch_game_time, is_active) VALUES (9, 1, 1.0, 0)")
        db.execute("INSERT INTO conversation (timeline_id, location, kind, started_gt) VALUES (9, 'x', 'player', 900)")
    assert store.scalar("SELECT now_gt FROM fade_clock") == pytest.approx(50.0)


def test_views_keep_their_columns(store):
    rel_cols = [r[1] for r in store.query("PRAGMA table_info(v_relationship)")]
    assert rel_cols == [r[1] for r in store.query("PRAGMA table_info(v_relationship_raw)")]
    assert rel_cols == ["from_id", "to_id", "kinship", "status", "trust", "affinity", "fear", "respect", "admiration",
                        "suspicion", "attraction", "familiarity", "intimacy", "commitment", "satisfaction", "jealousy"]
    assert [r[1] for r in store.query("PRAGMA table_info(v_standing)")] == \
        ["holder_id", "subject_id", "domain", "value", "evidence", "firsthand", "last_gt"]


def test_reading_one_pair_uses_the_indexes(store):
    plan = " ".join(r[3] for r in store.query(
        "EXPLAIN QUERY PLAN SELECT * FROM v_relationship WHERE from_id = 1 AND to_id = 2"))
    assert "relationship_change_pair" in plan and "SCAN c" not in plan
