"""Tests for S34 backup enhancements: list-backups, verify-backup, encrypted backup/restore.

Tests: 12 tests covering backup management, encryption, and field protection.
"""
import os
import sqlite3
import sys

import db_query
from helpers import _call_action, _run_init_db

# Add shared lib for crypto imports
sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))


# ---------------------------------------------------------------------------
# list-backups
# ---------------------------------------------------------------------------

def test_list_backups_empty(tmp_path):
    """List backups when no backup dir exists returns empty list."""
    db_path = str(tmp_path / "test.sqlite")
    _run_init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Temporarily override BACKUP_DIR
    orig_dir = db_query.BACKUP_DIR
    db_query.BACKUP_DIR = str(tmp_path / "nonexistent_backups")
    try:
        result = _call_action(db_query.list_backups, conn)
        assert result["status"] == "ok"
        assert result["count"] == 0
        assert result["backups"] == []
    finally:
        db_query.BACKUP_DIR = orig_dir
        conn.close()


def test_list_backups_with_files(tmp_path):
    """List backups finds and parses backup files."""
    db_path = str(tmp_path / "test.sqlite")
    _run_init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Create fake backup files
    backup_dir = str(tmp_path / "backups")
    os.makedirs(backup_dir)
    for name in ["erpclaw_backup_20260217_100000.sqlite",
                 "erpclaw_backup_20260216_090000.sqlite"]:
        path = os.path.join(backup_dir, name)
        with open(path, "wb") as f:
            f.write(b"\x00" * 1024)

    orig_dir = db_query.BACKUP_DIR
    db_query.BACKUP_DIR = backup_dir
    try:
        result = _call_action(db_query.list_backups, conn)
        assert result["status"] == "ok"
        assert result["count"] == 2
        assert result["total_size_bytes"] == 2048
        # Sorted newest first
        assert "20260217" in result["backups"][0]["filename"]
        assert result["backups"][0]["timestamp"] is not None
    finally:
        db_query.BACKUP_DIR = orig_dir
        conn.close()


def test_list_backups_includes_encrypted(tmp_path):
    """S37 fix: list-backups shows both .sqlite and .enc backup files."""
    db_path = str(tmp_path / "test.sqlite")
    _run_init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    backup_dir = str(tmp_path / "backups")
    os.makedirs(backup_dir)
    # Create plain and encrypted backup files
    for name in ["erpclaw_backup_20260217_100000.sqlite",
                 "erpclaw_backup_20260217_120000.enc"]:
        path = os.path.join(backup_dir, name)
        with open(path, "wb") as f:
            f.write(b"\x00" * 2048)

    orig_dir = db_query.BACKUP_DIR
    db_query.BACKUP_DIR = backup_dir
    try:
        result = _call_action(db_query.list_backups, conn)
        assert result["status"] == "ok"
        assert result["count"] == 2
        filenames = [b["filename"] for b in result["backups"]]
        assert any(".sqlite" in f for f in filenames)
        assert any(".enc" in f for f in filenames)
        # Encrypted backup has encrypted flag
        enc_backup = [b for b in result["backups"] if b["filename"].endswith(".enc")][0]
        assert enc_backup["encrypted"] is True
        plain_backup = [b for b in result["backups"] if b["filename"].endswith(".sqlite")][0]
        assert plain_backup["encrypted"] is False
    finally:
        db_query.BACKUP_DIR = orig_dir
        conn.close()


# ---------------------------------------------------------------------------
# verify-backup
# ---------------------------------------------------------------------------

