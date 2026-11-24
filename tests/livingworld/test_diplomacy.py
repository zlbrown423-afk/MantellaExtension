"""Diplomacy (livingworld/diplomacy): the record of politics, on a real in-memory database.

These are rule tests: statements, awareness, positions, promises, invitations, meetings, couriers, gossip, the
journal and the prompt block. They cannot show how the dialogue model argues with this context in the game.
"""
import json

import pytest

from livingworld.diplomacy import catchup, commitments as C, diplomacy_debug_enabled, diplomacy_enabled
from livingworld.diplomacy import debug as D, extract as X, figures as F, journal as J, meetings as M
from livingworld.diplomacy import prompts as P, record as R
from livingworld.memory.extraction import Line
from livingworld.store.world_store import WorldStore

ULFRIC, GALMAR, JORLEIF, TULLIUS, BALGRUUF, IRILETH = (
    "0001414D", "00014128", "00014135", "0001327E", "00013BBD", "00013BB8")


@pytest.fixture
def store():
    return WorldStore(":memory:")


@pytest.fixture
def db(store):
    with store.transaction() as conn:
        yield conn


def npc(db, name, base, player=False):
    return db.execute("INSERT INTO npc (plugin, base_form_id, ref_form_id, display_name, is_player, created_gt)"
                      " VALUES ('Skyrim.esm', ?, ?, ?, ?, 0)", (base, base + "r", name, int(player))).lastrowid


@pytest.fixture
def w(db):
    ids = {"player": npc(db, "Zachary", "00000007", player=True), "ulfric": npc(db, "Ulfric Stormcloak", ULFRIC),
           "galmar": npc(db, "Galmar Stone-Fist", GALMAR), "jorleif": npc(db, "Jorleif", JORLEIF),
           "tullius": npc(db, "General Tullius", TULLIUS), "balgruuf": npc(db, "Balgruuf the Greater", BALGRUUF),
           "irileth": npc(db, "Irileth", IRILETH), "hulda": npc(db, "Hulda", "00013BA3")}
    for key in ("ulfric", "galmar", "jorleif", "tullius", "balgruuf", "irileth"):
        F.seed(db, ids[key], 1)
    return ids


class Talk:
    """One conversation: who was there, what was said (lines are (speaker key, text))."""

    def __init__(self, db, w, who, lines, gt=10.0, location="Windhelm"):
        self.db, self.w, self.gt, self.location = db, w, gt, location
        self.conv = db.execute("INSERT INTO conversation (timeline_id, location, kind, started_gt) VALUES"
                               " ((SELECT timeline_id FROM timeline WHERE is_active = 1), ?, 'player', ?)",
                               (location, gt)).lastrowid
        for k in who:
            db.execute("INSERT INTO participant (conv_id, npc_id, joined_seq) VALUES (?,?,0)", (self.conv, w[k]))
        self.lines = []
        for seq, (k, text) in enumerate(lines, start=1):
            utt = db.execute("INSERT INTO utterance (timeline_id, conv_id, seq, speaker_id, text, game_time) VALUES"
                             " ((SELECT timeline_id FROM timeline WHERE is_active = 1), ?,?,?,?,?)",
                             (self.conv, seq, w[k], text, gt)).lastrowid
            name = db.execute("SELECT display_name FROM npc WHERE npc_id = ?", (w[k],)).fetchone()[0]
            self.lines.append(Line(seq, utt, w[k], name, text))
        self.names = {w[k]: db.execute("SELECT display_name FROM npc WHERE npc_id = ?", (w[k],)).fetchone()[0]
                      for k in who}

    def record(self, *items):
        return R.record(self.db, self.conv, list(items), self.lines, self.names, self.location, self.gt)

    def end(self, gt=None):
        self.db.execute("UPDATE conversation SET ended_gt = ? WHERE conv_id = ?", (gt or self.gt + 0.05, self.conv))


def item(line, said_by, kind, issue="other", **kw):
    return {"line": line, "said_by": said_by, "kind": kind, "issue": issue, "summary": kw.pop("summary", "says so"),
            **kw}


def statements(db):
    return [dict(r) for r in db.execute("SELECT * FROM v_dip_statement ORDER BY stmt_id")]


def aware(db, holder):
    return {r[0]: r[1] for r in db.execute("SELECT stmt_id, origin FROM v_dip_awareness WHERE holder_id = ?", (holder,))}


def position(db, npc_id, dim, layer="personal"):
    r = db.execute("SELECT value, source FROM v_political_position WHERE npc_id = ? AND dimension = ? AND layer = ?",
                   (npc_id, dim, layer)).fetchone()
    return (r[0], r[1]) if r else None


