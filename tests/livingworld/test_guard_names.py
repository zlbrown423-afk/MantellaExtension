"""Generic guards show their own name in game: "Whiterun Guard" becomes the personal name ``profiles.py``
gave them, once the player has learned it (fog on) or straight away (fog off). Who the guard is does not
change: same npc row, same voice binding, and Mantella keeps hearing "Whiterun Guard".
"""
import sqlite3

import pytest

from livingworld import fog
from livingworld.guard import GUARD_ENV, names as GN, orgs as O
from livingworld.harness import actors
from livingworld.harness.scripted_llm import ScriptedLLM
from livingworld.harness.session import HeadlessSession
from livingworld.integration import (ENV_VAR, KEY_CRIME_FACTION, KEY_CRIME_PLUGIN, KEY_IS_GUARD, KEY_PLAYER_BOUNTY,
                                     LivingWorld)
from livingworld.memory import jobs

WHITERUN = O.by_key("whiterun_guard")
REF_A, REF_B = 0x0001A6B2, 0x0001A6B3


@pytest.fixture(autouse=True)
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.delenv(GUARD_ENV, raising=False)
    monkeypatch.delenv(GN.ENV, raising=False)
    monkeypatch.delenv(fog.ENV, raising=False)
    monkeypatch.setattr(LivingWorld, "start_workers", False)
    monkeypatch.setattr(jobs, "BACKOFF_SECONDS", (0, 0, 0, 0, 0))


def player(fog_game=True):
    p = actors.player("Torvar")
    return {**p, fog.KEY_FOG_LABEL: "Nord man"} if fog_game else p


def guard(ref=REF_A):
    a = actors.actor("Whiterun Guard", 0x000A0DA4, ref, 0, "Nord", "MaleGuard")
    a[KEY_IS_GUARD] = True
    a[KEY_CRIME_FACTION] = int(WHITERUN.crime_faction, 16)
    a[KEY_CRIME_PLUGIN] = "Skyrim.esm"
    a[KEY_PLAYER_BOUNTY] = 0
    a[fog.KEY_FOG_LABEL] = ""     # the game never hides generic NPCs
    return a


class Spy:
    def __init__(self, lw):
        self.keys = []
        original = lw.line_reply_keys

        def capture(sentence):
            keys = original(sentence)
            self.keys.append(dict(keys))
            return keys
        lw.line_reply_keys = capture

    def shown(self):
        return [(i, n) for k in self.keys for i, n in zip(k.get(GN.KEY_IDS) or [], k.get(GN.KEY_NAMES) or [])]


