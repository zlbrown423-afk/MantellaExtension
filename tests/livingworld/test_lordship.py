"""Lordship L0 (civic institutions + paper ledger) and L1 (the charter and its monthly reckoning).

Rule tests on a real in-memory database. They do not prove how officials sound in game or that the cavalry
mod's charter reaches the server (L2 bridge); see the in-game checklist in the README.
"""
import json

import pytest

from livingworld.civic import institutions as I, ledger as L
from livingworld.dialogue import debug_lines
from livingworld.lordship import COMPANY, COURT, lordship_debug_enabled, lordship_enabled
from livingworld.lordship import charter as C, data as D, debug as LD, prompts as P, reckoning as R
from livingworld.lordship import scenarios as S
from livingworld.social import processor
from livingworld.store.world_store import WorldStore

DAYS = 30


@pytest.fixture
def db():
    store = WorldStore(":memory:")
    with store.transaction() as conn:
        yield conn


@pytest.fixture
def ids(db):
    return S.world(db)


def month(db, charter, m, riders=4, patrols=13, report=True, horses=4):
    at = charter.signed_gt + (m - 1) * DAYS + 1.0
    C.report_fact(db, "strength", at, charter.holder_npc_id, {"riders": riders, "horses": horses})
    if patrols:
        C.report_fact(db, "patrol", at + 1, charter.holder_npc_id, {"route": "west_road", "count": patrols})
    if report:
        C.report_fact(db, "report", at + 2, charter.holder_npc_id, {})
    return R.catch_up(db, charter.signed_gt + m * DAYS + 0.001)


# ---- flags ----

def test_off_by_default(monkeypatch):
    monkeypatch.delenv("MANTELLA_LW_LORDSHIP", raising=False)
    assert not lordship_enabled() and not lordship_debug_enabled()
    monkeypatch.setenv("MANTELLA_LW_LORDSHIP", "1")
    assert lordship_enabled() and lordship_debug_enabled()
    monkeypatch.setenv("MANTELLA_LW_LORDSHIP_DEBUG", "0")
    assert not lordship_debug_enabled()


# ---- L0: civic core ----

def test_institution_is_founded_once(db):
    a = I.ensure(db, "plains_wardens", "the Plains Wardens", "company", 5.0, "Whiterun")
    b = I.ensure(db, "plains_wardens", "renamed", "company", 9.0)
    assert a.inst_id == b.inst_id and b.name == "the Plains Wardens"
    with pytest.raises(ValueError):
        I.ensure(db, "x", "x", "army", 1.0)


def test_ledger_balance_is_the_sum_and_transfers_pair_up(db):
    key = L.transfer(db, "inst:court", "inst:co", 100, "stipend", 1.0)
    L.record(db, "inst:co", -30, "wage", 2.0, "riders")
    assert L.balance(db, "inst:co") == 70 and L.balance(db, "inst:court") == -100
    assert L.balance(db, "inst:co", at_gt=1.5) == 100
    rows = db.execute("SELECT SUM(amount), COUNT(*) FROM ledger_entry WHERE transfer_key = ?", (key,)).fetchone()
    assert tuple(rows) == (0, 2)
    assert L.transfer(db, "a", "b", 0, "gift", 1.0) is None
    with pytest.raises(ValueError):
        L.record(db, "a", 5, "bribe", 1.0)


def test_ledger_is_append_only(db):
    L.record(db, "inst:co", 10, "gift", 1.0)
    with pytest.raises(Exception):
        db.execute("UPDATE ledger_entry SET amount = 99")


def test_ledger_and_charter_roll_back_with_an_older_save(db, ids):
    charter = C.grant(db, ids["player"], 1.0)
    month(db, charter, 1)
    month(db, charter, 2)
    assert R.periods_done(db, charter) == 2
    money = C.treasury(db)
    # load a save made between month 1 and month 2
    db.execute("UPDATE timeline SET is_active = 0")
    db.execute("INSERT INTO timeline (parent_id, branch_game_time, is_active) VALUES (1, ?, 1)", (charter.signed_gt + 45,))
    assert R.periods_done(db, charter) == 1
    assert C.treasury(db) < money
    assert C.current(db).charter_id == charter.charter_id
    # a save from before the charter shows none
    db.execute("UPDATE timeline SET is_active = 0")
    db.execute("INSERT INTO timeline (parent_id, branch_game_time, is_active) VALUES (1, 0.5, 1)")
    assert C.current(db) is None and C.treasury(db) == 0


# ---- data ----

