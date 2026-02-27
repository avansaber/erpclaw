"""Shared pytest fixtures for erpclaw-support tests."""
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

from helpers import _run_init_db, ConnectionWrapper  # noqa: E402
from erpclaw_lib.db import _DecimalSum  # noqa: E402


@pytest.fixture
def fresh_db(tmp_path):
    """Fresh database with all tables but no data.

    Returns a ConnectionWrapper that delegates to sqlite3.Connection but
    also allows arbitrary attributes (e.g. company_id) needed by
    get_next_name() in the shared naming module.
    """
    db_path = str(tmp_path / "test.sqlite")
    _run_init_db(db_path)
    raw = sqlite3.connect(db_path)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA journal_mode=WAL")
    raw.execute("PRAGMA foreign_keys=ON")
    raw.execute("PRAGMA busy_timeout=5000")
    raw.create_aggregate("decimal_sum", 1, _DecimalSum)
    conn = ConnectionWrapper(raw)
    yield conn
    raw.close()
