"""Verified backups before schema migrations, and restoring them when a migration fails."""
import os
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from livingworld.store import migration_backup, world_store
from livingworld.store.migration_backup import MigrationBackupError, MigrationFailed
from livingworld.store.world_store import WorldStore

FILES = world_store._migration_files()
LATEST = FILES[-1][0]


def old_world(path, upto=2, rows=3):
    """A world database at schema ``upto`` that already holds some NPCs, left in WAL mode with
    uncheckpointed writes, like a live server's file."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA wal_autocheckpoint = 0")
    for version, sql in FILES:
        if version <= upto:
            conn.executescript(f"BEGIN;\n{sql}\nCOMMIT;")
    for i in range(rows):
        conn.execute("INSERT INTO npc (plugin, base_form_id, display_name, created_gt) VALUES ('Skyrim.esm',?,?,1)",
                     (f"0x{i + 1:x}", f"NPC {i}"))
    return conn   # kept open so the -wal file is not folded back in


def backups(tmp_path):
    folder = tmp_path / "backups"
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def test_existing_world_is_backed_up_and_verified_before_migrating(tmp_path):
    path = str(tmp_path / "World.sqlite")
    holder = old_world(path)
    assert os.path.getsize(path + "-wal") > 0          # the NPC rows are still only in the WAL
    store = WorldStore(path)
    holder.close()
    assert store.schema_version() == LATEST
    names = backups(tmp_path)
    assert len(names) == 1 and names[0].startswith(f"World.v002-to-v{LATEST:03d}.")
    assert not any(n.endswith(".partial") for n in names)
    copy = sqlite3.connect(tmp_path / "backups" / names[0])
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert copy.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 2
    assert copy.execute("SELECT COUNT(*) FROM npc").fetchone()[0] == 3   # WAL content was included
    assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"  # one self-contained file
    copy.close()


def test_new_or_current_world_takes_no_backup(tmp_path):
    path = str(tmp_path / "World.sqlite")
    WorldStore(path).close()       # brand new: nothing to protect
    WorldStore(path).close()       # already current: nothing to migrate
    assert backups(tmp_path) == []


def test_in_memory_store_takes_no_backup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    WorldStore(":memory:")
    assert backups(tmp_path) == []


def test_off_switch_migrates_without_backup(tmp_path, monkeypatch):
    monkeypatch.setenv(migration_backup.ENV_ENABLED, "0")
    path = str(tmp_path / "World.sqlite")
    old_world(path).close()
    assert WorldStore(path).schema_version() == LATEST
    assert backups(tmp_path) == []


def test_failed_backup_stops_migration_and_leaves_database_alone(tmp_path, monkeypatch):
    path = str(tmp_path / "World.sqlite")
    old_world(path).close()

    def broken(conn, *a, **k):
        raise MigrationBackupError("disk full")
    monkeypatch.setattr(migration_backup, "take", broken)
    with pytest.raises(MigrationBackupError):
        WorldStore(path)
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM npc").fetchone()[0] == 3


def test_backup_that_fails_verification_is_deleted_and_nothing_migrates(tmp_path, monkeypatch):
    path = str(tmp_path / "World.sqlite")
    old_world(path).close()
    real = migration_backup._row_counts
    calls = {"n": 0}

    def drift(conn):           # the second count (of the copy) disagrees with the first
        calls["n"] += 1
        counts = real(conn)
        if calls["n"] == 2:
            counts["npc"] += 1
        return counts
    monkeypatch.setattr(migration_backup, "_row_counts", drift)
    with pytest.raises(MigrationBackupError, match="row counts differ"):
        WorldStore(path)
    assert backups(tmp_path) == []          # neither the .partial nor a final copy is left
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 2


def test_low_disk_space_refuses_backup(tmp_path, monkeypatch):
    path = str(tmp_path / "World.sqlite")
    old_world(path).close()
    monkeypatch.setattr(migration_backup.shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(0, 0, 10))
    with pytest.raises(MigrationBackupError, match="disk space"):
        WorldStore(path)
    assert backups(tmp_path) == []


def test_failed_migration_restores_the_backup(tmp_path, monkeypatch):
    path = str(tmp_path / "World.sqlite")
    old_world(path).close()
    # migration 4 fails after 3 has committed: the restore must undo 3 as well
    broken = [(v, sql if v != 4 else sql + "\nTHIS IS NOT SQL;") for v, sql in FILES]
    monkeypatch.setattr(world_store, "_migration_files", lambda: broken)
    with pytest.raises(MigrationFailed, match="migration 004") as err:
        WorldStore(path)
    assert err.value.restored and os.path.exists(err.value.backup_path)
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM npc").fetchone()[0] == 3
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()

    monkeypatch.setattr(world_store, "_migration_files", lambda: FILES)   # the fixed build migrates fine
    store = WorldStore(path)
    assert store.schema_version() == LATEST
    assert store.scalar("SELECT COUNT(*) FROM npc") == 3


def test_failed_restore_reports_where_the_copy_is(tmp_path, monkeypatch):
    path = str(tmp_path / "World.sqlite")
    old_world(path).close()
    broken = [(v, sql if v != 3 else sql + "\nTHIS IS NOT SQL;") for v, sql in FILES]
    monkeypatch.setattr(world_store, "_migration_files", lambda: broken)

    def no_restore(backup, conn):
        raise MigrationBackupError("locked")
    monkeypatch.setattr(migration_backup, "restore", no_restore)
    with pytest.raises(MigrationFailed, match="restoring the backup failed") as err:
        WorldStore(path)
    assert not err.value.restored and os.path.exists(err.value.backup_path)


def test_failed_migration_on_new_world_rolls_back_that_file(tmp_path, monkeypatch):
    path = str(tmp_path / "World.sqlite")
    broken = [(v, sql if v != 2 else sql + "\nTHIS IS NOT SQL;") for v, sql in FILES]
    monkeypatch.setattr(world_store, "_migration_files", lambda: broken)
    with pytest.raises(MigrationFailed) as err:
        WorldStore(path)
    assert not err.value.restored and err.value.backup_path is None
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 1
    assert not conn.in_transaction


def test_retention_keeps_newest_per_world(tmp_path, monkeypatch):
    monkeypatch.setenv(migration_backup.ENV_KEEP, "2")
    path = str(tmp_path / "World.sqlite")
    other = tmp_path / "backups" / "Other.v001-to-v002.20200101T000000000000Z.sqlite"
    other.parent.mkdir()
    other.write_bytes(b"")
    stranger = tmp_path / "backups" / "notes.txt"
    stranger.write_text("keep me")
    conn = old_world(path)
    start = datetime(2026, 11, 16, tzinfo=timezone.utc)
    taken = [migration_backup.take(conn, path, 2, 3, now=start + timedelta(minutes=i)).path for i in range(4)]
    conn.close()
    left = backups(tmp_path)
    assert sorted(os.path.basename(p) for p in taken[-2:]) == [n for n in left if n.startswith("World.")]
    assert other.name in left and "notes.txt" in left


def test_backup_rename_retries_while_file_is_held(tmp_path, monkeypatch):
    path = str(tmp_path / "World.sqlite")
    conn = old_world(path)
    real = os.replace
    tries = {"n": 0}

    def held(src, dst):           # OneDrive holding the new file for a moment
        tries["n"] += 1
        if tries["n"] < 3:
            raise PermissionError("in use")
        real(src, dst)
    monkeypatch.setattr(migration_backup.os, "replace", held)
    monkeypatch.setattr(migration_backup, "_RENAME_WAIT", 0)
    backup = migration_backup.take(conn, path, 2, 3)
    conn.close()
    assert tries["n"] == 3 and os.path.exists(backup.path)


def test_backup_folder_is_not_a_world(tmp_path):
    from livingworld.store.worlds import world_exists
    save = tmp_path / "save"
    path = str(save / "data" / "livingworld" / "World.sqlite")
    os.makedirs(os.path.dirname(path))
    old_world(path).close()
    WorldStore(path).close()
    assert world_exists(str(save), "World")
    names = os.listdir(save / "data" / "livingworld" / "backups")
    assert len(names) == 1 and not world_exists(str(save), names[0][:-len(".sqlite")])