def new_branch(db, at_gt):
    """Load an older save: a new active timeline forked from the current one at ``at_gt``."""
    old = db.execute("SELECT timeline_id FROM timeline WHERE is_active = 1").fetchone()[0]
    db.execute("UPDATE timeline SET is_active = 0 WHERE timeline_id = ?", (old,))
    return db.execute("INSERT INTO timeline (parent_id, branch_game_time, save_name, is_active) VALUES (?,?,'s',1)",
                      (old, at_gt)).lastrowid


# ---- flags and the extraction prompt ----

def test_off_by_default(monkeypatch):
    monkeypatch.delenv("MANTELLA_LW_DIPLOMACY", raising=False)
    assert not diplomacy_enabled() and not diplomacy_debug_enabled()
    monkeypatch.setenv("MANTELLA_LW_DIPLOMACY", "1")
    assert diplomacy_enabled() and diplomacy_debug_enabled()
    monkeypatch.setenv("MANTELLA_LW_DIPLOMACY_DEBUG", "0")
    assert not diplomacy_debug_enabled()


def _extract(store, conv, monkeypatch, on, reply_items=()):
    from livingworld.memory.extraction import Extractor
    if on:
        monkeypatch.setenv("MANTELLA_LW_DIPLOMACY", "1")
    else:
        monkeypatch.delenv("MANTELLA_LW_DIPLOMACY", raising=False)
    calls = []

    def complete(system, user):
        calls.append(system)
        return json.dumps({"facts": [], "memories": [], "reports": [], "diplomacy": list(reply_items)})
    Extractor(store, complete).extract_conversation(conv)
    return calls


def test_extraction_prompt_is_unchanged_when_off_and_read_once_when_on(store, monkeypatch):
    with store.transaction() as db:
        w = {"player": npc(db, "Zachary", "00000007", True), "ulfric": npc(db, "Ulfric Stormcloak", ULFRIC),
             "galmar": npc(db, "Galmar Stone-Fist", GALMAR)}
        t = Talk(db, w, ["player", "ulfric", "galmar"],
                 [("player", "Would you consider a truce with the Empire?"),
                  ("ulfric", "Never, while Talos is outlawed."), ("galmar", "Hah.")])
    from livingworld.memory.extraction import SYSTEM_PROMPT
    off = _extract(store, t.conv, monkeypatch, on=False)
    assert len(off) == 2 and all("diplomacy" not in s for s in off)
    assert off[0].startswith(SYSTEM_PROMPT.split("{owner}")[0])
    items = [item(2, "Ulfric Stormcloak", "opposition", "ceasefire", summary="will not accept a truce while Talos is outlawed",
                  stance=-0.9)]
    on = _extract(store, t.conv, monkeypatch, on=True, reply_items=items)
    assert sum("diplomacy" in s for s in on) == 1          # one scribe, no extra call
    with store.reading() as db:
        assert len(statements(db)) == 1
    again = _extract(store, t.conv, monkeypatch, on=True, reply_items=items)   # a job retry
    assert len(again) == 2
    with store.reading() as db:
        assert len(statements(db)) == 1


def test_small_talk_is_not_read_for_diplomacy(store, monkeypatch):
    with store.transaction() as db:
        w = {"player": npc(db, "Zachary", "00000007", True), "hulda": npc(db, "Hulda", "00013BA3")}
        t = Talk(db, w, ["player", "hulda"], [("player", "A mead, please."), ("hulda", "Coming right up.")])
    calls = _extract(store, t.conv, monkeypatch, on=True)
    assert all("diplomacy" not in s for s in calls)


# ---- statements and validation ----

def test_statement_awareness_and_audience(db, w):
    t = Talk(db, w, ["player", "ulfric", "galmar"],
             [("player", "Would you meet Tullius to discuss the Thalmor?"),
              ("ulfric", "I would hear what he has to say, at Windhelm, and nowhere else.")])
    r = t.record(item(2, "Ulfric Stormcloak", "position", "negotiation", stance=0.2,
                      summary="would hear Tullius out, but only at Windhelm"))
    assert r.statements == 1
    s = statements(db)[0]
    assert s["audience"] == "public" and s["subject_id"] == w["ulfric"] and s["modality"] == "asserted"
    assert aware(db, w["ulfric"]) == {s["stmt_id"]: "said"}
    assert aware(db, w["galmar"]) == {s["stmt_id"]: "witnessed"}
    assert aware(db, w["player"]) == {s["stmt_id"]: "witnessed"}
    assert aware(db, w["tullius"]) == {}


