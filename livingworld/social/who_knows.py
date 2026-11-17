"""Who knows what: a read-only look at what each NPC knows, and how they came to know it.

Answers questions like "why did Hilde say that?" without reading SQLite by hand. It reads the same views the
prompts use (``v_known_facts``, ``v_claim_knowledge``, ``v_visible_memory``, ``v_belief``, ``v_name_known``),
so it shows what an NPC can actually be told about on the active save timeline, plus where each item came
from: seen with their own eyes, told by someone (and who told them), overheard, inferred, or planted.

It is a command line tool and is never imported by the server (a test enforces that)::

    python -m livingworld.social.who_knows --db World.sqlite summary
    python -m livingworld.social.who_knows --db World.sqlite npc "Hilde"
    python -m livingworld.social.who_knows --db World.sqlite about "Hod"
    python -m livingworld.social.who_knows --db World.sqlite claim 12
    python -m livingworld.social.who_knows --db World.sqlite search "flirt" [--npc Hilde]

Options: ``--timeline N`` looks at another save timeline instead of the active one, ``--json`` prints the
result as JSON, ``--limit N`` caps each list (default 40).

Safety: the database file is opened read-only (``mode=ro``) only long enough to copy it into memory with
SQLite's backup API, then closed. Every query runs on that private in-memory copy, so nothing is ever written
to the world (not even the active timeline when ``--timeline`` is used), and it is safe while Mantella runs.
It still works best on a copy of the database.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

DEFAULT_LIMIT = 40
ORIGIN_WORDS = {
    "self": "about themselves",
    "witnessed": "saw it",
    "told": "was told",
    "overheard": "overheard it",
    "read": "read it",
    "inferred": "worked it out",
    "rumor": "heard a rumour",
}


class Refused(Exception):
    """The tool cannot answer (no such database, person or claim). The message says why."""


# ---------------------------------------------------------------- opening (read only)

def snapshot(path: str, timeline: int | None = None) -> sqlite3.Connection:
    """A private in-memory copy of the world. The file itself is only ever opened read-only."""
    if not os.path.isfile(path):
        raise Refused(f"no such database: {path}")
    # With no -wal file next to it nothing has the world open (Mantella keeps one while it runs), so it is read
    # as immutable: SQLite then creates no -wal/-shm files beside a copy either.
    quiet = "" if os.path.exists(path + "-wal") else "&immutable=1"
    src = sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro{quiet}", uri=True, timeout=5.0)
    try:
        try:
            src.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()
        except sqlite3.DatabaseError as e:
            raise Refused(f"not a Living World database ({e})")
        mem = sqlite3.connect(":memory:")
        src.backup(mem)
    finally:
        src.close()
    mem.row_factory = sqlite3.Row
    if timeline is not None:
        _switch_timeline(mem, timeline)
    return mem


def _switch_timeline(db: sqlite3.Connection, timeline: int) -> None:
    """Make another timeline the active one, in the in-memory copy only (the scope triggers follow it)."""
    if not db.execute("SELECT 1 FROM timeline WHERE timeline_id = ?", (timeline,)).fetchone():
        known = [r[0] for r in db.execute("SELECT timeline_id FROM timeline ORDER BY timeline_id")]
        raise Refused(f"no timeline {timeline} (this world has {_ranges(known)})")
    db.execute("UPDATE timeline SET is_active = 0 WHERE is_active = 1")
    db.execute("UPDATE timeline SET is_active = 1 WHERE timeline_id = ?", (timeline,))
    db.commit()


def _ranges(ids: list[int]) -> str:
    if not ids:
        return "none"
    out, start, prev = [], ids[0], ids[0]
    for i in ids[1:] + [None]:
        if i is not None and i == prev + 1:
            prev = i
            continue
        out.append(str(start) if start == prev else f"{start}-{prev}")
        if i is not None:
            start = prev = i
    return ", ".join(out)


def has_table(db, name: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE name = ? AND type IN ('table','view')", (name,)).fetchone() is not None


def has_column(db, table: str, column: str) -> bool:
    return any(r[1] == column for r in db.execute(f"PRAGMA table_info({table})"))


# ---------------------------------------------------------------- people

def names(db) -> dict[int, str]:
    return {r[0]: r[1] for r in db.execute("SELECT npc_id, display_name FROM npc")}


def find_npc(db, text: str) -> dict:
    """One person by id ('#12' or '12'), 'player', exact name or unique part of a name (any case)."""
    t = text.strip()
    if t.lower() in ("player", "the player", "me"):
        row = db.execute("SELECT * FROM npc WHERE is_player = 1").fetchone()
        if row:
            return dict(row)
        raise Refused("this world has no player row yet")
    if t.lstrip("#").isdigit():
        row = db.execute("SELECT * FROM npc WHERE npc_id = ?", (int(t.lstrip("#")),)).fetchone()
        if row:
            return dict(row)
        raise Refused(f"no person with id {t}")
    rows = db.execute("SELECT * FROM npc WHERE display_name = ? COLLATE NOCASE ORDER BY npc_id", (t,)).fetchall()
    if not rows:
        rows = db.execute("SELECT * FROM npc WHERE display_name LIKE ? ORDER BY npc_id", (f"%{t}%",)).fetchall()
    if not rows:
        raise Refused(f"nobody called {t!r} in this world")
    if len(rows) > 1:
        listed = ", ".join(f"#{r['npc_id']} {r['display_name']}" for r in rows[:12])
        raise Refused(f"{t!r} matches {len(rows)} people: {listed}. Use the #id.")
    return dict(rows[0])


# ---------------------------------------------------------------- reading what someone knows

def claim_text(db, claim: dict, who: dict[int, str]) -> str:
    """The claim in plain words with everyone's real name (the admin's view, not any NPC's)."""
    from livingworld.social import acts
    subject = who.get(claim["subject_id"], f"#{claim['subject_id']}")
    place = f" in {claim['location']}" if claim.get("location") else ""
    detail = claim.get("detail") or ""
    if claim["act"] == "reported":
        return f"{subject} {detail.strip()}"
    if claim["act"] == "drank":
        return f"{subject} was drinking{detail}{place}"
    if claim.get("object_id") is not None:
        return f"{subject} {acts.act(claim['act']).verb} {who.get(claim['object_id'], '#' + str(claim['object_id']))}{detail}{place}"
    return f"{subject} {acts.NO_OBJECT.get(claim['act'], acts.act(claim['act']).verb)}{detail}{place}"


