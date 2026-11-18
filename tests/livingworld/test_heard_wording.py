"""What NPCs heard, worded right (2026-11-17 play log).

- "Zachary Lex told me that theypped by to check on Ahlam": the teller's full name was cut off a sentence that
  named them by first name only, eating the start of the verb.
- "Zachary arrested me Mikael in the Plains District market": the superior the arrest was reported to was put in
  as the object of 'arrested'."""
import pytest

from livingworld.fog import service as fog
from livingworld.memory import recall as R
from livingworld.social import claims


@pytest.fixture
def names(monkeypatch):
    table = {1: "Zachary", 2: "Mikael", 39: "Commander Caius"}
    monkeypatch.setattr(fog, "shown_name", lambda db, viewer, npc_id: table[npc_id])
    return table


def test_the_teller_becomes_they_without_eating_the_verb(names):
    claim = {"subject_id": 1}
    assert R._they(None, claim, 39, "Zachary stopped by to check on Ahlam", "Zachary Lex") == \
        "they stopped by to check on Ahlam"
    assert R._they(None, claim, 39, "Zachary Lex cleared out some bandits", "Zachary Lex") == \
        "they cleared out some bandits"
    assert R._they(None, claim, 39, "a stranger in armour helped", "Zachary Lex") == "a stranger in armour helped"


def test_an_arrest_is_about_the_culprit_not_the_superior(names):
    claim = {"subject_id": 1, "object_id": 39, "act": "arrested", "detail": " Mikael in the Plains District market",
             "location": None}
    assert claims.sentence(None, claim, 39) == "Zachary arrested Mikael in the Plains District market"
    cleared = dict(claim, act="cleared", detail=" the bandits at Halted Stream Camp")
    assert claims.sentence(None, cleared, 39) == "Zachary drove off the bandits at Halted Stream Camp"
