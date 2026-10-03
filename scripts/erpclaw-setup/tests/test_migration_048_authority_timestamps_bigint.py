"""Migration 048: authority timestamps hold real clock values everywhere.

The thirteen timestamp columns keep eight-byte millisecond values on a fresh
install. An older install carries narrow columns there, so a real clock value
fails to store. The migration widens exactly those columns and rewrites no
row. The stored SQLite text is unchanged throughout.
"""
import importlib.util
import json
import os
import sys
from urllib.parse import urlparse

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SETUP_DIR = os.path.dirname(_TESTS_DIR)
_MIGRATION = os.path.join(_SETUP_DIR, "migrations",
                          "048_authority_timestamps_bigint.py")
_REPO_ROOT = os.path.abspath(
    os.path.join(_TESTS_DIR, "..", "..", "..", "..", ".."))
_ARTIFACT = os.path.join(
    _REPO_ROOT, "testing", "unit", "L0",
    "fixtures", "m242_authority_core_schema_v1.json")

if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import setup_helpers  # noqa: E402  (binds erpclaw_lib to this tree)
from erpclaw_lib import seam  # noqa: E402  (after the lib binding)
from erpclaw_lib.authority_clock import now_ms  # noqa: E402
from erpclaw_lib.db import get_connection  # noqa: E402
from erpclaw_lib.query import Field, P, Q, Table  # noqa: E402

TIMESTAMP_COLUMNS = (
    ("authority_principal", "disabled_at"),
    ("authority_delegation", "issued_at"),
    ("authority_delegation", "expires_at"),
    ("authority_delegation", "revoked_at"),
    ("authority_delegation_cap", "window_start"),
    ("authority_delegation_cap", "window_end"),
    ("operation_authorization", "issued_at"),
    ("operation_authorization", "expires_at"),
    ("operation_authorization", "revoked_at"),
    ("operation_authorization", "consumed_at"),
    ("operation_authorization_result", "recorded_at"),
    ("authority_delegation_usage", "window_start"),
    ("authority_delegation_usage", "window_end"),
)

SMALL_COLUMNS = (
    ("authority_install", "singleton"),
    ("authority_install", "schema_version"),
    ("authority_install", "revision"),
    ("authority_delegation_cap", "scale"),
    ("operation_authorization_envelope", "envelope_version"),
)

_NARROW_AUTHORITY_PRINCIPAL_DISABLED_AT = "ALTER TABLE authority_principal ALTER COLUMN disabled_at TYPE INTEGER"
_NARROW_AUTHORITY_DELEGATION_ISSUED_AT = "ALTER TABLE authority_delegation ALTER COLUMN issued_at TYPE INTEGER"
_NARROW_AUTHORITY_DELEGATION_EXPIRES_AT = "ALTER TABLE authority_delegation ALTER COLUMN expires_at TYPE INTEGER"
_NARROW_AUTHORITY_DELEGATION_REVOKED_AT = "ALTER TABLE authority_delegation ALTER COLUMN revoked_at TYPE INTEGER"
_NARROW_AUTHORITY_DELEGATION_CAP_WINDOW_START = "ALTER TABLE authority_delegation_cap ALTER COLUMN window_start TYPE INTEGER"
_NARROW_AUTHORITY_DELEGATION_CAP_WINDOW_END = "ALTER TABLE authority_delegation_cap ALTER COLUMN window_end TYPE INTEGER"
_NARROW_OPERATION_AUTHORIZATION_ISSUED_AT = "ALTER TABLE operation_authorization ALTER COLUMN issued_at TYPE INTEGER"
_NARROW_OPERATION_AUTHORIZATION_EXPIRES_AT = "ALTER TABLE operation_authorization ALTER COLUMN expires_at TYPE INTEGER"
_NARROW_OPERATION_AUTHORIZATION_REVOKED_AT = "ALTER TABLE operation_authorization ALTER COLUMN revoked_at TYPE INTEGER"
_NARROW_OPERATION_AUTHORIZATION_CONSUMED_AT = "ALTER TABLE operation_authorization ALTER COLUMN consumed_at TYPE INTEGER"
_NARROW_OPERATION_AUTHORIZATION_RESULT_RECORDED_AT = "ALTER TABLE operation_authorization_result ALTER COLUMN recorded_at TYPE INTEGER"
_NARROW_AUTHORITY_DELEGATION_USAGE_WINDOW_START = "ALTER TABLE authority_delegation_usage ALTER COLUMN window_start TYPE INTEGER"
_NARROW_AUTHORITY_DELEGATION_USAGE_WINDOW_END = "ALTER TABLE authority_delegation_usage ALTER COLUMN window_end TYPE INTEGER"