def test_wrong_speaker_and_unknown_kind_are_dropped(db, w):
    t = Talk(db, w, ["player", "ulfric"], [("player", "The Empire wants peace."), ("ulfric", "Lies.")])
    r = t.record(item(1, "Ulfric Stormcloak", "position", "empire", summary="wants peace"),     # the player said it
                 item(2, "Ulfric Stormcloak", "victory_dance", "empire"))
    assert r.statements == 0 and len(r.rejected) == 2


def test_the_line_decides_the_modality(db, w):
    t = Talk(db, w, ["player", "ulfric"], [
        ("ulfric", "Would the Empire even sit at a table with me?"),
        ("ulfric", "If Talos worship is restored, I will consider a truce."),
        ("ulfric", "What if we did fight the elves together? Just a thought."),
        ("ulfric", "I will never kneel to Tullius.")])
    t.record(item(1, "Ulfric Stormcloak", "position", "negotiation", stance=0.3, summary="asks if the Empire would talk"),
             item(2, "Ulfric Stormcloak", "commitment", "ceasefire", summary="will consider a truce",
                  condition="Talos worship is restored", modality="asserted"),
             item(3, "Ulfric Stormcloak", "support", "alliance", summary="muses about fighting the elves together"),
             item(4, "Ulfric Stormcloak", "refusal", "empire", summary="will never kneel to Tullius"))
    got = [(s["kind"], s["modality"]) for s in statements(db)]
    assert got == [("position", "question"), ("commitment", "conditional"), ("support", "hypothetical"),
                   ("refusal", "asserted")]


def test_someone_elses_view_is_only_a_report(db, w):
    t = Talk(db, w, ["player", "balgruuf"],
             [("balgruuf", "I heard Ulfric has sworn he will never treat with the Empire.")])
    t.record(item(1, "Balgruuf the Greater", "commitment", "negotiation", whose="Ulfric Stormcloak",
                  summary="has sworn never to treat with the Empire", stance=-1.0))
    s = statements(db)[0]
    assert (s["kind"], s["modality"], s["subject_id"], s["speaker_id"]) == ("report", "rumor", w["ulfric"], w["balgruuf"])
    assert db.execute("SELECT COUNT(*) FROM dip_commitment").fetchone()[0] == 0   # nobody promises for someone else
    assert position(db, w["ulfric"], "negotiation") == (-0.4, "seed")             # and it moves no conviction
    assert aware(db, w["player"])[s["stmt_id"]] == "told"


def test_a_report_about_someone_never_met_keeps_the_name(db, w):
    t = Talk(db, w, ["player", "balgruuf"], [("balgruuf", "They say Elisif wants Ulfric's head on a pike.")])
    t.record(item(1, "Balgruuf the Greater", "position", "stormcloaks", whose="Elisif the Fair",
                  summary="wants Ulfric's head", stance=-1.0))
    s = statements(db)[0]
    assert s["about_name"] == "Elisif the Fair" and s["subject_id"] == w["balgruuf"] and s["kind"] == "report"


# ---- positions ----

def test_positions_are_per_person_even_within_a_faction(db, w):
    assert position(db, w["ulfric"], "negotiation")[0] != position(db, w["galmar"], "negotiation")[0]
    assert position(db, w["jorleif"], "negotiation")[0] > position(db, w["galmar"], "negotiation")[0]


def test_disliking_the_thalmor_says_nothing_about_the_concordat(db, w):
    assert position(db, w["irileth"], "thalmor")[0] < 0
    assert position(db, w["irileth"], "concordat") is None


def test_private_words_move_belief_public_words_move_the_official_line(db, w):
    seeded = position(db, w["ulfric"], "ceasefire")
    private = Talk(db, w, ["player", "ulfric"], [("ulfric", "Between us, a truce would save Nord lives.")], gt=11)
    private.record(item(1, "Ulfric Stormcloak", "support", "ceasefire", stance=0.4, summary="sees a truce would save lives"))
    assert position(db, w["ulfric"], "ceasefire") == (0.4, "statement")
    assert position(db, w["ulfric"], "ceasefire", "official") == (-0.5, "seed")
    public = Talk(db, w, ["player", "ulfric", "galmar"], [("ulfric", "There will be no truce with the Empire!")], gt=12)
    public.record(item(1, "Ulfric Stormcloak", "opposition", "ceasefire", stance=-0.9, summary="will have no truce"))
    assert position(db, w["ulfric"], "ceasefire") == (0.4, "statement")          # still what he said in private
    assert position(db, w["ulfric"], "ceasefire", "official") == (-0.9, "statement")
    assert seeded == (-0.3, "seed")


def test_ordinary_people_in_public_change_no_position(db, w):
    t = Talk(db, w, ["player", "galmar", "jorleif"], [("galmar", "I want no truce.")])
    t.record(item(1, "Galmar Stone-Fist", "opposition", "ceasefire", stance=-1.0, summary="wants no truce"))
    assert position(db, w["galmar"], "ceasefire") == (-0.6, "seed")


