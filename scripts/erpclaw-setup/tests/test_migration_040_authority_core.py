"""Migration 040: every install carries the authority-core tables.

Fresh installs gain the eight tables plus one install record from init_db;
upgraded installs gain them from migration 040; status reports the inspector
observation. Pins: frozen-profile agreement, observation branches, migration
create/no-op/report-only/refusal, and the data-class declaration.
"""
import importlib.util
import io
import os
import re
import sqlite3
import sys
from contextlib import redirect_stdout

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SETUP_DIR = os.path.dirname(_TESTS_DIR)
_MIGRATION = os.path.join(
    _SETUP_DIR, "migrations", "040_authority_core_tables.py")
_REPO_ROOT = os.path.abspath(
    os.path.join(_TESTS_DIR, "..", "..", "..", "..", ".."))
_SCHEMA_PATH = os.path.join(
    _REPO_ROOT, "testing", "unit", "L0",
    "test_m242_authority_core_schema.py")
_INIT_SCHEMA_PATH = os.path.join(_SETUP_DIR, "init_schema.py")

if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mig():
    return _load("migration_040", _MIGRATION)


_schema = _load("_authority_core_schema_test", _SCHEMA_PATH)

from setup_helpers import load_db_query, call_action, ns  # noqa: E402
from erpclaw_lib import seam  # noqa: E402
from erpclaw_lib.db import get_connection  # noqa: E402
from erpclaw_lib.query import Q, P, Table, dynamic_update, fn  # noqa: E402

mod = load_db_query()

_CORE = tuple(seam._AUTHORITY_CORE_TABLES)
_PROFILE = "authority-core-v1-sqlite"


def _make_pre040(conn):
    conn.execute("DROP TABLE IF EXISTS operation_authorization")
    conn.execute("DROP TABLE IF EXISTS authority_delegation_cap")
    conn.execute("DROP TABLE IF EXISTS authority_delegation_right")
    conn.execute("DROP TABLE IF EXISTS authority_delegation")
    conn.execute("DROP TABLE IF EXISTS authority_right")
    conn.execute("DROP TABLE IF EXISTS authority_membership")
    conn.execute("DROP TABLE IF EXISTS authority_principal")
    conn.execute("DROP TABLE IF EXISTS authority_install")
    conn.commit()


def _count(conn, table):
    t = Table(table)
    q = Q.from_(t).select(fn.Count("*").as_("cnt")).get_sql()
    return conn.execute(q).fetchone()["cnt"]


def _counts(conn, db_path):
    return {name: _count(conn, name) for name in seam.table_names(db_path)}


def _install_rows(conn):
    t = Table("authority_install")
    q = Q.from_(t).select(
        t.singleton, t.install_id, t.schema_version, t.phase,
        t.revision).get_sql()
    return [{
        "singleton": r["singleton"],
        "install_id": r["install_id"],
        "schema_version": r["schema_version"],
        "phase": r["phase"],
        "revision": r["revision"],
    } for r in conn.execute(q).fetchall()]


def _run(db_path, report_only=False):
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = _mig().run_migration(db_path, report_only=report_only)
    return result, buf.getvalue()


def _status(conn):
    return call_action(mod.status, conn, ns())


def test_fresh_install_has_the_core_and_one_install_row(db_path, conn):
    names = seam.table_names(db_path)
    for table in _CORE:
        assert table in names
    rows = _install_rows(conn)
    assert len(rows) == 1
    row = rows[0]
    assert row["singleton"] == 1
    assert row["schema_version"] == 1
    assert row["phase"] == "STAGED"
    assert row["revision"] == 0
    assert re.match(r"^[0-9a-f-]{36}$", row["install_id"])
    for table in _CORE:
        if table == "authority_install":
            continue
        assert _count(conn, table) == 0


def test_fresh_install_matches_the_frozen_profile(db_path):
    assert _schema._serialize_metadata(
        seam.authority_core_metadata()) == _schema._load_artifact()["declared"]
    probe = get_connection(db_path)
    try:
        probe.execute("BEGIN")
        try:
            rows = seam.authority_core_schema_evidence(probe)
        finally:
            probe.rollback()
    finally:
        probe.close()
    assert [tuple(r) for r in rows] == [
        tuple(r) for r in _schema._load_artifact()["captured"]]