def open_db(work_dir) -> sqlite3.Connection:
    conn = sqlite3.connect(work_dir / "data" / "livingworld" / "HeadlessTest.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def given(work_dir, ref=REF_A):
    return open_db(work_dir).execute(
        "SELECT p.given_name FROM guard_profile p JOIN npc n ON n.npc_id = p.npc_id WHERE n.ref_form_id = ?",
        (f"Skyrim.esm|0x{ref:08X}",)).fetchone()[0]


def converse(work_dir, replies, day, npcs=None, say=None, fog_game=True):
    llm = ScriptedLLM(replies=replies)
    with HeadlessSession(str(work_dir), llm) as s:
        start = s.start([player(fog_game), *(npcs or [guard()])], location="Whiterun", game_days=day)
        spy = Spy(LivingWorld.latest)
        s.continue_until_player_turn()
        if say:
            s.say(say)
        s.end(game_days=day + 0.01)
    return start, spy, llm


def test_fog_on_the_name_shows_once_the_guard_says_it(tmp_path):
    start, spy, _ = converse(tmp_path, ["Move along."], 2.0)
    assert start[GN.KEY_ON] is True
    assert GN.KEY_IDS not in start and spy.shown() == []          # met, but never said his name
    name = given(tmp_path)
    _, spy, _ = converse(tmp_path, [f"Name's {name}. Keep out of trouble."], 2.5)
    assert spy.shown() == [(REF_A, name)]
    start, spy, _ = converse(tmp_path, ["You again."], 3.0)       # the next time, from the start
    assert start[GN.KEY_IDS] == [REF_A] and start[GN.KEY_NAMES] == [name]
    assert spy.shown() == []                                       # sent once per conversation


def test_the_player_calling_a_guard_by_name_counts(tmp_path):
    converse(tmp_path, ["Move along."], 2.0)
    name = given(tmp_path)
    _, spy, _ = converse(tmp_path, ["Hm.", "Aye, that's me."], 2.5, say=f"you're {name.lower()}, right?")
    assert spy.shown() == [(REF_A, name)]


def test_the_name_stays_with_the_guard_and_identity_does_not_change(tmp_path):
    converse(tmp_path, ["Move along."], 2.0, npcs=[guard(REF_A)])
    converse(tmp_path, ["Move along."], 2.1, npcs=[guard(REF_B)])
    a, b = given(tmp_path, REF_A), given(tmp_path, REF_B)
    assert a != b
    db = open_db(tmp_path)
    before = db.execute("SELECT npc_id, display_name FROM npc WHERE is_player = 0 ORDER BY npc_id").fetchall()
    _, spy, _ = converse(tmp_path, [f"I'm {b}. Who's asking?"], 2.5, npcs=[guard(REF_B)])
    assert spy.shown() == [(REF_B, b)]
    db = open_db(tmp_path)
    after = db.execute("SELECT npc_id, display_name FROM npc WHERE is_player = 0 ORDER BY npc_id").fetchall()
    assert [tuple(r) for r in after] == [tuple(r) for r in before]   # no new npc rows, still "Whiterun Guard"
    assert given(tmp_path, REF_A) == a and given(tmp_path, REF_B) == b


def shown_all(start, spy):
    return list(zip(start.get(GN.KEY_IDS) or [], start.get(GN.KEY_NAMES) or [])) + spy.shown()


def test_fog_off_shows_the_name_straight_away(tmp_path, monkeypatch):
    monkeypatch.setenv(fog.ENV, "0")
    start, spy, _ = converse(tmp_path, ["Move along."], 2.0)
    assert shown_all(start, spy) == [(REF_A, given(tmp_path))]


def test_an_older_game_build_shows_names_straight_away(tmp_path):
    start, spy, _ = converse(tmp_path, ["Move along."], 2.0, fog_game=False)
    assert shown_all(start, spy) == [(REF_A, given(tmp_path))]


def test_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv(GN.ENV, "0")
    monkeypatch.setenv(fog.ENV, "0")
    start, spy, llm = converse(tmp_path, ["Move along."], 2.0)
    assert start[GN.KEY_ON] is False and spy.shown() == []
    assert "this guard's own name is" in llm.dialogue_calls[0].system_prompt   # the guard still knows it


def test_a_guard_name_learned_after_the_save_is_forgotten_on_loading_it():
    from livingworld.store.timeline import resolve_load
    from livingworld.store.world_store import WorldStore
    store = WorldStore(":memory:")
    with store.transaction() as db:
        db.execute("INSERT INTO npc (npc_id, plugin, base_form_id, ref_form_id, display_name, sex, is_generic, is_player,"
                   " created_gt) VALUES (1,'Skyrim.esm','0x7',NULL,'Torvar','male',0,1,1.0),"
                   " (2,'Skyrim.esm','0xA0DA4','0x0001A6B2','Whiterun Guard','male',1,0,1.0)")
        db.execute("INSERT INTO guard_profile (npc_id, org_key, given_name, first_seen_gt) VALUES (2,'whiterun_guard','Hrolf',1.0)")
        assert GN.shown(db, 2, 1, fog_on=True) is None and GN.shown(db, 2, 1, fog_on=False) == "Hrolf"
        assert GN.npc_line(db, 2, "Hrolf. Now move along.", [1, 2], 1, 3.0, None)
        assert GN.npc_line(db, 2, "Hrolf, I said.", [1, 2], 1, 3.1, None) == []    # already known
    resolve_load(store, 1, 1, 3.5)
    with store.reading() as db:
        assert GN.shown(db, 2, 1, fog_on=True) == "Hrolf"
    with store.transaction() as db:   # something after the save's time, so loading it branches
        db.execute("INSERT INTO conversation (conv_id, location, kind, started_gt, ended_gt) VALUES (9,'Whiterun','player',4.0,4.1)")
    resolve_load(store, 2, 1, 2.0)
    with store.reading() as db:
        assert GN.shown(db, 2, 1, fog_on=True) is None
        assert GN.given_name(db, 2) == "Hrolf"     # the name itself stays with the guard
