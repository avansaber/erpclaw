"""Test fixtures for erpclaw-tax tests."""
import os
import sys
import sqlite3
import pytest

# Add scripts to path for importing db_query
SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

# Add shared lib
LIB_DIR = os.path.expanduser("~/.openclaw/erpclaw/lib")
if LIB_DIR not in sys.path:
    sys.path.insert(0, LIB_DIR)

# Add tests dir so helpers can be imported
TESTS_DIR = os.path.dirname(__file__)
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

from helpers import _run_init_db  # noqa: E402
from erpclaw_lib.db import _DecimalSum  # noqa: E402


@pytest.fixture
def fresh_db(tmp_path):
    """Fresh database with all tables but no data."""
    db_path = str(tmp_path / "test.sqlite")
    _run_init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.create_aggregate("decimal_sum", 1, _DecimalSum)
    # Add columns that db_query.py expects but init_db.py may not define
    _ensure_supplier_tax_columns(conn)
    yield conn
    conn.close()


def _ensure_supplier_tax_columns(conn):
    """Add withholding-related columns to supplier if they don't exist."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(supplier)").fetchall()]
    if "w9_on_file" not in cols:
        conn.execute("ALTER TABLE supplier ADD COLUMN w9_on_file INTEGER NOT NULL DEFAULT 0")
    if "tax_withholding_category_id" not in cols:
        conn.execute("ALTER TABLE supplier ADD COLUMN tax_withholding_category_id TEXT")
    conn.commit()
