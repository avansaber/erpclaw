"""Behavioural depth for two erpclaw-meta actions (task m503-depth-erpclaw-meta-1).

Each action below previously had only a routability test (the contract
suite's ``"Unknown action" not in ...`` check). Neither observes the
database, so an action could return a perfectly shaped envelope while
reading the wrong state — or, for the stub, while naming the wrong
replacement — and stay green. Every test here reads the stored rows back
with PyPika-built queries through ``erpclaw_lib.query`` on a connection
from ``erpclaw_lib.db.get_connection`` (catalog questions go through
``erpclaw_lib.seam``) and compares exact values.

Per-action depth (stored row vs ledger effect):

- install-guide: stored-row-grounded read (``company_count`` plus
  ``database_exists`` pinned against the ``company`` rows read back through
  the seam; tier progress pinned against the skills directory listing).
  This action reaches no ledger, so no balanced-legs assertion can hold;
  the tests pin the ledgers as untouched instead.
- setup-web-dashboard: stub refusal (every invocation exits 1 with the
  migration error naming the replacement; no stored row is created or
  changed). This action reaches no ledger either, so no balanced-legs
  assertion can hold; the tests pin the ledgers as untouched instead.

Neither action carries a monetary field, so no money-as-text assertion can
hold here; every comparison below is on exact strings and ints, never
float.
"""
import argparse
import importlib.util
import io
import json
import os
import sys
import uuid
from unittest.mock import patch

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_META_DIR = os.path.dirname(_TESTS_DIR)
_SCRIPTS_DIR = os.path.dirname(_META_DIR)
_SETUP_DIR = os.path.join(_SCRIPTS_DIR, "erpclaw-setup")
_META_DBQUERY = os.path.join(_META_DIR, "db_query.py")
_INIT_SCHEMA = os.path.join(_SETUP_DIR, "init_schema.py")

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
from erpclaw_lib.query import Field, P, Q, Table, fn, insert_row  # noqa: E402
import erpclaw_lib.seam as seam  # noqa: E402


def _load_meta():
    spec = importlib.util.spec_from_file_location(
        "db_query_meta_m503", _META_DBQUERY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


META = _load_meta()


def _init_db(db_path):
    spec = importlib.util.spec_from_file_location(
        "init_schema_m503", _INIT_SCHEMA)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.init_db(db_path)


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "meta.sqlite")
    _init_db(path)
    prev = os.environ.get("ERPCLAW_DB_PATH")
    os.environ["ERPCLAW_DB_PATH"] = path
    yield path
    if prev is None:
        os.environ.pop("ERPCLAW_DB_PATH", None)
    else:
        os.environ["ERPCLAW_DB_PATH"] = prev


@pytest.fixture
def conn(db_path):
    connection = get_connection(db_path)
    yield connection
    connection.close()


_NO_ARGS = object()


def _call(fn, args=_NO_ARGS):
    """Invoke an action function, capture stdout JSON and the exit code."""
    buf = io.StringIO()
    code = None
    with patch("sys.stdout", buf):
        try:
            if args is _NO_ARGS:
                fn()
            else:
                fn(args)
        except SystemExit as exc:
            code = exc.code
    return code, json.loads(buf.getvalue().strip())


def _ns(**kwargs):
    return argparse.Namespace(**kwargs)


def _is_ok(result):
    return result.get("status") == "ok"


def _is_error(result):
    return result.get("status") == "error"


def _row(conn, table, row_id):
    t = Table(table)
    q = Q.from_(t).select(t.star).where(t.id == P())
    found = conn.execute(q.get_sql(), (row_id,)).fetchone()
    assert found is not None, f"{table} {row_id} not found"
    return dict(found)


def _where(conn, table, **filters):
    t = Table(table)
    q = Q.from_(t).select(t.star)
    params = []
    for column, value in filters.items():
        q = q.where(Field(column) == P())
        params.append(value)
    return [dict(r) for r in conn.execute(q.get_sql(), params).fetchall()]


def _all(conn, table):
    t = Table(table)
    q = Q.from_(t).select(t.star).orderby(t.id)
    return [dict(r) for r in conn.execute(q.get_sql()).fetchall()]


def _count(conn, table):
    t = Table(table)
    q = Q.from_(t).select(fn.Count("*").as_("n"))
    return conn.execute(q.get_sql()).fetchone()["n"]


def _snapshot(conn, tables):
    return {name: _all(conn, name) for name in tables}


def _seed_company(conn, name, abbr):
    cid = str(uuid.uuid4())
    sql, _cols = insert_row(
        "company", {"id": P(), "name": P(), "abbr": P()})
    conn.execute(sql, (cid, name, abbr))
    conn.commit()
    return cid


_TABLES = ("company", "gl_entry", "payment_ledger_entry",
           "stock_ledger_entry", "audit_log", "naming_series")
_LEDGERS = ("gl_entry", "payment_ledger_entry", "stock_ledger_entry")


