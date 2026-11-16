"""Growth benchmark for NPC context: how the cost of building an NPC's prompt context grows with a world's history.

This answers one question before any NPC context cache is built: is rebuilding context from SQLite slow
enough, now or after months of play, to be worth caching? It changes no game behaviour and is never imported
by the server.

What it does:

* ``synthetic`` (the default): builds throw-away worlds in a temporary folder, one per scale. Scale 1 matches
  the densities of a real 12-game-day save (busiest NPC: about 67 memories, 36 known facts, 22 relationship
  changes towards the player; about 10 conversations a day; a save reload every few hours of game time).
  Scale 10 is about four months of play, scale 50 about a year and a half, and so on. For each world it times
  ``recall_for`` (and each part of it) for three NPCs talking with the player, and counts the SQL statements.
* ``--db PATH``: does the same on a COPY of a real world database. The original file is never opened; the copy
  goes to a temporary folder and is deleted afterwards (opening it applies any pending migrations, to the copy).
* ``--full-prompt``: also times ``LivingWorld.recall_text`` (the whole per-NPC context block that goes into the
  prompt, every subsystem included) in a headless group conversation, the way group talk rebuilds it for each
  speaker on each turn.

Usage::

    python -m livingworld.perf.context_bench                       # scales 1, 10, 50, 200
    python -m livingworld.perf.context_bench --scales 1,10 --reps 30
    python -m livingworld.perf.context_bench --db "C:\\...\\ZacharyLex1.sqlite"
    python -m livingworld.perf.context_bench --full-prompt --out bench.json

Nothing is sent to an AI service and no audio is played.
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import random
import shutil
import statistics
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterator

from livingworld.store.world_store import WorldStore

DAYS_PER_SCALE = 12.0   # the real save the densities come from covered about 12 game days

# Per game day, measured on a 12-day real save (schema 17, 2026-11-12) and rounded.
DENSITY = {
    "conversations": 10.0,       # player conversations
    "lines_per_conversation": 28,
    "memories_busy": 5.6,        # per day, for each of the NPCs in the measured group (the busiest NPC: 67 in 12 days)
    "memories_other": 1.0,       # per day, for every other NPC
    "facts_busy": 3.0,           # knowledge rows per day, per group NPC (self + told)
    "claims_heard_busy": 1.4,    # gossip heard (told + claim) per day, per group NPC
    "claims_seen_busy": 1.0,     # things seen (witnessed + claim) per day, per group NPC
    "rel_changes_to_player": 1.8,   # per day, per group NPC (the busiest pair: 22 in 12 days)
    "rel_changes_other": 4.0,    # per day, across the rest of the world
    "standing_changes": 9.0,     # per day, across the world
    "beliefs": 2.4,              # per day, across the world
    "timelines": 3.5,            # save loads that start a timeline, per day
    "chain": 2.0,                # of those, how many stay on the active timeline's ancestry, per day
}
OTHER_NPCS = 37   # the real save had about 40 NPCs in conversations


@dataclass
class Timing:
    median_ms: float
    p95_ms: float
    statements: int


@dataclass
class WorldResult:
    label: str
    scale: float
    rows: dict[str, int]
    recall: dict[str, Timing] = field(default_factory=dict)        # by NPC name
    sections_ms: dict[str, float] = field(default_factory=dict)    # median per part, summed over the NPCs
    full_prompt: Timing | None = None                              # recall_text for the whole group
    full_prompt_parts_ms: dict[str, float] = field(default_factory=dict)   # median per block, whole group
    cold_recall_ms: float | None = None                            # first recall after opening the world


# ---- statement counting ---------------------------------------------------------------------------------------

@contextmanager
def counting(store: WorldStore) -> Iterator[list[int]]:
    """Count SQL statements run on the store's connection (SQLite trace callback)."""
    box = [0]

    def on(_sql: str) -> None:
        box[0] += 1
    store._conn.set_trace_callback(on)
    try:
        yield box
    finally:
        store._conn.set_trace_callback(None)


def timed(fn: Callable[[], Any], reps: int, store: WorldStore) -> Timing:
    times = []
    statements = 0
    for _ in range(reps):
        with counting(store) as box:
            start = time.perf_counter()
            fn()
            times.append((time.perf_counter() - start) * 1000)
        statements = box[0]
    times.sort()
    return Timing(round(statistics.median(times), 3), round(times[min(len(times) - 1, int(len(times) * 0.95))], 3),
                  statements)


