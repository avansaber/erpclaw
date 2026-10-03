"""Migration 045: envelope storage lands on fresh and upgraded installs.

Fresh installs carry the three envelope tables and the two audit columns
from the start. Older installs gain them through the migration, which
rewrites nothing. Until the migration runs, every audit write keeps its
old shape and an envelope reference is refused rather than dropped.
"""
import importlib.util
import io
import os
import sys
from contextlib import redirect_stdout
from urllib.parse import urlparse

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SETUP_DIR = os.path.dirname(_TESTS_DIR)
_MIGRATION = os.path.join(_SETUP_DIR, "migrations",
                          "045_authorization_envelope.py")
_INIT_SCHEMA_PATH = os.path.join(_SETUP_DIR, "init_schema.py")

if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import setup_helpers  # noqa: E402  (binds erpclaw_lib to this tree)
from setup_helpers import read_all  # noqa: E402
from erpclaw_lib import seam  # noqa: E402  (after the lib binding)
from erpclaw_lib.db import get_connection  # noqa: E402
from erpclaw_lib.query import Field, P, Q, Table, fn  # noqa: E402

FIRST_COLS = ["id", "timestamp", "user_id", "skill", "action", "entity_type",
              "entity_id", "old_values", "new_values", "description"]
ACTOR_COLS = ["actor_os_account", "actor_channel", "actor_principal_claim",
              "actor_status", "actor_hop"]
AUTH_COLS = ["authorization_id", "authorization_status"]
SCOPE_COLS = ["scope_company_ids", "scope_status"]
FRESH_COLUMNS = FIRST_COLS + ACTOR_COLS + AUTH_COLS + SCOPE_COLS
# Re-upgraded order differs: the rewind drops only the authorization columns,
# so migration 045 re-adds them after the scope columns.
REUPGRADED_COLUMNS = FIRST_COLS + ACTOR_COLS + SCOPE_COLS + AUTH_COLS

_DROP_TABLES = (
    "DROP TABLE IF EXISTS operation_authorization_envelope",
    "DROP TABLE IF EXISTS operation_authorization_result",
    "DROP TABLE IF EXISTS authority_delegation_usage",
)
_DROP_COLUMNS = (
    "ALTER TABLE audit_log DROP COLUMN authorization_id",
    "ALTER TABLE audit_log DROP COLUMN authorization_status",
)


@pytest.fixture(autouse=True)
def _engines():
    yield
    seam.dispose_engines()