NARROW_STATEMENTS = (
    _NARROW_AUTHORITY_PRINCIPAL_DISABLED_AT,
    _NARROW_AUTHORITY_DELEGATION_ISSUED_AT,
    _NARROW_AUTHORITY_DELEGATION_EXPIRES_AT,
    _NARROW_AUTHORITY_DELEGATION_REVOKED_AT,
    _NARROW_AUTHORITY_DELEGATION_CAP_WINDOW_START,
    _NARROW_AUTHORITY_DELEGATION_CAP_WINDOW_END,
    _NARROW_OPERATION_AUTHORIZATION_ISSUED_AT,
    _NARROW_OPERATION_AUTHORIZATION_EXPIRES_AT,
    _NARROW_OPERATION_AUTHORIZATION_REVOKED_AT,
    _NARROW_OPERATION_AUTHORIZATION_CONSUMED_AT,
    _NARROW_OPERATION_AUTHORIZATION_RESULT_RECORDED_AT,
    _NARROW_AUTHORITY_DELEGATION_USAGE_WINDOW_START,
    _NARROW_AUTHORITY_DELEGATION_USAGE_WINDOW_END,
)

WANT_ALTERED = ["%s.%s" % pair for pair in TIMESTAMP_COLUMNS]


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "migration_048", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _artifact_captured():
    with open(_ARTIFACT, encoding="utf-8") as handle:
        artifact = json.load(handle)
    return tuple(tuple(entry) for entry in artifact["captured"])


def _evidence(path):
    conn = get_connection(path)
    try:
        conn.execute("BEGIN")
        try:
            return seam.authority_core_schema_evidence(conn)
        finally:
            conn.rollback()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _engines():
    yield
    seam.dispose_engines()


def test_sqlite_text_unchanged_and_migration_is_a_no_op(tmp_path):
    path = str(tmp_path / "ts.sqlite")
    seam.provision_authority_core(path)
    assert _evidence(path) == _artifact_captured()
    mig = _load_migration()
    assert mig.run_migration(path) == {
        "altered": [], "report_only": False}
    assert _evidence(path) == _artifact_captured()
    assert mig.run_migration(path, report_only=True) == {
        "altered": [], "report_only": True}
    assert _evidence(path) == _artifact_captured()
    assert mig.run_migration(path) == {
        "altered": [], "report_only": False}
    assert _evidence(path) == _artifact_captured()


def test_declared_types_per_backend(tmp_path, monkeypatch):
    from erpclaw_lib.db import get_dialect as _dialect
    assert _dialect() == "sqlite"
    sqlite_target = str(tmp_path / "ts-types.sqlite")
    meta = seam.authority_envelope_metadata()
    for table, column in TIMESTAMP_COLUMNS:
        assert seam.declared_type(
            meta.tables[table].columns[column],
            sqlite_target) == "INTEGER"
    for table, column in SMALL_COLUMNS:
        assert seam.declared_type(
            meta.tables[table].columns[column],
            sqlite_target) == "INTEGER"
    pg_url = os.environ.get("ERPCLAW_PG_TEST_URL")
    if not pg_url:
        pytest.skip("ERPCLAW_PG_TEST_URL not set (no live server needed)")
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", pg_url)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    for table, column in TIMESTAMP_COLUMNS:
        assert seam.declared_type(
            meta.tables[table].columns[column], pg_url) == "BIGINT"
    for table, column in SMALL_COLUMNS:
        assert seam.declared_type(
            meta.tables[table].columns[column], pg_url) == "INTEGER"