def _holder_view(db, claim: dict, holder: int) -> str | None:
    """The same claim the way the holder's own prompt words it (names they have not learned stay descriptions)."""
    try:
        from livingworld.social.claims import sentence
        return sentence(db, claim, holder)
    except Exception:   # an old world or a person row the fog code cannot place: the admin wording is enough
        return None


def _quote(db, utt_id: int | None, who: dict[int, str]) -> dict | None:
    if utt_id is None:
        return None
    row = db.execute("SELECT u.utt_id, u.speaker_id, u.text, u.game_time, c.conv_id, c.location FROM utterance u"
                     " JOIN conversation c ON c.conv_id = u.conv_id WHERE u.utt_id = ?", (utt_id,)).fetchone()
    if not row:
        return None
    return {"utt_id": row["utt_id"], "speaker": who.get(row["speaker_id"], f"#{row['speaker_id']}"),
            "text": row["text"], "game_time": row["game_time"], "conv_id": row["conv_id"], "location": row["location"]}


def _event(db, event_id: int | None) -> dict | None:
    if event_id is None:
        return None
    row = db.execute("SELECT event_id, type, location, source, game_time FROM world_event WHERE event_id = ?",
                     (event_id,)).fetchone()
    return dict(row) if row else None


def _how_learned(origin: str, teller: str | None) -> str:
    if origin in ("told", "overheard", "read", "rumor") and teller:
        return {"told": f"told by {teller}", "overheard": f"overheard from {teller}", "read": f"read, written by {teller}",
                "rumor": f"rumour passed on by {teller}"}[origin]
    return ORIGIN_WORDS.get(origin, origin)


def facts(db, npc_id: int, who: dict[int, str], limit: int) -> list[dict]:
    """Plain facts (not claims) in this person's head, exactly as v_known_facts gives them to the prompt."""
    rows = db.execute("SELECT f.*, k.source_utt_id, k.source_event_id, k.parent_know_id FROM v_known_facts f"
                      " JOIN knowledge k ON k.know_id = f.know_id"
                      " WHERE f.holder_id = ? AND f.predicate NOT LIKE 'claim:%'"
                      " ORDER BY f.learned_gt DESC, f.know_id DESC LIMIT ?", (npc_id, limit)).fetchall()
    out = []
    for r in rows:
        out.append({"know_id": r["know_id"], "prompt_line": r["prompt_line"], "subject": r["subject"], "topic": r["topic"],
                    "predicate": r["predicate"], "value": r["value"], "origin": r["origin"], "told_by": r["told_by"],
                    "how": _how_learned(r["origin"], r["told_by"]), "confidence": r["confidence"],
                    "learned_gt": r["learned_gt"], "source": _quote(db, r["source_utt_id"], who),
                    "event": _event(db, r["source_event_id"])})
    return out


