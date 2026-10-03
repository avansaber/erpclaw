"""Foundation migrations on PostgreSQL (m137e).

On SQLite "foundation DB initialized" starts with "the database file exists".
On PostgreSQL there is no file: the probe must connect to the configured
database and look for the core schema. Before the fix the probe checked the
SQLite default path first and answered False without ever connecting, so a
PostgreSQL install with its target configured never ran foundation migrations.
"""
import importlib.util
import os
import sys

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(_TESTS_DIR)
_LIB_DIR = os.path.join(_SCRIPTS_DIR, "erpclaw-setup", "lib")
if importlib.util.find_spec("erpclaw_lib") is None:
    sys.path.insert(0, _LIB_DIR)

from erpclaw_lib.db import get_connection
from erpclaw_lib.query import Q, Table, Field, Star, insert_row, P
import erpclaw_lib.seam as seam

_MM_PATH = os.path.join(_SCRIPTS_DIR, "module_manager.py")
_mm_spec = importlib.util.spec_from_file_location("erpclaw_module_manager_m137e", _MM_PATH)
mm = importlib.util.module_from_spec(_mm_spec)
_mm_spec.loader.exec_module(mm)

_RUNNER_PATH = os.path.join(_SCRIPTS_DIR, "erpclaw-setup", "migration_runner.py")

PROBE_URL = "postgresql://probe-test@db.invalid/url_db"
MISSING_TARGET_TEXT = (
    "ERPCLAW_DB_DIALECT=postgresql but the migration runner has no target: "
    "set ERPCLAW_DB_URL, pass a postgresql:// URL, or set ERPCLAW_DB_PATH to a postgresql:// URL.")


@pytest.fixture
def runner():
    """A fresh migration-runner module, so per-test patches never leak."""
    spec = importlib.util.spec_from_file_location("erpclaw_migration_runner_m137e", _RUNNER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _install_fake_psycopg2(monkeypatch, behaviour):
    """Swap the psycopg2 module for a recording fake and return its call log.

    behaviour is "ok" (the core-schema probe succeeds), "missing-table" (the
    probe statement raises) or "connect-failure" (connect raises).
    """
    calls = {"connect": [], "execute": [], "close": 0}

    class _FakeCursor:
        def execute(self, statement, *args, **kwargs):
            calls["execute"].append(statement)
            if behaviour == "missing-table":
                raise Exception("relation does not exist")
            return None

    class _FakeConnection:
        def cursor(self):
            return _FakeCursor()

        def close(self):
            calls["close"] += 1

    class _FakePsycopg2:
        @staticmethod
        def connect(target):
            calls["connect"].append(target)
            if behaviour == "connect-failure":
                raise Exception("could not connect")
            return _FakeConnection()

    monkeypatch.setitem(sys.modules, "psycopg2", _FakePsycopg2)
    return calls


def test_postgresql_probe_connects_to_configured_url(runner, tmp_path, monkeypatch):
    """The PostgreSQL probe connects to the configured URL, not the SQLite file."""
    monkeypatch.setenv("ERPCLAW_HOME", str(tmp_path))
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", PROBE_URL)
    calls = _install_fake_psycopg2(monkeypatch, "ok")
    db_path = str(tmp_path / "data.sqlite")

    assert mm._foundation_db_initialized(runner, db_path) is True
    assert calls["connect"] == [PROBE_URL]
    assert len(calls["execute"]) == 1
    assert calls["close"] == 1
    assert not os.path.exists(db_path)


def test_postgresql_probe_without_core_schema_is_false(runner, tmp_path, monkeypatch):
    """A reachable PostgreSQL database without the core schema reads as not initialized."""
    monkeypatch.setenv("ERPCLAW_HOME", str(tmp_path))
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", PROBE_URL)
    calls = _install_fake_psycopg2(monkeypatch, "missing-table")
    db_path = str(tmp_path / "data.sqlite")

    assert mm._foundation_db_initialized(runner, db_path) is False
    assert calls["connect"] == [PROBE_URL]
    assert calls["close"] == 1


def test_postgresql_probe_connect_failure_raises(runner, tmp_path, monkeypatch):
    """A failed PostgreSQL connect propagates instead of reading as not initialized."""
    monkeypatch.setenv("ERPCLAW_HOME", str(tmp_path))
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", PROBE_URL)
    calls = _install_fake_psycopg2(monkeypatch, "connect-failure")
    db_path = str(tmp_path / "data.sqlite")

    with pytest.raises(Exception, match="could not connect"):
        mm._foundation_db_initialized(runner, db_path)
    assert calls["connect"] == [PROBE_URL]


def test_postgresql_probe_without_target_does_not_connect(runner, tmp_path, monkeypatch):
    """With no PostgreSQL target the probe raises before connecting."""
    monkeypatch.setenv("ERPCLAW_HOME", str(tmp_path))
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    connect_calls = []

    def _stub_connect(db_arg):
        connect_calls.append(db_arg)
        raise AssertionError("must not connect without a target")

    monkeypatch.setattr(runner, "_connect", _stub_connect)

    with pytest.raises(RuntimeError) as exc:
        mm._foundation_db_initialized(runner, None)
    assert str(exc.value) == MISSING_TARGET_TEXT
    assert connect_calls == []


def test_run_foundation_migrations_surfaces_missing_postgresql_target(tmp_path, monkeypatch):
    """A missing PostgreSQL target surfaces as a probe error, not a clean skip."""
    monkeypatch.setenv("ERPCLAW_HOME", str(tmp_path))
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")

    res = mm._run_foundation_migrations()

    assert res["ran"] is True
    assert res["ok"] is False
    assert res["failed"] == "<probe>"
    assert res["error"] == MISSING_TARGET_TEXT


def test_sqlite_missing_file_short_circuit_unchanged(runner, tmp_path, monkeypatch):
    """The SQLite missing-file short-circuit never connects, even with a URL configured."""
    monkeypatch.setenv("ERPCLAW_HOME", str(tmp_path))
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
    monkeypatch.setenv("ERPCLAW_DB_URL", PROBE_URL)
    connect_calls = []

    def _stub_connect(db_arg):
        connect_calls.append(db_arg)
        raise AssertionError("must not connect for a missing SQLite file")

    monkeypatch.setattr(runner, "_connect", _stub_connect)
    db_path = str(tmp_path / "data.sqlite")

    assert mm._foundation_db_initialized(runner, db_path) is False
    assert connect_calls == []
    assert not os.path.exists(db_path)


def test_run_foundation_migrations_on_postgresql_reaches_the_runner(tmp_path, monkeypatch):
    """With a configured PostgreSQL target the probe passes and the runner takes over."""
    monkeypatch.setenv("ERPCLAW_HOME", str(tmp_path))
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", PROBE_URL)
    calls = _install_fake_psycopg2(monkeypatch, "ok")
    seam_calls = []

    def _stub_table_exists(name, db_arg=None):
        seam_calls.append((name, db_arg))
        raise RuntimeError("stop after probe")

    monkeypatch.setattr(seam, "table_exists", _stub_table_exists)
    try:
        res = mm._run_foundation_migrations()
    finally:
        seam.dispose_engines()

    assert res["ran"] is True
    assert res["ok"] is False
    assert res["error"] == "stop after probe"
    assert calls["connect"] == [PROBE_URL]
    assert ("erpclaw_schema_migration", PROBE_URL) in seam_calls