def test_verify_valid_backup(tmp_path):
    """Verify a valid ERPClaw backup succeeds."""
    db_path = str(tmp_path / "backup.sqlite")
    _run_init_db(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    result = _call_action(db_query.verify_backup, conn, backup_path=db_path)
    conn.close()

    assert result["status"] == "ok"
    assert result["valid"] is True
    assert result["integrity"] == "ok"
    assert result["tables"] > 100  # 167+ tables
    assert result["encrypted"] is False


def test_verify_non_erpclaw_db(tmp_path):
    """Verify rejects a non-ERPClaw SQLite database."""
    foreign_db = str(tmp_path / "foreign.sqlite")
    fc = sqlite3.connect(foreign_db)
    fc.execute("CREATE TABLE foo (id INTEGER PRIMARY KEY)")
    fc.commit()
    fc.close()

    conn = sqlite3.connect(foreign_db)
    conn.row_factory = sqlite3.Row
    result = _call_action(db_query.verify_backup, conn, backup_path=foreign_db)
    conn.close()

    assert result["status"] == "error"
    assert "schema_version" in result["message"]


def test_verify_missing_file(tmp_path):
    """Verify returns error for missing file."""
    db_path = str(tmp_path / "test.sqlite")
    _run_init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    result = _call_action(db_query.verify_backup, conn,
                          backup_path="/tmp/nonexistent_xyz.sqlite")
    conn.close()

    assert result["status"] == "error"
    assert "not found" in result["message"].lower()


# ---------------------------------------------------------------------------
# Encrypted backup
# ---------------------------------------------------------------------------

def test_encrypted_backup_and_restore(tmp_path):
    """Full encrypted backup → restore cycle."""
    db_path = str(tmp_path / "source.sqlite")
    _run_init_db(db_path)

    # Add marker data
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("INSERT INTO currency (code, name, symbol, enabled) VALUES ('XYZ', 'Test', 'X', 1)")
    conn.commit()

    # Backup with encryption
    enc_path = str(tmp_path / "backup.sqlite.enc")
    result = _call_action(
        db_query.backup_database, conn,
        db_path=db_path, backup_path=enc_path,
        encrypt=True, passphrase="my-secret-pass",
    )
    conn.close()

    assert result["status"] == "ok"
    assert result["encrypted"] is True
    assert os.path.exists(enc_path)

    # Verify the encrypted file is not a valid SQLite (it's encrypted)
    from erpclaw_lib.crypto import is_encrypted_backup
    assert is_encrypted_backup(enc_path) is True

    # Restore from encrypted backup to a new DB
    new_db = str(tmp_path / "restored.sqlite")
    _run_init_db(new_db)
    conn2 = sqlite3.connect(new_db)
    conn2.row_factory = sqlite3.Row
    result2 = _call_action(
        db_query.restore_database, conn2,
        db_path=new_db, backup_path=enc_path,
        passphrase="my-secret-pass",
    )

    assert result2["status"] == "ok"
    assert result2["was_encrypted"] is True

    # Verify marker data survived
    vc = sqlite3.connect(new_db)
    vc.row_factory = sqlite3.Row
    xyz = vc.execute("SELECT * FROM currency WHERE code = 'XYZ'").fetchone()
    vc.close()
    assert xyz is not None
    assert xyz["name"] == "Test"


def test_encrypted_backup_wrong_passphrase(tmp_path):
    """Restoring encrypted backup with wrong passphrase fails."""
    db_path = str(tmp_path / "source.sqlite")
    _run_init_db(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    enc_path = str(tmp_path / "backup.enc")
    _call_action(
        db_query.backup_database, conn,
        db_path=db_path, backup_path=enc_path,
        encrypt=True, passphrase="correct-pass",
    )
    conn.close()

    # Try restore with wrong passphrase
    conn2 = sqlite3.connect(db_path)
    conn2.row_factory = sqlite3.Row
    result = _call_action(
        db_query.restore_database, conn2,
        db_path=db_path, backup_path=enc_path,
        passphrase="wrong-pass",
    )

    assert result["status"] == "error"
    assert "passphrase" in result["message"].lower() or "invalid" in result["message"].lower()


def test_encrypted_backup_no_passphrase(tmp_path):
    """Restoring encrypted backup without passphrase gives clear error."""
    db_path = str(tmp_path / "source.sqlite")
    _run_init_db(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    enc_path = str(tmp_path / "backup.enc")
    _call_action(
        db_query.backup_database, conn,
        db_path=db_path, backup_path=enc_path,
        encrypt=True, passphrase="secret",
    )
    conn.close()

    # Try restore without passphrase
    conn2 = sqlite3.connect(db_path)
    conn2.row_factory = sqlite3.Row
    result = _call_action(
        db_query.restore_database, conn2,
        db_path=db_path, backup_path=enc_path,
    )

    assert result["status"] == "error"
    assert "passphrase" in result["message"].lower()


def test_verify_encrypted_backup(tmp_path):
    """Verify encrypted backup with correct passphrase."""
    db_path = str(tmp_path / "source.sqlite")
    _run_init_db(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    enc_path = str(tmp_path / "backup.enc")
    _call_action(
        db_query.backup_database, conn,
        db_path=db_path, backup_path=enc_path,
        encrypt=True, passphrase="verify-me",
    )
    conn.close()

    # Verify with passphrase
    conn2 = sqlite3.connect(db_path)
    conn2.row_factory = sqlite3.Row
    result = _call_action(
        db_query.verify_backup, conn2,
        backup_path=enc_path, passphrase="verify-me",
    )
    conn2.close()

    assert result["status"] == "ok"
    assert result["valid"] is True
    assert result["encrypted"] is True
    assert result["tables"] > 100


def test_verify_encrypted_no_passphrase(tmp_path):
    """Verify encrypted backup without passphrase returns helpful message."""
    db_path = str(tmp_path / "source.sqlite")
    _run_init_db(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    enc_path = str(tmp_path / "backup.enc")
    _call_action(
        db_query.backup_database, conn,
        db_path=db_path, backup_path=enc_path,
        encrypt=True, passphrase="secret",
    )
    conn.close()

    # Verify without passphrase
    conn2 = sqlite3.connect(db_path)
    conn2.row_factory = sqlite3.Row
    result = _call_action(
        db_query.verify_backup, conn2,
        backup_path=enc_path,
    )
    conn2.close()

    assert result["status"] == "ok"
    assert result["encrypted"] is True
    assert result["valid"] is None  # Can't verify without passphrase


def test_backup_encrypt_requires_passphrase(tmp_path):
    """Backup with --encrypt but no --passphrase fails."""
    db_path = str(tmp_path / "source.sqlite")
    _run_init_db(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    result = _call_action(
        db_query.backup_database, conn,
        db_path=db_path, encrypt=True,
    )
    conn.close()

    assert result["status"] == "error"
    assert "passphrase" in result["message"].lower()
