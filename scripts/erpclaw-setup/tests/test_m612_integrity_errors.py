"""Unit test for the lazy integrity-error lookup in erpclaw_lib.db.

The function returns SQLite's integrity-error class always, plus the
PostgreSQL driver's class only when the active dialect is PostgreSQL
(imported inside the function), so product code can refuse unique
violations with one portable handler instead of one handler per backend,
without pulling in the driver on other backends.
"""
import os
import sqlite3
import subprocess
import sys

import pytest

SETUP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_IN_TREE_LIB = os.path.join(SETUP_DIR, "lib")
ERPCLAW_LIB = (_IN_TREE_LIB if os.path.isdir(os.path.join(_IN_TREE_LIB, "erpclaw_lib"))
               else os.path.join(os.path.expanduser(
                   os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))
if ERPCLAW_LIB not in sys.path:
    import importlib.util
    if importlib.util.find_spec("erpclaw_lib") is None:
        sys.path.insert(0, ERPCLAW_LIB)

from erpclaw_lib.db import integrity_error_types


def test_sqlite_dialect_holds_sqlite_class(monkeypatch):
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
    result = integrity_error_types()
    assert isinstance(result, tuple)
    assert sqlite3.IntegrityError in result
    assert Exception not in result
    assert len(result) == 1


def test_postgresql_dialect_adds_driver_class(monkeypatch):
    psycopg2 = pytest.importorskip(
        "psycopg2",
        reason="PostgreSQL driver does not import in this test process, "
               "so the PostgreSQL half cannot run here",
    )
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    result = integrity_error_types()
    assert isinstance(result, tuple)
    assert sqlite3.IntegrityError in result
    assert psycopg2.IntegrityError in result
    assert Exception not in result


def test_sqlite_lookup_leaves_driver_unimported():
    pytest.importorskip(
        "psycopg2",
        reason="without the driver importable in the test process it "
               "cannot fail on the old import-time code, so this "
               "regression test has nothing to pin",
    )
    env = dict(os.environ)
    env["ERPCLAW_DB_DIALECT"] = "sqlite"
    env.pop("ERPCLAW_DB_URL", None)
    env["PYTHONPATH"] = ERPCLAW_LIB + os.pathsep + env.get("PYTHONPATH", "")
    code = (
        "import sys, erpclaw_lib.db; "
        "erpclaw_lib.db.integrity_error_types(); "
        "print('psycopg2' in sys.modules)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False"
