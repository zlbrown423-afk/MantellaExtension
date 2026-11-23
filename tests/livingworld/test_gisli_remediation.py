"""Gisli remediation (2026-11-23): corrected speech, relationship materialisation, earned status, encounter
diaries, timeline safety. Built on an in-memory world; the live database is never touched.

The case behind it: the player told Gisli "you've shown me everything tonight" and the recognition stored
"served"; after a finished intimate scene she had intimacy memories but no diary entry (she had 4 talks with
the player, the diary needs 5), and she was still a 'stranger' with familiarity 0 because nothing turned
conversations into familiarity.
"""
import json

import pytest

from livingworld import diary
from livingworld.diary import service as diaries
from livingworld.intimacy import scenes
from livingworld.memory.corrections import correct
from livingworld.memory.extraction import Extractor
from livingworld.memory.jobs import JobQueue
from livingworld.memory.recall import opinion_line
from livingworld.social import familiarity as F
from livingworld.social.relationship_dims import unknown_dimensions, unknown_keys
from livingworld.store.conversation_log import ConversationLog
from livingworld.store.timeline import resolve_load
from livingworld.store.world_store import WorldStore

SERVED = "Me too friend. I won't be upset if it doesn't because you've served me everything tonight."
SHOWN = "Me too friend. I won't be upset if it doesn't because you've shown me everything tonight."


@pytest.fixture(autouse=True)
def diary_on(monkeypatch):
    monkeypatch.delenv("MANTELLA_LW_DIARY", raising=False)
    monkeypatch.delenv("MANTELLA_LW_FAMILIARITY", raising=False)


def world():
    store = WorldStore(":memory:")
    with store.transaction() as db:
        db.execute("INSERT INTO npc (npc_id, plugin, base_form_id, display_name, sex, is_player, created_gt)"
                   " VALUES (1, 'Skyrim.esm', '0x00000007', 'Zachary Lex', 'male', 1, 1.0)")
        db.execute("INSERT INTO npc (npc_id, plugin, base_form_id, display_name, sex, created_gt)"
                   " VALUES (146, 'Skyrim.esm', '0x00013BA0', 'Gisli', 'female', 1.0)")
    return store


def talk(store, gt, player_lines, npc=146, npc_line="Mm."):
    log = ConversationLog(store)
    conv = log.open("Whiterun", "player", gt)
    log.add_participant(conv, 1)
    log.add_participant(conv, npc)
    utts = []
    for i, text in enumerate(player_lines):
        utts.append(log.append(conv, 1, text, None, gt + 0.001 * (2 * i + 1)))
        log.append(conv, npc, npc_line, None, gt + 0.001 * (2 * i + 2))
    log.close(conv, gt + 0.01)
    return conv, utts


def rel(store, npc=146):
    with store.reading() as db:
        return dict(db.execute("SELECT * FROM v_relationship WHERE from_id = ? AND to_id = 1", (npc,)).fetchone())


def scene_event(store, gt, ongoing=False, npc=146):
    with store.transaction() as db:
        payload = {"acts": ["kissing"], "names": ["Zachary Lex", "Gisli"]}
        if ongoing:
            payload["ongoing"] = True
        tl = db.execute("SELECT timeline_id FROM timeline WHERE is_active = 1").fetchone()[0]
        ev = db.execute("INSERT INTO world_event (timeline_id, type, location, actors_json, payload_json, source,"
                        " game_time) VALUES (?, 'romance.scene', 'Dragonsreach', ?, ?, 'game', ?)",
                        (tl, json.dumps([1, npc]), json.dumps(payload), gt)).lastrowid
        for who in (1, npc):
            db.execute("INSERT INTO event_witness (event_id, npc_id, how) VALUES (?,?, 'participated')", (ev, who))
    return ev


def jobs(store, kind=diary.JOB):
    with store.reading() as db:
        return [(r[0], json.loads(r[1])) for r in db.execute(
            "SELECT idempotency_key, payload_json FROM job WHERE kind = ? ORDER BY job_id", (kind,))]


# 1. corrected speech is what extraction reads; the raw transcript stays
def test_corrected_speech_feeds_extraction_and_the_raw_text_stays():
    store = world()
    conv, (utt, _) = talk(store, 47.5, [SERVED, "Let's meet again soon, I would like that."])
    with store.transaction() as db:
        c = correct(db, utt, SHOWN, note="player confirmed: 'shown', not 'served'")
    assert c.raw_text == SERVED and c.after == SHOWN
    lines = Extractor(store, lambda *a: "{}").lines(conv)
    assert SHOWN in [l.text for l in lines] and SERVED not in [l.text for l in lines]
    with store.reading() as db:
        raw = db.execute("SELECT text, conv_id, speaker_id, game_time FROM utterance WHERE utt_id = ?", (utt,)).fetchone()
        view = db.execute("SELECT text, raw_text, corrected FROM v_utterance WHERE utt_id = ?", (utt,)).fetchone()
    assert raw[0] == SERVED and (raw[1], raw[2]) == (conv, 1)        # untouched: same line, same speaker
    assert (view[0], view[1], view[2]) == (SHOWN, SERVED, 1)


