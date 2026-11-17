"""The read-only "who knows what" tool: shows what each person knows and how they learned it, follows a claim
from the witness through each retelling, respects save timelines, and never writes to the world file."""
import ast
import hashlib
import json
import os
from pathlib import Path

import pytest

from livingworld.social import who_knows as wk
from livingworld.store.world_store import WorldStore


def _npc(db, name, player=0):
    return db.execute("INSERT INTO npc (plugin, base_form_id, display_name, race, sex, is_player, created_gt)"
                      " VALUES ('Skyrim.esm', ?, ?, 'Nord', 'female', ?, 1.0)",
                      (f"0x{abs(hash(name)) % 0xFFFFFF:06X}", name, player)).lastrowid


def make_world(tmp_path) -> tuple[Path, dict]:
    """Riverwood in miniature: Hilde saw Hod flirt with Camilla, told Sigrid, who told Gerdur (and made it
    sound worse). Gerdur also heard the player say his name, and Sigrid learned a fact about Hod in talk."""
    path = tmp_path / "World.sqlite"
    store = WorldStore(str(path))
    with store.transaction() as db:
        ids = {n: _npc(db, n) for n in ("Hilde", "Hod", "Camilla Valerius", "Sigrid", "Gerdur")}
        ids["player"] = _npc(db, "Torvar", player=1)
        conv = db.execute("INSERT INTO conversation (location, kind, started_gt) VALUES ('Riverwood', 'player', 2.0)").lastrowid
        db.execute("INSERT INTO participant (conv_id, npc_id) VALUES (?, ?)", (conv, ids["player"]))
        db.execute("INSERT INTO participant (conv_id, npc_id) VALUES (?, ?)", (conv, ids["Gerdur"]))
        utt = db.execute("INSERT INTO utterance (conv_id, seq, speaker_id, text, game_time) VALUES (?, 0, ?, ?, 2.0)",
                         (conv, ids["player"], "I'm Torvar, I hunt wolves for coin.")).lastrowid
        ev = db.execute("INSERT INTO world_event (type, location, actors_json, source, game_time)"
                        " VALUES ('romance.flirt', 'Riverwood', ?, 'sim', 3.0)",
                        (json.dumps([ids["Hod"], ids["Camilla Valerius"]]),)).lastrowid
        db.execute("INSERT INTO event_witness (event_id, npc_id, how) VALUES (?, ?, 'saw')", (ev, ids["Hilde"]))
        claim = db.execute("INSERT INTO claim (subject_id, act, object_id, detail, valence, magnitude, location, game_time,"
                           " truth_event_id) VALUES (?, 'flirted', ?, '', -0.2, 0.4, 'Riverwood', 3.0, ?)",
                           (ids["Hod"], ids["Camilla Valerius"], ev)).lastrowid
        k_hilde = db.execute("INSERT INTO knowledge (holder_id, subject_id, predicate, value, origin, source_event_id, claim_id,"
                             " heard_magnitude, believed, will_share, learned_gt) VALUES (?, ?, ?, '', 'witnessed', ?, ?, 0.4, 1.0,"
                             " 'yes', 3.0)", (ids["Hilde"], ids["Hod"], f"claim:{claim}", ev, claim)).lastrowid
        k_sigrid = db.execute("INSERT INTO knowledge (holder_id, subject_id, predicate, value, origin, told_by_id, parent_know_id,"
                              " claim_id, heard_magnitude, believed, will_share, learned_gt) VALUES (?, ?, ?, '', 'told', ?, ?, ?,"
                              " 0.4, 0.8, 'yes', 4.0)",
                              (ids["Sigrid"], ids["Hod"], f"claim:{claim}", ids["Hilde"], k_hilde, claim)).lastrowid
        db.execute("INSERT INTO knowledge (holder_id, subject_id, predicate, value, origin, told_by_id, parent_know_id,"
                   " claim_id, heard_magnitude, believed, will_share, learned_gt) VALUES (?, ?, ?, '', 'told', ?, ?, ?,"
                   " 0.7, 0.55, 'trusted_only', 5.0)",
                   (ids["Gerdur"], ids["Hod"], f"claim:{claim}", ids["Sigrid"], k_sigrid, claim))
        db.execute("INSERT INTO knowledge (holder_id, subject_id, predicate, value, origin, told_by_id, source_utt_id, learned_gt)"
                   " VALUES (?, ?, 'job', 'wolf hunter', 'told', ?, ?, 2.0)", (ids["Gerdur"], ids["player"], ids["player"], utt))
        mem = db.execute("INSERT INTO memory (owner_id, kind, content, about_ids_json, location, occurred_gt, importance,"
                         " created_by) VALUES (?, 'episodic', 'Torvar told me he hunts wolves for coin.', ?, 'Riverwood', 2.0,"
                         " 0.5, 'extraction')", (ids["Gerdur"], json.dumps([ids["player"]]))).lastrowid
        db.execute("INSERT INTO memory_source (mem_id, utt_id) VALUES (?, ?)", (mem, utt))
        db.execute("INSERT INTO belief (holder_id, about_id, topic, statement, stance, confidence, evidence_json, formed_gt)"
                   " VALUES (?, ?, 'decency', 'Hod has a wandering eye', -0.3, 0.6, ?, 5.0)",
                   (ids["Gerdur"], ids["Hod"], json.dumps([{"claim": claim}])))
        db.execute("INSERT INTO name_known (knower_id, subject_id, name, how, game_time) VALUES (?, ?, 'Torvar', 'introduced', 2.0)",
                   (ids["Gerdur"], ids["player"]))
    store.close()
    return path, {**ids, "claim": claim}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)


