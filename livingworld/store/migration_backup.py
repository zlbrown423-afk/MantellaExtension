"""A verified copy of a world's database, taken before schema migrations touch it.

``WorldStore.migrate`` calls :func:`take` when a database that already holds data has migrations
waiting. The copy is made with SQLite's online backup API, so whatever still sits in the ``-wal`` file
is included and the copy is one self-contained file. It is checked before any migration runs:

* ``PRAGMA integrity_check`` says ``ok``;
* it holds the same schema version as the live database;
* every table has the same number of rows as in the live database.

Only then is the copy given its final name (written as ``*.partial`` first and renamed, so a sync tool
such as OneDrive never uploads a half-written file under a real name). If anything fails the copy is
deleted and :class:`MigrationBackupError` is raised: the migrations do not run, and the database stays
exactly as it was. ``MANTELLA_LW_MIGRATION_BACKUP=0`` skips the copy (and with it that safety).

When a migration fails after the copy was taken, :func:`restore` writes the copy back over the live
database through the same backup API, so migrations that already committed in that run are undone too.

Copies go to ``<database folder>/backups/`` named ``<world>.v<from>-to-v<to>.<UTC time>.sqlite``. The
newest ``MANTELLA_LW_MIGRATION_BACKUP_KEEP`` (default 5) per world are kept; older ones are deleted.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

ENV_ENABLED = "MANTELLA_LW_MIGRATION_BACKUP"
ENV_KEEP = "MANTELLA_LW_MIGRATION_BACKUP_KEEP"
DEFAULT_KEEP = 5
BACKUP_DIR = "backups"
_RENAME_TRIES = 5          # OneDrive or a virus scanner can hold a fresh file for a moment
_RENAME_WAIT = 0.2


class MigrationBackupError(RuntimeError):
    """The pre-migration copy could not be made or did not verify; nothing was migrated."""


class MigrationFailed(RuntimeError):
    """A migration failed. ``restored`` says whether the database was put back from the copy."""

    def __init__(self, message: str, restored: bool, backup_path: str | None) -> None:
        super().__init__(message)
        self.restored = restored
        self.backup_path = backup_path


@dataclass(frozen=True)
class Backup:
    path: str
    from_version: int
    to_version: int
    row_counts: dict[str, int]


def enabled() -> bool:
    return os.environ.get(ENV_ENABLED, "1").strip() != "0"


def keep_count() -> int:
    try:
        return max(1, int(os.environ.get(ENV_KEEP, DEFAULT_KEEP)))
    except ValueError:
        return DEFAULT_KEEP


def backup_folder(db_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), BACKUP_DIR)


def _stem(db_path: str) -> str:
    return os.path.splitext(os.path.basename(db_path))[0]


def _name_pattern(stem: str) -> re.Pattern:
    return re.compile(rf"^{re.escape(stem)}\.v\d{{3}}-to-v\d{{3}}\.\d{{8}}T\d{{6}}\d*Z\.sqlite$")


def _schema_version(conn: sqlite3.Connection) -> int:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone():
        return 0
    return conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]


def _row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    counts = {}
    for name in names:
        try:
            counts[name] = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        except sqlite3.OperationalError:   # a virtual table whose module is not loaded here
            continue
    return counts


def _verify(copy: sqlite3.Connection, version: int, counts: dict[str, int]) -> None:
    result = copy.execute("PRAGMA integrity_check").fetchall()
    if [r[0] for r in result] != ["ok"]:
        raise MigrationBackupError(f"backup failed its integrity check: {result[:3]}")
    got = _schema_version(copy)
    if got != version:
        raise MigrationBackupError(f"backup holds schema version {got}, the database {version}")
    copied = _row_counts(copy)
    if copied != counts:
        differ = sorted(t for t in set(copied) | set(counts) if copied.get(t) != counts.get(t))
        raise MigrationBackupError(f"backup row counts differ from the database in: {', '.join(differ[:5])}")


def _replace(src: str, dst: str) -> None:
    for attempt in range(_RENAME_TRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == _RENAME_TRIES - 1:
                raise
            time.sleep(_RENAME_WAIT)


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("Living World: could not delete %s", path)


def take(conn: sqlite3.Connection, db_path: str, from_version: int, to_version: int,
         now: datetime | None = None) -> Backup:
    """Copy the database behind ``conn`` into the backup folder and verify it, or raise."""
    folder = backup_folder(db_path)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S%fZ")
    final = os.path.join(folder, f"{_stem(db_path)}.v{from_version:03d}-to-v{to_version:03d}.{stamp}.sqlite")
    partial = final + ".partial"
    try:
        os.makedirs(folder, exist_ok=True)
        size = sum(os.path.getsize(p) for p in (db_path, db_path + "-wal") if os.path.exists(p))
        free = shutil.disk_usage(folder).free
        if free < 2 * size + 1024 * 1024:
            raise MigrationBackupError(f"not enough free disk space for a backup ({free} bytes free, database {size})")
        counts = _row_counts(conn)
        copy = sqlite3.connect(partial, isolation_level=None)
        try:
            conn.backup(copy)
            copy.execute("PRAGMA journal_mode = DELETE")   # the copy inherits WAL; make it one plain file
            _verify(copy, from_version, counts)
        finally:
            copy.close()
        _replace(partial, final)
    except MigrationBackupError:
        _remove(partial)
        raise
    except Exception as exc:
        _remove(partial)
        raise MigrationBackupError(f"could not back up {db_path}: {exc}") from exc
    logger.info("Living World: backed up %s to %s before migrating v%d -> v%d",
                db_path, final, from_version, to_version)
    prune(db_path, keep_count())
    return Backup(final, from_version, to_version, counts)


def restore(backup: Backup, conn: sqlite3.Connection) -> None:
    """Write the copy back over the database behind ``conn`` and check it matches the copy."""
    if conn.in_transaction:
        conn.execute("ROLLBACK")
    source = sqlite3.connect(f"file:{backup.path}?mode=ro", uri=True)
    try:
        source.backup(conn)
    finally:
        source.close()
    result = conn.execute("PRAGMA integrity_check").fetchall()
    if [r[0] for r in result] != ["ok"]:
        raise MigrationBackupError(f"restored database failed its integrity check: {result[:3]}")
    if _schema_version(conn) != backup.from_version or _row_counts(conn) != backup.row_counts:
        raise MigrationBackupError("restored database does not match the backup")


def prune(db_path: str, keep: int) -> list[str]:
    """Delete all but the newest ``keep`` backups of this world. Returns what was deleted."""
    folder = backup_folder(db_path)
    if not os.path.isdir(folder):
        return []
    pattern = _name_pattern(_stem(db_path))
    mine = sorted((n for n in os.listdir(folder) if pattern.match(n)), key=lambda n: n.rsplit(".", 2)[1])
    removed = []
    for name in mine[:-keep] if keep > 0 else mine:
        path = os.path.join(folder, name)
        _remove(path)
        removed.append(path)
    return removed