def _depth(db, know_id: int) -> int:
    depth, seen, cur = 0, set(), know_id
    while True:
        row = db.execute("SELECT parent_know_id FROM knowledge WHERE know_id = ?", (cur,)).fetchone()
        if not row or row[0] is None or row[0] in seen:
            return depth
        seen.add(row[0])
        cur, depth = row[0], depth + 1


def _truth(db, claim: dict) -> str:
    if claim.get("truth_event_id") is None:
        return "said in conversation (nobody saw it happen)" if claim["act"] == "reported" else "no event behind it"
    ev = _event(db, claim["truth_event_id"])
    return f"happened (event {claim['truth_event_id']}, {ev['type']})" if ev else f"event {claim['truth_event_id']} (missing)"


def claims_held(db, npc_id: int, who: dict[int, str], limit: int) -> list[dict]:
    """Claims (things that happened or were said to) this person holds, and how each reached them."""
    if not has_table(db, "v_claim_knowledge"):
        return []
    rows = db.execute("SELECT * FROM v_claim_knowledge WHERE holder_id = ? ORDER BY learned_gt DESC, know_id DESC LIMIT ?",
                      (npc_id, limit)).fetchall()
    out = []
    for r in rows:
        c = dict(r)
        teller = who.get(c["told_by_id"]) if c["told_by_id"] is not None else None
        out.append({"know_id": c["know_id"], "claim_id": c["claim_id"], "text": claim_text(db, c, who),
                    "as_they_say_it": _holder_view(db, c, npc_id), "origin": c["origin"],
                    "how": _how_learned(c["origin"], teller), "told_by": teller,
                    "hops_from_source": _depth(db, c["know_id"]), "believed": c["believed"],
                    "heard_magnitude": c["heard_magnitude"], "magnitude": c["magnitude"],
                    "distorted": c["heard_magnitude"] is not None and abs(c["heard_magnitude"] - c["magnitude"]) > 0.05,
                    "will_share": c["will_share"], "sensitivity": c["sensitivity"], "truth": _truth(db, c),
                    "claim_gt": c["claim_gt"], "learned_gt": c["learned_gt"], "source": _quote(db, c["source_utt_id"], who)})
    return out


def memories(db, npc_id: int, who: dict[int, str], limit: int) -> list[dict]:
    rows = db.execute("SELECT * FROM v_visible_memory WHERE owner_id = ? ORDER BY occurred_gt DESC, mem_id DESC LIMIT ?",
                      (npc_id, limit)).fetchall()
    has_source_kind = has_column(db, "memory", "source_kind")
    out = []
    for r in rows:
        m = dict(r)
        sources = []
        for s in db.execute("SELECT * FROM memory_source WHERE mem_id = ?", (m["mem_id"],)):
            if s["utt_id"] is not None:
                q = _quote(db, s["utt_id"], who)
                if q:
                    sources.append({"kind": "conversation", **q})
            elif s["event_id"] is not None:
                sources.append({"kind": "event", **(_event(db, s["event_id"]) or {"event_id": s["event_id"]})})
            elif s["know_id"] is not None:
                sources.append({"kind": "knowledge", "know_id": s["know_id"]})
            elif s["child_mem_id"] is not None:
                sources.append({"kind": "memory", "mem_id": s["child_mem_id"]})
        if not sources and m.get("event_id") is not None:
            sources.append({"kind": "event", **(_event(db, m["event_id"]) or {"event_id": m["event_id"]})})
        about = [who.get(i, f"#{i}") for i in json.loads(m.get("about_ids_json") or "[]") if isinstance(i, int)]
        how = (m.get("source_kind") if has_source_kind else None) or {
            "extraction": "from a conversation", "admin": "planted by hand", "seed": "seeded",
            "consolidation": "summed up from older memories", "perception": "perceived"}.get(m["created_by"], m["created_by"])
        out.append({"mem_id": m["mem_id"], "content": m["content"], "how": how, "created_by": m["created_by"],
                    "interpretation": m.get("interpretation"), "confidence": m.get("confidence"),
                    "importance": m["importance"], "about": about, "location": m["location"],
                    "occurred_gt": m["occurred_gt"], "recalled": m["rehearsal_count"], "sources": sources})
    return out


