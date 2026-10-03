"""Fiscal-year step of seed-demo-data keeps the company's year (m838).

setup-company auto-creates the fiscal year containing today, so the seed's
hand-added FY 2025 / FY 2026 can collide with it and turn the whole seed
"partial". The Phase 1 fiscal-years helper must read the company's existing
years through erpclaw-gl list-fiscal-years and skip (with a progress note,
no error) any seed year whose range overlaps one that is already there.

These tests drive the module-level helper against the real erpclaw-gl
actions on a fresh temporary SQLite, with isolated HOME/ERPCLAW_HOME. They
never depend on today's date: every overlapping year is created explicitly.
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
_GL_DIR = os.path.join(_SCRIPTS_DIR, "erpclaw-gl")
_META_DBQUERY = os.path.join(_META_DIR, "db_query.py")
_GL_DBQUERY = os.path.join(_GL_DIR, "db_query.py")
_INIT_SCHEMA = os.path.join(_SETUP_DIR, "init_schema.py")

_IN_TREE_LIB = os.path.join(_SETUP_DIR, "lib")
assert os.path.isdir(os.path.join(_IN_TREE_LIB, "erpclaw_lib")), (
    "in-tree erpclaw_lib not found: %s" % _IN_TREE_LIB)
if _IN_TREE_LIB not in sys.path:
    if importlib.util.find_spec("erpclaw_lib") is None:
        sys.path.insert(0, _IN_TREE_LIB)

from erpclaw_lib.db import get_connection  # noqa: E402
from erpclaw_lib.query import Field, P, Q, Table, fn, insert_row  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


META = _load("db_query_meta_m838", _META_DBQUERY)
GL = _load("db_query_gl_m838", _GL_DBQUERY)
_INIT = _load("init_schema_m838", _INIT_SCHEMA)


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Fresh SQLite plus isolated HOME/ERPCLAW_HOME/ERPCLAW_DB_PATH."""
    home = tmp_path / "home"
    erpclaw_home = tmp_path / "erpclaw_home"
    home.mkdir()
    erpclaw_home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ERPCLAW_HOME", str(erpclaw_home))
    db_path = str(tmp_path / "m838.sqlite")
    _INIT.init_db(db_path)
    monkeypatch.setenv("ERPCLAW_DB_PATH", db_path)
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    return db_path


def _seed_company(db_path, name="Seed Co", abbr="SC"):
    conn = get_connection(db_path)
    try:
        cid = str(uuid.uuid4())
        sql, _cols = insert_row(
            "company", {"id": P(), "name": P(), "abbr": P()})
        conn.execute(sql, (cid, name, abbr))
        conn.commit()
        return cid
    finally:
        conn.close()


def _fiscal_years(db_path, company_id):
    conn = get_connection(db_path)
    try:
        t = Table("fiscal_year")
        q = (Q.from_(t).select(t.star)
             .where(t.company_id == P())
             .orderby(t.start_date))
        return [dict(r) for r in conn.execute(q.get_sql(), (company_id,)).fetchall()]
    finally:
        conn.close()


def _make_run_skill(db_path):
    """Run the real erpclaw-gl fiscal-year actions against the temp DB.

    Mirrors the seed's _run_skill failure wording
    ("<skill>/<action> failed: <message>") without subprocess overhead.
    """
    def run_skill(skill_name, action_name, **kwargs):
        conn = get_connection(db_path)
        try:
            args = argparse.Namespace(
                company_id=kwargs.get("company_id"),
                company_name=kwargs.get("company_name"),
                name=kwargs.get("name"),
                start_date=kwargs.get("start_date"),
                end_date=kwargs.get("end_date"),
                limit=kwargs.get("limit"),
                offset=kwargs.get("offset"),
            )
            buf = io.StringIO()
            code = 0
            with patch("sys.stdout", buf):
                try:
                    if action_name == "list-fiscal-years":
                        GL.list_fiscal_years(conn, args)
                    elif action_name == "add-fiscal-year":
                        GL.add_fiscal_year(conn, args)
                    else:
                        raise AssertionError(
                            "unexpected action %r" % action_name)
                except SystemExit as exc:
                    code = exc.code
            data = json.loads(buf.getvalue().strip())
            if code != 0:
                message = data.get("error", data.get("message", "failed"))
                raise RuntimeError(
                    "%s/%s failed: %s" % (skill_name, action_name, message))
            return data
        finally:
            conn.close()
    return run_skill


class Recorder:
    def __init__(self):
        self.messages = []

    def __call__(self, msg):
        self.messages.append(msg)


def _run_step(db_path, company_id, run_skill=None):
    errors = []
    notes = Recorder()
    META._seed_demo_fiscal_years(
        company_id,
        run_skill or _make_run_skill(db_path),
        notes, errors)
    return notes, errors


def test_existing_2026_kept_and_2025_added(isolated_db):
    db_path = isolated_db
    company_id = _seed_company(db_path)
    run_skill = _make_run_skill(db_path)
    run_skill("erpclaw-gl", "add-fiscal-year",
              name="SMI FY 2026-01-01 to 2026-12-31",
              start_date="2026-01-01", end_date="2026-12-31",
              company_id=company_id)

    notes, errors = _run_step(db_path, company_id, run_skill)

    assert errors == []
    rows = _fiscal_years(db_path, company_id)
    assert [(r["name"], r["start_date"], r["end_date"]) for r in rows] == [
        ("FY 2025", "2025-01-01", "2025-12-31"),
        ("SMI FY 2026-01-01 to 2026-12-31", "2026-01-01", "2026-12-31"),
    ]
    assert any("2026-01-01" in m and "2026-12-31" in m for m in notes.messages)


def test_no_existing_year_adds_both(isolated_db):
    db_path = isolated_db
    company_id = _seed_company(db_path)

    notes, errors = _run_step(db_path, company_id)

    assert errors == []
    rows = _fiscal_years(db_path, company_id)
    assert [(r["name"], r["start_date"], r["end_date"]) for r in rows] == [
        ("FY 2025", "2025-01-01", "2025-12-31"),
        ("FY 2026", "2026-01-01", "2026-12-31"),
    ]


def test_partly_overlapping_year_skips_both(isolated_db):
    db_path = isolated_db
    company_id = _seed_company(db_path)
    run_skill = _make_run_skill(db_path)
    run_skill("erpclaw-gl", "add-fiscal-year",
              name="Split year",
              start_date="2025-07-01", end_date="2026-06-30",
              company_id=company_id)

    notes, errors = _run_step(db_path, company_id, run_skill)

    assert errors == []
    rows = _fiscal_years(db_path, company_id)
    assert [(r["name"], r["start_date"], r["end_date"]) for r in rows] == [
        ("Split year", "2025-07-01", "2026-06-30"),
    ]
    assert len(notes.messages) == 2


def test_list_failure_is_an_error_and_adds_nothing(isolated_db):
    db_path = isolated_db
    company_id = _seed_company(db_path)
    real = _make_run_skill(db_path)
    calls = []

    def run_skill(skill_name, action_name, **kwargs):
        calls.append(action_name)
        if action_name == "list-fiscal-years":
            raise RuntimeError(
                "erpclaw-gl/list-fiscal-years failed: boom")
        return real(skill_name, action_name, **kwargs)

    notes, errors = _run_step(db_path, company_id, run_skill)

    assert len(errors) == 1
    assert errors[0].startswith("Phase 1 (fiscal-years): ")
    assert "boom" in errors[0]
    assert "add-fiscal-year" not in calls
    assert _fiscal_years(db_path, company_id) == []
