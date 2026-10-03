"""Shared pytest fixtures for ERPClaw Buying unit tests."""
import os
import sys

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import pytest
from buying_helpers import init_all_tables, get_conn, build_buying_env

try:
    from erpclaw_lib.db import get_dialect
except ImportError:  # lib not on path in some minimal contexts
    def get_dialect():
        return os.environ.get("ERPCLAW_DB_DIALECT", "sqlite")


@pytest.fixture
def db_path(tmp_path):
    """Per-test fresh database with full ERPClaw core schema.

    SQLite (default): a throwaway per-test file under ``tmp_path`` —
    today's behaviour, unchanged.
    PostgreSQL (``ERPCLAW_DB_DIALECT=postgresql``): the expendable database
    named by ``ERPCLAW_PG_TEST_URL``, reset to a fresh ``public`` schema per
    test (see ``buying_helpers._reset_pg_schema``, whose guard refuses any
    database outside the expendable-test pattern) and exposed to the seam as
    ``ERPCLAW_DB_URL``. Skips when the variable is unset rather than
    silently running on SQLite.
    """
    if get_dialect() == "postgresql":
        base_url = os.environ.get("ERPCLAW_PG_TEST_URL")
        if not base_url:
            pytest.skip(
                "ERPCLAW_DB_DIALECT=postgresql but ERPCLAW_PG_TEST_URL is not set"
            )
        old_db_url = os.environ.get("ERPCLAW_DB_URL")
        old_db_path = os.environ.get("ERPCLAW_DB_PATH")
        os.environ["ERPCLAW_DB_URL"] = base_url
        os.environ.pop("ERPCLAW_DB_PATH", None)
        try:
            init_all_tables(None)
            yield base_url
        finally:
            if old_db_url is None:
                os.environ.pop("ERPCLAW_DB_URL", None)
            else:
                os.environ["ERPCLAW_DB_URL"] = old_db_url
            if old_db_path is None:
                os.environ.pop("ERPCLAW_DB_PATH", None)
            else:
                os.environ["ERPCLAW_DB_PATH"] = old_db_path
        return
    path = str(tmp_path / "test.sqlite")
    init_all_tables(path)
    os.environ["ERPCLAW_DB_PATH"] = path
    yield path
    os.environ.pop("ERPCLAW_DB_PATH", None)


@pytest.fixture
def conn(db_path):
    connection = get_conn(db_path)
    yield connection
    connection.close()


@pytest.fixture
def fresh_db(conn):
    return conn


@pytest.fixture
def env(conn):
    return build_buying_env(conn)