def opinions(db, npc_id: int, who: dict[int, str], limit: int) -> list[dict]:
    if not has_table(db, "v_belief"):
        return []
    rows = db.execute("SELECT * FROM v_belief WHERE holder_id = ? ORDER BY formed_gt DESC LIMIT ?", (npc_id, limit)).fetchall()
    out = []
    for r in rows:
        evidence = []
        for e in json.loads(r["evidence_json"] or "[]"):
            if "claim" in e:
                c = db.execute("SELECT * FROM claim WHERE claim_id = ?", (e["claim"],)).fetchone()
                evidence.append(f"claim {e['claim']}: {claim_text(db, dict(c), who)}" if c else f"claim {e['claim']}")
            elif "mem" in e:
                m = db.execute("SELECT content FROM memory WHERE mem_id = ?", (e["mem"],)).fetchone()
                evidence.append(f"memory {e['mem']}: {m[0]}" if m else f"memory {e['mem']}")
            else:
                evidence.append(json.dumps(e))
        out.append({"belief_id": r["belief_id"], "about": who.get(r["about_id"]) if r["about_id"] is not None else None,
                    "topic": r["topic"], "statement": r["statement"], "stance": r["stance"],
                    "confidence": r["confidence"], "formed_gt": r["formed_gt"], "evidence": evidence})
    return out


def names_known(db, npc_id: int, who: dict[int, str], limit: int) -> list[dict]:
    if not has_table(db, "v_name_known"):
        return []
    rows = db.execute("SELECT v.*, k.told_by_id FROM v_name_known v LEFT JOIN name_known k ON k.knower_id = v.knower_id"
                      " AND k.subject_id = v.subject_id AND k.game_time = v.game_time AND k.name = v.name"
                      " WHERE v.knower_id = ? GROUP BY v.subject_id ORDER BY v.game_time DESC LIMIT ?",
                      (npc_id, limit)).fetchall()
    return [{"subject": who.get(r["subject_id"], f"#{r['subject_id']}"), "calls_them": r["name"], "how": r["how"],
             "told_by": who.get(r["told_by_id"]) if r["told_by_id"] is not None else None, "game_time": r["game_time"]}
            for r in rows]


def report_npc(db, text: str, limit: int = DEFAULT_LIMIT) -> dict:
    who = names(db)
    p = find_npc(db, text)
    return {"npc": {"id": p["npc_id"], "name": p["display_name"], "is_player": bool(p["is_player"])},
            "timeline": active_timeline(db), "facts": facts(db, p["npc_id"], who, limit),
            "claims": claims_held(db, p["npc_id"], who, limit), "memories": memories(db, p["npc_id"], who, limit),
            "opinions": opinions(db, p["npc_id"], who, limit), "names": names_known(db, p["npc_id"], who, limit)}


# ---------------------------------------------------------------- who knows about someone

def report_about(db, text: str, limit: int = DEFAULT_LIMIT) -> dict:
    """Everyone who knows something about this person, grouped by who knows it."""
    who = names(db)
    p = find_npc(db, text)
    pid = p["npc_id"]
    holders: dict[int, dict] = {}

    def entry(h: int) -> dict:
        return holders.setdefault(h, {"holder": who.get(h, f"#{h}"), "holder_id": h, "facts": [], "claims": [],
                                      "memories": [], "opinions": [], "name": None})

    for r in db.execute("SELECT * FROM v_known_facts WHERE subject = ? AND predicate NOT LIKE 'claim:%' AND holder_id <> ?"
                        " AND know_id IN (SELECT know_id FROM knowledge WHERE subject_id = ?) ORDER BY learned_gt",
                        (p["display_name"], pid, pid)):
        entry(r["holder_id"])["facts"].append({"line": r["prompt_line"], "how": _how_learned(r["origin"], r["told_by"]),
                                               "learned_gt": r["learned_gt"]})
    if has_table(db, "v_claim_knowledge"):
        for r in db.execute("SELECT * FROM v_claim_knowledge WHERE (subject_id = ? OR object_id = ?) AND holder_id <> ?"
                            " ORDER BY learned_gt", (pid, pid, pid)):
            c = dict(r)
            teller = who.get(c["told_by_id"]) if c["told_by_id"] is not None else None
            entry(c["holder_id"])["claims"].append({"claim_id": c["claim_id"], "text": claim_text(db, c, who),
                                                    "how": _how_learned(c["origin"], teller), "believed": c["believed"],
                                                    "learned_gt": c["learned_gt"]})
    for r in db.execute("SELECT m.mem_id, m.owner_id, m.content, m.occurred_gt FROM v_visible_memory m, json_each(m.about_ids_json) j"
                        " WHERE j.value = ? AND m.owner_id <> ? ORDER BY m.occurred_gt", (pid, pid)):
        entry(r["owner_id"])["memories"].append({"mem_id": r["mem_id"], "content": r["content"], "occurred_gt": r["occurred_gt"]})
    if has_table(db, "v_belief"):
        for r in db.execute("SELECT * FROM v_belief WHERE about_id = ? AND holder_id <> ?", (pid, pid)):
            entry(r["holder_id"])["opinions"].append({"statement": r["statement"], "stance": r["stance"], "topic": r["topic"]})
    if has_table(db, "v_name_known"):
        for r in db.execute("SELECT * FROM v_name_known WHERE subject_id = ?", (pid,)):
            entry(r["knower_id"])["name"] = {"calls_them": r["name"], "how": r["how"]}
    rows = sorted(holders.values(), key=lambda h: -(len(h["facts"]) + len(h["claims"]) + len(h["memories"]) + len(h["opinions"])))
    for h in rows:
        for k in ("facts", "claims", "memories", "opinions"):
            h[k] = h[k][-limit:]
    return {"about": {"id": pid, "name": p["display_name"]}, "timeline": active_timeline(db), "holders": rows}