@contextmanager
def section_timer() -> Iterator[dict[str, list[float]]]:
    """Time each part of recall_for by wrapping the functions it calls (restored afterwards)."""
    from livingworld.memory import recall as R
    from livingworld.social import reputation, romance
    found: dict[str, list[float]] = {}
    patched: list[tuple[Any, str, Any]] = []

    def wrap(module: Any, attr: str, label: str) -> None:
        original = getattr(module, attr)

        def inner(*a, **k):
            start = time.perf_counter()
            try:
                return original(*a, **k)
            finally:
                found.setdefault(label, []).append((time.perf_counter() - start) * 1000)
        setattr(module, attr, inner)
        patched.append((module, attr, original))

    wrap(R, "_pick_memories", "memories (pick)")
    wrap(R, "_recap", "recap of last talk")
    wrap(R, "_seen", "village life seen")
    wrap(R, "_heard", "gossip heard")
    wrap(R, "opinion_line", "opinion of player (v_relationship)")
    wrap(romance, "recall_lines", "romance")
    wrap(reputation, "belief_lines", "beliefs + reputation")
    try:
        yield found
    finally:
        for module, attr, original in patched:
            setattr(module, attr, original)


# ---- synthetic history ------------------------------------------------------------------------------------------

def _npc(db, name: str, form: int, now: float, player: bool = False) -> int:
    return db.execute(
        "INSERT INTO npc (plugin, base_form_id, ref_form_id, display_name, race, sex, is_player, created_gt)"
        " VALUES ('Skyrim.esm', ?, ?, ?, 'Nord', ?, ?, ?)",
        (f"{form:08X}", f"{form + 0x100000:08X}", name, "female" if form % 2 else "male", int(player), now)).lastrowid


