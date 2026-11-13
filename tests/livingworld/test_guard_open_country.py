"""Road bandits are met out in the open country: the game names the worldspace ("Skyrim") for a fight in unnamed
wilderness, and the river fight came in as "White River Watch". Both count for an open road_bandits duty, while a
camp raid still needs the camp itself."""
from livingworld.guard import duty as DU, incident as INC, orgs as O, store as S
from livingworld.store.world_store import WorldStore

from tests.livingworld.test_guard import DAY, db_path, living_world_inline, npc_id  # noqa: F401
from tests.livingworld.test_guard_duty import enlisted

WHITERUN = O.by_key("whiterun_guard")


def _duty(work_dir, kind_key):
    enlisted(work_dir)
    store = WorldStore(db_path(work_dir))
    with store.transaction() as db:
        svc = S.current(db, npc_id(db, "Torvar"))
        kind = next(k for k in INC.kinds(WHITERUN.key) if k.key == kind_key)
        inc = INC._open(db, WHITERUN, kind, int(DAY), DAY, 12345)
        DU.assign(db, WHITERUN, svc, DU.superior_id(db, WHITERUN, svc), DU.incident_duty(inc), DAY)
    return store, svc.member_id


def _fight(store, member_id, location, now):
    with store.transaction() as db:
        DU.mark_fought(db, member_id, location, now)
        task = DU.in_hand(db, member_id)
        d = DU.duty_for(db, task)
        return DU.fought_count(db, task), DU.ready(db, task, d, now)


def test_road_bandits_fight_in_the_wilderness_counts(tmp_path):
    store, member = _duty(tmp_path, "road_bandits")
    assert _fight(store, member, "Whiterun", DAY + 0.05) == (0, False)     # the city is not the road
    assert _fight(store, member, "Skyrim", DAY + 0.1) == (1, True)


def test_road_bandits_fight_at_the_white_river_counts(tmp_path):
    store, member = _duty(tmp_path, "road_bandits")
    assert _fight(store, member, "White River Watch", DAY + 0.1) == (1, True)


def test_camp_raid_still_needs_the_camp(tmp_path):
    store, member = _duty(tmp_path, "camp_raid")
    assert _fight(store, member, "Skyrim", DAY + 0.1) == (0, False)
