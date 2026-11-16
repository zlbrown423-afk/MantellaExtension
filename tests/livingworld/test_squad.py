"""Temporary squads (livingworld/squad/): asked in words, joined only when the NPC says yes, ended by dismissal,
decline or the end of the conversation that holds them. Rule tests on a real in-memory database; they do not
prove how the game moves anyone (see the in-game checklist in the README).
"""
import pytest

from livingworld.squad import MAX_SQUAD, service as SQ, words as W
from livingworld.store.world_store import WorldStore


@pytest.fixture
def db():
    store = WorldStore(":memory:")
    with store.transaction() as conn:
        yield conn


def add_npc(db, ref, name="Whiterun Guard", player=False, child=False):
    cur = db.execute(
        "INSERT INTO npc (plugin, base_form_id, ref_form_id, display_name, is_player, is_generic, created_gt, is_child)"
        " VALUES ('Skyrim.esm', '0x000A0DA4', ?, ?, ?, ?, 0, ?)", (ref, name, int(player), int(not player), int(child)))
    return cur.lastrowid


@pytest.fixture
def patrol(db):
    return {"player": add_npc(db, "r14", "Zachary", player=True), "kolbeinn": add_npc(db, "a"),
            "brand": add_npc(db, "b"), "sigurd": add_npc(db, "c")}


# ---- words ----
@pytest.mark.parametrize("line", ["Brand, come with me", "Follow me, all of you", "will you walk with me?",
                                  "Fall in, men", "I need you with me on this patrol", "stay with me"])
def test_asks_to_come(line):
    assert W.asks_to_come(line)


@pytest.mark.parametrize("line", ["Don't follow me", "if you come with me, bring a torch", "come with me when you are ready",
                                  "Brand, how are you?", "I will follow the road north", ""])
def test_not_an_ask(line):
    assert not W.asks_to_come(line)


@pytest.mark.parametrize("line", ["Stand down.", "You're dismissed, Brand.", "Go back to your post.", "You can go.",
                                  "Stop following me."])
def test_dismissals(line):
    assert W.dismisses(line)


@pytest.mark.parametrize("line", ["You can go first.", "Don't stand down.", "I'll not dismiss anyone yet", "Hello there"])
def test_not_a_dismissal(line):
    assert not W.dismisses(line)


@pytest.mark.parametrize("line,formation", [
    ("Spread out a bit.", "loose"), ("Give me some room.", "loose"), ("Stay close.", "close"),
    ("Close ranks, everyone.", "close"), ("Use your own judgment.", "autonomous"), ("Scout ahead.", "autonomous"),
    ("Keep patrol formation.", "patrol"), ("March in order.", "patrol"), ("Nice weather.", None)])
def test_formation_words(line, formation):
    assert W.formation_in(line) == formation


def test_targets_by_given_name_group_and_lone_npc():
    people = {2: ["Whiterun Guard", "Kolbeinn"], 3: ["Whiterun Guard", "Brand"], 4: ["Whiterun Guard", "Sigurd"]}
    assert W.targets("Brand, come with me", people) == [3]
    assert W.targets("Kolbeinn and Sigurd, follow me", people) == [2, 4]
    assert W.targets("Everyone, follow me", people) == [2, 3, 4]
    assert W.targets("Follow me", people) == []                 # three unnamed guards: nobody is pulled in
    assert W.targets("Follow me", {3: ["Whiterun Guard", "Brand"]}) == [3]


@pytest.mark.parametrize("reply,answer", [
    ("Aye, lead the way.", True), ("Right behind you, friend.", True), ("I'll follow you anywhere.", True),
    ("No, I must stay at my post.", False), ("Not now. I'm on duty.", False), ("I can't leave the gate.", False),
    ("The road is long.", None), ("Aye, I'll come. Can't say I like it.", True)])
def test_npc_answers(reply, answer):
    assert W.answer_in(reply) is answer


# ---- membership ----
def test_asked_then_agreed_becomes_active(db, patrol):
    assert SQ.ask(db, patrol["player"], [patrol["brand"]], 1.0) == [patrol["brand"]]
    assert [m.status for m in SQ.members(db, SQ.active_squad(db, patrol["player"]).squad_id)] == ["asked"]
    assert SQ.answer(db, patrol["brand"], True, 1.1) == "active"
    assert SQ.squad_of(db, patrol["brand"]).leader_id == patrol["player"]


def test_declined_leaves_and_empty_squad_disbands(db, patrol):
    SQ.ask(db, patrol["player"], [patrol["brand"]], 1.0)
    assert SQ.answer(db, patrol["brand"], False, 1.1) == "declined"
    assert SQ.active_squad(db, patrol["player"]) is None
    assert SQ.squad_of(db, patrol["brand"]) is None


