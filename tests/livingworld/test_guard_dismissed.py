"""A dismissed member is told so: the commander who dismissed them says it plainly instead of handing out shifts.

2026-11-17 play log: the player was dismissed at the end of an extended probation while away; Caius kept telling
them when their next shift began, and his uniform line said he did not know the player as one of the guard."""
from livingworld.guard import duty as DU, orgs as O, store as S
from livingworld.store.world_store import WorldStore

from tests.livingworld.test_guard import DAY, db_path, living_world_inline, npc_id, open_db  # noqa: F401
from tests.livingworld.test_guard_duty import enlisted, talk, whiterun_guard

WHITERUN = O.by_key("whiterun_guard")


def dismissed(work_dir, day):
    store = WorldStore(db_path(work_dir))
    with store.transaction() as db:
        svc = S.current(db, npc_id(db, "Torvar"))
        boss = DU.superior_id(db, WHITERUN, svc)
        DU.reprimand(db, svc, boss, "false report about the duty to clear out the bandits at Valtheim Towers", -3,
                     day - 0.5)
        S.change(db, S.get(db, svc.service_id), "dismissed at the end of an extended probation", day,
                 status="dismissed", kind="dismissed", actor_id=boss)


def test_the_commander_who_dismissed_them_says_so(tmp_path):
    enlisted(tmp_path)
    dismissed(tmp_path, DAY + 1)
    prompt = talk(tmp_path, DAY + 2, say="When does my shift begin?")
    assert "Commander Caius dismissed them" in prompt
    assert "Valtheim Towers" in prompt and "never hands them a shift or a duty" in prompt
    assert "has a duty for Torvar" not in prompt


def test_other_guards_know_too(tmp_path):
    enlisted(tmp_path)
    dismissed(tmp_path, DAY + 1)
    prompt = talk(tmp_path, DAY + 2, npcs=[whiterun_guard()])
    assert "no longer one of the guard" in prompt


def test_reinstated_members_hear_nothing_of_it(tmp_path):
    enlisted(tmp_path)
    dismissed(tmp_path, DAY + 1)
    talk(tmp_path, DAY + 1.5, say="debug guard reinstate")
    assert S.current(open_db(tmp_path), npc_id(open_db(tmp_path), "Torvar")).status == "active"
    assert "dismissed them" not in talk(tmp_path, DAY + 2)


def test_after_a_month_it_is_no_longer_brought_up(tmp_path):
    enlisted(tmp_path)
    dismissed(tmp_path, DAY + 1)
    assert "dismissed them" not in talk(tmp_path, DAY + 40)
