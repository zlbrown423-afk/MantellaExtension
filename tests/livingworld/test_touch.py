"""Touch consent: an NPC agrees to a hug or other touch, and it is remembered and talked about as affection.

The game side (Mantella-Spell: PLANCK ignore list, no pull-away, open arms) is simulated with records built
as the game writes them; everything after a record or an NPC line is the real code path.
"""
import json
import sqlite3

import pytest

from livingworld import touch as T
from livingworld.harness import actors
from livingworld.harness.perception import KEY_EVENTS, consume, grab, seen
from livingworld.harness.scripted_llm import ScriptedLLM
from livingworld.harness.session import HeadlessSession
from livingworld.integration import ENV_VAR, LivingWorld
from livingworld.perception.records import parse_record
from livingworld.touch import words as W
from src.http.communication_constants import communication_constants as comm_consts

PLAYER = actors.player("Torvar")
SPINE = "NPC Spine2 [Spn2]"


@pytest.fixture(autouse=True)
def living_world_inline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    monkeypatch.setenv("MANTELLA_LW_TASKS", "0")
    monkeypatch.setattr(LivingWorld, "start_workers", False)


def open_db(work_dir) -> sqlite3.Connection:
    conn = sqlite3.connect(work_dir / "data" / "livingworld" / "HeadlessTest.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def talk(work_dir, npcs, day, records=None, replies=("Hm.",), player_says=None):
    llm = ScriptedLLM(replies=list(replies))
    with HeadlessSession(str(work_dir), llm) as s:
        s.start([PLAYER, *npcs], game_days=day, context_extra={KEY_EVENTS: records or []})
        s.continue_until_player_turn()
        if player_says:
            s.say(player_says, game_days=day + 0.001)
        s.end(game_days=day + 0.01)
    lines = [r.get(comm_consts.KEY_REPLYTYPE_NPCTALK) or {} for _, r in s.exchanges
             if r.get(comm_consts.KEY_REPLYTYPE) == comm_consts.KEY_REPLYTYPE_NPCTALK]
    starts = [r for _, r in s.exchanges if r.get(comm_consts.KEY_REPLYTYPE) == comm_consts.KEY_REPLYTTYPE_STARTCONVERSATIONCOMPLETED]
    return llm, lines, starts


def memories(db, name):
    return db.execute("SELECT m.* FROM memory m JOIN npc n ON n.npc_id = m.owner_id WHERE n.display_name = ?"
                      " AND m.created_by = 'perception' ORDER BY m.mem_id", (name,)).fetchall()


def hugged(day=3.6, key="h1", consent="hug", area="upper back", allowed=None, witnesses=None):
    return grab(PLAYER, actors.gerdur(), SPINE, 4.0, day, key=key, area=area, consent=consent, allowed=allowed,
                witnesses=witnesses if witnesses is not None else [seen(actors.hod(), 4.0, rank_to_actor=0, rank_to_target=4)])


# ---- kinds and areas ----------------------------------------------------------------------------

def test_kinds_are_normalised_from_lists_strings_and_everyday_words():
    assert T.normalize_kinds(["Hug", "hold hands"]) == ["hug", "hands"]
    assert T.normalize_kinds("boobs, bum") == ["butt", "chest"]
    assert T.normalize_kinds("kiss, nonsense") == []


def test_each_kind_covers_its_own_areas_only():
    assert T.covers(["hug"], "upper back") == "hug"
    assert T.covers(["hug"], "left shoulder") == "hug"
    assert T.covers(["hug"], "chest") is None and T.covers(["hug"], "backside") is None
    assert T.covers(["hug"], "neck") == "hug" and T.covers(["hug"], "right hip") == "hug"   # hands drift in a hug
    assert T.covers(["hug"], "hips") is None and T.covers(["hug"], "face") is None
    assert T.covers(["hands"], "right hand") == "hands"
    assert T.covers(["butt"], "backside") == "butt" and T.covers(["butt"], "hips") is None   # the front is not the backside
    assert T.covers(["hug", "chest"], "chest") == "chest"
    assert T.covers(["anywhere"], "hips") == "anywhere"
    assert T.covers(["hug"], None) == "hug" and T.covers(["hands"], None) is None


# ---- words ----------------------------------------------------------------------------------------

def test_what_the_player_asks_for():
    assert W.asked_kinds("Can I give you a hug?") == ["hug"]
    assert W.asked_kinds("May I hold your hand?") == ["hands"]
    assert W.asked_kinds("let me rub your back for a bit") == ["back"]
    assert W.asked_kinds("Could I touch your face?") == ["face"]
    assert W.asked_kinds("I gave Hod a hug yesterday.") == []          # a story, not a request


def test_only_a_plain_yes_agrees():
    assert W.agrees("Of course, come here.")
    assert W.agrees("Yes.")
    assert not W.agrees("No, not here.")
    assert not W.agrees("Yes... but not now.")
    assert not W.agrees("Do you think that's wise?")


def test_the_npc_can_offer_and_take_back():
    assert W.offered_kinds("Come here, give me a hug!") == ["hug"]
    assert W.offered_kinds("Take my hand, it's slippery.") == ["hands"]
    assert W.offered_kinds("Don't give me a hug.") == []
    assert W.withdraws("Stop that, right now.")
    assert W.withdraws("Get your hands off me!")
    assert not W.withdraws("Let's stop by the mill later.")


def test_the_watcher_adds_allow_touch_to_the_npc_yes():
    watcher = W.TouchWatcher("Can I hug you?", {T.ALLOW, T.WITHDRAW})
    acts = []
    assert watcher.keep("Gerdur", "Of course, come here.", acts) == [T.ALLOW]
    assert acts == [{"identifier": T.ALLOW, "arguments": {"source": "Gerdur", "touch": ["hug"], "via": "words"}}]
    assert watcher.keep("Gerdur", "Yes.", acts) == []                 # once per reply


def test_the_watcher_leaves_a_reply_that_already_called_the_tool():
    watcher = W.TouchWatcher("Can I hug you?", {T.ALLOW})
    acts = [{"identifier": T.ALLOW, "arguments": {"source": "Gerdur", "touch": ["hug"]}}]
    assert watcher.keep("Gerdur", "Of course.", acts) == [] and len(acts) == 1


# ---- the conversation ------------------------------------------------------------------------------

def test_the_start_reply_switches_the_game_side_on(tmp_path):
    _, _, starts = talk(tmp_path, [actors.gerdur()], 3.5)
    assert starts and starts[0].get(T.KEY_ON) is True


def test_allow_touch_is_sent_to_the_game_and_logged(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5)
    llm, lines, _ = talk(tmp_path, [actors.gerdur()], 3.6, replies=["Hm.", "AllowTouch: Of course, come here."],
                         player_says="Can I give you a hug?")
    sent = [l[T.KEY_ALLOW] for l in lines if T.KEY_ALLOW in l]
    assert sent == [["Gerdur|hug|0"]]
    db = open_db(tmp_path)
    row = db.execute("SELECT * FROM consent_decision WHERE kind = 'touch'").fetchone()
    assert row["answer"] == "yes" and json.loads(row["reasons_json"])["kinds"] == ["hug"]


def test_a_yes_in_words_counts_too(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5)
    _, lines, _ = talk(tmp_path, [actors.gerdur()], 3.6, replies=["Hm.", "Of course, come here."],
                       player_says="May I hold your hand?")
    assert [l[T.KEY_ALLOW] for l in lines if T.KEY_ALLOW in l] == [["Gerdur|hands|0"]]


def test_the_agreement_is_in_the_npc_prompt_afterwards(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5)
    llm = ScriptedLLM(replies=["Hm.", "AllowTouch: Come here.", "Mm."])
    with HeadlessSession(str(tmp_path), llm) as s:
        s.start([PLAYER, actors.gerdur()], game_days=3.6)
        s.continue_until_player_turn()
        s.say("Can I hug you?", game_days=3.601)
        s.say("That was nice.", game_days=3.602)
        s.end(game_days=3.61)
    later = llm.dialogue_calls[2:]
    assert later and any("Gerdur has agreed to be touched in these ways: hug" in str(c.messages) for c in later)


def test_a_new_prompt_lists_what_was_agreed():
    from livingworld.touch.consent import Desk, Grant
    desk = Desk(grants={7: Grant("Gerdur", ["hug", "hands"])})
    (line,) = desk.prompt_lines(7, "Gerdur", "Torvar")
    assert line.startswith("Gerdur has agreed to let Torvar hug them and hold their hand, for now.")
    assert desk.prompt_lines(8, "Hod", "Torvar") == []


def test_stop_touch_takes_it_back(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5)
    _, lines, _ = talk(tmp_path, [actors.gerdur()], 3.6, replies=["Hm.", "That's enough, get your hands off me."],
                       player_says="Come on, one more?")
    assert [l[T.KEY_WITHDRAW] for l in lines if T.KEY_WITHDRAW in l] == [["Gerdur"]]


def test_no_intimate_yes_while_drunk(tmp_path):
    drinks = [consume(actors.gerdur(), "Nord Mead", 3.55 + i * 0.001, is_food=False, key=f"d{i}") for i in range(8)]
    talk(tmp_path, [actors.gerdur()], 3.56, records=drinks)
    _, lines, _ = talk(tmp_path, [actors.gerdur()], 3.57, replies=["Hm.", "AllowTouch: Mm, yes."],
                       player_says="Can I touch your breasts?")
    assert not any(T.KEY_ALLOW in l for l in lines)
    db = open_db(tmp_path)
    row = db.execute("SELECT * FROM consent_decision WHERE kind = 'touch'").fetchone()
    assert row["answer"] == "no" and json.loads(row["reasons_json"])["not_while_drunk"] == ["chest"]


# ---- what the touch becomes ----------------------------------------------------------------------

def test_a_record_carries_the_agreement():
    rec = parse_record(hugged(consent="Hug"))
    assert rec.consent == "hug"
    rec = parse_record(hugged(consent="breach", area="chest", allowed="hug,hands"))
    assert rec.consent == "breach" and rec.allowed == ["hug", "hands"]
    assert parse_record(hugged(consent="whatever")).consent is None


def test_an_agreed_hug_is_remembered_warmly_by_her_and_as_friendly_by_onlookers(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5)
    witnesses = [seen(actors.hod(), 4.0, rank_to_actor=1, rank_to_target=1)]   # Hod as a friend, not her husband
    talk(tmp_path, [actors.gerdur()], 3.65, records=[hugged(witnesses=witnesses)])
    db = open_db(tmp_path)
    (g,) = memories(db, "Gerdur")
    assert g["content"] == "Torvar hugged me for about 4 seconds in Riverwood, as I had agreed."
    assert g["valence"] > 0 and "said yes" in g["interpretation"]
    (h,) = memories(db, "Hod")
    assert h["content"].startswith("I saw Torvar hug Gerdur") and "willing" in h["content"]
    assert "welcomed" in h["interpretation"] and h["valence"] >= 0
    trust = db.execute("SELECT r.trust FROM v_relationship r JOIN npc n ON n.npc_id = r.from_id"
                       " JOIN npc p ON p.npc_id = r.to_id AND p.is_player = 1 WHERE n.display_name = 'Gerdur'").fetchone()[0]
    assert trust > 0


def test_gossip_about_an_agreed_hug_is_friendly(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5)
    talk(tmp_path, [actors.gerdur()], 3.65, records=[hugged()])
    db = open_db(tmp_path)
    claim = db.execute("SELECT * FROM claim").fetchone()
    assert claim["act"] == "hugged" and claim["valence"] > 0


def test_her_husband_does_not_like_seeing_it(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5)
    talk(tmp_path, [actors.gerdur()], 3.65, records=[hugged(consent="butt", area="backside")])
    db = open_db(tmp_path)
    (h,) = memories(db, "Hod")
    assert "stings" in h["interpretation"] and h["valence"] < 0
    claim = db.execute("SELECT * FROM claim").fetchone()
    assert claim["act"] == "caressed" and claim["sensitivity"] == "private"


def test_going_past_what_was_agreed_is_worse_than_a_grab(tmp_path):
    talk(tmp_path, [actors.gerdur()], 3.5)
    talk(tmp_path, [actors.gerdur()], 3.65, records=[hugged(consent="breach", area="chest", allowed="hug")])
    db = open_db(tmp_path)
    (g,) = memories(db, "Gerdur")
    assert "only agreed to let them hug them" in g["content"] and g["valence"] <= -0.7
    claim = db.execute("SELECT * FROM claim").fetchone()
    assert claim["act"] in ("grabbed", "held") and claim["valence"] <= -0.7


def test_off_switch_leaves_the_actions_out(monkeypatch):
    from livingworld.actions import action_files
    monkeypatch.setenv(T.ENV, "0")
    assert not any(f.name.startswith("touch_") for f in action_files())
    monkeypatch.setenv(T.ENV, "1")
    assert {f.name for f in action_files()} >= {"touch_allow.json", "touch_withdraw.json"}



def test_open_arms_is_a_gesture_the_npc_notices():
    from livingworld.bodylang import presence as P
    from livingworld.harness.perception import actor_field
    base = f"v=1|key=p1|type=presence|gt=3.6|actor={actor_field(PLAYER)}|target={actor_field(actors.gerdur())}|what=open_arms"
    offer = P.parse(base + "|dist=1.4")
    assert P.is_gesture_record(base) and "offering a hug" in P.observation(offer, "Torvar")
    agreed = P.parse(base + "|agreed=1")
    assert "opened theirs back" in P.observation(agreed, "Torvar")
