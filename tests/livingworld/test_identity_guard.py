"""A line where an NPC introduces themselves by another actor's name is refused (livingworld/guard/identity_guard.py).

Seen in the game: on a patrol, one Whiterun guard said he was Kolbeinn, and a second, different guard then said
he was Kolbeinn too. Each guard is its own npc row with its own name; only the spoken line was wrong. These are
rule tests on a real in-memory database. They do not prove the in-game behaviour; that is in the checklist.
"""
import pytest

from livingworld.guard import identity_guard as IG
from livingworld.store.world_store import WorldStore


@pytest.fixture
def db():
    store = WorldStore(":memory:")
    with store.transaction() as conn:
        yield conn


def add_npc(db, base, ref, name="Whiterun Guard", generic=True, given=None, player=False):
    cur = db.execute(
        "INSERT INTO npc (plugin, base_form_id, ref_form_id, display_name, is_player, is_generic, created_gt)"
        " VALUES ('Skyrim.esm', ?, ?, ?, ?, ?, 0)", (base, ref, name, int(player), int(generic)))
    npc_id = cur.lastrowid
    if given:
        db.execute("INSERT INTO guard_profile (npc_id, org_key, given_name, first_seen_gt) VALUES (?, 'whiterun_guard', ?, 0)",
                   (npc_id, given))
    return npc_id


@pytest.fixture
def patrol(db):
    return {
        "player": add_npc(db, "0x00000007", "Skyrim.esm|0x00000014", "Zachary", generic=False, player=True),
        "kolbeinn": add_npc(db, "0x000A0DA4", "Skyrim.esm|0x0001A6B2", given="Kolbeinn"),
        "brand": add_npc(db, "0x000A0DA4", "Skyrim.esm|0x0001A6B3", given="Brand"),
        "sigurd": add_npc(db, "0x000A0DA4", "Skyrim.esm|0x0001A6B4", given="Sigurd"),
    }


def everyone(p):
    return list(p.values())


def test_second_guard_claiming_the_first_guards_name_is_a_claim(db, patrol):
    claim = IG.find_claim(db, patrol["brand"], "Aye, I'm Kolbeinn. Lead on.", everyone(patrol))
    assert claim is not None
    assert claim.claimant_id == patrol["brand"]
    assert claim.owner_id == patrol["kolbeinn"]
    assert claim.owner_name == "Kolbeinn"
    assert claim.claimant_own == "Brand"


@pytest.mark.parametrize("line", [
    "My name is Kolbeinn.",
    "Call me Kolbeinn, friend.",
    "They call me Kolbeinn around the barracks.",
    "I am Kolbeinn of the Whiterun guard.",
    "i'm kolbeinn".title().replace("I'M", "I'm"),
])
def test_self_introductions_are_claims(db, patrol, line):
    assert IG.find_claim(db, patrol["brand"], line, everyone(patrol)) is not None


@pytest.mark.parametrize("line", [
    "This is Kolbeinn, my sergeant.",
    "Kolbeinn thinks the bandits went north.",
    "I'm Kolbeinn's friend from the barracks.",
    "I'm told Kolbeinn knows the road.",
    "Ask Kolbeinn, he knows.",
    "I'm tired of this road.",
    "",
])
def test_talking_about_someone_is_not_a_claim(db, patrol, line):
    assert IG.find_claim(db, patrol["brand"], line, everyone(patrol)) is None


def test_a_guard_saying_their_own_name_is_fine(db, patrol):
    assert IG.find_claim(db, patrol["brand"], "I'm Brand.", everyone(patrol)) is None
    assert IG.find_claim(db, patrol["kolbeinn"], "My name is Kolbeinn.", everyone(patrol)) is None


def test_a_name_nobody_here_has_is_not_a_claim(db, patrol):
    assert IG.find_claim(db, patrol["brand"], "I'm Hrolf.", everyone(patrol)) is None


def test_only_people_in_the_conversation_can_be_claimed(db, patrol):
    here = [patrol["brand"], patrol["sigurd"], patrol["player"]]
    assert IG.find_claim(db, patrol["brand"], "I'm Kolbeinn.", here) is None


def test_named_npcs_are_protected_too(db, patrol):
    caius = add_npc(db, "0x0001347B", "Skyrim.esm|0x0001347C", "Caius Tertius", generic=False)
    claim = IG.find_claim(db, patrol["brand"], "My name is Caius.", everyone(patrol) + [caius])
    assert claim is not None and claim.owner_id == caius


def test_a_generic_label_is_never_a_claimable_name(db, patrol):
    assert IG.find_claim(db, patrol["brand"], "I am a Whiterun Guard.", everyone(patrol)) is None


def test_guard_without_a_given_name_can_still_be_caught_and_corrected(db, patrol):
    nameless = add_npc(db, "0x000A0DA4", "Skyrim.esm|0x0001A6B9")
    claim = IG.find_claim(db, nameless, "I'm Sigurd.", everyone(patrol) + [nameless])
    assert claim is not None and claim.claimant_own is None
    assert "Sigurd" in IG.correction(claim) and "Whiterun Guard" in IG.correction(claim)


def test_correction_names_the_real_speaker(db, patrol):
    claim = IG.find_claim(db, patrol["brand"], "I'm Kolbeinn.", everyone(patrol))
    text = IG.correction(claim)
    assert "Brand" in text and "Kolbeinn" in text and "different person" in text


def test_record_keeps_the_attempt_and_changes_no_identity(db, patrol):
    before = db.execute("SELECT npc_id, display_name, ref_form_id FROM npc ORDER BY npc_id").fetchall()
    claim = IG.find_claim(db, patrol["brand"], "I'm Kolbeinn.", everyone(patrol))
    IG.record(db, claim, "I'm Kolbeinn.", 12.5, conv_id=None)
    IG.record(db, claim, "No, really, I'm Kolbeinn.", 12.6, conv_id=None)
    rows = IG.conflicts(db)
    assert len(rows) == 2
    assert rows[0]["claimant_id"] == patrol["brand"] and rows[0]["owner_id"] == patrol["kolbeinn"]
    assert rows[0]["claimed_name"] == "Kolbeinn"
    after = db.execute("SELECT npc_id, display_name, ref_form_id FROM npc ORDER BY npc_id").fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]
    given = {r[0]: r[1] for r in db.execute("SELECT npc_id, given_name FROM guard_profile")}
    assert given[patrol["brand"]] == "Brand" and given[patrol["kolbeinn"]] == "Kolbeinn"


def test_the_same_line_is_recorded_once(db, patrol):
    claim = IG.find_claim(db, patrol["brand"], "I'm Kolbeinn.", everyone(patrol))
    assert IG.record(db, claim, "I'm Kolbeinn.", 5.0, conv_id=3) is True
    assert IG.record(db, claim, "I'm Kolbeinn.", 5.0, conv_id=3) is False
    assert len(IG.conflicts(db)) == 1


def test_the_line_excerpt_is_capped(db, patrol):
    claim = IG.find_claim(db, patrol["brand"], "I'm Kolbeinn.", everyone(patrol))
    IG.record(db, claim, "x" * 1000, 1.0, conv_id=None)
    assert len(IG.conflicts(db)[0]["line"]) == IG.LINE_EXCERPT


def test_migration_18_is_applied_once_on_an_existing_world(tmp_path):
    path = str(tmp_path / "w.sqlite")
    WorldStore(path)
    again = WorldStore(path)
    assert 18 in again.applied_versions()
    assert again.scalar("SELECT COUNT(*) FROM identity_conflict") == 0