def _mig():
    spec = importlib.util.spec_from_file_location(
        "migration_045", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pre045(conn, db_path):
    """Rewind the database to its shape before the envelope storage existed."""
    from erpclaw_lib import seam as _seam
    for statement in _DROP_TABLES:
        conn.execute(statement)
    conn.commit()
    present = _seam.column_names("audit_log", db_path)
    for statement, name in zip(_DROP_COLUMNS, AUTH_COLS):
        if name in present:
            conn.execute(statement)
    conn.commit()
    assert all(name not in _seam.column_names("audit_log", db_path)
               for name in AUTH_COLS)


def _count(conn, table):
    probe = Table(table)
    query = Q.from_(probe).select(fn.Count("*").as_("cnt")).get_sql()
    return conn.execute(query).fetchone()["cnt"]


def _counts(conn, db_path):
    return {name: _count(conn, name)
            for name in seam.table_names(db_path)}


def _declared(db_path=None):
    meta = seam.authority_envelope_metadata()
    shapes = {}
    for name in ("operation_authorization_envelope",
                 "operation_authorization_result",
                 "authority_delegation_usage"):
        table = meta.tables[name]
        shapes[name] = (
            [(column.name, seam.declared_type(column, db_path), bool(column.nullable))
             for column in table.columns],
            sorted(column.name for column in table.primary_key.columns))
    return shapes


def _live(db_path, name):
    shape = seam.describe_table(name, db_path)
    return ([(column["name"], str(column["type"]).upper(),
              column["nullable"]) for column in shape["columns"]],
            sorted(shape["primary_key"]))


def _install_id(conn):
    probe = Table("authority_install")
    query = Q.from_(probe).select(Field("install_id")).get_sql()
    return conn.execute(query).fetchone()["install_id"]


def _insert(conn, table, columns, values):
    probe = Table(table)
    query = Q.into(probe).columns(*columns).insert(
        *[P() for _ in columns]).get_sql()
    conn.execute(query, values)


def _seed_principal_and_authorizations(conn, install_id):
    _insert(conn, "authority_principal",
            ("install_id", "id", "kind"), (install_id, "owner-1", "human"))
    for auth_id in ("auth-1", "auth-2"):
        _insert(conn, "operation_authorization",
                ("id", "install_id", "principal_id", "action",
                 "binding_digest", "issued_at", "expires_at"),
                (auth_id, install_id, "owner-1", "test-action",
                 "a" * 64, 100, 200))
    conn.commit()


def _envelope_values(auth_id, install_id, idem="idem-1"):
    return (auth_id, install_id, "owner-1", 2, "b" * 64, "owner-1",
            "delegation", "month-end-rent", "September rent", idem, None,
            "c" * 64)


_ENVELOPE_COLUMNS = ("authorization_id", "install_id", "principal_id",
                     "envelope_version", "args_digest", "issuer_id",
                     "issued_route", "reason_code", "reason_text",
                     "idempotency_key", "call_id", "envelope_digest")


def _audit_columns_case(target):
    from erpclaw_lib.audit import audit
    conn = get_connection(target)
    try:
        audit(conn, "probe-skill", "probe-action", "probe-type", "probe-1",
              authorization_id="auth-1", authorization_status="verified")
        conn.commit()
        rows = read_all(conn, "audit_log",
                        ["entity_id"] + AUTH_COLS)
        match = [row for row in rows if row["entity_id"] == "probe-1"]
        assert len(match) == 1
        assert match[0]["authorization_id"] == "auth-1"
        assert match[0]["authorization_status"] == "verified"
        audit(conn, "probe-skill", "probe-action", "probe-type", "probe-2")
        conn.commit()
        rows = read_all(conn, "audit_log",
                        ["entity_id"] + AUTH_COLS)
        plain = [row for row in rows if row["entity_id"] == "probe-2"]
        assert len(plain) == 1
        assert plain[0]["authorization_id"] is None
        assert plain[0]["authorization_status"] is None
        with pytest.raises(ValueError):
            audit(conn, "probe-skill", "probe-action", "probe-type",
                  "probe-3", authorization_status="bogus")
    finally:
        conn.close()


def _foreign_keys_case(target):
    from erpclaw_lib.db import integrity_error_types
    conn = get_connection(target)
    try:
        install_id = _install_id(conn)
        _seed_principal_and_authorizations(conn, install_id)
        try:
            _insert(conn, "operation_authorization_envelope",
                    _ENVELOPE_COLUMNS,
                    _envelope_values("auth-missing", install_id))
            conn.commit()
            raise AssertionError("missing authorization id was stored")
        except integrity_error_types():
            conn.rollback()
        _insert(conn, "operation_authorization_envelope",
                _ENVELOPE_COLUMNS,
                _envelope_values("auth-1", install_id, "idem-1"))
        conn.commit()
        try:
            _insert(conn, "operation_authorization_envelope",
                    _ENVELOPE_COLUMNS,
                    _envelope_values("auth-2", install_id, "idem-1"))
            conn.commit()
            raise AssertionError("duplicate idempotency key was stored")
        except integrity_error_types():
            conn.rollback()
        _insert(conn, "operation_authorization_envelope",
                _ENVELOPE_COLUMNS,
                _envelope_values("auth-2", install_id, "idem-2"))
        conn.commit()
        rows = read_all(conn, "operation_authorization_envelope",
                        ["authorization_id", "idempotency_key"])
        assert sorted(row["authorization_id"] for row in rows) == [
            "auth-1", "auth-2"]
    finally:
        conn.close()


# ── storage ───────────────────────────────────────────────────────────────

def test_fresh_install_has_the_storage(conn, db_path):
    for name in ("operation_authorization_envelope",
                 "operation_authorization_result",
                 "authority_delegation_usage"):
        assert name in seam.table_names(db_path)
        assert _count(conn, name) == 0
    assert seam.column_names("audit_log", db_path) == FRESH_COLUMNS
    declared = _declared(db_path)
    for name in declared:
        assert _live(db_path, name) == declared[name]


def test_upgrade_creates_and_changes_no_row(conn, db_path):
    _pre045(conn, db_path)
    before = _counts(conn, db_path)
    assert "operation_authorization_envelope" not in before
    mig = _mig()
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = mig.run_migration(db_path)
    assert result == {"created": list(seam._AUTHORITY_ENVELOPE_TABLES),
                      "added": list(AUTH_COLS), "report_only": False}
    after = _counts(conn, db_path)
    for name, total in before.items():
        assert after[name] == total
    for name in seam._AUTHORITY_ENVELOPE_TABLES:
        assert after[name] == 0
    declared = _declared(db_path)
    for name in declared:
        assert _live(db_path, name) == declared[name]
    assert seam.column_names("audit_log", db_path) == REUPGRADED_COLUMNS


def test_second_run_is_a_no_op(conn, db_path):
    _pre045(conn, db_path)
    mig = _mig()
    assert mig.run_migration(db_path) == {
        "created": list(seam._AUTHORITY_ENVELOPE_TABLES),
        "added": list(AUTH_COLS), "report_only": False}
    assert mig.run_migration(db_path) == {
        "created": [], "added": [], "report_only": False}


def test_report_only_writes_nothing(conn, db_path):
    _pre045(conn, db_path)
    mig = _mig()
    result = mig.run_migration(db_path, report_only=True)
    assert result == {"would_create": list(seam._AUTHORITY_ENVELOPE_TABLES),
                      "would_add": list(AUTH_COLS), "report_only": True}
    assert all(name not in seam.table_names(db_path)
               for name in seam._AUTHORITY_ENVELOPE_TABLES)
    assert all(name not in seam.column_names("audit_log", db_path)
               for name in AUTH_COLS)


def test_wrong_shape_table_refuses(conn, db_path):
    _pre045(conn, db_path)
    meta = seam.MetaData()
    seam.Table(
        "operation_authorization_result", meta,
        seam.Column("authorization_id", seam.Text, primary_key=True),
        seam.Column("consumed_txn", seam.Text, nullable=False),
        seam.Column("result_kind", seam.Text, nullable=False),
        seam.Column("result_status", seam.Text, nullable=False),
        seam.Column("recorded_at", seam.Integer, nullable=False),
    )
    seam.provision(meta, db_path)
    before = _counts(conn, db_path)
    with pytest.raises(RuntimeError) as excinfo:
        _mig().run_migration(db_path)
    assert "STRUCTURE" in str(excinfo.value)
    assert all(name not in seam.column_names("audit_log", db_path)
               for name in AUTH_COLS)
    after = _counts(conn, db_path)
    for name, total in before.items():
        assert after[name] == total


def test_refuses_without_the_authority_core(tmp_path):
    path = str(tmp_path / "empty.sqlite")
    with pytest.raises(RuntimeError) as excinfo:
        _mig().run_migration(path)
    assert "CORE_ABSENT" in str(excinfo.value)


def test_declares_no_data_change():
    mig = _mig()
    assert mig.MIGRATION_DATA_CLASS == "none"
    assert not hasattr(mig, "MIGRATION_DATA_EXEMPTIONS")


# ── audit trail ───────────────────────────────────────────────────────────

def test_pre045_audit_still_written(conn, db_path, capsys):
    _pre045(conn, db_path)
    from erpclaw_lib.audit import audit, audit_safe
    probe = get_connection(db_path)
    try:
        before = _count(probe, "audit_log")
        audit(probe, "probe-skill", "probe-action", "probe-type", "probe-1",
              old_values={"name": "before"}, new_values={"name": "after"},
              description="probe row")
        probe.commit()
        audit_safe(probe, "probe-skill", "probe-action", "probe-type",
                   "probe-2", description="safe row")
        probe.commit()
        rows = read_all(probe, "audit_log", FIRST_COLS)
        first = [row for row in rows if row["entity_id"] == "probe-1"][0]
        assert (first["skill"], first["action"], first["entity_type"],
                first["entity_id"], first["description"]) == (
            "probe-skill", "probe-action", "probe-type", "probe-1",
            "probe row")
        assert first["old_values"] == '{"name": "before"}'
        assert first["new_values"] == '{"name": "after"}'
        second = [row for row in rows if row["entity_id"] == "probe-2"][0]
        assert (second["skill"], second["action"],
                second["description"]) == (
            "probe-skill", "probe-action", "safe row")
        assert "WARN" not in capsys.readouterr().err
        with pytest.raises(ValueError) as excinfo:
            audit(probe, "probe-skill", "probe-action", "probe-type",
                  "probe-3", authorization_status="verified")
        assert excinfo.value.args == ("AUTHORIZATION_AUDIT_UNAVAILABLE",)
        assert _count(probe, "audit_log") == before + 2
    finally:
        probe.close()


def test_audit_writes_the_columns_when_present(db_path):
    _audit_columns_case(db_path)


def test_foreign_keys_hold(conn, db_path):
    _foreign_keys_case(db_path)


_PG_URL = os.environ.get("ERPCLAW_PG_TEST_URL")


@pytest.mark.skipif(
    not _PG_URL,
    reason="ERPCLAW_PG_TEST_URL not set (live Postgres required; the PG lane "
           "runs on the box leg)")
def test_pg_fresh_install_and_migration(monkeypatch):
    """Expendable database only: the shared public schema is reset, so never point ERPCLAW_PG_TEST_URL at real data."""
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", _PG_URL)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
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
    spec = importlib.util.spec_from_file_location(
        "init_schema_pg", _INIT_SCHEMA_PATH)
    init_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(init_mod)
    init_mod.init_db(None)
    for name in ("operation_authorization_envelope",
                 "operation_authorization_result",
                 "authority_delegation_usage"):
        assert name in seam.table_names(None)
    assert seam.column_names("audit_log", None) == FRESH_COLUMNS
    seam.dispose_engines()
    check = get_connection(None)
    try:
        for statement in _DROP_TABLES:
            check.execute(statement)
        check.commit()
        present = seam.column_names("audit_log", None)
        for statement, name in zip(_DROP_COLUMNS, AUTH_COLS):
            if name in present:
                check.execute(statement)
        check.commit()
    finally:
        check.close()
    seam.dispose_engines()
    mig = _mig()
    assert mig.run_migration(_PG_URL) == {
        "created": list(seam._AUTHORITY_ENVELOPE_TABLES),
        "added": list(AUTH_COLS), "report_only": False}
    assert mig.run_migration(_PG_URL) == {
        "created": [], "added": [], "report_only": False}
    _audit_columns_case(_PG_URL)
    _foreign_keys_case(_PG_URL)
