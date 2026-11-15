"""Background context preparation for group talk: each NPC's recalled rows read while the player speaks.

In group talk every speaker's prompt is rebuilt on every turn, and the biggest piece of that is
``recall_for`` (facts, memories, recap, gossip heard, opinion of the player, beliefs about who is
present). This module reads it for every NPC in the conversation on one background thread while the
mic is open, and hands it over when the turn's prompt is built. One-to-one talk does not use it: its
prompt is built once, when the conversation starts.

Safety rules:

* **Nothing is generated or guessed.** Only stored rows are read; the player's words are not involved and
  no AI call is made. The reply is still written from the player's actual words.
* **No writes in the background.** ``recall_for`` runs with ``mark_rehearsed=False``; the rehearsal count is
  added at hand-over, in the foreground, exactly as the normal path does.
* **One NPC's rows only reach that NPC.** A snapshot is stored and taken by NPC id, and the key also holds
  the store, timeline, conversation, player, everyone present and the game time.
* **Never stale.** Every write to the world store while a snapshot exists is seen (a SQLite trace callback on
  the store's one connection). A write to any table recall may read (``RECALL_SOURCES`` and every table behind
  those views, worked out from the schema) makes every snapshot taken before it unusable, and the prompt is
  built fresh as before. Writes to other tables (tasks, perception, hygiene, AI call records...) cannot change
  what recall shows and are ignored, and so are lines of the current conversation (``utterance``: recall's
  recap only reads earlier, closed conversations) and new events (``world_event`` is append-only and only
  reached through a knowledge or memory row, which is watched). Unknown statements count as writes.
* **Cancelled** when a new player turn starts, the conversation ends or its people change. A job that is
  cancelled stops between NPCs and stores nothing.

One reusable daemon thread; the hand-over never waits for it (a snapshot not ready yet = build fresh).

Off: ``MANTELLA_LW_CONTEXT_PREP=0``.
"""
from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from src import utils

logger = utils.get_logger()

ENV_VAR = "MANTELLA_LW_CONTEXT_PREP"

# What recall_for (memory/recall.py, with social/romance.recall_lines and social/reputation.belief_lines) reads,
# tables and views. Views are expanded to their tables from the schema. tests/livingworld/test_context_prep.py
# checks this against what SQLite reports as read during real recalls and against the SQL in those files.
RECALL_SOURCES = frozenset({
    "npc", "conversation", "participant", "utterance", "world_event", "knowledge", "memory",
    "rumor", "belief", "claim", "relationship", "relationship_change", "standing_change", "name_known",
    "romance_bond_change", "community", "membership", "community_stance", "timeline", "game_load",
    "v_known_facts", "v_visible_memory", "v_timeline_scope", "v_relationship", "v_claim_knowledge", "v_belief",
    "v_standing", "v_name_known", "v_romance_bond", "v_community_reputation", "v_faction_reputation",
})
# Written for the conversation in progress; recall reads them only for other conversations (recap) or in ways
# a row of the current conversation cannot change (its participant rows exist from the start; who is present is
# part of the snapshot key). A write here is ignored only when its SQL names the current conversation.
CURRENT_CONVERSATION_ONLY = frozenset({"utterance", "participant"})
# Append-only tables (update and delete are refused by triggers) that recall only reaches by joining from a
# row of another watched table: a new row here changes nothing until such a row points at it, and that
# write is watched. Every conversation turn logs an event, so without this no snapshot would survive.
INSERT_ONLY_JOINED = frozenset({"world_event"})

_READ_ONLY = re.compile(r"^\s*(SELECT|BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE|PRAGMA|EXPLAIN|ANALYZE)\b",
                        re.IGNORECASE)
_WRITE = re.compile(r"^\s*(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM)"
                    r"\s+[\"`\[]?(\w+)", re.IGNORECASE)
_FROM = re.compile(r"\b(?:FROM|JOIN)\s+[\"`\[]?(\w+)", re.IGNORECASE)
_TRIGGER = re.compile(r"^\s*--\s*TRIGGER\s+[\"`\[]?(\w+)", re.IGNORECASE)
_BODY_WRITE = re.compile(r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM)"
                         r"\s+[\"`\[]?(\w+)", re.IGNORECASE)


