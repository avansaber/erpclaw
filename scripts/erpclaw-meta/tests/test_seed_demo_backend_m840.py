"""m840: the demo seed reads through the seam, so it runs on PostgreSQL.

The seed drives every write through the child skills but used to open the
database with the SQLite driver for its own lookups, which fails on a
PostgreSQL backend. These tests pin the fix without running the whole seed:

- static: ``seed_demo_data`` keeps no driver open outside the SQLite-only
  idempotency guard;
- SQLite behaviour: on a fresh temp SQLite with isolated HOME/ERPCLAW_HOME,
  initialize + setup-company + chart of accounts run through the real
  actions, then the Phase 2 helper the seed calls resolves every mapped
  account number to the exact id read back from ``account``;
- PostgreSQL behaviour (needs ``ERPCLAW_PG_TEST_URL``): the same helper
  against a fresh schema resolves every mapped account;
- full seed on PostgreSQL (needs ``ERPCLAW_SEED_FULL=1`` too): the whole
  ``seed-demo-data`` runs with the ops/growth addons linked, and its
  ``errors`` list holds nothing except at most the Phase 1 fiscal-years
  overlap entry.
"""
import argparse
import ast
import importlib.util
import io
import json
import os
import sys

import pytest
from unittest.mock import patch

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_META_DIR = os.path.dirname(_TESTS_DIR)
_SCRIPTS_DIR = os.path.dirname(_META_DIR)
_SETUP_DIR = os.path.join(_SCRIPTS_DIR, "erpclaw-setup")
_META_DBQUERY = os.path.join(_META_DIR, "db_query.py")
_INIT_SCHEMA = os.path.join(_SETUP_DIR, "init_schema.py")
_SETUP_DBQUERY = os.path.join(_SETUP_DIR, "db_query.py")
_GL_DBQUERY = os.path.join(_SCRIPTS_DIR, "erpclaw-gl", "db_query.py")
_ADDONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(_SCRIPTS_DIR)), "erpclaw-addons")

# Bind erpclaw_lib to the tree under test, never a deployed lib symlink.
_IN_TREE_LIB = os.path.join(_SETUP_DIR, "lib")
if os.path.isdir(os.path.join(_IN_TREE_LIB, "erpclaw_lib")):
    _LIB = _IN_TREE_LIB
else:
    _LIB = os.path.join(os.path.expanduser(
        os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib")
if _LIB not in sys.path:
    if importlib.util.find_spec("erpclaw_lib") is None:
        sys.path.insert(0, _LIB)

from erpclaw_lib.db import get_connection  # noqa: E402


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


META = _load_module("db_query_meta_m840", _META_DBQUERY)
SETUP_MOD = _load_module("db_query_setup_m840", _SETUP_DBQUERY)
GL_MOD = _load_module("db_query_gl_m840", _GL_DBQUERY)

_PG_URL = os.environ.get("ERPCLAW_PG_TEST_URL")

_REQUIRES_PG = pytest.mark.skipif(
    not _PG_URL,
    reason="ERPCLAW_PG_TEST_URL not set (live Postgres required)",
)
_REQUIRES_FULL_SEED = pytest.mark.skipif(
    not (_PG_URL and os.environ.get("ERPCLAW_SEED_FULL") == "1"),
    reason="opt-in slow test: needs ERPCLAW_PG_TEST_URL and ERPCLAW_SEED_FULL=1",
)


def _ns(**kwargs):
    return argparse.Namespace(**kwargs)


def _capture(fn, *args):
    """Run an action function, capture stdout JSON and the exit code."""
    buf = io.StringIO()
    code = None
    with patch("sys.stdout", buf):
        try:
            fn(*args)
        except SystemExit as exc:
            code = exc.code
    return code, json.loads(buf.getvalue().strip())


def _is_sqlite_guard(test):
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "_seed_dialect"
        and any(isinstance(c, ast.Constant) and c.value == "sqlite"
                for c in test.comparators)
    )


def _unguarded_driver_opens():
    """Line numbers of driver opens in seed_demo_data outside the guard."""
    with open(_META_DBQUERY, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    seed_fn = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "seed_demo_data"
    )
    bad = []

    def visit(node, guarded):
        for child in ast.iter_child_nodes(node):
            now_guarded = guarded or (
                isinstance(child, ast.If) and _is_sqlite_guard(child.test)
            )
            func = child.func if isinstance(child, ast.Call) else None
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "connect"
                and isinstance(func.value, ast.Name)
                and func.value.id == "sqlite3"
            ):
                if not now_guarded:
                    bad.append(child.lineno)
            visit(child, now_guarded)

    visit(seed_fn, False)
    return bad


def test_seed_demo_data_has_no_unguarded_driver_open():
    """Every lookup in the seed body goes through the seam connection."""
    assert _unguarded_driver_opens() == []


def _isolate_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ERPCLAW_HOME", str(home / "erpclaw"))
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    return home


def _reset_pg_schema():
    conn = get_connection()
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
    finally:
        conn.close()


def _company_with_chart(db_path, db_arg):
    """Run setup-company + us_gaap chart through the real actions."""
    conn = get_connection(db_arg)
    try:
        code, created = _capture(
            SETUP_MOD.setup_company, conn,
            _ns(name="Seed Check Inc.", abbr=None, currency="USD",
                country="United States", fiscal_year_start_month=1,
                industry=None),
        )
        assert code == 0, created
        company_id = created["company_id"]
        code, chart = _capture(
            GL_MOD.setup_chart_of_accounts, conn,
            _ns(template="us_gaap", company_id=company_id,
                db_path=db_arg),
        )
        assert code == 0, chart
        return company_id
    finally:
        conn.close()