def test_migration_declaration():
    mig = _load_migration()
    assert mig.MIGRATION_DATA_CLASS == "none"
    assert len(mig.ALTER_STATEMENTS) == 13
    assert [key for key in mig.ALTER_STATEMENTS] == list(TIMESTAMP_COLUMNS)
    for statement in mig.ALTER_STATEMENTS.values():
        assert statement.startswith("ALTER TABLE")
        assert statement.endswith("TYPE BIGINT")


def test_unexpected_shape_refuses_before_any_write(monkeypatch):
    mig = _load_migration()
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", "postgresql://refused/ts")
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    monkeypatch.setattr(
        seam, "table_exists",
        lambda name, path=None: name == "authority_principal")
    monkeypatch.setattr(
        seam, "describe_table",
        lambda name, path=None: {
            "columns": [{"name": "disabled_at", "type": "TEXT",
                         "nullable": True}],
            "primary_key": [], "indexes": []})
    with pytest.raises(RuntimeError) as excinfo:
        mig.run_migration("postgresql://refused/ts")
    assert str(excinfo.value) == "authority timestamps: STRUCTURE"


_PG_URL = os.environ.get("ERPCLAW_PG_TEST_URL")


def _pg_reset_schema(monkeypatch):
    from erpclaw_lib.db import _resolve_pg_url
    expected_db = urlparse(_PG_URL).path.strip("/")
    assert expected_db, (
        "refusing the reset: ERPCLAW_PG_TEST_URL names no database")
    probe = get_connection(_PG_URL)
    try:
        actual_db = probe.execute("SELECT current_database()").fetchone()[0]
        assert actual_db == expected_db, (
            "refusing the reset: connected to %r, URL names %r"
            % (actual_db, expected_db))
        assert _resolve_pg_url(None) == _PG_URL
        probe.execute("DROP SCHEMA IF EXISTS public CASCADE")
        probe.execute("CREATE SCHEMA public")
        probe.commit()
    finally:
        probe.close()


def _pg_live_type(target, table, column):
    shape = seam.describe_table(table, target)
    return next(entry["type"] for entry in shape["columns"]
                if entry["name"] == column)


def _insert(target, table, columns, values):
    probe = Table(table)
    query = Q.into(probe).columns(*columns).insert(
        *[P() for _ in columns]).get_sql()
    conn = get_connection(target)
    try:
        conn.execute(query, values)
        conn.commit()
    finally:
        conn.close()


def _row(target, table, columns, name, value):
    probe = Table(table)
    query = Q.from_(probe).select(
        *[Field(column) for column in columns]).where(
        Field(name) == P()).get_sql()
    conn = get_connection(target)
    try:
        return dict(conn.execute(query, (value,)).fetchone())
    finally:
        conn.close()


def _install_id(target):
    probe = Table("authority_install")
    query = Q.from_(probe).select(Field("install_id")).get_sql()
    conn = get_connection(target)
    try:
        return conn.execute(query).fetchone()["install_id"]
    finally:
        conn.close()


@pytest.mark.skipif(
    not _PG_URL,
    reason="ERPCLAW_PG_TEST_URL not set (live PostgreSQL required)")
