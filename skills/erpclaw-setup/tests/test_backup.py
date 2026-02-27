"""Tests for the backup-database action.

Test ID: S-BK-01

NOTE: backup_database opens a NEW connection from args.db_path, independent
of the conn parameter. We must point db_path at our test database file.
"""
import os
import sqlite3

import db_query
from helpers import _call_action, _run_init_db


# ---------------------------------------------------------------------------
# S-BK-01: Backup to tmp_path, verify file exists and size > 0
# ---------------------------------------------------------------------------
def test_backup_database(tmp_path):
    # Create a test database (not using fresh_db fixture because we need
    # the db_path string, not just the connection)
    db_path = str(tmp_path / "source.sqlite")
    _run_init_db(db_path)

    # Open a connection for the action call (backup_database uses args.db_path
    # to open its own source connection, but we still pass conn for signature)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    backup_path = str(tmp_path / "backup.sqlite")

    result = _call_action(
        db_query.backup_database, conn,
        db_path=db_path, backup_path=backup_path
    )
    conn.close()

    assert result["status"] == "ok"
    assert result["backup_path"] == backup_path
    assert result["size_bytes"] > 0
    assert "timestamp" in result

    # Verify the backup file actually exists and has content
    assert os.path.exists(backup_path)
    assert os.path.getsize(backup_path) > 0

    # Verify backup is a valid SQLite database with tables
    backup_conn = sqlite3.connect(backup_path)
    tables = backup_conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
    ).fetchone()[0]
    backup_conn.close()
    assert tables > 0