def test_data_files_are_consistent():
    places = {m["name"] for m in json.load(open("livingworld/fog/data/map_markers.json", encoding="utf-8"))
              if isinstance(m, dict) and "name" in m}
    for d in D.districts().values():
        assert set(d.places) <= places, set(d.places) - places
        for route in d.routes.values():
            assert set(route) <= places
    levels = D.terms().levels
    assert sorted(levels) == [1, 2, 3, 4, 5]
    assert all(levels[n]["expected_power"] < levels[n + 1]["expected_power"] for n in range(1, 5))
    assert {o.key for o in D.officials()} == {"balgruuf", "proventus", "irileth"}
    assert D.official_for("Skyrim.esm", "0x13BBD").key == "balgruuf"
    assert D.norm_form("13bba") == D.norm_form("0x00013BBA") == "00013BBA"


def test_a_level_one_company_at_its_cap_pays_its_way():
    t, lv = D.terms(), D.level_terms(1)
    income = (lv["stipend_per_year"] + lv["equipment_per_year"]) / 12
    cost = (lv["personnel_cap"] - 1) * t.wage_per_rider_month + 4 * t.feed_per_horse_month
    assert income >= cost


# ---- grant ----

def test_grant_founds_both_institutions_and_supersedes(db, ids):
    c1 = C.grant(db, ids["player"], 1.0)
    assert c1.status == "active" and c1.version == 1 and c1.terms["personnel_cap"] == 8
    assert I.get(db, COURT).head_id == ids["balgruuf"] and I.get(db, COMPANY).head_id == ids["player"]
    c2 = C.grant(db, ids["player"], 50.0, level=2)
    assert c2.version == 2 and c2.parent_charter_id == c1.charter_id and c2.level == 2
    assert db.execute("SELECT status FROM v_charter WHERE charter_id = ?", (c1.charter_id,)).fetchone()[0] == "superseded"
    ev = db.execute("SELECT COUNT(*) FROM world_event WHERE type = 'lordship.charter_granted'").fetchone()[0]
    assert ev == 2
    with pytest.raises(ValueError):
        C.grant(db, ids["player"], 60.0, district_key="nowhere")


# ---- compliance ----

def test_unreported_terms_are_excused_not_punished(db, ids):
    charter = C.grant(db, ids["player"], 1.0)
    R.catch_up(db, charter.signed_gt + DAYS + 0.01)
    outcomes = dict(db.execute("SELECT term_key, outcome FROM compliance").fetchall())
    assert outcomes == {"personnel": "excused", "patrols": "excused", "report": "excused"}
    assert C.treasury(db) > 0          # full stipend paid


def test_declared_watching_turns_silence_into_a_missed_term(db, ids):
    charter = C.grant(db, ids["player"], 1.0)
    C.declare_watching(db, ["patrol", "report"], 1.5)
    R.catch_up(db, charter.signed_gt + DAYS + 0.01)
    outcomes = dict(db.execute("SELECT term_key, outcome FROM compliance").fetchall())
    assert outcomes == {"personnel": "excused", "patrols": "missed", "report": "missed"}
    with pytest.raises(ValueError):
        C.declare_watching(db, ["taxes"], 2.0)


def test_terms_are_judged_once_reported(db, ids):
    charter = C.grant(db, ids["player"], 1.0)
    C.report_fact(db, "report", 2.0, ids["player"], {})     # one report filed early in the month
    month(db, charter, 1, riders=9, patrols=7, report=False)
    outcomes = dict(db.execute("SELECT term_key, outcome FROM compliance WHERE period = 1").fetchall())
    assert outcomes == {"personnel": "short", "patrols": "short", "report": "met"}


def test_missed_term_stops_the_stipend_and_costs_jarl_trust(db, ids):
    charter = C.grant(db, ids["player"], 1.0)
    before = R.regard(db, ids["balgruuf"], ids["player"])
    month(db, charter, 1, riders=20, patrols=0, report=False)
    stipend = db.execute("SELECT COALESCE(SUM(amount), 0) FROM ledger_entry WHERE purse = 'inst:plains_wardens'"
                         " AND kind = 'stipend'").fetchone()[0]
    assert stipend == 0
    assert R.regard(db, ids["balgruuf"], ids["player"]) < before


def test_catch_up_runs_each_month_once_and_is_capped(db, ids):
    charter = C.grant(db, ids["player"], 1.0)
    r = R.catch_up(db, charter.signed_gt + 95)
    assert r.periods == 3 and R.periods_done(db, charter) == 3
    assert R.catch_up(db, charter.signed_gt + 95).periods == 0
    r = R.catch_up(db, charter.signed_gt + 1000, limit=4)
    assert r.periods == 4 and R.periods_done(db, charter) == 7