def grow(store: WorldStore, group: list[int], player_id: int, scale: float, start_gt: float = 1.0,
         seed: int = 7, others: list[int] | None = None) -> float:
    """Add ``scale`` x 12 game days of history around ``group`` (the NPCs who will talk with the player).

    Rows go in the way the real code writes them (same tables, same columns, same views read them back), on a
    chain of save-load timelines like a player who reloads now and then. Returns the game time at the end."""
    rnd = random.Random(seed)
    days = DAYS_PER_SCALE * scale
    end_gt = start_gt + days
    d = DENSITY
    with store.transaction() as db:
        if others is None:
            others = [_npc(db, f"Villager {i}", 0x7A0000 + i, start_gt) for i in range(OTHER_NPCS)]
        everyone = group + others
        # save loads: most branch off the active line (the chain), the rest are dead ends left behind
        active = db.execute("SELECT timeline_id FROM timeline WHERE is_active = 1").fetchone()[0]
        n_chain = int(d["chain"] * days)
        n_dead = int((d["timelines"] - d["chain"]) * days)
        chain_times = sorted(rnd.uniform(start_gt, end_gt - 0.01) for _ in range(n_chain))
        for gt in chain_times:
            if rnd.random() < n_dead / max(1, n_chain):
                db.execute("INSERT INTO timeline (parent_id, branch_game_time, is_active) VALUES (?,?,0)",
                           (active, gt + 0.001))
            db.execute("UPDATE timeline SET is_active = 0 WHERE timeline_id = ?", (active,))
            active = db.execute("INSERT INTO timeline (parent_id, branch_game_time, is_active) VALUES (?,?,1)",
                                (active, gt)).lastrowid

        # the active line, oldest first: rows made at game time gt belong to the timeline active then
        chain_ids = [r[0] for r in db.execute(
            "WITH RECURSIVE a(id) AS (SELECT ? UNION ALL SELECT t.parent_id FROM timeline t JOIN a ON t.timeline_id = a.id"
            " WHERE t.parent_id IS NOT NULL) SELECT id FROM a", (active,))][::-1][-(n_chain + 1):]

        def tl_for(gt: float) -> int:
            return chain_ids[bisect.bisect_right(chain_times, gt)]

        # relationship base rows (the code inserts these before any change)
        for npc in everyone:
            db.execute("INSERT OR IGNORE INTO relationship (from_id, to_id, updated_gt) VALUES (?,?,?)",
                       (npc, player_id, start_gt))
        pairs = [(a, b) for a in everyone for b in everyone if a != b and rnd.random() < 0.08]
        for a, b in pairs:
            db.execute("INSERT OR IGNORE INTO relationship (from_id, to_id, updated_gt) VALUES (?,?,?)", (a, b, start_gt))

        # conversations with the player (recap, last spoke, and the utterances recap reads)
        for _ in range(int(d["conversations"] * days)):
            gt = rnd.uniform(start_gt, end_gt - 0.02)
            npcs = [rnd.choice(group if rnd.random() < 0.5 else everyone)]
            if rnd.random() < 0.15:
                npcs.append(rnd.choice(everyone))
            conv = db.execute("INSERT INTO conversation (timeline_id, location, kind, started_gt, ended_gt)"
                              " VALUES (?, 'Riverwood', 'player', ?, ?)", (tl_for(gt), gt, gt + 0.01)).lastrowid
            for npc in {player_id, *npcs}:
                db.execute("INSERT OR IGNORE INTO participant (conv_id, npc_id) VALUES (?,?)", (conv, npc))
            speakers = [player_id] + npcs
            db.executemany(
                "INSERT INTO utterance (timeline_id, conv_id, seq, speaker_id, text, game_time) VALUES (?,?,?,?,?,?)",
                [(tl_for(gt), conv, seq, speakers[seq % len(speakers)],
                  f"Line {seq} of a conversation about the weather, the war and the price of bread.", gt + seq * 1e-4)
                 for seq in range(1, d["lines_per_conversation"] + 1)])

        # memories: what was said (extraction) and what was seen or done (perception)
        def memories(owner: int, per_day: float) -> None:
            rows = []
            for _ in range(int(per_day * days)):
                gt = rnd.uniform(start_gt, end_gt)
                perceived = rnd.random() < 0.25
                rows.append((tl_for(gt), owner, "episodic",
                             (f"Someone grabbed me by the shoulder for {rnd.randint(2, 60)} seconds" if perceived
                              else f"The traveller told me about {rnd.choice(['Helgen', 'the dragon', 'Whiterun', 'my brother', 'the mill'])} ({rnd.randint(1, 10**6)})"),
                             gt, round(rnd.uniform(0.2, 0.9), 2), "perception" if perceived else "extraction",
                             rnd.choice(["experienced", "witnessed"]) if perceived else None))
            db.executemany("INSERT INTO memory (timeline_id, owner_id, kind, content, occurred_gt, importance, created_by,"
                           " source_kind) VALUES (?,?,?,?,?,?,?,?)", rows)
        for npc in group:
            memories(npc, d["memories_busy"])
        for npc in others:
            memories(npc, d["memories_other"])

        # world events + claims, known as gossip (told) or first hand (witnessed)
        def claim(gt: float) -> int:
            event = db.execute("INSERT INTO world_event (timeline_id, type, source, game_time) VALUES (?, 'romance.flirt', 'sim', ?)",
                               (tl_for(gt), gt)).lastrowid
            subj, obj = rnd.sample(everyone, 2)
            return db.execute("INSERT INTO claim (timeline_id, subject_id, act, object_id, detail, magnitude, location,"
                              " game_time, truth_event_id) VALUES (?, ?, 'reported', ?, 'helped at the mill', 0.4,"
                              " 'Riverwood', ?, ?)", (tl_for(gt), subj, obj, gt, event)).lastrowid
        for npc in group:
            for _ in range(int(d["claims_heard_busy"] * days)):
                gt = rnd.uniform(start_gt, end_gt)
                teller = rnd.choice([n for n in everyone if n != npc])
                db.execute("INSERT INTO knowledge (timeline_id, holder_id, subject_id, predicate, value, origin, told_by_id,"
                           " learned_gt, claim_id, believed) VALUES (?,?,?,?,?,'told',?,?,?,0.7)",
                           (tl_for(gt), npc, teller, "claim:reported", "x", teller, gt, claim(gt), ))
            for _ in range(int(d["claims_seen_busy"] * days)):
                gt = rnd.uniform(start_gt, end_gt)
                db.execute("INSERT INTO knowledge (timeline_id, holder_id, subject_id, predicate, value, origin,"
                           " learned_gt, claim_id) VALUES (?,?,?,?,?,'witnessed',?,?)",
                           (tl_for(gt), npc, npc, "claim:reported", "x", gt, claim(gt)))
            for i in range(int(d["facts_busy"] * days)):
                gt = rnd.uniform(start_gt, end_gt)
                if rnd.random() < 0.4:
                    db.execute("INSERT INTO knowledge (timeline_id, holder_id, subject_id, predicate, value, origin,"
                               " learned_gt) VALUES (?,?,?,?,?,'self',?)", (tl_for(gt), npc, npc, f"detail_{i}", "yes", gt))
                else:
                    about = rnd.choice([n for n in everyone if n != npc])
                    db.execute("INSERT INTO knowledge (timeline_id, holder_id, subject_id, predicate, value, origin,"
                               " told_by_id, learned_gt) VALUES (?,?,?,?,?,'told',?,?)",
                               (tl_for(gt), npc, about, f"detail_{i}", "yes", about, gt))

        # relationship history: the view re-adds all of it on every read
        rows = []
        for npc in group:
            for _ in range(int(d["rel_changes_to_player"] * days)):
                gt = rnd.uniform(start_gt, end_gt)
                rows.append((tl_for(gt), npc, player_id, json.dumps({"trust": 0.03, "affinity": 0.02}), "talk", gt))
        for _ in range(int(d["rel_changes_other"] * days)):
            gt = rnd.uniform(start_gt, end_gt)
            a, b = rnd.choice(pairs) if pairs else (others[0], player_id)
            rows.append((tl_for(gt), a, b, json.dumps({"affinity": 0.01}), "village", gt))
        db.executemany("INSERT INTO relationship_change (timeline_id, from_id, to_id, deltas_json, reason, game_time)"
                       " VALUES (?,?,?,?,?,?)", rows)

        # reputation: standings and beliefs
        db.executemany(
            "INSERT INTO standing_change (timeline_id, holder_id, subject_id, domain, delta, firsthand, game_time)"
            " VALUES (?,?,?,?,?,?,?)",
            [(tl_for(gt), rnd.choice(everyone), rnd.choice([player_id] + group), rnd.choice(["kindness", "honesty", "law"]),
              0.02, int(rnd.random() < 0.3), gt)
             for gt in (rnd.uniform(start_gt, end_gt) for _ in range(int(d["standing_changes"] * days)))])
        db.executemany(
            "INSERT INTO belief (timeline_id, holder_id, about_id, topic, statement, stance, confidence, formed_gt)"
            " VALUES (?,?,?,?,?,?,?,?)",
            [(tl_for(gt), rnd.choice(everyone), rnd.choice([player_id] + group), "kindness", "is kind", 0.4, 0.6, gt)
             for gt in (rnd.uniform(start_gt, end_gt) for _ in range(int(d["beliefs"] * days)))])
    return end_gt


