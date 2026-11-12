"""Guard attire: people take the player in a guard's uniform for a guard, and feel about it by lore."""
from livingworld.guard import uniform as U
from livingworld.harness import actors
from livingworld.harness.perception import KEY_ATTIRE, attire
from livingworld.harness.scripted_llm import ScriptedLLM
from livingworld.harness.session import HeadlessSession
from livingworld.integration import LivingWorld

from tests.livingworld.test_guard import DAY, PLAYER, living_world_inline, open_db, whiterun_guard  # noqa: F401

OLFINA = actors.actor("Olfina Gray-Mane", 0x00013B9F, 0x00013BA0, 1, "Nord", "FemaleYoungEager")
YSOLDA = actors.actor("Ysolda", 0x00013BAB, 0x00013BAC, 1, "Nord", "FemaleYoungEager")
ARMOR = "Whiterun Guard's Armor"


def prompt(work_dir, npc, body=ARMOR, location="Whiterun"):
    llm = ScriptedLLM(replies=["Hm."])
    with HeadlessSession(str(work_dir), llm) as s:
        s.start([PLAYER, npc], location=location, game_days=DAY,
                context_extra={KEY_ATTIRE: [attire(PLAYER, "armor", DAY, body=body, location=location)]})
        s.continue_until_player_turn()
        s.end(game_days=DAY + 0.001)
        LivingWorld.latest.wait_for_jobs()
    return llm.dialogue_calls[0].system_prompt if llm.dialogue_calls else ""


def test_a_townsperson_takes_the_player_for_a_guard(tmp_path):
    p = prompt(tmp_path, YSOLDA)
    assert "Torvar is wearing Whiterun Guard's Armor, the uniform of the Whiterun Guard." in p
    assert "Ysolda takes Torvar for one of the Whiterun Guard" in p
    assert "respects the Whiterun Guard" in p


def test_a_gray_mane_distrusts_it_but_can_be_won_over(tmp_path):
    p = prompt(tmp_path, OLFINA)
    assert "Olfina Gray-Mane distrusts the Whiterun Guard, because the Gray-Manes back Ulfric" in p
    assert "can be won over by what Torvar actually does" in p


def test_a_real_guard_does_not_know_the_player(tmp_path):
    p = prompt(tmp_path, whiterun_guard())
    assert "does not know Torvar as one of their own" in p


def test_no_uniform_no_lines(tmp_path):
    assert "takes Torvar for" not in prompt(tmp_path, YSOLDA, body="Farm Clothes")


def test_lore_table_rules():
    wr = U.uniform_for(ARMOR, "Whiterun")
    assert wr.key == "whiterun_guard" and wr.org == "whiterun_guard"
    generic = U.uniform_for("Guard's Armor", "Riften Marketplace")
    assert generic.key == "rift_guard"
    assert U.attitude(generic, "Mjoll the Lioness", "Nord", "Riften").stance == "despise"
    assert U.attitude(generic, "Maven Black-Briar", "Nord", "Riften").stance == "approve"
    assert U.attitude(wr, "Ri'saad", "[Race <KhajiitRace (00013745)>]", "Whiterun").stance == "distrust"
    assert U.attitude(wr, "Hroar", "Nord", "Whiterun").stance == "neutral"
    eastmarch = U.uniform_for("Windhelm Guard's Armor", "Windhelm")
    assert U.attitude(eastmarch, "Brunwulf Free-Winter", "DarkElfRace", "Windhelm, Grey Quarter").stance == "despise"
    thalmor = U.uniform_for("Thalmor Robes", "Solitude")
    assert U.attitude(thalmor, "Sorex Vinius", "Imperial", "Solitude").stance == "despise"
    imperial = U.uniform_for("Imperial Armor", "Windhelm")
    assert U.attitude(imperial, "Hroar", "Nord", "Windhelm").stance == "distrust"
    assert U.uniform_for("Iron Armor", "Whiterun") is None


def test_regard_softens_a_dislike():
    u = U.uniform_for(ARMOR, "Whiterun")
    att = U.Attitude("distrust", "reasons")
    won = U.lines("Olfina", "Torvar", u, serves=False, is_member=False, knows_player=True, att=att, reg=0.3)
    assert any("treats Torvar as the exception" in x for x in won)
    warming = U.lines("Olfina", "Torvar", u, serves=False, is_member=False, knows_player=True, att=att, reg=0.15)
    assert any("may be different from the rest" in x for x in warming)