def test_status_reports_match_on_a_fresh_install(db_path):
    probe = get_connection(db_path)
    try:
        before = _install_rows(probe)
        result = _status(probe)
        after = _install_rows(probe)
        settled = probe.in_transaction
    finally:
        probe.close()
    assert after == before
    assert settled is False
    for key in ("companies", "currencies", "uoms", "payment_terms",
                "schema_versions"):
        assert key in result
    core = result["authority_core"]
    if sqlite3.sqlite_version == seam._AUTHORITY_CORE_SQLITE_VERSION:
        print("status branch: MATCH on the pinned library")
        assert core == {"profile": _PROFILE, "status": "MATCH",
                        "phase": "STAGED", "reason": "MATCH"}
    else:
        print("status branch: UNSUPPORTED off the pinned library")
        assert core == {"profile": _PROFILE, "status": "UNSUPPORTED",
                        "phase": None, "reason": "SQLITE_VERSION"}


def test_status_reports_structure_when_the_core_is_absent(db_path, conn):
    _make_pre040(conn)
    probe = get_connection(db_path)
    try:
        result = _status(probe)
    finally:
        probe.close()
    assert result["authority_core"] == {
        "profile": _PROFILE, "status": "MISMATCH",
        "phase": None, "reason": "STRUCTURE"}


def test_status_reports_install_when_the_row_is_missing(db_path, conn):
    conn.execute("DELETE FROM authority_install")
    conn.commit()
    probe = get_connection(db_path)
    try:
        result = _status(probe)
    finally:
        probe.close()
    core = result["authority_core"]
    if sqlite3.sqlite_version == seam._AUTHORITY_CORE_SQLITE_VERSION:
        print("status branch: INSTALL on the pinned library")
        assert core == {"profile": _PROFILE, "status": "MISMATCH",
                        "phase": None, "reason": "INSTALL"}
    else:
        print("status branch: UNSUPPORTED off the pinned library")
        assert core == {"profile": _PROFILE, "status": "UNSUPPORTED",
                        "phase": None, "reason": "SQLITE_VERSION"}


def test_status_on_a_raw_connection_is_unsupported(conn):
    result = _status(conn)
    assert result["authority_core"] == {
        "profile": _PROFILE, "status": "UNSUPPORTED",
        "phase": None, "reason": "BACKEND"}


def test_status_with_an_open_transaction_is_busy(db_path):
    probe = get_connection(db_path)
    try:
        probe.execute("BEGIN")
        assert probe.in_transaction is True
        _cur = Table("currency")
        probe.execute(
            Q.into(_cur).columns(
                "code", "name", "symbol", "decimal_places",
                "enabled").insert(P(), P(), P(), P(), P()).get_sql(),
            ("ZZZ", "Busy Probe", "Z", 2, 1))
        result = _status(probe)
        assert result["authority_core"] == {
            "profile": _PROFILE, "status": "UNAVAILABLE",
            "phase": None, "reason": "BUSY"}
        assert probe.in_transaction is True
        _seen = probe.execute(
            Q.from_(_cur).select(_cur.code).where(
                _cur.code == P()).get_sql(), ("ZZZ",)).fetchone()
        assert _seen["code"] == "ZZZ"
        probe.rollback()
        _gone = probe.execute(
            Q.from_(_cur).select(_cur.code).where(
                _cur.code == P()).get_sql(), ("ZZZ",)).fetchone()
        assert _gone is None
    finally:
        probe.close()


def test_migration_creates_the_core_on_an_upgraded_install(db_path, conn):
    _make_pre040(conn)
    before = _counts(conn, db_path)
    result, _out = _run(db_path)
    assert result["created"] == list(seam._AUTHORITY_CORE_TABLES)
    assert result["install_seeded"] is True
    assert result["status"] == "CHECKED"
    rows = _install_rows(conn)
    assert len(rows) == 1
    assert rows[0]["singleton"] == 1
    assert rows[0]["schema_version"] == 1
    assert rows[0]["phase"] == "STAGED"
    assert rows[0]["revision"] == 0
    assert re.match(r"^[0-9a-f-]{36}$", rows[0]["install_id"])
    after = _counts(conn, db_path)
    for name, count in before.items():
        assert after[name] == count
    for table in _CORE:
        assert table in seam.table_names(db_path)