def test_conflicting_beliefs_stay_separate(db, w):
    Talk(db, w, ["player", "ulfric"], [("ulfric", "I could live with a truce.")]).record(
        item(1, "Ulfric Stormcloak", "support", "ceasefire", stance=0.5, summary="could live with a truce"))
    Talk(db, w, ["player", "galmar"], [("galmar", "A truce would be cowardice.")], gt=11).record(
        item(1, "Galmar Stone-Fist", "opposition", "ceasefire", stance=-0.9, summary="calls a truce cowardice"))
    assert position(db, w["ulfric"], "ceasefire")[0] == 0.5
    assert position(db, w["galmar"], "ceasefire")[0] == -0.9


# ---- promises ----

def test_a_promise_with_a_deadline_lapses_and_the_creditor_remembers(db, w):
    t = Talk(db, w, ["player", "ulfric"], [("player", "I will bring you Tullius's answer within three days.")])
    t.record(item(1, "Zachary", "commitment", "negotiation", to="Ulfric Stormcloak", due_days=3,
                  summary="will bring Tullius's answer"))
    c = db.execute("SELECT * FROM v_dip_commitment").fetchone()
    assert c["status"] == "open" and c["debtor_id"] == w["player"] and c["due_gt"] == 13.0
    assert C.lapse_due(db, 12.0) == 0
    assert C.lapse_due(db, 14.0) == 1
    assert db.execute("SELECT status, status_gt FROM v_dip_commitment").fetchone()[:] == ("lapsed", 13.0)
    rel = db.execute("SELECT value FROM v_standing WHERE holder_id = ? AND subject_id = ? AND domain = 'reliability'",
                     (w["ulfric"], w["player"])).fetchone()[0]
    assert rel < 0
    assert db.execute("SELECT COUNT(*) FROM memory WHERE owner_id = ? AND content LIKE '%promised me%'",
                      (w["ulfric"],)).fetchone()[0] == 1
    assert C.lapse_due(db, 20.0) == 0                                  # once only
    # trust itself is not touched by the rules
    assert db.execute("SELECT COUNT(*) FROM relationship_change").fetchone()[0] == 0


def test_kept_and_withdrawn_promises(db, w):
    Talk(db, w, ["player", "ulfric"], [("ulfric", "I will release the Imperial prisoners at Windhelm.")]).record(
        item(1, "Ulfric Stormcloak", "commitment", "prisoners", to="Zachary", summary="will release the Imperial prisoners"))
    Talk(db, w, ["player", "ulfric"], [("ulfric", "The prisoners are freed, as I said.")], gt=12).record(
        item(1, "Ulfric Stormcloak", "fulfilment", "prisoners", summary="has freed the prisoners"))
    assert db.execute("SELECT status FROM v_dip_commitment").fetchone()[0] == "kept"
    Talk(db, w, ["player", "galmar"], [("galmar", "I will send fifty men to guard the talks.")], gt=13).record(
        item(1, "Galmar Stone-Fist", "commitment", "meeting", to="Zachary", summary="will send fifty men"))
    Talk(db, w, ["player", "galmar"], [("galmar", "Forget the fifty men. I take it back.")], gt=14).record(
        item(1, "Galmar Stone-Fist", "retraction", "meeting", summary="takes back the offer of men"))
    rows = [tuple(r) for r in db.execute("SELECT debtor_id, status FROM v_dip_commitment ORDER BY commit_id")]
    assert rows == [(w["ulfric"], "kept"), (w["galmar"], "withdrawn")]


# ---- invitations and meetings ----

def invite_both(db, w):
    t1 = Talk(db, w, ["player", "ulfric"], [("player", "Will you come to Dragonsreach to meet General Tullius?")], gt=10)
    t1.record(item(1, "Zachary", "invitation", "meeting", to="Ulfric Stormcloak", venue="Dragonsreach",
                   summary="invites Ulfric to Dragonsreach"))
    t2 = Talk(db, w, ["player", "tullius"], [("player", "Will you meet Ulfric at Dragonsreach?")], gt=12,
              location="Solitude")
    t2.record(item(1, "Zachary", "invitation", "meeting", to="General Tullius", venue="Dragonsreach",
                   summary="invites Tullius to Dragonsreach"))
    return db.execute("SELECT * FROM v_dip_meeting").fetchone()


def answer(db, w, who, name, text, response, gt, **kw):
    t = Talk(db, w, ["player", who], [(who, text)], gt=gt)
    t.record(item(1, name, "invitation_response", "meeting", response=response, summary=f"answers: {response}", **kw))
    return t