# ---------------------------------------------------------------- one claim and how it spread

def report_claim(db, claim_id: int) -> dict:
    who = names(db)
    c = db.execute("SELECT * FROM claim WHERE claim_id = ?", (claim_id,)).fetchone()
    if not c:
        raise Refused(f"no claim {claim_id}")
    c = dict(c)
    visible = {r["know_id"]: dict(r) for r in db.execute("SELECT * FROM v_claim_knowledge WHERE claim_id = ?", (claim_id,))}
    hidden = db.execute("SELECT COUNT(*) FROM knowledge WHERE claim_id = ? AND superseded_by IS NULL", (claim_id,)).fetchone()[0] - len(visible)
    children: dict[int | None, list[dict]] = {}
    for k in visible.values():
        parent = k["parent_know_id"] if k["parent_know_id"] in visible else None
        children.setdefault(parent, []).append(k)

    def node(k: dict, depth: int) -> dict:
        teller = who.get(k["told_by_id"]) if k["told_by_id"] is not None else None
        kids = sorted(children.get(k["know_id"], []), key=lambda x: (x["learned_gt"], x["know_id"]))
        return {"know_id": k["know_id"], "holder": who.get(k["holder_id"], f"#{k['holder_id']}"), "origin": k["origin"],
                "how": _how_learned(k["origin"], teller), "depth": depth, "learned_gt": k["learned_gt"],
                "believed": k["believed"], "heard_magnitude": k["heard_magnitude"], "will_share": k["will_share"],
                "as_they_say_it": _holder_view(db, {**c, **k}, k["holder_id"]),
                "told": [node(x, depth + 1) for x in kids]}

    roots = sorted(children.get(None, []), key=lambda x: (x["learned_gt"], x["know_id"]))
    return {"claim": {"claim_id": claim_id, "text": claim_text(db, c, who), "act": c["act"], "valence": c["valence"],
                      "magnitude": c["magnitude"], "sensitivity": c["sensitivity"], "game_time": c["game_time"],
                      "truth": _truth(db, c), "timeline_id": c["timeline_id"]},
            "timeline": active_timeline(db), "knowers": len(visible), "on_other_timelines": hidden,
            "spread": [node(r, 0) for r in roots]}


# ---------------------------------------------------------------- search