def test_three_single_months_equal_one_long_catch_up():
    def run(steps):
        store = WorldStore(":memory:")
        with store.transaction() as db:
            ids = S.world(db)
            C.grant(db, ids["player"], 1.0)
            C.report_fact(db, "strength", 2.0, ids["player"], {"riders": 6, "horses": 4})
            for now in steps:
                R.catch_up(db, now)
            return C.treasury(db), db.execute("SELECT period, band, gap FROM influence_snapshot ORDER BY period").fetchall()
    one = run([1.0 + 3 * DAYS + 0.01])
    three = run([1.0 + DAYS + 0.01, 1.0 + 2 * DAYS + 0.01, 1.0 + 3 * DAYS + 0.01])
    assert one[0] == three[0] and [tuple(r) for r in one[1]] == [tuple(r) for r in three[1]]


# ---- measures and stances ----

def test_gap_grows_with_riders_and_bands_follow(db, ids):
    charter = C.grant(db, ids["player"], 1.0)
    snaps = []
    for riders in (4, 12, 30):
        C.report_fact(db, "strength", 2.0 + riders / 100, ids["player"], {"riders": riders, "horses": 4})
        snaps.append(R.measure(db, charter, 1, ids["player"]))
    assert snaps[0].gap < snaps[1].gap < snaps[2].gap
    assert snaps[0].band == "none" and snaps[2].band in ("large", "extreme")


def test_bands_and_positions():
    assert [R.band_of(g) for g in (-1, 0.1, 0.3, 0.6, 2)] == ["none", "small", "medium", "large", "extreme"]
    assert [R.position_of(s) for s in (0.9, 0.2, 0, -0.3, -0.9)] == ["champion", "support", "wary", "oppose", "hostile"]


def test_a_growing_gap_never_makes_irileth_friendlier(db, ids):
    charter = C.grant(db, ids["player"], 1.0)
    irileth = D.official("irileth")
    scores = []
    for riders in (2, 8, 12, 16, 24, 40):
        C.report_fact(db, "strength", 2.0 + riders / 100, ids["player"], {"riders": riders, "horses": 4})
        scores.append(R.stance(db, irileth, R.measure(db, charter, 1, ids["player"]), ids["player"]).score)
    assert scores == sorted(scores, reverse=True)


def test_officials_disagree_on_the_same_snapshot(db, ids):
    charter = C.grant(db, ids["player"], 1.0)
    C.report_fact(db, "strength", 2.0, ids["player"], {"riders": 16, "horses": 4})
    snap = R.measure(db, charter, 1, ids["player"])
    scores = {o.key: R.stance(db, o, snap, ids["player"]).score for o in D.officials()}
    assert scores["irileth"] < scores["balgruuf"]
    assert len(set(round(s, 3) for s in scores.values())) == 3


def test_traits_change_an_officials_view(db, ids):
    charter = C.grant(db, ids["player"], 1.0)
    C.report_fact(db, "strength", 2.0, ids["player"], {"riders": 16, "horses": 4})
    snap = R.measure(db, charter, 1, ids["player"])
    irileth = D.official("irileth")
    base = R.stance(db, irileth, snap, ids["player"]).score
    db.execute("INSERT INTO persona_version (npc_id, version, bio, traits_json, source, created_gt)"
               " VALUES (?, 1, '', ?, 'admin', 0)", (ids["irileth"], json.dumps({"trusting": 0.95})))
    assert R.stance(db, irileth, snap, ids["player"]).score > base


def test_stance_rows_only_when_the_view_moves(db, ids):
    charter = C.grant(db, ids["player"], 1.0)
    for m in (1, 2, 3, 4):
        month(db, charter, m, riders=4)
    n = db.execute("SELECT COUNT(*) FROM official_stance WHERE move IS NULL").fetchone()[0]
    assert 3 <= n < 12


def test_unmet_officials_get_no_stance_row(db):
    player = S._npc(db, "0x00000007", "Zachary", player=True)
    charter = C.grant(db, player, 1.0)
    R.catch_up(db, charter.signed_gt + DAYS + 0.01)
    assert db.execute("SELECT COUNT(*) FROM official_stance").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM influence_snapshot").fetchone()[0] == 1


# ---- scenarios (no predetermined outcome: each lands where its choices put it) ----

def test_loyal_service_stays_in_good_standing():
    rows = S.play("loyal")
    assert all(r["band"] == "none" and r["status"] == "active" for r in rows)
    assert rows[-1]["positions"]["balgruuf"] in ("support", "champion") and rows[-1]["purse"] > 0
    assert sum(len(r["moves"]) for r in rows) <= 2          # commendations are rare


def test_overreach_without_backing_gets_the_charter_suspended():
    rows = S.play("overreach")
    statuses = [r["status"] for r in rows]
    assert "under_review" in statuses and statuses[-1] == "suspended"
    assert rows[-1]["band"] == "extreme" and rows[-1]["purse"] < 0
    assert rows[-1]["positions"]["irileth"] in ("oppose", "hostile")