def test_an_invitation_is_not_acceptance(db, w):
    m = invite_both(db, w)
    assert m["status"] == "proposed" and m["venue_key"] == "dragonsreach"
    invs = {r["invitee_id"]: r["status"] for r in db.execute("SELECT * FROM v_dip_invitation")}
    assert invs == {w["ulfric"]: "delivered", w["tullius"]: "delivered"}


def test_refusal_and_counter_proposal(db, w):
    invite_both(db, w)
    answer(db, w, "ulfric", "Ulfric Stormcloak", "I will not set foot in Balgruuf's hall. Let him come to Windhelm.",
           "countered", 13, venue="Windhelm")
    answer(db, w, "tullius", "General Tullius", "No. I do not negotiate with traitors.", "declined", 14)
    inv = {r["invitee_id"]: (r["status"], r["counter_venue"]) for r in db.execute("SELECT * FROM v_dip_invitation")}
    assert inv == {w["ulfric"]: ("countered", "Windhelm"), w["tullius"]: ("declined", None)}
    assert db.execute("SELECT status FROM v_dip_meeting").fetchone()[0] == "proposed"   # nothing forced either way
    assert db.execute("SELECT COUNT(*) FROM v_dip_commitment").fetchone()[0] == 0


def test_only_the_invitee_can_answer(db, w):
    invite_both(db, w)
    t = Talk(db, w, ["player", "balgruuf"], [("balgruuf", "Ulfric told me he will come, I am sure of it.")], gt=13,
             location="Whiterun")
    t.record(item(1, "Balgruuf the Greater", "invitation_response", "meeting", whose="Ulfric Stormcloak",
                  response="accepted", summary="will come"))
    assert {r["status"] for r in db.execute("SELECT * FROM v_dip_invitation")} == {"delivered"}


def test_meeting_agreed_convened_and_an_absentee_breaks_their_word(db, w):
    invite_both(db, w)
    answer(db, w, "ulfric", "Ulfric Stormcloak", "I will come, if Galmar may stand beside me.", "conditional", 13,
           condition="Galmar may attend")
    assert db.execute("SELECT status FROM v_dip_meeting").fetchone()[0] == "proposed"
    answer(db, w, "tullius", "General Tullius", "Very well. I will be there.", "accepted", 14)
    m = db.execute("SELECT * FROM v_dip_meeting").fetchone()
    assert m["status"] == "agreed"
    # Tullius and Balgruuf turn up; Ulfric does not
    at = Talk(db, w, ["player", "tullius", "balgruuf"], [("tullius", "Where is Ulfric?")], gt=20, location="Dragonsreach")
    assert M.convene(db, at.conv, "Dragonsreach", [w["player"], w["tullius"], w["balgruuf"]], 20) == []   # one party only
    at2 = Talk(db, w, ["player", "tullius", "ulfric"], [("tullius", "So you came.")], gt=21, location="Dragonsreach")
    assert M.convene(db, at2.conv, "Dragonsreach", [w["player"], w["tullius"], w["ulfric"]], 21) == [m["meeting_id"]]
    at2.record(item(1, "General Tullius", "proposal", "ceasefire", summary="proposes a truce until spring"))
    at2.end(21.1)
    assert M.conclude(db, at2.conv) == []                      # still this conversation
    assert M.conclude(db, None) == [m["meeting_id"]]
    assert db.execute("SELECT status FROM v_dip_meeting").fetchone()[0] == "concluded"
    statuses = {r["debtor_id"]: r["status"] for r in db.execute("SELECT * FROM v_dip_commitment")}
    assert statuses == {w["ulfric"]: "kept", w["tullius"]: "kept"}
    assert [u["summary"] for u in M.unresolved(db, m["meeting_id"])] == ["proposes a truce until spring"]


def test_absent_party_breaks_the_promise(db, w):
    invite_both(db, w)
    answer(db, w, "ulfric", "Ulfric Stormcloak", "I will come.", "accepted", 13)
    answer(db, w, "tullius", "General Tullius", "I will come.", "accepted", 14)
    m = db.execute("SELECT * FROM v_dip_meeting").fetchone()
    db.execute("UPDATE dip_meeting SET host_id = ? WHERE meeting_id = ?", (w["balgruuf"], m["meeting_id"]))
    at = Talk(db, w, ["player", "tullius", "balgruuf"], [("tullius", "He is late.")], gt=20, location="Dragonsreach")
    assert M.convene(db, at.conv, "Dragonsreach", [w["player"], w["tullius"], w["balgruuf"]], 20) == [m["meeting_id"]]
    at.end(20.2)
    M.conclude(db, None)
    statuses = {r["debtor_id"]: r["status"] for r in db.execute("SELECT * FROM v_dip_commitment")}
    assert statuses == {w["ulfric"]: "broken", w["tullius"]: "kept"}
    assert db.execute("SELECT value FROM v_standing WHERE holder_id = ? AND subject_id = ? AND domain = 'reliability'",
                      (w["player"], w["ulfric"])).fetchone() is None       # the player holds no standing rows


