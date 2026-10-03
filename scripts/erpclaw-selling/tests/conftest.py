"""Shared pytest fixtures for ERPClaw Selling unit tests."""
import os
import sys

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import pytest
from selling_helpers import init_all_tables, get_conn, build_selling_env

try:
    from erpclaw_lib.db import get_dialect
except ImportError:  # lib not on path in some minimal contexts
    def get_dialect():
        return os.environ.get("ERPCLAW_DB_DIALECT", "sqlite")

# ── M97 canonical block: product SUBPROCESSES bind this checkout ─────────────
# selling_helpers has already bound erpclaw_lib to the tree under test (M54);
# `_M97_CHILD_LIB` is that same directory, read off the imported package so it
# cannot drift from the real binding. resume-billing-run dispatches
# generate-recurring-invoices to the foundation router as a SUBPROCESS, and the
# shipped bootstrap resolves erpclaw_lib from $ERPCLAW_HOME/lib FIRST
# (ADR-0017) -- right on a user machine, wrong here, where that symlink points
# at whichever checkout last ran an install. The symlink into the temp home is
# seeded deliberately: under a BARE temp home the child dies with a structured
# "foundation not installed" error that most assertions accept, so the suite
# would go green having verified nothing.
# Full reasoning + the poison proof: testing/unit/L0/test_subprocess_home_pin.py
import erpclaw_lib

_M97_CHILD_LIB = os.path.dirname(os.path.dirname(
    os.path.abspath(erpclaw_lib.__file__)))


@pytest.fixture(scope="session", autouse=True)
def _isolated_erpclaw_home(tmp_path_factory):
    """Pin ERPCLAW_HOME at a throwaway install seeded with this tree's lib."""
    if not os.path.isdir(os.path.join(_M97_CHILD_LIB, "erpclaw_lib")):
        yield None          # published module repo: the deployed install is right
        return
    home = str(tmp_path_factory.mktemp("erpclaw_home"))
    os.symlink(_M97_CHILD_LIB, os.path.join(home, "lib"))
    _prev = os.environ.get("ERPCLAW_HOME")
    os.environ["ERPCLAW_HOME"] = home
    yield home
    if _prev is None:
        os.environ.pop("ERPCLAW_HOME", None)
    else:
        os.environ["ERPCLAW_HOME"] = _prev


@pytest.fixture
def db_path(tmp_path):
    """Per-test fresh database with full ERPClaw core schema.

    SQLite (default): a throwaway per-test file under ``tmp_path`` —
    today's behaviour, unchanged.
    PostgreSQL (``ERPCLAW_DB_DIALECT=postgresql``): the expendable database
    named by ``ERPCLAW_PG_TEST_URL``, reset to a fresh ``public`` schema per
    test (see ``selling_helpers.init_all_tables``) and exposed to the seam as
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


# Tests that cannot hold on PostgreSQL, with the construct that stops each.
# The marks are applied here — never by editing a test body, an assertion, or
# a production file — so the same files run unmarked on SQLite. Keys are the
# ``file::class::test`` (or ``file::test``) tails of the node IDs.
_PG_SKIP_MARKS = {}


def pytest_collection_modifyitems(items):
    """Skip the tests in ``_PG_SKIP_MARKS`` on PostgreSQL only.

    Gated on ``ERPCLAW_PG_TEST_URL`` being set as well as the dialect, so the
    honest "URL not set" skip in the ``db_path`` fixture keeps its message for
    every test when PostgreSQL is selected but not configured.
    """
    if get_dialect() != "postgresql":
        return
    if not os.environ.get("ERPCLAW_PG_TEST_URL"):
        return
    for item in items:
        for tail, reason in _PG_SKIP_MARKS.items():
            if item.nodeid.endswith(tail):
                item.add_marker(pytest.mark.skip(reason=reason))
                break


@pytest.fixture
def conn(db_path):
    """Per-test database connection (auto-closes after test)."""
    connection = get_conn(db_path)
    yield connection
    connection.close()


@pytest.fixture
def fresh_db(conn):
    """Alias for conn — enables invariant engine auto-hook from root conftest."""
    return conn


@pytest.fixture
def env(conn):
    """Full selling environment: company, accounts, FY, CC, items, warehouse, customer."""
    return build_selling_env(conn)