def test_a_second_correction_supersedes_the_first_and_nonsense_is_refused():
    store = world()
    _, (utt, _) = talk(store, 47.5, [SERVED, "One more line to make it a talk."])
    with store.transaction() as db:
        correct(db, utt, "first try")
        correct(db, utt, SHOWN)
        assert db.execute("SELECT text FROM v_utterance WHERE utt_id = ?", (utt,)).fetchone()[0] == SHOWN
        assert db.execute("SELECT COUNT(*) FROM utterance_correction WHERE utt_id = ?", (utt,)).fetchone()[0] == 2
        with pytest.raises(ValueError):
            correct(db, utt, SHOWN)          # already reads that way
        with pytest.raises(ValueError):
            correct(db, 999999, SHOWN)       # no such line
        with pytest.raises(ValueError):
            correct(db, utt, "  ")


# 2. a change is counted exactly once
def test_a_talk_counts_once_however_often_it_is_processed():
    store = world()
    conv, _ = talk(store, 40.0, ["I hope our paths cross again soon.", "Tell me about your brother Brenuin."])
    with store.transaction() as db:
        first = F.after_talk(db, conv, 1)
        again = F.after_talk(db, conv, 1)
        rows = db.execute("SELECT COUNT(*) FROM relationship_change WHERE from_id = 146").fetchone()[0]
    assert first and not again and rows == 1


# 3. a dimension nothing reads is reported, not silently lost
def test_unknown_relationship_dimensions_are_detected():
    store = world()
    assert unknown_keys({"intimacy": 0.06, "affinity": 0.03}) == []     # intimacy is read by v_relationship
    with store.transaction() as db:
        db.execute("INSERT INTO relationship (from_id, to_id, status, updated_gt) VALUES (146, 1, 'stranger', 1)")
        db.execute("INSERT INTO relationship_change (from_id, to_id, deltas_json, reason, game_time)"
                   " VALUES (146, 1, '{\"intimacy\": 0.06, \"lust\": 0.5}', 'test', 2.0)")
        assert unknown_dimensions(db) == {"lust": 1}


# 4. getting to know someone is not liking them
def test_meaningful_talk_ends_stranger_status_without_granting_affection():
    store = world()
    with store.transaction() as db:
        db.execute("INSERT INTO relationship (from_id, to_id, status, updated_gt) VALUES (146, 1, 'stranger', 1)")
    before = rel(store)
    talk(store, 40.0, ["Hello.", "Hi.", "Hello."])                 # greetings only: nothing
    with store.transaction() as db:
        assert F.after_talk(db, 1, 1) == {}
        assert F.effective_status(db, 146, 1) == "stranger"
    for day in (41.0, 42.0):
        conv, _ = talk(store, day, ["Your father was a hard man to please.", "I hope your Jarl hears you out."])
        with store.transaction() as db:
            F.after_talk(db, conv, 1)
    after = rel(store)
    assert after["familiarity"] > 0.05
    for dim in ("affinity", "trust", "attraction", "intimacy", "commitment"):
        assert after[dim] == before[dim]
    with store.reading() as db:
        assert F.effective_status(db, 146, 1) == "acquaintance"   # not 'friend': she does not like or trust him more


def test_diminishing_returns():
    store = world()
    steps = []
    for day in range(30):
        conv, _ = talk(store, 10.0 + day, ["Tell me about the war again.", "What happened at Helgen then?"])
        with store.transaction() as db:
            steps.append(F.after_talk(db, conv, 1)[146])
    assert steps[0] == pytest.approx(F.TALK_STEP) and steps[-1] < steps[0] * 0.5
    assert all(b <= a for a, b in zip(steps, steps[1:]))


def test_a_stored_romantic_or_hostile_status_stands():
    store = world()
    with store.transaction() as db:
        db.execute("INSERT INTO relationship (from_id, to_id, status, updated_gt) VALUES (146, 1, 'rival', 1)")
    conv, _ = talk(store, 40.0, ["We should settle this like adults.", "I am not leaving without an answer."])
    with store.transaction() as db:
        F.after_talk(db, conv, 1)
        assert F.effective_status(db, 146, 1) == "rival"