def report_search(db, text: str, npc: str | None = None, limit: int = DEFAULT_LIMIT) -> dict:
    """Who knows anything matching the words: facts, claims, memories, opinions, and lines said in front of them."""
    who = names(db)
    holder = find_npc(db, npc)["npc_id"] if npc else None
    like = f"%{text}%"
    only = " AND {col} = ?" if holder is not None else ""
    extra = (holder,) if holder is not None else ()
    out = {"search": text, "npc": who.get(holder) if holder is not None else None, "timeline": active_timeline(db),
           "facts": [], "claims": [], "memories": [], "opinions": [], "said_in_front_of": []}
    for r in db.execute("SELECT * FROM v_known_facts WHERE predicate NOT LIKE 'claim:%' AND (prompt_line LIKE ? OR value LIKE ?)"
                        + only.format(col="holder_id") + " ORDER BY learned_gt DESC LIMIT ?", (like, like, *extra, limit)):
        out["facts"].append({"holder": r["holder"], "line": r["prompt_line"], "how": _how_learned(r["origin"], r["told_by"]),
                             "learned_gt": r["learned_gt"]})
    if has_table(db, "v_claim_knowledge"):
        matched = {}
        for r in db.execute("SELECT * FROM claim"):
            t = claim_text(db, dict(r), who)
            if text.lower() in t.lower():
                matched[r["claim_id"]] = t
        for cid, t in list(matched.items())[:limit]:
            for k in db.execute("SELECT * FROM v_claim_knowledge WHERE claim_id = ?" + only.format(col="holder_id")
                                + " ORDER BY learned_gt", (cid, *extra)):
                teller = who.get(k["told_by_id"]) if k["told_by_id"] is not None else None
                out["claims"].append({"holder": who.get(k["holder_id"]), "claim_id": cid, "text": t,
                                      "how": _how_learned(k["origin"], teller), "believed": k["believed"],
                                      "learned_gt": k["learned_gt"]})
    for r in db.execute("SELECT * FROM v_visible_memory WHERE content LIKE ?" + only.format(col="owner_id")
                        + " ORDER BY occurred_gt DESC LIMIT ?", (like, *extra, limit)):
        out["memories"].append({"holder": who.get(r["owner_id"]), "mem_id": r["mem_id"], "content": r["content"],
                                "created_by": r["created_by"], "occurred_gt": r["occurred_gt"]})
    if has_table(db, "v_belief"):
        for r in db.execute("SELECT * FROM v_belief WHERE statement LIKE ?" + only.format(col="holder_id") + " LIMIT ?",
                            (like, *extra, limit)):
            out["opinions"].append({"holder": who.get(r["holder_id"]), "statement": r["statement"], "stance": r["stance"]})
    # Lines spoken while someone was in the conversation: they may know it without any record saying so.
    utts = db.execute("SELECT u.* FROM utterance u JOIN v_timeline_scope s ON s.timeline_id = u.timeline_id"
                      " AND u.game_time <= s.cutoff_gt WHERE u.text LIKE ? ORDER BY u.game_time DESC LIMIT ?",
                      (like, limit)).fetchall()
    for u in utts:
        present = [r[0] for r in db.execute("SELECT npc_id FROM participant WHERE conv_id = ? AND joined_seq <= ?"
                                            " AND (left_seq IS NULL OR left_seq >= ?)", (u["conv_id"], u["seq"], u["seq"]))]
        if holder is not None and holder not in present:
            continue
        out["said_in_front_of"].append({"utt_id": u["utt_id"], "speaker": who.get(u["speaker_id"]), "text": u["text"],
                                        "game_time": u["game_time"],
                                        "present": [who.get(i, f"#{i}") for i in present if i != u["speaker_id"]]})
    return out


# ---------------------------------------------------------------- summary

def active_timeline(db) -> dict | None:
    row = db.execute("SELECT timeline_id, parent_id, branch_game_time, save_name FROM timeline WHERE is_active = 1").fetchone()
    return dict(row) if row else None


def report_summary(db, limit: int = 10) -> dict:
    who = names(db)
    version = db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
    counts = {
        "people": db.execute("SELECT COUNT(*) FROM npc").fetchone()[0],
        "facts": db.execute("SELECT COUNT(*) FROM v_known_facts WHERE predicate NOT LIKE 'claim:%'").fetchone()[0],
        "claim_copies": db.execute("SELECT COUNT(*) FROM v_claim_knowledge").fetchone()[0] if has_table(db, "v_claim_knowledge") else 0,
        "memories": db.execute("SELECT COUNT(*) FROM v_visible_memory").fetchone()[0],
        "opinions": db.execute("SELECT COUNT(*) FROM v_belief").fetchone()[0] if has_table(db, "v_belief") else 0,
        "timelines": db.execute("SELECT COUNT(*) FROM timeline").fetchone()[0],
    }
    knowers = db.execute(
        "SELECT holder_id, SUM(n) AS n FROM ("
        " SELECT holder_id, COUNT(*) AS n FROM v_known_facts WHERE predicate NOT LIKE 'claim:%' GROUP BY holder_id"
        " UNION ALL SELECT owner_id, COUNT(*) FROM v_visible_memory GROUP BY owner_id"
        + (" UNION ALL SELECT holder_id, COUNT(*) FROM v_claim_knowledge GROUP BY holder_id" if has_table(db, "v_claim_knowledge") else "")
        + ") GROUP BY holder_id ORDER BY n DESC LIMIT ?", (limit,)).fetchall()
    spread = []
    if has_table(db, "v_claim_knowledge"):
        for r in db.execute("SELECT claim_id, COUNT(*) AS n, SUM(origin = 'witnessed') AS saw FROM v_claim_knowledge"
                            " GROUP BY claim_id ORDER BY n DESC, claim_id DESC LIMIT ?", (limit,)):
            c = db.execute("SELECT * FROM claim WHERE claim_id = ?", (r["claim_id"],)).fetchone()
            spread.append({"claim_id": r["claim_id"], "text": claim_text(db, dict(c), who), "knowers": r["n"],
                           "saw_it": r["saw"], "heard_it": r["n"] - r["saw"]})
    return {"schema_version": version, "timeline": active_timeline(db), "counts": counts,
            "most_knowledge": [{"name": who.get(r[0], f"#{r[0]}"), "items": r[1]} for r in knowers],
            "most_spread_claims": spread}


# ---------------------------------------------------------------- text output