def test_npc_report_shows_what_they_know_and_how(world):
    path, ids = world
    db = wk.snapshot(str(path))
    r = wk.report_npc(db, "gerdur")
    assert r["npc"]["name"] == "Gerdur"
    fact = r["facts"][0]
    assert fact["value"] == "wolf hunter" and fact["how"] == "told by Torvar"
    assert fact["source"]["text"].startswith("I'm Torvar")
    claim = r["claims"][0]
    assert claim["text"] == "Hod flirted with Camilla Valerius in Riverwood"
    assert claim["how"] == "told by Sigrid" and claim["hops_from_source"] == 2
    assert claim["distorted"] and claim["will_share"] == "trusted_only"
    assert claim["truth"].startswith("happened")
    assert r["memories"][0]["sources"][0]["kind"] == "conversation"
    assert r["opinions"][0]["statement"] == "Hod has a wandering eye"
    assert "flirted" in r["opinions"][0]["evidence"][0]
    assert r["names"][0] == {"subject": "Torvar", "calls_them": "Torvar", "how": "introduced", "told_by": None, "game_time": 2.0}
    text = wk.render_npc(r)
    assert "told by Sigrid" in text and "2 hop(s) from the source" in text and "heard as 70% strong, really 40%" in text


def test_claim_report_follows_the_retelling_chain(world):
    path, ids = world
    r = wk.report_claim(wk.snapshot(str(path)), ids["claim"])
    assert r["knowers"] == 3
    hilde = r["spread"][0]
    assert hilde["holder"] == "Hilde" and hilde["how"] == "saw it"
    sigrid = hilde["told"][0]
    assert sigrid["holder"] == "Sigrid" and sigrid["depth"] == 1
    assert sigrid["told"][0]["holder"] == "Gerdur" and sigrid["told"][0]["depth"] == 2
    text = wk.render_claim(r)
    assert text.index("Hilde") < text.index("Sigrid") < text.index("Gerdur")


def test_about_and_search(world):
    path, ids = world
    db = wk.snapshot(str(path))
    about = wk.report_about(db, "Hod")
    holders = {h["holder"] for h in about["holders"]}
    assert holders == {"Hilde", "Sigrid", "Gerdur"}
    gerdur = next(h for h in about["holders"] if h["holder"] == "Gerdur")
    assert gerdur["opinions"][0]["statement"] == "Hod has a wandering eye"

    player = wk.report_about(db, "player")
    gerdur = player["holders"][0]
    assert gerdur["holder"] == "Gerdur" and gerdur["name"]["calls_them"] == "Torvar" and gerdur["memories"]

    found = wk.report_search(db, "wolves")
    assert found["memories"][0]["holder"] == "Gerdur"
    assert found["said_in_front_of"][0]["present"] == ["Gerdur"]
    assert wk.report_search(db, "flirted", npc="Sigrid")["claims"][0]["how"] == "told by Hilde"
    assert wk.report_search(db, "wolves", npc="Hilde")["said_in_front_of"] == []