def row_counts(store: WorldStore) -> dict[str, int]:
    tables = ["npc", "timeline", "conversation", "utterance", "memory", "knowledge", "claim", "world_event",
              "relationship_change", "standing_change", "belief"]
    return {t: store.scalar(f"SELECT COUNT(*) FROM {t}") for t in tables}


# ---- measuring ----------------------------------------------------------------------------------------------------

def measure_recall(store: WorldStore, group: list[int], player_id: int, now: float, reps: int,
                   result: WorldResult) -> None:
    from livingworld.memory.recall import recall_for
    names = {n: store.scalar("SELECT display_name FROM npc WHERE npc_id = ?", (n,)) for n in group}
    present = [player_id] + group
    first = group[0]
    start = time.perf_counter()
    recall_for(store, first, now, player_id, mark_rehearsed=False, about_ids=present)
    result.cold_recall_ms = round((time.perf_counter() - start) * 1000, 3)
    with section_timer() as parts:
        for npc in group:
            result.recall[names[npc]] = timed(
                lambda npc=npc: recall_for(store, npc, now, player_id, mark_rehearsed=False, about_ids=present),
                reps, store)
    result.sections_ms = {label: round(statistics.median(ms) * len(group), 3) for label, ms in sorted(parts.items())}


def synthetic_world(folder: str, scale: float, reps: int) -> WorldResult:
    store = WorldStore(os.path.join(folder, f"bench_x{scale:g}.sqlite"))
    try:
        with store.transaction() as db:
            player = _npc(db, "Torvar", 0x7, 1.0, player=True)
            group = [_npc(db, name, form, 1.0) for name, form in
                     (("Gerdur", 0x1A675), ("Hod", 0x1A677), ("Sven", 0x1347D))]
        now = grow(store, group, player, scale) + 0.05
        store.close()
        store = WorldStore(store.path)   # measure a freshly opened world, like the first talk after a game load
        result = WorldResult(f"synthetic x{scale:g} (~{DAYS_PER_SCALE * scale:.0f} game days)", scale, row_counts(store))
        measure_recall(store, group, player, now, reps, result)
        return result
    finally:
        store.close()