def _day(gt) -> str:
    return "?" if gt is None else f"day {gt:.2f}"


def _pct(x) -> str:
    return "?" if x is None else f"{round(x * 100)}%"


def _timeline_line(t: dict | None) -> str:
    if not t:
        return "Timeline: none active"
    fork = f", branched at {_day(t['branch_game_time'])} from {t['parent_id']}" if t.get("parent_id") else ""
    return f"Timeline {t['timeline_id']} ({t.get('save_name') or 'unnamed'}{fork})"


def _src(q: dict | None) -> str:
    if not q:
        return ""
    return f'\n        from: {q["speaker"]}: "{q["text"]}" ({q["location"]}, {_day(q["game_time"])})'


def render_npc(r: dict) -> str:
    n = r["npc"]
    out = [f"What {n['name']} (#{n['id']}) knows", _timeline_line(r["timeline"]), ""]
    out.append(f"Facts ({len(r['facts'])})")
    for f in r["facts"]:
        out.append(f"  - {f['prompt_line']}  [{f['how']}, {_day(f['learned_gt'])}]{_src(f['source'])}")
    out.append(f"\nThings that happened, or were said to ({len(r['claims'])})")
    for c in r["claims"]:
        bits = [c["how"]]
        if c["hops_from_source"]:
            bits.append(f"{c['hops_from_source']} hop(s) from the source")
        if c["origin"] != "witnessed":
            bits.append(f"believes {_pct(c['believed'])}")
        if c["distorted"]:
            bits.append(f"heard as {_pct(c['heard_magnitude'])} strong, really {_pct(c['magnitude'])}")
        if c["will_share"]:
            bits.append(f"shares: {c['will_share']}")
        out.append(f"  - [claim {c['claim_id']}] {c['text']}  ({'; '.join(bits)}; {_day(c['learned_gt'])})")
        out.append(f"        truth: {c['truth']}")
        if c["as_they_say_it"] and c["as_they_say_it"] != c["text"]:
            out.append(f"        in their prompt: {c['as_they_say_it']}")
        if c["source"]:
            out.append(_src(c["source"]).lstrip("\n"))
    out.append(f"\nMemories ({len(r['memories'])})")
    for m in r["memories"]:
        about = f", about {', '.join(m['about'])}" if m["about"] else ""
        out.append(f"  - {m['content']}  [{m['how']}{about}, {_day(m['occurred_gt'])}, importance {m['importance']:.2f}]")
        if m.get("interpretation"):
            out.append(f"        made of it: {m['interpretation']}")
        for s in m["sources"]:
            if s["kind"] == "conversation":
                out.append(f'        from: {s["speaker"]}: "{s["text"]}" ({s["location"]})')
            elif s["kind"] == "event":
                out.append(f"        from event {s.get('event_id')} ({s.get('type', '?')}, {s.get('location') or '?'})")
            else:
                out.append(f"        from {s['kind']} {s.get('know_id') or s.get('mem_id')}")
    out.append(f"\nOpinions ({len(r['opinions'])})")
    for o in r["opinions"]:
        out.append(f"  - {o['statement']}  [stance {o['stance']:+.2f}, sure {_pct(o['confidence'])}, {_day(o['formed_gt'])}]")
        for e in o["evidence"]:
            out.append(f"        because: {e}")
    out.append(f"\nNames they know ({len(r['names'])})")
    for nm in r["names"]:
        told = f" from {nm['told_by']}" if nm["told_by"] else ""
        alias = "" if nm["calls_them"] == nm["subject"] else f" as {nm['calls_them']!r}"
        out.append(f"  - {nm['subject']}{alias} ({nm['how']}{told}, {_day(nm['game_time'])})")
    return "\n".join(out)


def render_about(r: dict) -> str:
    out = [f"Who knows something about {r['about']['name']} (#{r['about']['id']})", _timeline_line(r["timeline"]), ""]
    if not r["holders"]:
        out.append("Nobody.")
    for h in r["holders"]:
        name = f" (calls them {h['name']['calls_them']!r}, learned: {h['name']['how']})" if h["name"] else ""
        out.append(f"{h['holder']} (#{h['holder_id']}){name}")
        for f in h["facts"]:
            out.append(f"  fact: {f['line']}  [{f['how']}]")
        for c in h["claims"]:
            belief = "" if c["how"] == "saw it" else f", believes {_pct(c['believed'])}"
            out.append(f"  claim {c['claim_id']}: {c['text']}  [{c['how']}{belief}]")
        for m in h["memories"]:
            out.append(f"  memory: {m['content']}")
        for o in h["opinions"]:
            out.append(f"  opinion: {o['statement']} ({o['stance']:+.2f})")
    return "\n".join(out)