def test_answer_from_someone_not_asked_does_nothing(db, patrol):
    assert SQ.answer(db, patrol["sigurd"], True, 1.0) is None


def test_several_followers_one_squad_each_one_person(db, patrol):
    asked = SQ.ask(db, patrol["player"], [patrol["kolbeinn"], patrol["brand"], patrol["sigurd"]], 1.0)
    assert len(asked) == 3
    for i in asked:
        SQ.answer(db, i, True, 1.1)
    squad = SQ.active_squad(db, patrol["player"])
    assert [m.npc_id for m in SQ.members(db, squad.squad_id)] == [patrol["kolbeinn"], patrol["brand"], patrol["sigurd"]]
    assert db.execute("SELECT COUNT(*) FROM squad WHERE disbanded_gt IS NULL").fetchone()[0] == 1


def test_nobody_is_in_two_squads_or_asked_twice(db, patrol):
    other = add_npc(db, "r15", "Other Leader", player=False)
    SQ.ask(db, patrol["player"], [patrol["brand"]], 1.0)
    assert SQ.ask(db, patrol["player"], [patrol["brand"]], 1.2) == []
    assert SQ.ask(db, other, [patrol["brand"]], 1.3) == []


def test_children_and_the_leader_are_never_asked(db, patrol):
    kid = add_npc(db, "k", "Child", child=True)
    assert SQ.ask(db, patrol["player"], [kid, patrol["player"]], 1.0) == []
    assert SQ.active_squad(db, patrol["player"]) is None


def test_squad_size_is_capped(db, patrol):
    many = [add_npc(db, f"x{i}") for i in range(MAX_SQUAD + 3)]
    assert len(SQ.ask(db, patrol["player"], many, 1.0)) == MAX_SQUAD


def test_dismiss_one_then_all(db, patrol):
    SQ.ask(db, patrol["player"], [patrol["kolbeinn"], patrol["brand"]], 1.0)
    for i in (patrol["kolbeinn"], patrol["brand"]):
        SQ.answer(db, i, True, 1.1)
    assert SQ.dismiss(db, patrol["player"], [patrol["brand"]], 2.0) == [patrol["brand"]]
    assert SQ.squad_of(db, patrol["brand"]) is None and SQ.squad_of(db, patrol["kolbeinn"]) is not None
    assert SQ.dismiss(db, patrol["player"], None, 3.0) == [patrol["kolbeinn"]]
    assert SQ.active_squad(db, patrol["player"]) is None
    assert SQ.dismiss(db, patrol["player"], None, 4.0) == []     # nothing left to dismiss


def test_a_dismissed_npc_can_be_asked_again(db, patrol):
    SQ.ask(db, patrol["player"], [patrol["brand"]], 1.0)
    SQ.answer(db, patrol["brand"], True, 1.1)
    SQ.dismiss(db, patrol["player"], None, 2.0)
    assert SQ.ask(db, patrol["player"], [patrol["brand"]], 5.0) == [patrol["brand"]]


def test_conversation_end_drops_members_and_unanswered_asks(db, patrol):
    SQ.ask(db, patrol["player"], [patrol["kolbeinn"], patrol["brand"]], 1.0)
    SQ.answer(db, patrol["kolbeinn"], True, 1.1)
    assert sorted(SQ.conversation_ended(db, patrol["player"], 9.0)) == sorted([patrol["kolbeinn"], patrol["brand"]])
    rows = {r["npc_id"]: (r["status"], r["reason"]) for r in SQ.report(db)[0]["members"]}
    assert rows[patrol["kolbeinn"]] == ("dropped", "conversation ended")
    assert SQ.active_squad(db, patrol["player"]) is None


def test_formation_changes_only_for_a_live_squad_and_a_known_formation(db, patrol):
    assert SQ.set_formation(db, patrol["player"], "close") is False        # no squad yet
    SQ.ask(db, patrol["player"], [patrol["brand"]], 1.0)
    assert SQ.active_squad(db, patrol["player"]).formation == "loose"
    assert SQ.set_formation(db, patrol["player"], "patrol") is True
    assert SQ.set_formation(db, patrol["player"], "patrol") is False
    assert SQ.set_formation(db, patrol["player"], "nonsense") is False
    assert SQ.active_squad(db, patrol["player"]).formation == "patrol"


def test_member_lines_ask_and_walk():
    asked = W.member_lines("Brand", "Zachary", "loose", [], "asked")
    assert "asked Brand to come along" in asked[0]
    walking = W.member_lines("Brand", "Zachary", "patrol", ["Kolbeinn", "Sigurd"], "active")
    assert "Kolbeinn and Sigurd" in walking[0] and "disciplined" in walking[0] and "dismisses" in walking[0]


def test_migration_19_applies_once(tmp_path):
    path = str(tmp_path / "w.sqlite")
    WorldStore(path)
    assert 19 in WorldStore(path).applied_versions()