def test_summary_counts_and_most_spread(world):
    path, ids = world
    r = wk.report_summary(wk.snapshot(str(path)))
    assert r["counts"]["claim_copies"] == 3 and r["counts"]["people"] == 6
    assert r["most_spread_claims"][0] == {"claim_id": ids["claim"], "text": "Hod flirted with Camilla Valerius in Riverwood",
                                         "knowers": 3, "saw_it": 1, "heard_it": 2}
    assert "Schema version" in wk.render_summary(r)


def test_older_timeline_hides_what_was_learned_after_it(world):
    """Load-an-older-save view: on a timeline branched before Sigrid passed it on, only Hilde knows."""
    path, ids = world
    store = WorldStore(str(path))
    with store.transaction() as db:
        db.execute("UPDATE timeline SET is_active = 0")
        db.execute("INSERT INTO timeline (parent_id, branch_game_time, save_name, is_active) VALUES (1, 3.5, 'older', 1)")
        db.execute("UPDATE timeline SET is_active = 0")
        db.execute("UPDATE timeline SET is_active = 1 WHERE timeline_id = 1")
    store.close()
    before = digest(path)
    branch = wk.report_claim(wk.snapshot(str(path), timeline=2), ids["claim"])
    assert branch["knowers"] == 1 and branch["on_other_timelines"] == 2
    assert branch["timeline"]["save_name"] == "older"
    assert wk.report_claim(wk.snapshot(str(path)), ids["claim"])["knowers"] == 3   # the file's active timeline is untouched
    assert digest(path) == before
    with pytest.raises(wk.Refused):
        wk.snapshot(str(path), timeline=99)


def test_never_writes_to_the_world(world, capsys):
    path, ids = world
    before = digest(path)
    stat = os.stat(path)
    for args in (["summary"], ["npc", "Gerdur"], ["about", "Hod"], ["claim", str(ids["claim"])], ["search", "wolves"],
                 ["--json", "npc", "player"], ["--timeline", "1", "summary"]):
        assert wk.main(["--db", str(path), *args]) == 0
    assert digest(path) == before and os.stat(path).st_mtime_ns == stat.st_mtime_ns
    assert not Path(str(path) + "-wal").exists() and not Path(str(path) + "-shm").exists()
    assert "Hod flirted with Camilla Valerius" in capsys.readouterr().out


def test_safe_while_the_server_has_the_world_open(world):
    """Mantella keeps the world open in WAL mode while it runs; the tool still sees committed rows and writes nothing."""
    path, ids = world
    store = WorldStore(str(path))
    with store.transaction() as db:
        db.execute("INSERT INTO knowledge (holder_id, subject_id, predicate, value, origin, learned_gt)"
                   " VALUES (?, ?, 'mood', 'tired', 'self', 6.0)", (ids["Hilde"], ids["Hilde"]))
    try:
        before = digest(path)
        r = wk.report_npc(wk.snapshot(str(path)), "Hilde")
        assert any(f["value"] == "tired" for f in r["facts"])
        assert digest(path) == before
        with store.transaction() as db:   # the server can still write afterwards: no lock was left behind
            db.execute("UPDATE knowledge SET value = 'rested' WHERE predicate = 'mood'")
    finally:
        store.close()


def test_json_output_and_refusals(world, capsys):
    path, ids = world
    assert wk.main(["--db", str(path), "--json", "claim", str(ids["claim"])]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["spread"][0]["holder"] == "Hilde"
    assert wk.main(["--db", str(path), "npc", "Nobody"]) == 2
    assert "nobody called" in capsys.readouterr().err
    assert wk.main(["--db", str(path), "claim", "999"]) == 2
    assert wk.main(["--db", str(path / "missing.sqlite"), "summary"]) == 2
    db = wk.snapshot(str(path))
    _npc(db, "Hilde the Younger")
    with pytest.raises(wk.Refused, match="matches 2 people"):
        wk.find_npc(db, "hild")
    assert wk.find_npc(db, "Hilde")["display_name"] == "Hilde"   # an exact name wins over a partial one
    assert wk.find_npc(db, f"#{ids['Hod']}")["display_name"] == "Hod"


def test_the_server_never_imports_the_tool():
    root = Path(wk.__file__).parents[1]
    offenders = []
    for f in root.rglob("*.py"):
        if f.name == "who_knows.py" or "tests" in f.parts:
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, (ast.Import, ast.ImportFrom)) else []
            mod = getattr(node, "module", None) or ""
            if any("who_knows" in n for n in names) or "who_knows" in mod:
                offenders.append(str(f))
    assert offenders == []
