"""Leads: once a name the player asks about has come up, a short phrase stands in for it ("the man I'm
looking for"), so the player need not fight speech-to-text over the name again."""
import pytest

from livingworld.dialogue import hearing, leads
from livingworld.harness import actors
from livingworld.harness.scripted_llm import ScriptedLLM
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, LivingWorld
from livingworld.store.timeline import resolve_load

PLAYER = actors.player("Torvar")


@pytest.fixture(autouse=True)
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.delenv(hearing.HEARING_ENV, raising=False)
    monkeypatch.delenv(leads.LEADS_ENV, raising=False)
    monkeypatch.setattr(LivingWorld, "start_workers", False)


# ---- the phrases ----
def test_phrases_that_stand_in_for_a_name():
    assert leads.shortcut("Have you seen the man I'm looking for?") == ("the man I'm looking for", "person")
    assert leads.shortcut("have you seen the guy im after") == ("the guy im after", "person")
    assert leads.shortcut("Do you know the place I am looking for?")[1] == "place"
    assert leads.shortcut("Any word on my lead?") == ("my lead", None)
    assert leads.shortcut("Have you seen who I'm looking for?")[1] == "person"
    assert leads.shortcut("I'm looking for a man named Vald.") is None
    assert leads.shortcut("Where is the blacksmith?") is None


def test_the_newest_lead_of_the_kind_asked_for():
    found = [leads.Lead("Vald", "person", 3.0), leads.Lead("Pawned Prawn", "place", 4.0)]
    assert leads.pick(found, "person").name == "Vald"
    assert leads.pick(found, "place").name == "Pawned Prawn"
    assert leads.pick(found, None).name == "Pawned Prawn"
    assert leads.pick(found, "thing").name == "Pawned Prawn"   # none of that kind: the newest
    assert leads.pick([], None) is None
    note = leads.shortcut_note("Have you seen the man I'm looking for?", found, "Torvar")
    assert note == ('When Torvar says "the man I\'m looking for", they mean Vald, the person Torvar '
                    "has been asking around about.")


# ---- learning ----
def test_names_in_an_npc_line():
    assert leads.names_in("Vald? Haven't seen him. Try the Pawned Prawn, or ask Maven Black-Briar.") == [
        "Vald", "Pawned Prawn", "Maven Black-Briar"]
    assert leads.names_in("Yes, Gerdur knows him.", ["Gerdur"]) == []
    assert leads.names_in("I don't know. Ask the Jarl.") == []


def test_learned_from_the_answer():
    found = leads.from_reply("Do you know where vault is?", "Vald? He owes Maven money. Try the docks.",
                             ["Gerdur", "Torvar"])
    assert found == [leads.Learned("Vald", "person", "npc_named", "vault")]
    place = leads.from_reply("Where's the pond prawn?", "The Pawned Prawn is by the market.")
    assert place == [leads.Learned("Pawned Prawn", "place", "npc_named", "pond prawn")]
    right = leads.from_reply("Have you seen Vald?", "Vald? Not today.")
    assert right == [leads.Learned("Vald", "person", "said", None)]


def test_nothing_learned_from_an_unrelated_answer():
    assert leads.from_reply("Where is the blacksmith?", "Alvor works the forge in Riverwood.") == []
    assert leads.from_reply("Nice weather.", "Vald? Who is that?") == []   # not a question about anyone
    assert leads.from_reply("Have you seen the man I'm looking for?", "Vald? No.") == []


def test_learned_from_the_misheard_list():
    assert leads.from_player("Looking for a gentleman named Wald.") == [
        leads.Learned("Vald", "person", "heard", "named Wald")]
    assert leads.from_player("As stain of white run, help me.") == []


def test_a_lead_sharpens_hearing_in_questions():
    assert hearing.notes("Where's vault hiding?", leads=["Vald"]) == ['"vault" may be "Vald"']
    assert hearing.notes("Where's Vald hiding?", leads=["Vald"]) == []
    assert hearing.notes("The vault door is open.", leads=["Vald"]) == []   # not a question
    assert hearing.notes("Have you been to the pond prawn?", leads=["Pawned Prawn"]) == [
        '"pond prawn" may be "Pawned Prawn"']


# ---- in conversations ----
def test_ask_once_then_use_the_phrase(tmp_path):
    llm = ScriptedLLM(replies=["Hello there.", "Vald? He owes Maven Black-Briar. Try the docks.",
                               "Hello again.", "Haven't seen him."])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([PLAYER, actors.gerdur()], game_days=2.0)
        s.continue_until_player_turn()
        s.say("Do you know where vault is?")
        s.end(2.01)
        store = LivingWorld.latest.store
        with store.reading() as db:
            assert [l.name for l in leads.current(db)] == ["Vald"]
            assert store.scalar("SELECT heard_as FROM player_lead") == "vault"

        s.start([PLAYER, actors.hod()], game_days=2.1)
        s.continue_until_player_turn()
        s.say("Have you seen the man I'm looking for?")
        user = llm.dialogue_calls[-1].last_user_text
        assert "they mean Vald, the person Torvar has been asking around about" in user
        assert "Have you seen the man I'm looking for?" in user   # the words themselves are kept


def test_an_older_save_forgets_newer_leads(tmp_path):
    llm = ScriptedLLM(replies=["Hello.", "Vald? No idea."])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([PLAYER, actors.gerdur()], game_days=5.0)
        s.continue_until_player_turn()
        s.say("Have you seen Vald?")
        s.end(5.01)
        store = LivingWorld.latest.store
        with store.reading() as db:
            assert [l.name for l in leads.current(db)] == ["Vald"]
        resolve_load(store, 777, None, 3.0)
        with store.reading() as db:
            assert leads.current(db) == []


def test_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv(leads.LEADS_ENV, "0")
    llm = ScriptedLLM(replies=["Hello.", "Vald? No idea.", "What man?"])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([PLAYER, actors.gerdur()], game_days=2.0)
        s.continue_until_player_turn()
        s.say("Have you seen Vald?")
        s.say("Have you seen the man I'm looking for?")
        assert "they mean" not in llm.dialogue_calls[-1].last_user_text
        assert LivingWorld.latest.store.scalar("SELECT COUNT(*) FROM player_lead") == 0
