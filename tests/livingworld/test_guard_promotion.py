"""G3 promotions: the promotion board, Caius's announcement, turning it down, and the board readout."""
import pytest

from livingworld.guard import duty as DU, promotion as PR, store as S
from livingworld.guard.service import Guard, Scene
from livingworld.store.world_store import WorldStore
from livingworld.work import tasks as T

from tests.livingworld.test_guard import DAY, db_path, npc_id, open_db, run_dev, service, living_world_inline  # noqa: F401
from tests.livingworld.test_guard_duty import WHITERUN, enlisted, talk
from livingworld.guard import dev

ROUTINE = ("gate_watch", "wall_patrol", "market_watch", "gate_watch", "wall_patrol")


def setup(work_dir, *, duties=ROUTINE, view=0.2, active=True, day=DAY):
    """A trained recruit with ``duties`` done well and Caius's good opinion. Returns the day the last one ended."""
    enlisted(work_dir, day)
    store = WorldStore(db_path(work_dir))
    with store.transaction() as db:
        me, boss = npc_id(db, "Torvar"), npc_id(db, "Commander Caius")
        svc = S.current(db, me)
        if active:
            svc = S.change(db, svc, "test: probation passed", day, status="active", source="admin")
        at = day
        for i, key in enumerate(duties):
            at = day + 0.3 * (i + 1)
            d = DU.rules(WHITERUN.key).duty(key)
            t = DU.assign(db, WHITERUN, svc, boss, d, at, source="admin")
            T.change(db, t, "completed", "reported back after the shift", at + 0.2, source="admin")
            svc = S.change(db, svc, f"duty done: {d.title}", at + 0.2, d_merit=d.merit, source="admin")
        if view:
            DU._relate(db, boss, me, {"trust": view, "respect": view}, None, "test", day)
    store.close()
    return at + 0.2


def board(work_dir, now):
    store = WorldStore(db_path(work_dir))
    with store.transaction() as db:
        svc = S.current(db, npc_id(db, "Torvar"))
        b = PR.board(db, WHITERUN, svc, now)
        out = (b.ok, {c.name: c.ok for c in b.checks}, PR.explain(b), b.merit)
    store.close()
    return out


def claims(work_dir, act):
    return open_db(work_dir).execute("SELECT COUNT(*) FROM claim WHERE act = ?", (act,)).fetchone()[0]


# ---- the board ----

def test_the_board_checks_time_training_duties_merit_and_the_superiors_view(tmp_path):
    setup(tmp_path, duties=ROUTINE[:3], view=0.0)
    ok, checks, lines, _ = board(tmp_path, DAY + 3)
    assert not ok
    assert checks == {"status": True, "time": False, "training": True, "duties": False, "merit": False,
                      "clean": True, "view": True}
    assert lines[0] == "the Whiterun Guard: Recruit -> Guardsman: not yet (3 of 7 checks to go)"
    assert any("NO  time: 3 of 7 days as Recruit" in l for l in lines)
    assert any("NO  duties: 3 of 5 duties done as Recruit" in l for l in lines)


def test_a_recruit_who_has_served_well_passes(tmp_path):
    setup(tmp_path)
    ok, checks, _, merit = board(tmp_path, DAY + 7.5)
    assert ok, checks
    assert merit >= 5


def test_the_same_duty_pays_less_after_three_times(tmp_path):
    setup(tmp_path, duties=("gate_watch",) * 6)
    _, _, _, merit = board(tmp_path, DAY + 8)
    assert merit == pytest.approx(3 * 1.0 + 3 * 0.5)


def test_probation_and_a_recent_reprimand_hold_it_back(tmp_path):
    setup(tmp_path, active=False)
    ok, checks, _, _ = board(tmp_path, DAY + 7.5)
    assert not ok and checks["status"] is False
    store = WorldStore(db_path(tmp_path))
    with store.transaction() as db:
        svc = S.change(db, S.current(db, npc_id(db, "Torvar")), "test", DAY + 7, status="active", source="admin")
        DU.reprimand(db, svc, None, "asleep at the gate", -1.0, DAY + 7.2)
    store.close()
    ok, checks, lines, _ = board(tmp_path, DAY + 7.5)
    assert not ok and checks["clean"] is False
    assert any("reprimanded 0 days ago (needs 7 clean days)" in l for l in lines)


def test_captain_needs_a_free_seat_and_caius_holds_it(tmp_path):
    setup(tmp_path)
    store = WorldStore(db_path(tmp_path))
    with store.transaction() as db:
        S.change(db, S.current(db, npc_id(db, "Torvar")), "test", DAY + 1, rank_key="lieutenant", source="admin")
    store.close()
    ok, checks, lines, _ = board(tmp_path, DAY + 200)
    assert checks["seat"] is False
    assert any("no Captain seat open (held by Commander Caius)" in l for l in lines)