def test_neglect_costs_money_and_the_stewards_support():
    rows = S.play("neglect")
    assert all(r["status"] == "active" for r in rows)
    assert rows[-1]["positions"]["proventus"] in ("oppose", "hostile")
    assert any("held back" in m for r in rows for m in r["moves"])


def test_cutting_back_can_win_the_charter_back():
    rows = S.play("reconcile")
    assert "suspended" in [r["status"] for r in rows[:3]]
    assert rows[-1]["status"] == "active" and rows[-1]["band"] == "none"
    assert any("restored" in m for r in rows for m in r["moves"])


# ---- prompts ----

def test_officials_get_charter_and_view_lines(db, ids):
    charter = C.grant(db, ids["player"], 1.0)
    month(db, charter, 1, riders=4)
    lines = P.official_lines(db, ids["proventus"], "Proventus Avenicci", ids["player"], "Zachary", 32.0)
    text = "\n".join(lines)
    assert "Zachary founded the Plains Wardens" in text and "the Western Plains" in text
    assert "1200 gold a year" in text and "Proventus Avenicci, as steward of Dragonsreach" in text
    assert "Last month's reckoning" in text and "agrees no new terms" in text


def test_ordinary_npcs_and_no_charter_get_nothing(db, ids):
    other = S._npc(db, "0x0001B07A", "Ysolda")
    assert P.official_lines(db, ids["balgruuf"], "Balgruuf", ids["player"], "Z", 2.0) == []   # no charter yet
    C.grant(db, ids["player"], 1.0)
    assert P.official_lines(db, other, "Ysolda", ids["player"], "Z", 2.0) == []


def test_recent_move_is_mentioned_by_its_maker(db, ids):
    charter = C.grant(db, ids["player"], 1.0)
    for m in (1, 2):
        month(db, charter, m, riders=10 + 6 * m)
    move = json.loads(db.execute("SELECT payload_json FROM world_event WHERE type = 'lordship.move'"
                                 " ORDER BY event_id DESC LIMIT 1").fetchone()[0])
    who = ids[move["by"]]
    lines = P.official_lines(db, who, "X", ids["player"], "Zachary", charter.signed_gt + 2 * DAYS + 1)
    assert any(line.startswith("Recently:") for line in lines)


# ---- debug commands ----

@pytest.mark.parametrize("line,parsed", [
    ("debug charter status", ("status", None)), ("Debug charter riders 12", ("riders", 12)),
    ("debug charter patrols five", ("patrol", 5)), ("debug charter tick", ("month", None)),
    ("debug charter grant level 2", ("grant", 2)), ("debug charter", ("status", None)),
    ("debug guard status", None), ("debug charter dance", None), ("hello", None)])
def test_debug_parse(line, parsed):
    assert LD.parse(line) == parsed


def test_debug_session_in_order(db, ids):
    p = ids["player"]
    assert "no charter yet" in LD.run(db, "debug charter riders 5", p, 1.0)
    assert "granted charter v1" in LD.run(db, "debug charter grant", p, 1.0)
    assert "now judged" in LD.run(db, "debug charter watch", p, 1.0)
    assert "5 riders" in LD.run(db, "debug charter riders 5", p, 2.0)
    assert "13 patrol" in LD.run(db, "debug charter patrol 13", p, 2.0)
    assert "report filed" in LD.run(db, "debug charter report", p, 2.0)
    out = LD.run(db, "debug charter month", p, 3.0)
    assert "reckoned month 1" in out and "charter active" in out
    # facts after a forced month land in the next month, not the one already reckoned
    LD.run(db, "debug charter patrol 13", p, 3.0)
    LD.run(db, "debug charter report", p, 3.0)
    assert "reckoned month 2" in LD.run(db, "debug charter month", p, 3.0)
    outcomes = dict(db.execute("SELECT term_key, outcome FROM compliance WHERE period = 2").fetchall())
    assert outcomes == {"personnel": "met", "patrols": "met", "report": "met"}
    assert LD.run(db, "debug guard status", p, 3.0) is None
    assert db.execute("SELECT COUNT(*) FROM world_event WHERE type = 'lordship.patrol' AND source = 'admin'"
                      ).fetchone()[0] == 2


def test_misheard_debug_prefix_reaches_charter_commands():
    assert debug_lines.command_text("the bug charter status") == "debug charter status"
    assert debug_lines.command_text("the bug bit me") is None


# ---- shared machinery copes with the new events ----

def test_gossip_processing_ignores_lordship_events_safely(db, ids):
    charter = C.grant(db, ids["player"], 1.0)
    for m in (1, 2, 3):
        month(db, charter, m, riders=12 + 8 * m)
    processor.process(db)          # must not raise on lordship.* events
