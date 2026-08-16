"""Shared pytest fixtures for ERPClaw Payments unit tests."""
import os
import sys

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import pytest
from payments_helpers import init_all_tables, get_conn


@pytest.fixture
def db_path(tmp_path):
    """Per-test fresh SQLite database with full ERPClaw core schema."""
    path = str(tmp_path / "test.sqlite")
    init_all_tables(path)
    os.environ["ERPCLAW_DB_PATH"] = path
    yield path
    os.environ.pop("ERPCLAW_DB_PATH", None)


@pytest.fixture
def conn(db_path):
    """Per-test database connection (auto-closes after test)."""
    connection = get_conn(db_path)
    yield connection
    connection.close()


@pytest.fixture
def fresh_db(conn):
    """Alias for conn. NOTE: no auto-hook exists — no root conftest runs the invariant
    engine on this fixture; tests that need invariant coverage must assert it explicitly
    (the INV-27 pins do: 22 explicit calls). Docstring corrected 2026-08-01 (QA advisory A1)."""
    return conn