class TestInstallGuideDepth:
    def test_reports_database_state_and_writes_nothing(
            self, conn, db_path, tmp_path, monkeypatch):
        assert seam.table_exists("company", db_path)
        assert seam.table_exists("gl_entry", db_path)
        cid_a = _seed_company(conn, "Guide Alpha", "GA")
        cid_b = _seed_company(conn, "Guide Beta", "GB")
        before = _snapshot(conn, _TABLES)

        skills = tmp_path / "skills"
        skills.mkdir()
        monkeypatch.setattr(META, "SKILLS_DIR", str(skills))

        code, result = _call(META.install_guide, _ns(db_path=db_path))
        assert code == 0
        assert _is_ok(result)
        assert result["database_exists"] is True
        assert result["company_count"] == 2
        assert result["company_count"] == _count(conn, "company")

        total = len(META.ALL_SKILLS)
        assert result["progress"] == (
            f"0 of {total} skills installed (0%)")
        assert result["current_tier"] == "Not started"
        assert result["install_command"] == META.TIERS[0]["install_cmd"]

        first = result["tiers"][0]
        assert first["name"] == META.TIERS[0]["name"]
        assert first["skills"] == META.TIERS[0]["skills"]
        assert first["installed"] == []
        assert first["missing"] == META.TIERS[0]["skills"]
        assert first["status"] == "not_started"
        assert all(tier["installed"] == [] for tier in result["tiers"])

        assert {row["id"] for row in _where(conn, "company")} == {
            cid_a, cid_b}
        assert _row(conn, "company", cid_a)["name"] == "Guide Alpha"
        assert _row(conn, "company", cid_b)["abbr"] == "GB"
        assert _snapshot(conn, _TABLES) == before
        for ledger in _LEDGERS:
            assert _count(conn, ledger) == 0

    def test_sees_an_installed_skill_and_still_writes_nothing(
            self, conn, db_path, tmp_path, monkeypatch):
        cid = _seed_company(conn, "Guide Gamma", "GG")
        before = _snapshot(conn, _TABLES)

        skills = tmp_path / "skills"
        (skills / "erpclaw-setup").mkdir(parents=True)
        (skills / "erpclaw-setup" / "SKILL.md").write_text(
            "---\nname: erpclaw-setup\nversion: 9.9.9\n---\n",
            encoding="utf-8")
        monkeypatch.setattr(META, "SKILLS_DIR", str(skills))

        code, result = _call(META.install_guide, _ns(db_path=db_path))
        assert code == 0
        assert _is_ok(result)
        assert result["company_count"] == 1
        assert result["company_count"] == _count(conn, "company")

        total = len(META.ALL_SKILLS)
        assert result["progress"] == (
            f"1 of {total} skills installed ({round(1 / total * 100)}%)")

        first = result["tiers"][0]
        assert first["installed"] == ["erpclaw-setup"]
        assert first["status"] == "partial"
        assert result["current_tier"] == META.TIERS[0]["name"]
        assert result["install_command"].endswith("erpclaw-gl")
        assert "erpclaw-setup" not in result["install_command"]

        assert _row(conn, "company", cid)["name"] == "Guide Gamma"
        assert _snapshot(conn, _TABLES) == before
        for ledger in _LEDGERS:
            assert _count(conn, ledger) == 0

    def test_unknown_flag_is_refused_and_changes_nothing(
            self, conn, db_path):
        cid = _seed_company(conn, "Guide Delta", "GD")
        before = _snapshot(conn, _TABLES)
        argv = ["db_query.py", "--action", "install-guide",
                "--db-path", db_path, "--no-such-flag-xyz"]
        with patch.object(sys, "argv", argv):
            code, result = _call(META.main)
        assert code == 1
        assert _is_error(result)
        assert "--no-such-flag-xyz" in result["message"]
        assert "--action" in result["valid_flags"]
        assert _row(conn, "company", cid)["name"] == "Guide Delta"
        assert _snapshot(conn, _TABLES) == before


class TestSetupWebDashboardDepth:
    def test_stub_refuses_with_migration_error_and_writes_nothing(
            self, conn, db_path):
        assert seam.table_exists("company", db_path)
        cid = _seed_company(conn, "Dash Alpha", "DA")
        before = _snapshot(conn, _TABLES)

        code, result = _call(
            META._setup_web_dashboard_moved_stub,
            _ns(db_path=db_path, domain="example.invalid",
                ssl=True, skip_build=False))
        assert code == 1
        assert _is_error(result)
        assert result["error"] == (
            "action 'setup-web-dashboard' moved to erpclaw-os-engine addon "
            "(renamed to 'os-setup-web-dashboard')")
        assert result["old_action"] == "setup-web-dashboard"
        assert result["new_action"] == "os-setup-web-dashboard"
        assert result["missing_addon"] == "erpclaw-os-engine"
        assert result["since_version"] == "4.0.0"
        assert "install_command" in result
        assert "github" in result

        assert _row(conn, "company", cid)["name"] == "Dash Alpha"
        assert _snapshot(conn, _TABLES) == before
        for ledger in _LEDGERS:
            assert _count(conn, ledger) == 0

    def test_refusal_is_unconditional_and_leaves_db_byte_identical(
            self, conn, db_path):
        cid = _seed_company(conn, "Dash Beta", "DB")
        before = _snapshot(conn, _TABLES)

        code_plain, plain = _call(
            META._setup_web_dashboard_moved_stub, _ns(db_path=db_path))
        code_flags, flagged = _call(
            META._setup_web_dashboard_moved_stub,
            _ns(db_path=db_path, domain="example.invalid",
                ssl=False, skip_build=True))
        assert (code_plain, code_flags) == (1, 1)
        assert plain == flagged
        assert plain["new_action"] == "os-setup-web-dashboard"

        assert _row(conn, "company", cid)["abbr"] == "DB"
        assert _snapshot(conn, _TABLES) == before
        for ledger in _LEDGERS:
            assert _count(conn, ledger) == 0