# 5-8. a finished scene: shared history and a diary reflection, once
def test_a_finished_scene_queues_one_reflection_and_shared_history():
    store = world()
    ev = scene_event(store, 47.57)
    for _ in range(2):                          # the same end processed twice
        scenes._shared_history(store, ev, [1, 146])
        scenes._queue_reflections(store, ev, [1, 146], 47.57)
    queued = jobs(store)
    assert [k for k, _ in queued] == [f"diary:event:{ev}:146"]   # one, for Gisli only (not the player)
    payload = queued[0][1]
    assert payload["encounter"] is True and payload["event_id"] == ev and payload["day"] == 47
    with store.reading() as db:
        rows = db.execute("SELECT deltas_json, cause_event_id FROM relationship_change WHERE from_id = 146").fetchall()
    assert len(rows) == 1 and rows[0][1] == ev and set(json.loads(rows[0][0])) == {"familiarity"}


def test_the_reflection_is_written_with_few_talks_and_its_tone_is_hers():
    store = world()
    talk(store, 46.0, ["You keep the Jarl's records, then?", "I'd like to hear more about it."])
    ev = scene_event(store, 47.57)
    with store.transaction() as db:
        db.execute("INSERT INTO memory (timeline_id, owner_id, kind, content, occurred_gt, importance, created_by,"
                   " event_id) VALUES (1, 146, 'episodic', 'Zachary Lex and I were intimate in Dragonsreach.', 47.57,"
                   " 0.8, 'perception', ?)", (ev,))
    scenes._queue_reflections(store, ev, [1, 146], 47.57)
    (_, payload), = jobs(store)
    prompts = []
    text = diaries.write_entry(store, lambda s, u: prompts.append(u) or '{"entry": "I am not sure what to make of '
                                                                         'last night. He was kind, but it was one night."}',
                               payload)
    assert text and "not sure" in text
    assert "may be warm, matter-of-fact, awkward, uncertain, regretful or mixed" in prompts[0]
    assert "not proof of love" in prompts[0]
    with store.reading() as db:
        assert diaries.is_keeper(db, 146, 1)     # she keeps the diary from now on, though she had only one talk
        assert db.execute("SELECT COUNT(*) FROM v_diary_entry WHERE npc_id = 146").fetchone()[0] == 1


def test_an_unfinished_scene_queues_nothing():
    store = world()
    ev = scene_event(store, 47.57, ongoing=True)
    # record_scene only calls these for a finished romance.scene; an ongoing one is stored without them
    with store.reading() as db:
        assert json.loads(db.execute("SELECT payload_json FROM world_event WHERE event_id = ?", (ev,)).fetchone()[0])["ongoing"]
    assert jobs(store) == []
    src = open(scenes.__file__, encoding="utf-8").read()
    assert "if not ongoing and etype == EVENT_SCENE:" in src     # forced scenes and unfinished ones excluded


def test_refusal_and_withdrawal_store_no_completed_scene():
    """A declined invitation or withdrawn consent goes through consent records (touch/consent.py), never a
    romance.scene, so nothing here can write a 'we were intimate' memory or a reflection for it."""
    store = world()
    with store.reading() as db:
        assert db.execute("SELECT COUNT(*) FROM world_event WHERE type = 'romance.scene'").fetchone()[0] == 0
    assert jobs(store) == []


# 9. a sibling branch never leaks
def test_familiarity_and_reflections_from_a_branch_stay_on_it():
    store = world()
    conv, _ = talk(store, 40.0, ["Your father was a hard man to please.", "I hope your Jarl hears you out."])
    with store.transaction() as db:
        F.after_talk(db, conv, 1)
    canon = rel(store)["familiarity"]
    with store.transaction() as db:
        tl = db.execute("SELECT timeline_id FROM timeline WHERE is_active = 1").fetchone()[0]
    resolve_load(store, 1, tl, 40.5)              # the player saved at 40.5 and plays on
    conv, _ = talk(store, 41.0, ["Let's find somewhere private.", "I want to know you better."])
    ev = scene_event(store, 41.2)
    with store.transaction() as db:
        F.after_talk(db, conv, 1)
    scenes._shared_history(store, ev, [1, 146])
    branch = rel(store)["familiarity"]
    assert branch > canon
    resolve_load(store, 2, tl, 40.5)              # ...then reloads that save
    assert rel(store)["familiarity"] == pytest.approx(canon)
    with store.reading() as db:
        assert db.execute("SELECT COUNT(*) FROM relationship_change WHERE from_id = 146").fetchone()[0] == 3  # kept


# 10. the feelings line carries familiarity and shared intimacy without turning it into a promise
def test_feelings_line_names_familiarity_and_intimacy():
    store = world()
    with store.transaction() as db:
        db.execute("INSERT INTO relationship (from_id, to_id, status, updated_gt) VALUES (146, 1, 'stranger', 1)")
        db.execute("INSERT INTO relationship_change (from_id, to_id, deltas_json, reason, game_time)"
                   " VALUES (146, 1, '{\"intimacy\": 0.06, \"affinity\": 0.03, \"familiarity\": 0.18}', 'test', 47.5)")
    line = opinion_line(store, 146, 1)
    assert "I've got to know them a little" in line
    assert "we have been intimate (something we shared, not a promise)" in line
    assert "I like them" not in line
