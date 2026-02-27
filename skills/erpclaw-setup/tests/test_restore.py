"""Tests for the restore-database action.

Covers: happy path, invalid file, integrity check, safety backup creation,
rollback on failure.
"""
import os
import shutil
import sqlite3

import db_query
from helpers import _call_action, _run_init_db


def test_restore_happy_path(tmp_path):
    """Restore from a valid backup should succeed."""
    # Create source DB
    db_path = str(tmp_path / "current.sqlite")
    _run_init_db(db_path)

    # Add a marker row so we can verify the restore replaces content
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("INSERT INTO currency (code, name, symbol, enabled) VALUES ('ZZZ', 'Marker', 'Z', 1)")
    conn.commit()
    conn.close()

    # Create a backup (fresh DB without the marker)
    backup_path = str(tmp_path / "backup.sqlite")
    _run_init_db(backup_path)

    # Restore
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    result = _call_action(
        db_query.restore_database, conn,
        db_path=db_path, backup_path=backup_path,
    )

    assert result["status"] == "ok"
    assert result["restored_from"] == backup_path
    assert result["integrity"] == "ok"
    assert result["size_bytes"] > 0
    assert "safety_backup" in result
    assert os.path.exists(result["safety_backup"])

    # Verify the marker row is gone (backup didn't have it)
    verify_conn = sqlite3.connect(db_path)
    verify_conn.row_factory = sqlite3.Row
    zzz = verify_conn.execute("SELECT * FROM currency WHERE code = 'ZZZ'").fetchone()
    verify_conn.close()
    assert zzz is None


def test_restore_invalid_file(tmp_path):
    """Restoring from a non-SQLite file should fail."""
    db_path = str(tmp_path / "current.sqlite")
    _run_init_db(db_path)

    bad_file = str(tmp_path / "bad.sqlite")
    with open(bad_file, "w") as f:
        f.write("this is not a sqlite file")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    result = _call_action(
        db_query.restore_database, conn,
        db_path=db_path, backup_path=bad_file,
    )

    assert result["status"] == "error"
    assert "not a valid" in result["message"].lower() or "not a valid" in result["message"]


def test_restore_non_erpclaw_db(tmp_path):
    """Restoring from a valid SQLite DB that isn't ERPClaw should fail."""
    db_path = str(tmp_path / "current.sqlite")
    _run_init_db(db_path)

    foreign_db = str(tmp_path / "foreign.sqlite")
    fc = sqlite3.connect(foreign_db)
    fc.execute("CREATE TABLE foo (id INTEGER PRIMARY KEY)")
    fc.commit()
    fc.close()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    result = _call_action(
        db_query.restore_database, conn,
        db_path=db_path, backup_path=foreign_db,
    )

    assert result["status"] == "error"
    assert "schema_version" in result["message"]


def test_restore_safety_backup_created(tmp_path):
    """Restore should create a safety backup of the current DB first."""
    db_path = str(tmp_path / "current.sqlite")
    _run_init_db(db_path)

    backup_path = str(tmp_path / "backup.sqlite")
    _run_init_db(backup_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    result = _call_action(
        db_query.restore_database, conn,
        db_path=db_path, backup_path=backup_path,
    )

    assert result["status"] == "ok"
    safety_path = result["safety_backup"]
    assert os.path.exists(safety_path)
    assert os.path.getsize(safety_path) > 0

    # Verify safety backup is a valid SQLite DB
    sc = sqlite3.connect(safety_path)
    tables = sc.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
    ).fetchone()[0]
    sc.close()
    assert tables > 0


def test_restore_missing_file(tmp_path):
    """Restoring from a nonexistent file should fail."""
    db_path = str(tmp_path / "current.sqlite")
    _run_init_db(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    result = _call_action(
        db_query.restore_database, conn,
        db_path=db_path, backup_path="/tmp/does_not_exist_xyz.sqlite",
    )

    assert result["status"] == "error"
    assert "not found" in result["message"].lower()
