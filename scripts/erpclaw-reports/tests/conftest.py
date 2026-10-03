"""Shared pytest fixtures for ERPClaw Reports unit tests.

Most reports coverage lives at the foundation tier (see README.md) because the
actions route through the foundation dispatcher. VALUE tests are the exception:
``ar-aging`` shipped with a single test that asserted only that the action does
not 404, which is exactly why the M38 party-level double count shipped silently
(Wave G F21). Value tests for the aging + outstanding readers therefore live
here, next to the code they pin.

The payments suite already owns the seed helpers these reports need (company,
accounts, parties, invoices with their voucher-level ledger rows), so they are
imported rather than re-implemented — the same cross-suite idiom the selling F1
suite uses for buying_helpers.
"""
import os
import sys

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_MODULE_DIR = os.path.dirname(_TESTS_DIR)                 # erpclaw-reports/
_SCRIPTS_DIR = os.path.dirname(_MODULE_DIR)               # scripts/
_SETUP_DIR = os.path.join(_SCRIPTS_DIR, "erpclaw-setup")

# Bind erpclaw_lib to THIS TREE's lib, not the deployed ~/.openclaw symlink
# (the fix selling_helpers.py carries): the symlink can point at another
# worktree/branch, which would make these tests exercise foreign lib code.
_IN_TREE_LIB = os.path.join(_SETUP_DIR, "lib")
ERPCLAW_LIB = (_IN_TREE_LIB if os.path.isdir(os.path.join(_IN_TREE_LIB, "erpclaw_lib"))
               else os.path.join(os.path.expanduser(
                   os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))
if ERPCLAW_LIB not in sys.path:
    import importlib.util
    if importlib.util.find_spec("erpclaw_lib") is None:
        sys.path.insert(0, ERPCLAW_LIB)

_PAY_TESTS = os.path.join(_SCRIPTS_DIR, "erpclaw-payments", "tests")
if _PAY_TESTS not in sys.path:
    sys.path.append(_PAY_TESTS)

from payments_helpers import get_conn, init_all_tables  # noqa: E402

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
    test (see ``payments_helpers.init_all_tables``) and exposed to the seam as
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
    """Alias for conn. NOTE: no auto-hook exists — nothing runs the invariant engine on
    this fixture automatically; assert invariants explicitly where a test needs them.
    Docstring corrected 2026-08-01 (QA advisory A1 — a claimed-but-absent safety net is
    the same fiction that let M38 ship)."""
    return conn
