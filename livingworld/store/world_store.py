"""The SQLite database that holds one world's NPC identities, conversations and (later) memories.

One file per Mantella world id, so a new MCM world number starts from an empty database.
SQLite is the source of truth; Mantella's own summary files keep working alongside it.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from importlib import resources
from typing import Iterator

MIGRATIONS_PACKAGE = "livingworld.store.migrations"


def _migration_files() -> list[tuple[int, str]]:
    """(version, sql) for every NNN_name.sql file, in version order."""
    found = []
    for entry in resources.files(MIGRATIONS_PACKAGE).iterdir():
        name = entry.name
        if name.endswith(".sql") and name[:3].isdigit():
            found.append((int(name[:3]), entry.read_text(encoding="utf-8")))
    return sorted(found)


class WorldStore:
    """A thread-safe handle on one world's database.

    Mantella answers game requests on worker threads, so every write goes through
    ``transaction()``, which holds a lock and commits or rolls back as one unit.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
        self.migrate()

    @classmethod
    def for_world(cls, save_folder: str, world_id: str) -> "WorldStore":
        return cls(os.path.join(save_folder, "data", "livingworld", f"{world_id}.sqlite"))

    # ---- schema ----
    def schema_version(self) -> int:
        exists = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone()
        if not exists:
            return 0
        return self._conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]

    def _applied_versions(self) -> set[int]:
        if self.schema_version() == 0:
            return set()
        return {row[0] for row in self._conn.execute("SELECT version FROM schema_version")}

    def migrate(self) -> None:
        with self._lock:
            # Apply every file not applied yet, not just those above the highest version: features land
            # in separate pull requests, so a lower-numbered file can arrive after a higher one.
            applied = self._applied_versions()
            for version, sql in _migration_files():
                if version in applied:
                    continue
                # executescript commits on its own, so wrap the file in an explicit transaction
                self._conn.executescript(f"BEGIN;\n{sql}\nCOMMIT;")

    # ---- access ----
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    @contextmanager
    def reading(self) -> Iterator[sqlite3.Connection]:
        """The connection under the lock, for several reads in a row (no transaction is opened)."""
        with self._lock:
            yield self._conn

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def scalar(self, sql: str, params: tuple | dict = ()):
        row = self.query(sql, params)
        return row[0][0] if row else None

    def active_timeline_id(self) -> int:
        return self.scalar("SELECT timeline_id FROM timeline WHERE is_active = 1")

    def close(self) -> None:
        with self._lock:
            self._conn.close()