def real_world_copy(db_path: str, folder: str, reps: int) -> WorldResult:
    """Measure on a copy of a real world database. The original is only read by the file copy."""
    copy = os.path.join(folder, "copy.sqlite")
    shutil.copyfile(db_path, copy)
    for ext in ("-wal", "-shm"):
        if os.path.exists(db_path + ext):
            shutil.copyfile(db_path + ext, copy + ext)
    store = WorldStore(copy)
    try:
        player_id = store.scalar("SELECT npc_id FROM npc WHERE is_player = 1 ORDER BY npc_id LIMIT 1")
        group = [r[0] for r in store.query(
            "SELECT owner_id FROM v_visible_memory WHERE owner_id != ? GROUP BY owner_id ORDER BY COUNT(*) DESC LIMIT 3",
            (player_id,))]
        now = store.scalar("SELECT MAX(COALESCE(ended_gt, started_gt)) FROM conversation c"
                           " JOIN v_timeline_scope s ON s.timeline_id = c.timeline_id AND c.started_gt <= s.cutoff_gt") or 1.0
        result = WorldResult(f"copy of {os.path.basename(db_path)}", 1.0, row_counts(store))
        measure_recall(store, group, player_id, now + 0.01, reps, result)
        return result
    finally:
        store.close()


CATCH_UP: dict[float, list[float]] = {}   # build times (ms) until the per-turn cost settled, by scale

# the blocks recall_text builds for each NPC (integration.py), timed one by one
PROMPT_PARTS = ("_prepared_recall", "_shared_history_block", "_noticed", "_body_lines", "_carried_mood",
                "_personality_lines", "_work_lines", "_guard_lines", "_fellow_guard_lines", "_uniform_lines",
                "_squad_lines", "_refresh_social", "_refresh_guard", "_refresh_work", "_scene", "_fog_person")


@contextmanager
def prompt_part_timer(lw: Any) -> Iterator[dict[str, list[float]]]:
    """Time each block of ``recall_text`` on this LivingWorld instance (instance attributes, removed after)."""
    found: dict[str, list[float]] = {}
    for name in PROMPT_PARTS:
        original = getattr(lw, name, None)
        if original is None:
            continue

        def inner(*a, _original=original, _name=name, **k):
            start = time.perf_counter()
            try:
                return _original(*a, **k)
            finally:
                found.setdefault(_name.lstrip("_"), []).append((time.perf_counter() - start) * 1000)
        setattr(lw, name, inner)
    try:
        yield found
    finally:
        for name in PROMPT_PARTS:
            lw.__dict__.pop(name, None)