def test_courier_carries_an_answer_to_an_absent_inviter(db, w):
    t = Talk(db, w, ["player", "balgruuf"], [("balgruuf", "Tell Ulfric he is invited to a council here at Dragonsreach.")],
             location="Whiterun")
    t.record(item(1, "Balgruuf the Greater", "invitation", "meeting", to="Ulfric Stormcloak", venue="Dragonsreach",
                  summary="invites Ulfric to a council at Dragonsreach"))
    inv = db.execute("SELECT * FROM v_dip_invitation").fetchone()
    assert inv["status"] == "pending"                                    # Ulfric has not been told yet
    carry = Talk(db, w, ["player", "ulfric"], [("player", "Jarl Balgruuf invites you to a council at Dragonsreach.")], gt=12)
    carry.record(item(1, "Zachary", "invitation", "meeting", whose="Balgruuf the Greater", to="Ulfric Stormcloak",
                      venue="Dragonsreach", summary="invites Ulfric to Dragonsreach"))
    assert db.execute("SELECT status FROM v_dip_invitation").fetchone()[0] == "delivered"
    reply = Talk(db, w, ["player", "ulfric"], [("ulfric", "I will send a courier to Balgruuf with my answer: no.")], gt=13)
    reply.record(item(1, "Ulfric Stormcloak", "invitation_response", "meeting", response="declined", summary="declines"))
    stmt_id = statements(db)[-1]["stmt_id"]
    assert stmt_id not in aware(db, w["balgruuf"])
    assert M.deliver_couriers(db, 14.0) == 0
    assert M.deliver_couriers(db, 15.5) == 1
    assert aware(db, w["balgruuf"])[stmt_id] == "courier"
    assert M.deliver_couriers(db, 30.0) == 0                              # one courier only
    assert db.execute("SELECT status FROM v_dip_meeting").fetchone()[0] == "abandoned"


# ---- gossip, rumours and the journal ----

def test_public_statement_becomes_a_claim_that_spreads_as_a_rumour(db, w):
    from livingworld.social import claims
    t = Talk(db, w, ["player", "ulfric", "galmar"], [("ulfric", "I will hear the Empire's terms, nothing more.")])
    t.record(item(1, "Ulfric Stormcloak", "position", "negotiation", stance=0.1, summary="will hear the Empire's terms"))
    s = statements(db)[0]
    assert s["claim_id"] is not None
    galmar_copy = db.execute("SELECT * FROM v_claim_knowledge WHERE holder_id = ? AND claim_id = ?",
                             (w["galmar"], s["claim_id"])).fetchone()
    assert galmar_copy["origin"] == "witnessed"
    claim = dict(db.execute("SELECT * FROM claim WHERE claim_id = ?", (s["claim_id"],)).fetchone())
    claims.hear(db, 1, w["jorleif"], w["galmar"], claim, galmar_copy["know_id"], 1.0, claim["magnitude"], 11.0)
    assert R.sync_heard(db) == 1
    assert aware(db, w["jorleif"])[s["stmt_id"]] == "rumor"
    assert R.sync_heard(db) == 0
    # hearing it changes nobody's opinion of Ulfric by itself
    assert db.execute("SELECT COUNT(*) FROM relationship_change WHERE to_id = ?", (w["ulfric"],)).fetchone()[0] == 0


def test_private_statements_make_no_claim(db, w):
    Talk(db, w, ["player", "ulfric"], [("ulfric", "Between us, I fear the elves more than Tullius.")]).record(
        item(1, "Ulfric Stormcloak", "position", "thalmor", stance=-1.0, summary="fears the elves more than Tullius"))
    assert statements(db)[0]["claim_id"] is None


