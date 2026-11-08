"""A small job queue in the ``job`` table and a background worker that drains it.

Ending a conversation only queues a job, so the game never waits on an AI call. Jobs survive a
restart (a job left 'running' by a crash is picked up again), a failed job retries with growing
delays, and the idempotency key means the same conversation is never queued twice.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from livingworld.perf.stats import STATS as PERF
from livingworld.store.world_store import WorldStore
from src import utils

logger = utils.get_logger()

MAX_ATTEMPTS = 6
BACKOFF_SECONDS = (5, 30, 120, 600, 1800)   # after attempt 1, 2, 3, ...


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _now() -> str:
    return _iso(datetime.now(timezone.utc))


class JobQueue:
    def __init__(self, store: WorldStore) -> None:
        self.store = store

    def enqueue(self, kind: str, payload: dict[str, Any], key: str) -> bool:
        """Queue a job; returns False if a job with this key already exists."""
        with self.store.transaction() as db:
            timeline = db.execute("SELECT timeline_id FROM timeline WHERE is_active = 1").fetchone()[0]
            cur = db.execute(
                "INSERT OR IGNORE INTO job (timeline_id, kind, payload_json, idempotency_key) VALUES (?,?,?,?)",
                (timeline, kind, json.dumps(payload), key))
            return cur.rowcount == 1

    def requeue_interrupted(self) -> int:
        """Jobs left 'running' by a crash or shutdown go back in the queue."""
        with self.store.transaction() as db:
            return db.execute("UPDATE job SET status = 'queued' WHERE status = 'running'").rowcount

    def claim(self) -> tuple[int, str, dict[str, Any]] | None:
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT job_id, kind, payload_json FROM job WHERE status IN ('queued','failed') AND next_run_at <= ?"
                " ORDER BY next_run_at, job_id LIMIT 1", (_now(),)).fetchone()
            if not row:
                return None
            db.execute("UPDATE job SET status = 'running', attempts = attempts + 1 WHERE job_id = ?", (row[0],))
            return row[0], row[1], json.loads(row[2])

    def done(self, job_id: int) -> None:
        with self.store.transaction() as db:
            db.execute("UPDATE job SET status = 'done', last_error = NULL WHERE job_id = ?", (job_id,))

    def failed(self, job_id: int, error: str) -> str:
        with self.store.transaction() as db:
            attempts = db.execute("SELECT attempts FROM job WHERE job_id = ?", (job_id,)).fetchone()[0]
            if attempts >= MAX_ATTEMPTS:
                status, next_run = "dead", _now()
            else:
                delay = BACKOFF_SECONDS[min(attempts, len(BACKOFF_SECONDS)) - 1]
                status, next_run = "failed", _iso(datetime.now(timezone.utc) + timedelta(seconds=delay))
            db.execute("UPDATE job SET status = ?, last_error = ?, next_run_at = ? WHERE job_id = ?",
                       (status, error[:2000], next_run, job_id))
            return status

    def counts(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self.store.query("SELECT status, COUNT(*) FROM job GROUP BY status")}


Handler = Callable[[str, dict[str, Any]], None]


class JobWorker:
    """Runs queued jobs on one daemon thread. ``run_pending()`` runs them inline (used by tests)."""

    def __init__(self, store: WorldStore, handler: Handler, poll_seconds: float = 2.0) -> None:
        self.queue = JobQueue(store)
        self.handler = handler
        self.poll_seconds = poll_seconds
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._run_lock = threading.Lock()
        self.queue.requeue_interrupted()

    def run_pending(self) -> int:
        """Run every job that is due now; returns how many ran."""
        ran = 0
        with self._run_lock:
            while True:
                job = self.queue.claim()
                if job is None:
                    break
                job_id, kind, payload = job
                ran += 1
                PERF.job_started()
                try:
                    self.handler(kind, payload)
                except Exception as e:  # any failure is retried later
                    status = self.queue.failed(job_id, f"{type(e).__name__}: {e}")
                    logger.warning(f"Living World: job {job_id} ({kind}) failed, now {status}: {e}")
                else:
                    self.queue.done(job_id)
                finally:
                    PERF.job_finished()
        return ran

    def notify(self) -> None:
        self._wake.set()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="livingworld-worker", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_pending()
            except Exception:
                logger.exception("Living World: worker loop error")
            self._wake.wait(self.poll_seconds)
            self._wake.clear()