def full_prompt(folder: str, scale: float, reps: int) -> tuple[Timing, dict[str, float]]:
    """``recall_text`` (every Living World block in the prompt) for a three-NPC group talk, as group talk
    rebuilds it for each speaker on each turn. Runs the real server headless with a scripted AI."""
    from livingworld.harness import actors
    from livingworld.harness.scripted_llm import ScriptedLLM
    from livingworld.harness.session import HeadlessSession
    from livingworld import integration
    from livingworld.integration import LivingWorld
    saved_env = {k: os.environ.get(k) for k in (integration.ENV_VAR, "MANTELLA_LW_CONTEXT_PREP")}
    os.environ[integration.ENV_VAR] = "1"                # the Living World on, as in the launcher
    os.environ["MANTELLA_LW_CONTEXT_PREP"] = "0"         # its write watch would also use the trace callback
    saved_workers, LivingWorld.start_workers = LivingWorld.start_workers, False   # no background AI jobs
    try:
        return _full_prompt(folder, scale, reps, actors, ScriptedLLM, HeadlessSession, LivingWorld)
    finally:
        LivingWorld.start_workers = saved_workers
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _full_prompt(folder: str, scale: float, reps: int, actors: Any, ScriptedLLM: Any, HeadlessSession: Any,
                 LivingWorld: Any) -> tuple[Timing, dict[str, float]]:
    work = os.path.join(folder, f"session_x{scale:g}")
    with HeadlessSession(work, ScriptedLLM(replies=["Well met."] * 8)) as s:
        s.start([actors.player("Torvar"), actors.gerdur(), actors.hod()], location="Riverwood", game_days=1.0)
        s.continue_until_player_turn()
        lw = LivingWorld.latest
        chars = lw.context.npcs_in_conversation.get_all_characters()
        ids = [lw._npc_ids[c.ref_id] for c in chars]
        player_id = next(lw._npc_ids[c.ref_id] for c in chars if c.is_player_character)
        group = [i for i in ids if i != player_id]
        lw.wait_for_jobs()
        end = grow(lw.store, group, player_id, scale)
        lw.context._Context__game_days = end + 0.05   # the conversation is now at the end of that history
        world = lw._world_id
        # The conversation jumped to the end of a long history in one step, so the first builds also catch
        # up time-based upkeep (hygiene days, task expiry) for the whole skipped span. That is a one-off of
        # the jump, not the per-turn cost: build until two builds in a row are within 25% of each other.
        warm, last = [], None
        for _ in range(60):
            start = time.perf_counter()
            lw.recall_text(chars, world)
            ms = (time.perf_counter() - start) * 1000
            warm.append(round(ms, 1))
            if last is not None and abs(ms - last) <= 0.25 * max(ms, last):
                break
            last = ms
        CATCH_UP[scale] = warm
        timing = timed(lambda: lw.recall_text(chars, world), reps, lw.store)
        with prompt_part_timer(lw) as parts:
            for _ in range(reps):
                lw.recall_text(chars, world)
        s.end(game_days=end + 0.06)
    # per call of recall_text: the calls of each block in one build, at their median
    per_build = {name: round(statistics.median(ms) * (len(ms) / reps), 3) for name, ms in parts.items()}
    return timing, per_build


# ---- report -------------------------------------------------------------------------------------------------------

def report(results: list[WorldResult]) -> str:
    lines = []
    for r in results:
        lines.append(f"== {r.label}")
        lines.append("   rows: " + ", ".join(f"{k} {v}" for k, v in r.rows.items()))
        if r.cold_recall_ms is not None:
            lines.append(f"   first recall after opening: {r.cold_recall_ms:.2f} ms")
        for name, t in r.recall.items():
            lines.append(f"   recall_for {name}: median {t.median_ms:.2f} ms, p95 {t.p95_ms:.2f} ms, {t.statements} statements")
        if r.sections_ms:
            lines.append("   parts (median, summed over the NPCs): "
                         + ", ".join(f"{k} {v:.2f} ms" for k, v in sorted(r.sections_ms.items(), key=lambda kv: -kv[1])))
        if r.full_prompt is not None:
            t = r.full_prompt
            lines.append(f"   recall_text, whole group (one speaker's turn in group talk): median {t.median_ms:.2f} ms,"
                         f" p95 {t.p95_ms:.2f} ms, {t.statements} statements")
            if r.scale in CATCH_UP:
                lines.append(f"   catching up after jumping to the end of the history: {len(CATCH_UP[r.scale])} builds,"
                             f" {sum(CATCH_UP[r.scale]) / 1000:.1f} s in all (first {CATCH_UP[r.scale][0]:.0f} ms)")
            if r.full_prompt_parts_ms:
                lines.append("   its blocks (median per build): " + ", ".join(
                    f"{k} {v:.2f} ms" for k, v in sorted(r.full_prompt_parts_ms.items(), key=lambda kv: -kv[1]) if v >= 0.01))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--scales", default="1,10,50,200", help="history sizes, in multiples of 12 game days")
    parser.add_argument("--reps", type=int, default=20)
    parser.add_argument("--db", help="also measure a COPY of this world database (the file itself is not opened)")
    parser.add_argument("--full-prompt", action="store_true", help="also time recall_text in a headless group talk")
    parser.add_argument("--out", help="write the results as JSON here")
    args = parser.parse_args(argv)
    scales = [float(s) for s in args.scales.split(",") if s.strip()]
    results = []
    with tempfile.TemporaryDirectory(prefix="lw_context_bench_") as folder:
        if args.db:
            results.append(real_world_copy(args.db, folder, args.reps))
        for scale in scales:
            result = synthetic_world(folder, scale, args.reps)
            if args.full_prompt:
                result.full_prompt, result.full_prompt_parts_ms = full_prompt(folder, scale, max(5, args.reps // 2))
            results.append(result)
            print(report([result]), flush=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump([asdict(r) for r in results], f, indent=2)
    if args.db:
        print(report(results[:1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