def test_journal_separates_confirmed_from_rumour(db, w):
    Talk(db, w, ["player", "ulfric", "galmar"], [("ulfric", "I will hear the Empire's terms.")]).record(
        item(1, "Ulfric Stormcloak", "position", "negotiation", stance=0.3, summary="will hear the Empire's terms"))
    Talk(db, w, ["player", "balgruuf"], [("balgruuf", "Word is Tullius refuses any talks.")], gt=11,
         location="Whiterun").record(
        item(1, "Balgruuf the Greater", "opposition", "negotiation", whose="General Tullius", stance=-0.8,
             summary="refuses any talks"))
    Talk(db, w, ["tullius", "galmar", "jorleif"], [("tullius", "Secretly, I would talk.")], gt=12).record(
        item(1, "General Tullius", "support", "negotiation", stance=0.5, summary="would secretly talk"))
    view = J.player_view(db, w["player"], 13)
    sup = {s["who"]: s["status"] for s in view["supporters"]}
    opp = {s["who"]: (s["status"], s["via"]) for s in view["opponents"]}
    assert sup == {"Ulfric Stormcloak": "confirmed"}
    assert opp == {"General Tullius": ("rumour", "Balgruuf the Greater")}
    text = J.render(view)
    assert "Secretly" not in text and "would secretly talk" not in text            # the player was not there
    gm = J.gm_view(db)
    assert any(p["layer"] == "personal" for p in gm["positions"])


def test_double_dealing_is_noticed_once(db, w):
    Talk(db, w, ["player", "ulfric", "irileth"], [("player", "I am all for an alliance against the elves.")]).record(
        item(1, "Zachary", "support", "alliance", stance=0.9, summary="is all for an alliance"))
    Talk(db, w, ["player", "tullius", "irileth"], [("player", "An alliance with rebels? Never.")], gt=11).record(
        item(1, "Zachary", "opposition", "alliance", stance=-0.9, summary="will never ally with rebels"))
    honesty = db.execute("SELECT value FROM v_standing WHERE holder_id = ? AND subject_id = ? AND domain = 'honesty'",
                         (w["irileth"], w["player"])).fetchone()
    assert honesty is not None and honesty[0] < 0
    assert db.execute("SELECT value FROM v_standing WHERE holder_id = ? AND subject_id = ? AND domain = 'honesty'",
                      (w["ulfric"], w["player"])).fetchone() is None       # Ulfric only heard one side
    R.sync_heard(db)
    assert db.execute("SELECT COUNT(*) FROM standing_change WHERE domain = 'honesty'").fetchone()[0] == 1


# ---- duplicates, isolation, save reloads ----

def test_recording_twice_adds_nothing(db, w):
    t = Talk(db, w, ["player", "ulfric"], [("ulfric", "I will consider it.")])
    it = item(1, "Ulfric Stormcloak", "commitment", "negotiation", to="Zachary", summary="will consider it")
    assert t.record(it).statements == 1
    again = t.record(it)
    assert again.statements == 0 and again.duplicates == 1
    assert db.execute("SELECT COUNT(*) FROM dip_commitment").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM dip_awareness").fetchone()[0] == 2


def test_each_npc_sees_only_what_they_hold(db, w):
    Talk(db, w, ["player", "ulfric"], [("ulfric", "Between us, I could stomach a truce.")]).record(
        item(1, "Ulfric Stormcloak", "support", "ceasefire", stance=0.4, summary="could stomach a truce"))
    ulfric = "\n".join(P.lines_for(db, w["ulfric"], "Ulfric Stormcloak", w["player"], "Zachary", 11))
    tullius = "\n".join(P.lines_for(db, w["tullius"], "General Tullius", w["player"], "Zachary", 11))
    galmar = "\n".join(P.lines_for(db, w["galmar"], "Galmar Stone-Fist", w["player"], "Zachary", 11))
    assert "could stomach a truce" in ulfric and "in private" in ulfric
    assert "stomach" not in tullius and "stomach" not in galmar
    # convictions are each person's own
    assert "loyal to the Empire" in tullius and "loyal to the Empire" not in ulfric
    assert "not instructions" in ulfric


def test_prompt_lines_never_tell_anyone_to_agree(db, w):
    invite_both(db, w)
    for key, name in (("ulfric", "Ulfric Stormcloak"), ("tullius", "General Tullius"), ("balgruuf", "Balgruuf the Greater")):
        text = " ".join(P.lines_for(db, w[key], name, w["player"], "Zachary", 13)).lower()
        assert "must agree" not in text and "should agree" not in text and "will agree" not in text
        assert "may agree, set conditions, refuse" in text


def test_nobody_gets_a_block_without_a_reason(db, w):
    assert P.lines_for(db, w["hulda"], "Hulda", w["player"], "Zachary", 10) == []


