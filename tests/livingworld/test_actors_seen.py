"""actors_seen: a stray non-actor entry in the request's actor list (seen in play on 2026-11-10, an
end-conversation request inside ``mantella_actors``) is skipped, and the actors around it are still kept."""
from livingworld.harness import actors
from livingworld.integration import LivingWorld


def bare_living_world() -> LivingWorld:
    lw = LivingWorld.__new__(LivingWorld)
    lw.enabled, lw.errors, lw.guard = True, 0, False
    lw._identities, lw._ref_raw, lw._fog_labels, lw._guard_facts = {}, {}, {}, {}
    lw._fog_game = False
    return lw


def test_a_stray_entry_is_skipped_and_the_rest_kept():
    lw = bare_living_world()
    stray = {"mantella_actor_is_in_combat": False, "mantella_request_type": "mantella_end_conversation"}
    lw.actors_seen([actors.player("Torvar"), stray, actors.gerdur()])
    assert lw.errors == 0
    assert sorted(i.name for i in lw._identities.values()) == ["Gerdur", "Torvar"]