def _assert_phase2_resolves(db_arg, company_id):
    """The helper the seed calls resolves every mapped account number."""
    assert set(META.SEED_DEMO_ACCOUNT_MAP) != set()
    check = get_connection(db_arg)
    try:
        found = META._seed_lookup_account_ids(check, company_id)
        assert set(found) == set(META.SEED_DEMO_ACCOUNT_MAP)
        for key, number in META.SEED_DEMO_ACCOUNT_MAP.items():
            row = check.execute(
                "SELECT id FROM account WHERE account_number = ?"
                " AND company_id = ?",
                (number, company_id),
            ).fetchone()
            assert row is not None, f"account {number} ({key}) missing"
            assert found[key] == row["id"]
    finally:
        check.close()


def test_phase2_lookup_resolves_on_sqlite(tmp_path, monkeypatch):
    """Phase 2 helper resolves every mapped account on a fresh SQLite."""
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
    db_path = str(tmp_path / "seed_sqlite.sqlite")
    monkeypatch.setenv("ERPCLAW_DB_PATH", db_path)
    _load_module("init_schema_m840_sqlite", _INIT_SCHEMA).init_db(db_path)
    company_id = _company_with_chart(db_path, db_path)
    _assert_phase2_resolves(db_path, company_id)


@_REQUIRES_PG
def test_phase2_lookup_resolves_on_postgresql(tmp_path, monkeypatch):
    """Same helper against a fresh PostgreSQL schema resolves everything."""
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", _PG_URL)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    _reset_pg_schema()
    _load_module("init_schema_m840_pg", _INIT_SCHEMA).init_db(None)
    company_id = _company_with_chart(None, None)
    _assert_phase2_resolves(None, company_id)


@_REQUIRES_FULL_SEED
def test_full_seed_runs_on_postgresql(tmp_path, monkeypatch):
    """Whole seed-demo-data on PostgreSQL leaves no errors but overlap."""
    home = _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", _PG_URL)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    old_pythonpath = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH",
        _LIB + (os.pathsep + old_pythonpath if old_pythonpath else ""),
    )
    ops_src = os.path.join(_ADDONS_DIR, "erpclaw-ops")
    growth_src = os.path.join(_ADDONS_DIR, "erpclaw-growth")
    if not (os.path.isdir(ops_src) and os.path.isdir(growth_src)):
        pytest.skip("addon sources absent from this tree")
    skills = home / "clawd" / "skills"
    skills.mkdir(parents=True)
    os.symlink(ops_src, os.path.join(str(skills), "erpclaw-ops"))
    os.symlink(growth_src, os.path.join(str(skills), "erpclaw-growth"))
    monkeypatch.setattr(META, "SKILLS_DIR", str(skills))
    _reset_pg_schema()
    _load_module("init_schema_m840_full", _INIT_SCHEMA).init_db(None)
    code, result = _capture(
        META.seed_demo_data, _ns(db_path=_PG_URL, company_id=None))
    assert code == 0, result
    errors = result.get("errors", [])
    assert all(
        err.startswith("Phase 1 (fiscal-years)") for err in errors
    ), errors


def _install_fake_psycopg2(monkeypatch):
    """Install a fake psycopg2 that records the DSN it was asked to connect to."""
    import types

    seen = []

    class _FakeCursor:
        def execute(self, *args, **kwargs):
            return None

        def fetchone(self):
            return [True]

        def close(self):
            return None

    class _FakeConn:
        def cursor(self, *args, **kwargs):
            return _FakeCursor()

        def commit(self):
            return None

        def close(self):
            return None

    def _connect(dsn, **kwargs):
        seen.append(dsn)
        return _FakeConn()

    fake = types.ModuleType("psycopg2")
    fake.connect = _connect

    class Error(Exception):
        pass

    class OperationalError(Error):
        pass

    fake.Error = Error
    fake.OperationalError = OperationalError
    extras = types.ModuleType("psycopg2.extras")
    extras.DictCursor = object
    fake.extras = extras
    errors = types.ModuleType("psycopg2.errors")

    class DuplicateObject(OperationalError):
        pass

    class DuplicateFunction(OperationalError):
        pass

    errors.DuplicateObject = DuplicateObject
    errors.DuplicateFunction = DuplicateFunction
    fake.errors = errors
    monkeypatch.setitem(sys.modules, "psycopg2", fake)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", extras)
    monkeypatch.setitem(sys.modules, "psycopg2.errors", errors)
    return seen


def test_seed_connection_explicit_url_wins(monkeypatch):
    """Explicit --db-path URL reaches the driver even when ERPCLAW_DB_URL differs."""
    explicit = "postgresql://x@localhost:1/explicit_db"
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", "postgresql://x@localhost:1/other_db")
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    monkeypatch.delenv("ERPCLAW_DB_READONLY", raising=False)
    seen = _install_fake_psycopg2(monkeypatch)
    conn = META._seed_connection(explicit)
    try:
        assert seen == [explicit]
    finally:
        conn.close()


def test_seed_connection_falls_back_to_env_url(monkeypatch):
    """With no explicit path the ERPCLAW_DB_URL value reaches the driver."""
    env_url = "postgresql://x@localhost:1/env_db"
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", env_url)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    monkeypatch.delenv("ERPCLAW_DB_READONLY", raising=False)
    seen = _install_fake_psycopg2(monkeypatch)
    conn = META._seed_connection(None)
    try:
        assert seen == [env_url]
    finally:
        conn.close()