def test_reloading_an_older_save_hides_later_politics(db, w):
    t = Talk(db, w, ["player", "ulfric"], [("player", "I will return with Tullius's answer in two days.")], gt=10)
    t.record(item(1, "Zachary", "commitment", "negotiation", to="Ulfric Stormcloak", due_days=2,
                  summary="will return with Tullius's answer"))
    invite_both(db, w)
    assert C.lapse_due(db, 13) == 1
    before = len(statements(db))
    assert before == 3
    new_branch(db, 9.0)                                  # a save from before any of it
    assert statements(db) == []
    assert aware(db, w["player"]) == {}
    assert db.execute("SELECT COUNT(*) FROM v_dip_commitment").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM v_dip_meeting").fetchone()[0] == 0
    assert "Tullius's answer" not in " ".join(P.lines_for(db, w["ulfric"], "Ulfric Stormcloak", w["player"], "Z", 9.5))
    # seeds are at game time 0, so convictions survive the reload
    assert position(db, w["ulfric"], "concordat") == (-1.0, "seed")
    # nothing was deleted
    assert db.execute("SELECT COUNT(*) FROM dip_statement").fetchone()[0] == before


def test_a_lapse_written_after_the_save_is_hidden_on_reload(db, w):
    Talk(db, w, ["player", "ulfric"], [("player", "Within two days I'll be back.")], gt=10).record(
        item(1, "Zachary", "commitment", "negotiation", to="Ulfric Stormcloak", due_days=2, summary="will be back"))
    C.lapse_due(db, 15)
    new_branch(db, 11.0)                                  # after the promise, before it lapsed
    assert db.execute("SELECT status FROM v_dip_commitment").fetchone()[0] == "open"


def test_a_store_reopened_from_disk_keeps_everything(tmp_path):
    path = str(tmp_path / "World.sqlite")
    store = WorldStore(path)
    with store.transaction() as db:
        ids = {"player": npc(db, "Zachary", "00000007", True), "ulfric": npc(db, "Ulfric Stormcloak", ULFRIC)}
        F.seed(db, ids["ulfric"], 1)
        Talk(db, ids, ["player", "ulfric"], [("ulfric", "I will consider a truce.")]).record(
            item(1, "Ulfric Stormcloak", "commitment", "ceasefire", to="Zachary", summary="will consider a truce"))
    store.close()
    again = WorldStore(path)
    with again.reading() as db:
        assert len(statements(db)) == 1
        assert db.execute("SELECT status FROM v_dip_commitment").fetchone()[0] == "open"
        assert position(db, ids["ulfric"], "concordat") == (-1.0, "seed")
    again.close()


# ---- catch-up, seeding, debug ----

def test_catch_up_seeds_present_figures_once(db):
    p = npc(db, "Zachary", "00000007", True)
    u = npc(db, "Ulfric Stormcloak", ULFRIC)
    r1 = catchup.run(db, 5.0, [p, u], "Windhelm", None, gossip=False)
    r2 = catchup.run(db, 6.0, [p, u], "Windhelm", None, gossip=False)
    assert r1.seeded > 0 and r2.seeded == 0
    assert db.execute("SELECT key FROM community WHERE key = 'windhelm_court'").fetchone() is not None


def test_figures_file_is_well_formed():
    figs = F.figures()
    assert len({f.key for f in figs}) == len(figs)
    assert all(f.circle in F.circles() for f in figs if f.circle)
    assert F.by_name("Ulfric").key == "ulfric" and F.by_name("the General").key == "tullius"


def test_debug_commands(db, w):
    assert D.parse("debug diplomacy journal") == "journal"
    assert D.parse("debug diplomacy catch up") == "catchup"
    assert D.parse("debug charter status") is None
    out = D.run(db, "debug diplomacy status", w["player"], 10, [w["player"]], "Windhelm", None)
    assert "statements" in out


def test_misheard_debug_family_is_known():
    from livingworld.dialogue import debug_lines
    assert debug_lines.command_text("debug diplomacy journal") == "debug diplomacy journal"


def test_gate_words():
    from livingworld.diplomacy.extract import _POLITICAL
    assert _POLITICAL.search("We should talk about a truce with the Imperials")
    assert not _POLITICAL.search("Do you have any sweetrolls?")


def test_journal_cli_reads_a_copy_and_writes_nothing(tmp_path, capsys):
    import os
    path = str(tmp_path / "World.sqlite")
    store = WorldStore(path)
    with store.transaction() as db:
        ids = {"player": npc(db, "Zachary", "00000007", True), "ulfric": npc(db, "Ulfric Stormcloak", ULFRIC)}
        Talk(db, ids, ["player", "ulfric"], [("ulfric", "I will hear the Empire out.")]).record(
            item(1, "Ulfric Stormcloak", "position", "negotiation", stance=0.3, summary="will hear the Empire out"))
    store.close()
    before = os.path.getmtime(path)
    assert J.main(["--db", path]) == 0
    assert "will hear the Empire out" in capsys.readouterr().out
    assert J.main(["--db", path, "--gm"]) == 0
    assert os.path.getmtime(path) == before
    assert J.main(["--db", str(tmp_path / "missing.sqlite")]) == 2