def enabled() -> bool:
    return os.environ.get(ENV_VAR, "").strip().lower() not in ("0", "false", "no", "off")


def expand_views(db, names: frozenset[str]) -> frozenset[str]:
    """``names`` plus every table and view the views among them read, recursively."""
    views = {n.lower(): (sql or "") for n, sql in db.execute("SELECT name, sql FROM sqlite_master WHERE type = 'view'")}
    out, todo = set(), [n.lower() for n in names]
    while todo:
        name = todo.pop()
        if name in out:
            continue
        out.add(name)
        if name in views:
            todo.extend(t.lower() for t in _FROM.findall(views[name]))
    return frozenset(out)


_CONV_EQ = re.compile(r"\bconv_id\s*=\s*(\d+)\b", re.IGNORECASE)
_INSERT_COLS = re.compile(r"\(\s*([\w\s,]+?)\s*\)\s*VALUES\s*\(\s*([^)]*)\)", re.IGNORECASE)


def conversation_of(sql: str) -> int | None:
    """The conversation a write names (``conv_id = 7`` or ``(conv_id, ...) VALUES (7, ...)``), or None."""
    m = _INSERT_COLS.search(sql)
    if m:
        cols = [c.strip().lower() for c in m.group(1).split(",")]
        vals = [v.strip() for v in m.group(2).split(",")]
        if "conv_id" in cols and len(vals) == len(cols) and vals[cols.index("conv_id")].isdigit():
            return int(vals[cols.index("conv_id")])
        return None
    ids = {int(x) for x in _CONV_EQ.findall(sql)}
    return ids.pop() if len(ids) == 1 else None


def statement_dirties(sql: str, watched: frozenset[str], writing_triggers: frozenset[str] = frozenset(),
                      current_conv: int | None = None) -> bool:
    """Whether executing ``sql`` may change what recall reads (``watched``: the tables it reads)."""
    trig = _TRIGGER.match(sql)
    if trig:
        return trig.group(1).lower() in writing_triggers
    if _READ_ONLY.match(sql):
        return False
    m = _WRITE.match(sql)
    if m:
        table = m.group(1).lower()
        if table in INSERT_ONLY_JOINED and m.group(0).lstrip()[:6].upper() == "INSERT":
            return False
        if table in CURRENT_CONVERSATION_ONLY:
            return current_conv is None or conversation_of(sql) != current_conv
        return table in watched
    return True   # CREATE, DROP, ALTER, WITH ... INSERT, anything unrecognised


@dataclass(frozen=True)
class PrepKey:
    store_id: int
    timeline_id: int | None
    conv_id: int
    npc_id: int
    player_id: int | None
    present: tuple[int, ...]
    now_gt: float


@dataclass
class Snapshot:
    key: PrepKey
    recall: Any
    write_gen: int
    ms: float


@dataclass
class PrepStats:
    jobs: int = 0
    prepared: int = 0
    hits: int = 0
    misses: dict[str, int] = field(default_factory=dict)
    prep_ms: float = 0.0
    last_write: str = ""   # the last statement that spoiled snapshots (for the log and tests)

    def miss(self, why: str) -> None:
        self.misses[why] = self.misses.get(why, 0) + 1


