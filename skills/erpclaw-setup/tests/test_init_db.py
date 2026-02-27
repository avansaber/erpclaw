"""Tests for database initialization via init_db.py.

Test IDs: S-I-01 through S-I-07

These tests exercise init_db directly (not via fresh_db fixture).
"""
import os
import sqlite3

from helpers import _run_init_db


# The 11 tables owned by erpclaw-setup
SETUP_TABLES = [
    "schema_version",
    "audit_log",
    "company",
    "currency",
    "exchange_rate",
    "payment_terms",
    "uom",
    "uom_conversion",
    "regional_settings",
    "custom_field",
    "property_setter",
]


# ---------------------------------------------------------------------------
# S-I-01: Check all 11 setup tables exist
# ---------------------------------------------------------------------------
def test_creates_all_tables(tmp_path):
    db_path = str(tmp_path / "test.sqlite")
    _run_init_db(db_path)

    conn = sqlite3.connect(db_path)
    existing = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()

    for table in SETUP_TABLES:
        assert table in existing, f"Table '{table}' not found in database"


# ---------------------------------------------------------------------------
# S-I-02: Running init_db twice is idempotent (no errors)
# ---------------------------------------------------------------------------
def test_idempotent(tmp_path):
    db_path = str(tmp_path / "test.sqlite")
    _run_init_db(db_path)
    # Second call should not raise
    _run_init_db(db_path)

    conn = sqlite3.connect(db_path)
    tables = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
    ).fetchone()[0]
    conn.close()
    assert tables > 0


# ---------------------------------------------------------------------------
# S-I-03: PRAGMA journal_mode returns WAL
# ---------------------------------------------------------------------------
def test_wal_mode(tmp_path):
    db_path = str(tmp_path / "test.sqlite")
    _run_init_db(db_path)

    conn = sqlite3.connect(db_path)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()

    assert mode.lower() == "wal"


# ---------------------------------------------------------------------------
# S-I-04: PRAGMA foreign_keys returns 1 (when enabled on connection)
# ---------------------------------------------------------------------------
def test_foreign_keys_enabled(tmp_path):
    db_path = str(tmp_path / "test.sqlite")
    _run_init_db(db_path)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.close()

    assert fk == 1


# ---------------------------------------------------------------------------
# S-I-05: schema_version table has erpclaw-setup entry
# ---------------------------------------------------------------------------
def test_schema_version_recorded(tmp_path):
    db_path = str(tmp_path / "test.sqlite")
    _run_init_db(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM schema_version WHERE module = 'erpclaw-setup'"
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["module"] == "erpclaw-setup"
    assert row["version"] == 1


# ---------------------------------------------------------------------------
# S-I-06: init_db creates intermediate directories
# ---------------------------------------------------------------------------
def test_creates_directory(tmp_path):
    db_path = str(tmp_path / "subdir" / "nested" / "test.sqlite")
    assert not os.path.exists(os.path.dirname(db_path))

    _run_init_db(db_path)

    assert os.path.exists(db_path)
    assert os.path.getsize(db_path) > 0


# ---------------------------------------------------------------------------
# S-I-07: PRAGMA busy_timeout returns 5000
# ---------------------------------------------------------------------------
def test_busy_timeout(tmp_path):
    db_path = str(tmp_path / "test.sqlite")
    _run_init_db(db_path)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=5000")
    timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    conn.close()

    assert timeout == 5000