def test_officer_duties_only_go_to_officers(tmp_path):
    setup(tmp_path, duties=())
    store = WorldStore(db_path(tmp_path))
    with store.transaction() as db:
        svc = S.current(db, npc_id(db, "Torvar"))
        recruit = {d.key for d in DU.choices(WHITERUN, svc, None, 10)}
        svc = S.change(db, svc, "test", DAY, rank_key="sergeant", source="admin")
        sergeant = {d.key for d in DU.choices(WHITERUN, svc, None, 10)}
        svc = S.change(db, svc, "test", DAY, rank_key="lieutenant", source="admin")
        lieutenant = {d.key for d in DU.choices(WHITERUN, svc, None, 10)}
    store.close()
    assert "inspect_posts" not in recruit and "council_report" not in recruit
    assert "inspect_posts" in sergeant and "council_report" not in sergeant
    assert "council_report" in lieutenant


# ---- Caius announces it ----

def test_caius_promotes_his_recruit_when_they_next_talk_and_word_gets_around(tmp_path):
    setup(tmp_path)
    prompt = talk(tmp_path, DAY + 7.5)
    assert "Commander Caius has just promoted Torvar from Recruit to Guardsman of the Whiterun Guard" in prompt
    assert any(h.startswith("Promoted: Commander Caius names you Guardsman") for h in talk.hints)
    db = open_db(tmp_path)
    assert service(db)["rank_key"] == "guardsman"
    assert db.execute("SELECT COUNT(*) FROM world_event WHERE type = 'guard.promoted'").fetchone()[0] == 1
    assert claims(tmp_path, "promoted") == 1
    # and the next time he talks about what Sergeant still needs
    prompt = talk(tmp_path, DAY + 8)
    assert "If Torvar asks about promotion, Commander Caius can say plainly what the next rank, Sergeant" in prompt
    assert service(open_db(tmp_path))["rank_key"] == "guardsman"


def test_turning_it_down_keeps_the_old_rank_and_the_board_waits(tmp_path):
    setup(tmp_path)
    talk(tmp_path, DAY + 7.5, say="Thank you, sir, but I must decline. I'm not ready for it.")
    assert any(h.startswith("Promotion turned down: you stay a Recruit") for h in talk.hints)
    db = open_db(tmp_path)
    assert service(db)["rank_key"] == "recruit"
    assert db.execute("SELECT COUNT(*) FROM world_event WHERE type = 'guard.promoted'").fetchone()[0] == 0
    assert claims(tmp_path, "promoted") == 0
    store = WorldStore(db_path(tmp_path))
    with store.transaction() as db:                               # within decline_days: not offered again
        me = npc_id(db, "Torvar")
        assert PR.run(db, WHITERUN, S.current(db, me), DAY + 9) is None
        T.change(db, DU.in_hand(db, me), "completed", "test: the duty Caius gave that day was done", DAY + 7.7)
    store.close()
    ok, checks, lines, _ = board(tmp_path, DAY + 9)
    assert checks["time"] and checks["duties"], lines                   # the clock was not reset by turning it down
    talk(tmp_path, DAY + 15)
    assert service(open_db(tmp_path))["rank_key"] == "guardsman"


def test_caius_wants_a_word_when_a_promotion_is_due(tmp_path):
    setup(tmp_path)
    store = WorldStore(db_path(tmp_path))
    with store.transaction() as db:
        me, boss = npc_id(db, "Torvar"), npc_id(db, "Commander Caius")
        svc = S.current(db, me)
        assert PR.run(db, WHITERUN, svc, DAY + 7.5) == "guardsman"
        assert PR.run(db, WHITERUN, svc, DAY + 7.6) is None       # offered once
    g = Guard(store)
    assert g.wants_a_word(Scene(DAY + 7.6, me, "Torvar", [me])) == [boss]
    store.close()


def test_switching_promotions_off(tmp_path, monkeypatch):
    setup(tmp_path)
    monkeypatch.setenv(PR.ENV, "0")
    prompt = talk(tmp_path, DAY + 7.5)
    assert service(open_db(tmp_path))["rank_key"] == "recruit"
    assert "promot" not in prompt.lower()


# ---- the readout and test commands ----

def test_the_board_shows_on_screen_after_a_report_and_on_request(tmp_path):
    setup(tmp_path, duties=ROUTINE[:2])
    talk(tmp_path, DAY + 3, say="debug guard board")
    assert any(h.startswith("Next rank (Guardsman): 3 of 7 days as Recruit; 2 of 5 duties done as Recruit")
               for h in talk.hints), talk.hints


def test_debug_guard_promote_makes_it_due_and_caius_announces_it(tmp_path):
    setup(tmp_path, duties=())
    talk(tmp_path, DAY + 1, say="debug guard promote")
    assert service(open_db(tmp_path))["rank_key"] == "recruit"
    prompt = talk(tmp_path, DAY + 1.1)
    assert "has just promoted Torvar from Recruit to Guardsman" in prompt
    assert service(open_db(tmp_path))["rank_key"] == "guardsman"


def test_developer_board(tmp_path, monkeypatch):
    monkeypatch.setattr(dev, "server_running", lambda: False)
    setup(tmp_path)
    code, lines = run_dev(tmp_path, "--now", str(DAY + 7.5), "explain-promotion")
    assert code == 0 and "PASSES the board" in lines[0]
    code, lines = run_dev(tmp_path, "--now", str(DAY + 7.5), "board")
    assert lines[-1] == "offered: promotion to Guardsman (the superior announces it)"