def test_second_run_is_a_no_op(db_path, conn):
    _make_pre040(conn)
    first, _out = _run(db_path)
    assert first["created"] == list(seam._AUTHORITY_CORE_TABLES)
    first_id = _install_rows(conn)[0]["install_id"]
    second, _out2 = _run(db_path)
    assert second["created"] == []
    assert second["install_seeded"] is False
    assert _install_rows(conn)[0]["install_id"] == first_id


def test_report_only_writes_nothing(db_path, conn):
    _make_pre040(conn)
    before = _counts(conn, db_path)
    result, _out = _run(db_path, report_only=True)
    assert result["would_create"] == list(seam._AUTHORITY_CORE_TABLES)
    names = seam.table_names(db_path)
    for table in _CORE:
        assert table not in names
    assert _counts(conn, db_path) == before


def test_wrong_shape_table_refuses(db_path, conn):
    _make_pre040(conn)
    meta = seam.MetaData()
    seam.Table(
        "authority_right", meta,
        seam.Column("install_id", seam.Text, nullable=False),
        seam.Column("principal_id", seam.Text, nullable=False),
        seam.Column("company_id", seam.Text, nullable=False),
        seam.Column("resource_kind", seam.Text, nullable=False),
        seam.Column("resource_id", seam.Text, nullable=False),
        seam.Column("action", seam.Text, nullable=False),
        seam.PrimaryKeyConstraint(
            "install_id", "principal_id", "company_id", "resource_kind",
            "resource_id", "action"),
    )
    seam.provision(meta, db_path)
    before = _counts(conn, db_path)
    with pytest.raises(RuntimeError) as excinfo:
        _run(db_path)
    assert str(excinfo.value) == \
        "authority core does not match its profile: STRUCTURE"
    assert "authority_install" in seam.table_names(db_path)
    assert _count(conn, "authority_install") == 0
    for table in _CORE:
        if table == "authority_install":
            continue
        assert table in seam.table_names(db_path)
        assert _count(conn, table) == 0
    after = _counts(conn, db_path)
    for name, count in before.items():
        if name == "authority_install":
            continue
        assert after[name] == count


def test_install_without_its_unique_refuses(db_path, conn):
    _make_pre040(conn)
    meta = seam.MetaData()
    seam.Table(
        "authority_install", meta,
        seam.Column("singleton", seam.Integer, nullable=False,
                    primary_key=True),
        seam.Column("install_id", seam.Text, nullable=False),
        seam.Column("schema_version", seam.Integer, nullable=False),
        seam.Column("phase", seam.Text, nullable=False),
        seam.Column("revision", seam.Integer, nullable=False),
        seam.CheckConstraint("singleton = 1"),
        seam.CheckConstraint("schema_version = 1"),
        seam.CheckConstraint("phase IN ('STAGED', 'ACTIVE')"),
        seam.CheckConstraint("revision >= 0"),
    )
    seam.provision(meta, db_path)
    with pytest.raises(RuntimeError) as excinfo:
        _run(db_path)
    assert str(excinfo.value) == \
        "authority core does not match its profile: STRUCTURE"
    assert _count(conn, "authority_install") == 0


def test_membership_without_its_foreign_key_refuses(db_path, conn):
    _make_pre040(conn)
    meta = seam.MetaData()
    seam.Table(
        "authority_membership", meta,
        seam.Column("install_id", seam.Text, nullable=False),
        seam.Column("principal_id", seam.Text, nullable=False),
        seam.Column("company_id", seam.Text, nullable=False),
        seam.Column("effect", seam.Text, nullable=False),
        seam.PrimaryKeyConstraint(
            "install_id", "principal_id", "company_id", "effect"),
        seam.CheckConstraint("effect IN ('allow', 'deny')"),
    )
    seam.provision(meta, db_path)
    with pytest.raises(RuntimeError) as excinfo:
        _run(db_path)
    assert str(excinfo.value) == \
        "authority core does not match its profile: STRUCTURE"
    assert _count(conn, "authority_install") == 0


def test_status_error_when_the_inspector_reports_unsupported(
        db_path, monkeypatch):
    monkeypatch.setattr(
        seam, "_AUTHORITY_CORE_SQLITE_VERSION", sqlite3.sqlite_version)

    def _unsupported(conn, **kwargs):
        raise RuntimeError("AUTHORITY_CORE_UNSUPPORTED")

    monkeypatch.setattr(seam, "inspect_authority_core", _unsupported)
    probe = get_connection(db_path)
    try:
        result = _status(probe)
        settled = probe.in_transaction
    finally:
        probe.close()
    assert result["authority_core"] == {
        "profile": _PROFILE, "status": "ERROR",
        "phase": None, "reason": "AUTHORITY_CORE_UNSUPPORTED"}
    assert settled is False