def test_pg_narrowed_install_widens_and_holds_real_clock(monkeypatch, capsys):
    """Expendable database only."""
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", _PG_URL)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    target = _PG_URL
    _pg_reset_schema(monkeypatch)
    seam.provision_authority_core(target)
    seam.provision_authority_envelope(target)
    conn = get_connection(target)
    try:
        for statement in NARROW_STATEMENTS:
            conn.execute(statement)
        conn.commit()
    finally:
        conn.close()
    assert [_pg_live_type(target, table, column)
            for table, column in TIMESTAMP_COLUMNS] == ["INTEGER"] * 13
    install_id = _install_id(target)
    probe = Table("authority_principal")
    query = Q.into(probe).columns(
        "install_id", "id", "kind", "disabled_at").insert(
        P(), P(), P(), P()).get_sql()
    conn = get_connection(target)
    try:
        with pytest.raises(Exception) as excinfo:
            conn.execute(query, (install_id, "overflow-1", "service",
                                 1_790_000_000_000))
            conn.commit()
    finally:
        conn.rollback()
        conn.close()
    assert "out of range" in str(excinfo.value)
    capsys.readouterr()
    mig = _load_migration()
    assert mig.run_migration(target, report_only=True) == {
        "would_alter": WANT_ALTERED, "report_only": True}
    assert [_pg_live_type(target, table, column)
            for table, column in TIMESTAMP_COLUMNS] == ["INTEGER"] * 13
    assert mig.run_migration(target) == {
        "altered": WANT_ALTERED, "report_only": False}
    assert [_pg_live_type(target, table, column)
            for table, column in TIMESTAMP_COLUMNS] == ["BIGINT"] * 13
    now = now_ms()
    issued = now - 3_600_000
    expires = now + 864_000_000
    _insert(target, "authority_principal",
            ("install_id", "id", "kind", "disabled_at"),
            (install_id, "ts-owner", "human", None))
    _insert(target, "authority_principal",
            ("install_id", "id", "kind", "disabled_at"),
            (install_id, "ts-service", "service", 1_790_000_000_000))
    _insert(target, "authority_delegation",
            ("install_id", "id", "issuer_id", "grantee_id",
             "issued_at", "expires_at", "revoked_at"),
            (install_id, "ts-delegation", "ts-owner", "ts-service",
             issued, expires, None))
    _insert(target, "authority_delegation_cap",
            ("install_id", "delegation_id", "action", "currency", "scale",
             "per_operation", "aggregate_limit", "window_start",
             "window_end"),
            (install_id, "ts-delegation", "add-uom", "USD", 2,
             "100.00", "60.00", now, now + 864_000_000))
    _insert(target, "operation_authorization",
            ("id", "install_id", "principal_id", "action",
             "binding_digest", "delegation_id", "issued_at", "expires_at",
             "revoked_at", "consumed_at", "consumed_txn"),
            ("ts-auth-1", install_id, "ts-service", "add-uom", "d" * 64,
             "ts-delegation", now, now + 3_600_000, None, None, None))
    _insert(target, "operation_authorization_result",
            ("authorization_id", "consumed_txn", "result_kind",
             "result_status", "result_id", "recorded_at"),
            ("ts-auth-1", "ts-txn-1", "uom", "created", "uom-1", now))
    assert _row(target, "authority_principal",
                ("id", "disabled_at"), "id", "ts-service") == {
        "id": "ts-service", "disabled_at": 1_790_000_000_000}
    assert _row(target, "authority_delegation",
                ("issued_at", "expires_at"), "id",
                "ts-delegation") == {
        "issued_at": issued, "expires_at": expires}
    assert _row(target, "authority_delegation_cap",
                ("window_start", "window_end"), "delegation_id",
                "ts-delegation") == {
        "window_start": now, "window_end": now + 864_000_000}
    assert _row(target, "operation_authorization",
                ("issued_at", "expires_at"), "id", "ts-auth-1") == {
        "issued_at": now, "expires_at": now + 3_600_000}
    assert _row(target, "operation_authorization_result",
                ("recorded_at",), "authorization_id", "ts-auth-1") == {
        "recorded_at": now}
    assert mig.run_migration(target) == {
        "altered": [], "report_only": False}


@pytest.mark.skipif(
    not _PG_URL,
    reason="ERPCLAW_PG_TEST_URL not set (live PostgreSQL required)")
def test_pg_fresh_provision_needs_no_widening(monkeypatch):
    """Expendable database only."""
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", _PG_URL)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    target = _PG_URL
    _pg_reset_schema(monkeypatch)
    seam.provision_authority_core(target)
    seam.provision_authority_envelope(target)
    mig = _load_migration()
    assert mig.run_migration(target) == {
        "altered": [], "report_only": False}
    assert [_pg_live_type(target, table, column)
            for table, column in TIMESTAMP_COLUMNS] == ["BIGINT"] * 13
