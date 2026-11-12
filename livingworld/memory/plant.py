"""Planted memories: give one NPC things they know and feel, by hand, for one playthrough.

Used when the game did something Living World never saw (a quest finished before it was installed, a
rescue it could not follow) or when the user wants an NPC to know something. A small JSON file says who,
what they remember and how they feel about the player; this writes it into that world's database only,
so other playthroughs are untouched (a Mantella character override would change the NPC everywhere).

    python -m livingworld.memory.plant --world <data/livingworld/World.sqlite> plant.json
        [--conversations <data/Skyrim/conversations>] [--dry-run]

The file::

    {
      "npc": "Fralia Gray-Mane",            # display name, or the ref form id ("01A684")
      "at": "after_latest",                 # or a game day such as 12.8
      "memories": [
        {"content": "first person, as she holds it", "importance": 1.0, "valence": 0.9,
         "about": ["player", "Thorald Gray-Mane"]}
      ],
      "feelings": {"affinity": 0.8, "trust": 0.6, "attraction": 0.8},   # towards the player, the values to end on
      "standing": {"reliability": 0.6, "decency": 0.0},   # her opinion of the player by domain (social/reputation.py)
      "facts": [{"predicate": "son_status", "value": "safe"}],   # 'My son_status is safe'; replaces older ones
      "drop_facts": ["feels_uneasy"],       # her own facts that no longer hold
      "reason": "why (shown in relationship history)",
      "summary": "a paragraph added to her Mantella summary file"
    }

Timelines: rows go into the timeline of the current save's chain that holds the moment ``at`` (by default
just after her newest memory, conversation or feeling change), so every later save on this chain sees them
and loading a save from before that moment does not, as with anything else she lived through
(``store/timeline.py``). The summary paragraph is noted the same way (``memory/summary_timeline.py``).

Feelings and standings are targets: each is moved from what she feels now to the given value with one relationship change,
so running the file again changes nothing. A standing
that ends near 0 drops the opinion it had formed ("has no decency"); one past 0.2 forms a new one. Attraction is only a feeling: nothing here makes an NPC act on it;
the consent and touch rules still decide every time.

Close Mantella before running it (it writes to the world database and the summary file).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from dataclasses import dataclass, field

from livingworld.memory import summary_timeline
from livingworld.store.world_store import WorldStore

EPSILON_DAYS = 0.0007   # about one game minute
FEELINGS = {  # name: (lowest, highest), as v_relationship clamps them
    "trust": (-1.0, 1.0), "affinity": (-1.0, 1.0), "respect": (-1.0, 1.0), "admiration": (-1.0, 1.0),
    "fear": (0.0, 1.0), "suspicion": (0.0, 1.0), "attraction": (0.0, 1.0), "familiarity": (0.0, 1.0),
}


@dataclass
class Result:
    npc_id: int
    npc_name: str
    timeline_id: int
    at: float
    memories_added: list[str] = field(default_factory=list)
    memories_skipped: list[str] = field(default_factory=list)
    deltas: dict[str, float] = field(default_factory=dict)
    feelings_after: dict[str, float] = field(default_factory=dict)
    standing_deltas: dict[str, float] = field(default_factory=dict)
    beliefs: list[str] = field(default_factory=list)
    facts_added: list[str] = field(default_factory=list)
    facts_dropped: list[str] = field(default_factory=list)
    summary_file: str | None = None
    summary_added: bool = False

    def report(self) -> str:
        out = [f"{self.npc_name} (npc {self.npc_id}): timeline {self.timeline_id}, game day {self.at:.4f}"]
        out += [f"  + memory: {m}" for m in self.memories_added]
        out += [f"  = already there: {m}" for m in self.memories_skipped]
        if self.deltas:
            out.append("  feelings change: " + ", ".join(f"{k} {v:+.3f}" for k, v in self.deltas.items()))
        if self.feelings_after:
            out.append("  feelings now: " + ", ".join(f"{k} {v:.2f}" for k, v in self.feelings_after.items()))
        if self.standing_deltas:
            out.append("  opinion change: " + ", ".join(f"{k} {v:+.3f}" for k, v in self.standing_deltas.items()))
        out += [f"  opinion: {b}" for b in self.beliefs]
        out += [f"  + fact: {f}" for f in self.facts_added]
        out += [f"  - fact no longer held: {f}" for f in self.facts_dropped]
        if self.summary_file:
            out.append(f"  summary {'added to' if self.summary_added else 'already in'} {self.summary_file}")
        return "\n".join(out)


def find_npc(db, who: str) -> tuple[int, str, str]:
    """(npc_id, display name, ref form id) for a display name or a ref form id."""
    hexpart = re.sub(r"^(0x)?", "", who.strip().lower())
    if re.fullmatch(r"[0-9a-f]{1,8}", hexpart):
        for r in db.execute("SELECT npc_id, display_name, ref_form_id FROM npc WHERE is_player = 0").fetchall():
            if r[2] and int(r[2].split("|")[-1], 16) & 0xFFFFFF == int(hexpart, 16) & 0xFFFFFF:
                return r[0], r[1], r[2]
    rows = db.execute("SELECT npc_id, display_name, ref_form_id FROM npc WHERE is_player = 0 AND display_name = ?"
                      " COLLATE NOCASE ORDER BY npc_id", (who.strip(),)).fetchall()
    if len(rows) > 1:
        raise SystemExit(f"more than one NPC is called {who}; use the ref form id: "
                         + ", ".join(r[2] for r in rows))
    if not rows:
        raise SystemExit(f"no NPC called {who} in this world (they must have spoken with Mantella at least once)")
    return rows[0][0], rows[0][1], rows[0][2]


def player_id(db) -> int:
    row = db.execute("SELECT npc_id FROM npc WHERE is_player = 1 ORDER BY npc_id LIMIT 1").fetchone()
    if not row:
        raise SystemExit("no player in this world yet")
    return row[0]


def latest_moment(db, npc: int) -> float:
    """Her newest visible memory, conversation or feeling change (0 if she has none)."""
    return db.execute(
        "SELECT MAX(t) FROM ("
        " SELECT MAX(occurred_gt) t FROM v_visible_memory WHERE owner_id = :n"
        " UNION ALL SELECT MAX(COALESCE(c.ended_gt, c.started_gt)) FROM conversation c"
        "   JOIN participant p ON p.conv_id = c.conv_id AND p.npc_id = :n"
        "   JOIN v_timeline_scope s ON s.timeline_id = c.timeline_id AND c.started_gt <= s.cutoff_gt"
        " UNION ALL SELECT MAX(c.game_time) FROM relationship_change c"
        "   JOIN v_timeline_scope s ON s.timeline_id = c.timeline_id AND c.game_time <= s.cutoff_gt"
        "   WHERE c.from_id = :n)", {"n": npc}).fetchone()[0] or 0.0


def owning_timeline(db, at: float) -> int:
    """The timeline of the current save's chain that holds game day ``at``: the oldest one whose rows up to
    ``at`` the current save still sees. Rows put there are seen by every later save on the chain."""
    chain = {}
    t = db.execute("SELECT timeline_id FROM timeline WHERE is_active = 1").fetchone()[0]
    order = []
    while t is not None:
        order.append(t)
        t = db.execute("SELECT parent_id FROM timeline WHERE timeline_id = ?", (t,)).fetchone()[0]
    for tid, cutoff in db.execute("SELECT timeline_id, cutoff_gt FROM v_timeline_scope").fetchall():
        chain[tid] = min(cutoff, chain.get(tid, cutoff))
    for tid in reversed(order):   # oldest first
        if at <= chain[tid] + 1e-9:
            return tid
    return order[0]


def _raw_feelings(db, npc: int, player: int) -> dict[str, float]:
    """Base value plus every change the current save sees, before v_relationship clamps it, so the change that
    reaches a target lands on it even when a long run of grabs has pushed the sum past -1."""
    base = db.execute("SELECT * FROM relationship WHERE from_id = ? AND to_id = ?", (npc, player)).fetchone()
    out = {k: float(base[k] or 0.0) if base is not None and k in base.keys() else 0.0 for k in FEELINGS}
    for (deltas,) in db.execute(
            "SELECT c.deltas_json FROM relationship_change c"
            " JOIN v_timeline_scope s ON s.timeline_id = c.timeline_id AND c.game_time <= s.cutoff_gt"
            " WHERE c.from_id = ? AND c.to_id = ?", (npc, player)).fetchall():
        for k, v in json.loads(deltas or "{}").items():
            if k in out and isinstance(v, (int, float)):
                out[k] += v
    return out


def _feelings(db, npc: int, player: int) -> dict[str, float]:
    row = db.execute("SELECT * FROM v_relationship WHERE from_id = ? AND to_id = ?", (npc, player)).fetchone()
    if not row:
        return {k: 0.0 for k in FEELINGS}
    return {k: float(row[k] or 0.0) for k in FEELINGS}


def _about_ids(db, names: list[str], player: int, npc: int) -> list[int]:
    ids = []
    for name in names or []:
        if name.lower() == "player":
            ids.append(player)
            continue
        row = db.execute("SELECT npc_id FROM npc WHERE display_name = ? COLLATE NOCASE ORDER BY npc_id LIMIT 1",
                         (name,)).fetchone()
        if row and row[0] != npc:
            ids.append(row[0])
    return sorted(set(ids))


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(x)))


def summary_path(conversations: str, world: str, npc_name: str, ref_form_id: str) -> tuple[str, str] | None:
    """(newest summary file, folder relative to the conversations folder) for this NPC in this world."""
    suffix = ref_form_id.split("|")[-1][-6:].upper() if ref_form_id else ""
    folders = sorted(glob.glob(os.path.join(glob.escape(conversations), glob.escape(world),
                                            glob.escape(npc_name) + " - *")))
    folders = [f for f in folders if f.upper().endswith(suffix)] or folders
    if not folders:
        return None
    folder = folders[0]
    files = glob.glob(os.path.join(glob.escape(folder), glob.escape(npc_name) + "_summary_*.txt"))
    numbered = sorted(((int(m.group(1)), f) for f in files
                       if (m := re.search(r"_summary_(\d+)\.txt$", f))), reverse=True)
    path = numbered[0][1] if numbered else os.path.join(folder, f"{npc_name}_summary_1.txt")
    return path, f"{world}/{os.path.basename(folder)}"


def plant(store: WorldStore, spec: dict, conversations: str | None = None, world: str | None = None,
          dry_run: bool = False) -> Result:
    with store.transaction() as db:
        npc, name, ref = find_npc(db, str(spec["npc"]))
        player = player_id(db)
        at_spec = spec.get("at", "after_latest")
        at = latest_moment(db, npc) + EPSILON_DAYS if at_spec in (None, "after_latest") else float(at_spec)
        timeline = owning_timeline(db, at)
        result = Result(npc, name, timeline, at)

        for m in spec.get("memories", []):
            content = " ".join(str(m["content"]).split())
            if db.execute("SELECT 1 FROM v_visible_memory WHERE owner_id = ? AND content = ?",
                          (npc, content)).fetchone():
                result.memories_skipped.append(content)
                continue
            db.execute(
                "INSERT INTO memory (timeline_id, owner_id, kind, content, about_ids_json, location, occurred_gt,"
                " importance, valence, intensity, created_by, source_kind, interpretation, confidence)"
                " VALUES (?,?,?,?,?,?,?,?,?,?, 'admin', ?, ?, 1.0)",
                (timeline, npc, m.get("kind", "episodic"), content,
                 json.dumps(_about_ids(db, m.get("about", []), player, npc)), m.get("location"), at,
                 _clamp(m.get("importance", 0.9), 0, 1), _clamp(m.get("valence", 0.0), -1, 1),
                 _clamp(m.get("intensity", 0.5), 0, 1), m.get("source_kind"), m.get("interpretation")))
            result.memories_added.append(content)

        targets = spec.get("feelings") or {}
        unknown = set(targets) - set(FEELINGS)
        if unknown:
            raise SystemExit(f"unknown feeling(s) {', '.join(sorted(unknown))}; known: {', '.join(FEELINGS)}")
        if targets:
            if not db.execute("SELECT 1 FROM relationship WHERE from_id = ? AND to_id = ?", (npc, player)).fetchone():
                db.execute("INSERT INTO relationship (from_id, to_id, status, updated_gt) VALUES (?,?, 'acquaintance', ?)",
                           (npc, player, at))
            now = _raw_feelings(db, npc, player)
            deltas = {}
            for key, value in targets.items():
                lo, hi = FEELINGS[key]
                d = round(_clamp(value, lo, hi) - now[key], 4)
                if abs(d) >= 0.001:
                    deltas[key] = d
            if deltas:
                db.execute("INSERT INTO relationship_change (timeline_id, from_id, to_id, deltas_json, reason, game_time)"
                           " VALUES (?,?,?,?,?,?)",
                           (timeline, npc, player, json.dumps(deltas),
                            spec.get("reason") or "set by hand (livingworld.memory.plant)", at))
            result.deltas = deltas
            after = _feelings(db, npc, player)
            result.feelings_after = {k: round(after[k], 3) for k in targets}

        standing = spec.get("standing") or {}
        if standing:
            from livingworld.social import reputation
            unknown = set(standing) - set(reputation.STATEMENTS)
            if unknown:
                raise SystemExit(f"unknown standing domain(s) {', '.join(sorted(unknown))}; "
                                 f"known: {', '.join(reputation.STATEMENTS)}")
            for domain, value in standing.items():
                raw = db.execute("SELECT COALESCE(SUM(c.delta), 0) FROM standing_change c JOIN v_timeline_scope s"
                                 " ON s.timeline_id = c.timeline_id AND c.game_time <= s.cutoff_gt"
                                 " WHERE c.holder_id = ? AND c.subject_id = ? AND c.domain = ?",
                                 (npc, player, domain)).fetchone()[0]
                d = round(_clamp(value, -1, 1) - raw, 4)
                if abs(d) < reputation.MIN_DELTA:
                    continue
                result.standing_deltas[domain] = d
                left = d
                while abs(left) >= reputation.MIN_DELTA:   # a row holds at most +-1
                    step = _clamp(left, -1, 1)
                    db.execute("INSERT INTO standing_change (timeline_id, holder_id, subject_id, domain, delta,"
                               " firsthand, reason, game_time) VALUES (?,?,?,?,?,1,?,?)",
                               (timeline, npc, player, domain, round(step, 4),
                                spec.get("reason") or "set by hand (livingworld.memory.plant)", at))
                    left = round(left - step, 4)
            reputation.review(db, timeline, npc, player, at)
            result.beliefs = [r[0] for r in db.execute(
                "SELECT statement FROM v_belief WHERE holder_id = ? AND about_id = ? AND statement <> ''",
                (npc, player)).fetchall()]

        new_facts = spec.get("facts") or []
        drop = {str(p) for p in spec.get("drop_facts") or []} | {str(f["predicate"]) for f in new_facts}
        old = db.execute("SELECT know_id, predicate, value FROM v_known_facts WHERE holder_id = ? AND origin = 'self'",
                         (npc,)).fetchall()
        replaced_by: dict[str, int] = {}
        for f in new_facts:
            predicate, value = str(f["predicate"]), str(f["value"])
            same = [k for k, p, v in old if p == predicate and v == value]
            if same:
                replaced_by[predicate] = same[0]
                continue
            replaced_by[predicate] = db.execute(
                "INSERT INTO knowledge (timeline_id, holder_id, subject_id, predicate, value, origin, confidence,"
                " privacy, importance, learned_gt) VALUES (?,?,?,?,?, 'self', 1.0, ?, ?, ?)",
                (timeline, npc, npc, predicate, value, f.get("privacy", "shareable"),
                 _clamp(f.get("importance", 0.7), 0, 1), at)).lastrowid
            result.facts_added.append(f"{predicate} = {value}")
        for know_id, predicate, value in old:
            if predicate in drop and replaced_by.get(predicate) != know_id:
                result.facts_dropped.append(f"{predicate} = {value}")
                db.execute("UPDATE knowledge SET superseded_by = ? WHERE know_id = ?",
                           (replaced_by.get(predicate, know_id), know_id))

        paragraph = " ".join(str(spec.get("summary") or "").split())
        if paragraph and conversations and world:
            found = summary_path(conversations, world, name, ref)
            if found:
                path, folder = found
                result.summary_file = path
                existing = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
                if paragraph not in existing:
                    result.summary_added = True
                    if not dry_run:
                        os.makedirs(os.path.dirname(path), exist_ok=True)
                        with open(path, "a", encoding="utf-8") as f:
                            f.write(("\n" if existing and not existing.endswith("\n") else "")
                                    + ("\n" if existing.strip() else "") + paragraph + "\n")
                    summary_timeline.note_lines(db, timeline, folder, paragraph, at)

        if dry_run:
            raise _DryRun(result)
    return result


class _DryRun(Exception):
    def __init__(self, result: Result) -> None:
        super().__init__("dry run")
        self.result = result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m livingworld.memory.plant", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--world", required=True, help="the world database (data/livingworld/<World>.sqlite)")
    ap.add_argument("--conversations", help="Mantella's conversations folder (data/Skyrim/conversations), "
                                            "to add the summary paragraph")
    ap.add_argument("--dry-run", action="store_true", help="show what would change and change nothing")
    ap.add_argument("file", help="the plant file (JSON)")
    a = ap.parse_args(argv)
    with open(a.file, encoding="utf-8") as f:
        spec = json.load(f)
    world = os.path.splitext(os.path.basename(a.world))[0]
    store = WorldStore(a.world)
    try:
        try:
            result = plant(store, spec, a.conversations, world, a.dry_run)
            print(result.report())
        except _DryRun as dry:
            print("DRY RUN, nothing changed:\n" + dry.result.report())
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