def test_status_error_when_the_inspector_raises_unexpected(
        db_path, monkeypatch):
    monkeypatch.setattr(
        seam, "_AUTHORITY_CORE_SQLITE_VERSION", sqlite3.sqlite_version)

    def _boom(conn, **kwargs):
        raise KeyError("x")

    monkeypatch.setattr(seam, "inspect_authority_core", _boom)
    probe = get_connection(db_path)
    try:
        result = _status(probe)
        settled = probe.in_transaction
    finally:
        probe.close()
    assert result["authority_core"] == {
        "profile": _PROFILE, "status": "ERROR",
        "phase": None, "reason": "KeyError"}
    assert settled is False


def test_status_unsupported_on_another_sqlite_build(db_path, monkeypatch):
    monkeypatch.setattr(seam, "_AUTHORITY_CORE_SQLITE_VERSION", "0.0.0")
    probe = get_connection(db_path)
    try:
        result = _status(probe)
        settled = probe.in_transaction
    finally:
        probe.close()
    assert result["authority_core"] == {
        "profile": _PROFILE, "status": "UNSUPPORTED",
        "phase": None, "reason": "SQLITE_VERSION"}
    assert settled is False


def test_status_reports_install_when_the_id_is_invalid(db_path, conn):
    if sqlite3.sqlite_version != seam._AUTHORITY_CORE_SQLITE_VERSION:
        pytest.skip("inspector frozen to %s, host runs %s" % (
            seam._AUTHORITY_CORE_SQLITE_VERSION, sqlite3.sqlite_version))
    sql, params = dynamic_update(
        "authority_install", {"install_id": "bad id!"}, {"singleton": 1})
    conn.execute(sql, params)
    conn.commit()
    probe = get_connection(db_path)
    try:
        result = _status(probe)
    finally:
        probe.close()
    assert result["authority_core"] == {
        "profile": _PROFILE, "status": "MISMATCH",
        "phase": None, "reason": "INSTALL"}


def test_declares_no_data_change():
    assert _mig().MIGRATION_DATA_CLASS == "none"


_PG_URL = os.environ.get("ERPCLAW_PG_TEST_URL")


@pytest.mark.skipif(
    not _PG_URL,
    reason="ERPCLAW_PG_TEST_URL not set (live Postgres required; the PG lane "
           "runs on the box leg, plan §8.3)")
def test_pg_fresh_install_and_migration(monkeypatch):
    """Expendable database only: the shared public schema is reset, so never point ERPCLAW_PG_TEST_URL at real data."""
    from urllib.parse import urlparse
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", _PG_URL)
    monkeypatch.setenv("ERPCLAW_DB_PATH", _PG_URL)
    expected_db = urlparse(_PG_URL).path.strip("/")
    assert expected_db, (
        "refusing the reset: ERPCLAW_PG_TEST_URL names no database")
    probe = get_connection(_PG_URL)
    try:
        actual_db = probe.execute("SELECT current_database()").fetchone()[0]
        assert actual_db == expected_db, (
            "refusing the reset: connected to %r, URL names %r"
            % (actual_db, expected_db))
        probe.execute("DROP SCHEMA IF EXISTS public CASCADE")
        probe.execute("CREATE SCHEMA public")
        probe.commit()
    finally:
        probe.close()
    init_mod = _load("init_schema_pg", _INIT_SCHEMA_PATH)
    init_mod.init_db(None)
    for table in _CORE:
        assert table in seam.table_names(None)
    check = get_connection(None)
    try:
        rows = _install_rows(check)
    finally:
        check.close()
    assert len(rows) == 1
    buf = io.StringIO()
    with redirect_stdout(buf):
        migrated = _mig().run_migration(None)
    assert migrated["created"] == []
    assert migrated["install_seeded"] is False
    assert migrated["status"] == "CHECKED"
    pg_conn = get_connection(None)
    try:
        result = _status(pg_conn)
    finally:
        pg_conn.close()
    assert result["authority_core"] == {
        "profile": _PROFILE, "status": "UNSUPPORTED",
        "phase": None, "reason": "BACKEND"}