class ContextPrep:
    """Snapshots for one world store. ``compute(npc_id, key)`` returns the Recall (read-only)."""

    def __init__(self, store: Any, compute: Callable[[PrepKey], Any]) -> None:
        self.store = store
        self._compute = compute
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._gen = 0                 # bumped by every new job and every cancel
        self._job: tuple[int, list[PrepKey]] | None = None
        self._snaps: dict[int, Snapshot] = {}
        self._write_gen = 0           # bumped by every write that may change recall
        self._quiet = 0               # >0 while our own hand-over rehearsal write runs
        self._thread: threading.Thread | None = None
        self._closed = False
        self._conv: int | None = None   # the conversation the snapshots belong to
        self._busy = False
        self.stats = PrepStats()
        self.watched, self._writing_triggers = self._find_watched()
        store._conn.set_trace_callback(self._on_statement)

    # ---- write watch ----
    def _find_watched(self) -> tuple[frozenset[str], frozenset[str]]:
        """The tables recall reads, and the triggers whose bodies write to one of them."""
        with self.store.reading() as db:
            watched = expand_views(db, RECALL_SOURCES)
            rows = db.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'").fetchall()
        writing = set()
        for name, sql in rows:
            body = (sql or "").split("BEGIN", 1)[-1]
            if any(t.lower() in watched for t in _BODY_WRITE.findall(body)):
                writing.add(name.lower())
        return watched, frozenset(writing)

    def _on_statement(self, sql: str) -> None:
        # runs on whichever thread executes a statement, under the store's lock; keep it cheap and never raise
        try:
            if self._quiet or not self._snaps and self._job is None:
                return
            if statement_dirties(sql, self.watched, self._writing_triggers, self._conv):
                self._write_gen += 1
                self.stats.last_write = sql[:120]
        except Exception:
            self._write_gen += 1

    @property
    def write_gen(self) -> int:
        return self._write_gen

    # ---- jobs ----
    def begin(self, keys: list[PrepKey]) -> None:
        """Start preparing ``keys`` (one per NPC), replacing any job or snapshot from before."""
        with self._lock:
            if self._closed:
                return
            self._gen += 1
            self._snaps.clear()
            self._conv = keys[0].conv_id if keys else None
            self._job = (self._gen, list(keys)) if keys else None
            self.stats.jobs += 1 if keys else 0
            if keys and (self._thread is None or not self._thread.is_alive()):
                self._thread = threading.Thread(target=self._run, name="LivingWorldContextPrep", daemon=True)
                self._thread.start()
            self._wake.notify()

    def cancel(self) -> None:
        with self._lock:
            self._gen += 1
            self._job = None
            self._snaps.clear()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._gen += 1
            self._job = None
            self._snaps.clear()
            self._wake.notify()
        try:
            self.store._conn.set_trace_callback(None)
        except Exception:
            pass

    def _run(self) -> None:
        while True:
            with self._lock:
                while self._job is None and not self._closed:
                    self._wake.wait()
                if self._closed:
                    return
                gen, keys = self._job
                self._job = None
                self._busy = True
            try:
                self._prepare(gen, keys)
            finally:
                with self._lock:
                    self._busy = False

    def _prepare(self, gen: int, keys: list[PrepKey]) -> None:
        for key in keys:
            with self._lock:
                if gen != self._gen:
                    break
            start_gen = self._write_gen
            started = time.perf_counter()
            try:
                recall = self._compute(key)
            except Exception:
                logger.exception("Living World: background context preparation failed; the prompt is built as usual")
                continue
            ms = (time.perf_counter() - started) * 1000
            with self._lock:
                if gen != self._gen:
                    break
                self._snaps[key.npc_id] = Snapshot(key, recall, start_gen, ms)
                self.stats.prepared += 1
                self.stats.prep_ms += ms

    def take(self, key: PrepKey) -> Any | None:
        """The prepared Recall for ``key``, or None (then build it as usual). A snapshot is used at most once."""
        with self._lock:
            snap = self._snaps.pop(key.npc_id, None)
            if snap is None:
                self.stats.miss("not ready")
                return None
            if snap.key != key:
                self.stats.miss("key changed")
                return None
            if snap.write_gen != self._write_gen:
                self.stats.miss("store written")
                return None
            self.stats.hits += 1
            return snap.recall

    def quiet_write(self, fn: Callable[[], None]) -> None:
        """Run our own hand-over write (rehearsal counts of the NPC just handed over: only that NPC's memories,
        which no other NPC's recall reads) without spoiling the other snapshots."""
        with self.store.reading():   # the store lock: no other thread's statement runs while quiet
            self._quiet += 1
            try:
                fn()
            finally:
                self._quiet -= 1

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """For tests and measurements: wait until no job is queued or running."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._job is None and not self._busy:
                    return True
            time.sleep(0.002)
        return False