def render_claim(r: dict) -> str:
    c = r["claim"]
    out = [f"Claim {c['claim_id']}: {c['text']}", f"Truth: {c['truth']}",
           f"Act {c['act']}, valence {c['valence']:+.2f}, strength {_pct(c['magnitude'])}, {c['sensitivity']}, {_day(c['game_time'])}",
           _timeline_line(r["timeline"]),
           f"{r['knowers']} people know it on this timeline" + (f" ({r['on_other_timelines']} more copies on other timelines)"
                                                                if r["on_other_timelines"] else ""), ""]

    def walk(n: dict) -> None:
        pad = "  " * (n["depth"] + 1)
        extra = "" if n["origin"] == "witnessed" else f", believes {_pct(n['believed'])}"
        heard = "" if n["heard_magnitude"] is None or abs(n["heard_magnitude"] - c["magnitude"]) <= 0.05 \
            else f", heard as {_pct(n['heard_magnitude'])} strong"
        out.append(f"{pad}{n['holder']}: {n['how']}{extra}{heard}, shares: {n['will_share'] or '?'} ({_day(n['learned_gt'])})")
        for k in n["told"]:
            walk(k)

    for n in r["spread"]:
        walk(n)
    return "\n".join(out)


def render_search(r: dict) -> str:
    scope = f" known by {r['npc']}" if r["npc"] else ""
    out = [f"Search {r['search']!r}{scope}", _timeline_line(r["timeline"]), ""]
    for f in r["facts"]:
        out.append(f"fact     {f['holder']}: {f['line']}  [{f['how']}]")
    for c in r["claims"]:
        out.append(f"claim {c['claim_id']:<3} {c['holder']}: {c['text']}  [{c['how']}]")
    for m in r["memories"]:
        out.append(f"memory   {m['holder']}: {m['content']}  [{m['created_by']}, {_day(m['occurred_gt'])}]")
    for o in r["opinions"]:
        out.append(f"opinion  {o['holder']}: {o['statement']}")
    for u in r["said_in_front_of"]:
        present = ", ".join(u["present"]) or "nobody else"
        out.append(f'said     {u["speaker"]}: "{u["text"]}"  (heard by {present}, {_day(u["game_time"])})')
    if len(out) == 3:
        out.append("Nothing found.")
    return "\n".join(out)


def render_summary(r: dict) -> str:
    c = r["counts"]
    out = [f"Schema version {r['schema_version']}", _timeline_line(r["timeline"]),
           f"{c['people']} people, {c['timelines']} timelines",
           f"Visible now: {c['facts']} facts, {c['claim_copies']} claim copies, {c['memories']} memories, {c['opinions']} opinions",
           "", "Who knows the most:"]
    out += [f"  {k['name']}: {k['items']}" for k in r["most_knowledge"]]
    out.append("\nMost widely known claims:")
    out += [f"  [claim {s['claim_id']}] {s['text']}: {s['knowers']} know it ({s['saw_it']} saw, {s['heard_it']} heard)"
            for s in r["most_spread_claims"]]
    return "\n".join(out)


# ---------------------------------------------------------------- command line

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m livingworld.social.who_knows", description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", required=True, help="the world database (data/livingworld/<World>.sqlite); opened read-only")
    ap.add_argument("--timeline", type=int, help="look at this save timeline instead of the active one")
    ap.add_argument("--json", action="store_true", help="print JSON instead of text")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"most rows per list (default {DEFAULT_LIMIT})")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("summary", help="counts, who knows the most, the most widely known claims")
    p = sub.add_parser("npc", help="everything one person knows and how they learned it")
    p.add_argument("name", help="name, part of a name, #id, or 'player'")
    a = sub.add_parser("about", help="everyone who knows something about this person")
    a.add_argument("name")
    c = sub.add_parser("claim", help="one claim, whether it is true, and how it spread from person to person")
    c.add_argument("claim_id", type=int)
    s = sub.add_parser("search", help="who knows anything matching these words")
    s.add_argument("text")
    s.add_argument("--npc", help="only what this person knows")
    args = ap.parse_args(argv)
    try:
        db = snapshot(args.db, args.timeline)
        try:
            if args.cmd == "summary":
                result, render = report_summary(db, min(args.limit, 20)), render_summary
            elif args.cmd == "npc":
                result, render = report_npc(db, args.name, args.limit), render_npc
            elif args.cmd == "about":
                result, render = report_about(db, args.name, args.limit), render_about
            elif args.cmd == "claim":
                result, render = report_claim(db, args.claim_id), render_claim
            else:
                result, render = report_search(db, args.text, args.npc, args.limit), render_search
        finally:
            db.close()
    except Refused as e:
        print(f"who_knows: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
