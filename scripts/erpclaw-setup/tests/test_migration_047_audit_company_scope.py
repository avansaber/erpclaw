"""Migration 047: the audit table gains company scope columns; no write needs them.

Fresh installs carry the two columns from the start. Older installs gain them
through the migration, which rewrites nothing. Until the migration runs, every
write keeps its old shape and a scope verdict is refused rather than dropped.
"""
import importlib.util
import os
import sys
from urllib.parse import urlparse

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SETUP_DIR = os.path.dirname(_TESTS_DIR)
_MIGRATION = os.path.join(_SETUP_DIR, "migrations",
                          "047_audit_company_scope.py")
_INIT_SCHEMA_PATH = os.path.join(_SETUP_DIR, "init_schema.py")

if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import setup_helpers  # noqa: E402  (binds erpclaw_lib to this tree)
from setup_helpers import read_all  # noqa: E402
from erpclaw_lib import seam  # noqa: E402  (after the lib binding)
from erpclaw_lib.db import get_connection  # noqa: E402

FIRST_COLS = ["id", "timestamp", "user_id", "skill", "action", "entity_type",
              "entity_id", "old_values", "new_values", "description"]
ACTOR_COLS = ["actor_os_account", "actor_channel", "actor_principal_claim",
              "actor_status", "actor_hop"]
AUTH_COLS = ["authorization_id", "authorization_status"]
SCOPE_COLS = ["scope_company_ids", "scope_status"]
FRESH_COLUMNS = FIRST_COLS + ACTOR_COLS + AUTH_COLS + SCOPE_COLS

_DROP_STATEMENTS = (
    "ALTER TABLE audit_log DROP COLUMN scope_company_ids",
    "ALTER TABLE audit_log DROP COLUMN scope_status",
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "migration_047", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _engines():
    yield
    seam.dispose_engines()


@pytest.fixture
def pre047(conn, db_path):
    """Rewind audit_log to its shape before the scope columns existed."""
    present = seam.column_names("audit_log", db_path)
    for statement in _DROP_STATEMENTS:
        name = statement.rsplit(" ", 1)[-1]
        if name in present:
            conn.execute(statement)
    conn.commit()
    assert all(name not in seam.column_names("audit_log", db_path)
               for name in SCOPE_COLS)
    return db_path


def _counts(conn, db_path):
    return {name: len(read_all(conn, name, seam.column_names(name, db_path)))
            for name in seam.table_names(db_path)}


def _scope_columns_case(target):
    from erpclaw_lib.audit import audit
    conn = get_connection(target)
    try:
        before = len(read_all(conn, "audit_log", ["id"]))
        audit(conn, "probe-skill", "probe-action", "probe-type", "scope-1",
              scope_company_ids=["c-2", "c-1", "c-2"],
              scope_status="out_of_scope")
        conn.commit()
        audit(conn, "probe-skill", "probe-action", "probe-type", "scope-2",
              scope_company_ids=[], scope_status="underived")
        conn.commit()
        audit(conn, "probe-skill", "probe-action", "probe-type", "scope-3")
        conn.commit()
        audit(conn, "probe-skill", "probe-action", "probe-type", "scope-4",
              authorization_id="auth-1", authorization_status="verified",
              scope_company_ids=["c-3"], scope_status="in_scope")
        conn.commit()
        rows = {row["entity_id"]: row for row in read_all(
            conn, "audit_log", ["entity_id"] + AUTH_COLS + SCOPE_COLS)}
        assert rows["scope-1"]["scope_company_ids"] == "c-1,c-2"
        assert rows["scope-1"]["scope_status"] == "out_of_scope"
        assert rows["scope-2"]["scope_company_ids"] is None
        assert rows["scope-2"]["scope_status"] == "underived"
        assert rows["scope-3"]["scope_company_ids"] is None
        assert rows["scope-3"]["scope_status"] is None
        assert rows["scope-4"]["authorization_id"] == "auth-1"
        assert rows["scope-4"]["authorization_status"] == "verified"
        assert rows["scope-4"]["scope_company_ids"] == "c-3"
        assert rows["scope-4"]["scope_status"] == "in_scope"
        mid = len(read_all(conn, "audit_log", ["id"]))
        assert mid == before + 4
        bad = [
            ({"scope_status": "bogus"}, "SCOPE_STATUS_INVALID"),
            ({"scope_company_ids": "c-1"}, "SCOPE_COMPANY_IDS_INVALID"),
            ({"scope_company_ids": ["a,b"]}, "SCOPE_COMPANY_IDS_INVALID"),
            ({"scope_company_ids": [""]}, "SCOPE_COMPANY_IDS_INVALID"),
            ({"scope_company_ids": [1]}, "SCOPE_COMPANY_IDS_INVALID"),
            ({"scope_company_ids": ["c-1"]}, "SCOPE_STATUS_INVALID"),
            ({"scope_company_ids": None, "scope_status": "in_scope"},
             "SCOPE_COMPANY_IDS_INVALID"),
            ({"scope_company_ids": [], "scope_status": "out_of_scope"},
             "SCOPE_COMPANY_IDS_INVALID"),
        ]
        for kwargs, code in bad:
            with pytest.raises(ValueError) as excinfo:
                audit(conn, "probe-skill", "probe-action", "probe-type",
                      "scope-bad", **kwargs)
            assert excinfo.value.args == (code,)
        assert len(read_all(conn, "audit_log", ["id"])) == mid
    finally:
        conn.close()


# ── the migration ─────────────────────────────────────────────────────────

def test_fresh_install_has_the_scope_columns(db_path):
    assert seam.column_names("audit_log", db_path) == FRESH_COLUMNS


def test_upgrade_adds_the_columns_and_changes_no_row(conn, db_path, pre047):
    from erpclaw_lib.audit import audit
    for entity_id in ("S1", "S2"):
        audit(conn, "probe-skill", "probe-action", "probe-type", entity_id,
              old_values={"name": "before"}, new_values={"name": "after"},
              description="probe row")
    conn.commit()
    before_counts = _counts(conn, db_path)
    assert before_counts["audit_log"] >= 2
    before = [row for row in read_all(conn, "audit_log", FIRST_COLS)
              if row["entity_id"] in ("S1", "S2")]
    assert len(before) == 2
    mig = _load_migration()
    result = mig.run_migration(db_path)
    assert result == {"added": list(mig.SCOPE_COLUMNS), "report_only": False}
    assert seam.column_names("audit_log", db_path) == FRESH_COLUMNS
    assert _counts(conn, db_path) == before_counts
    after = [row for row in read_all(conn, "audit_log", FRESH_COLUMNS)
             if row["entity_id"] in ("S1", "S2")]
    assert len(after) == 2
    for row in after:
        match = [item for item in before if item["id"] == row["id"]][0]
        for column in FIRST_COLS:
            assert row[column] == match[column]
        assert row["scope_company_ids"] is None
        assert row["scope_status"] is None


def test_second_run_is_a_no_op(conn, db_path, pre047):
    mig = _load_migration()
    assert mig.run_migration(db_path) == {
        "added": list(mig.SCOPE_COLUMNS), "report_only": False}
    assert mig.run_migration(db_path) == {"added": [], "report_only": False}


def test_report_only_writes_nothing(conn, db_path, pre047):
    mig = _load_migration()
    before = _counts(conn, db_path)
    result = mig.run_migration(db_path, report_only=True)
    assert result == {"would_add": list(mig.SCOPE_COLUMNS),
                      "report_only": True}
    assert all(name not in seam.column_names("audit_log", db_path)
               for name in SCOPE_COLS)
    assert _counts(conn, db_path) == before


def test_absent_table(tmp_path):
    mig = _load_migration()
    empty = str(tmp_path / "empty.sqlite")
    with open(empty, "wb"):
        pass
    assert mig.run_migration(empty) == {
        "added": [], "report_only": False, "reason": "table absent"}


def test_declares_no_data_change():
    mig = _load_migration()
    assert mig.MIGRATION_DATA_CLASS == "none"
    assert not hasattr(mig, "MIGRATION_DATA_EXEMPTIONS")
    assert mig._ADD_STATEMENTS == {
        "scope_company_ids":
            "ALTER TABLE audit_log ADD COLUMN scope_company_ids TEXT",
        "scope_status":
            "ALTER TABLE audit_log ADD COLUMN scope_status TEXT",
    }


# ── no write depends on the migration ─────────────────────────────────────

def test_pre047_audit_still_written(conn, db_path, pre047, capsys):
    from erpclaw_lib.audit import audit, audit_safe
    capsys.readouterr()
    probe = get_connection(db_path)
    try:
        before = len(read_all(probe, "audit_log", ["id"]))
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
        audit(probe, "probe-skill", "probe-action", "probe-type", "probe-3",
              authorization_id="auth-1", authorization_status="verified")
        probe.commit()
        wide = read_all(probe, "audit_log", FIRST_COLS + AUTH_COLS)
        third = [row for row in wide if row["entity_id"] == "probe-3"][0]
        assert third["authorization_id"] == "auth-1"
        assert third["authorization_status"] == "verified"
        with pytest.raises(ValueError) as excinfo:
            audit(probe, "probe-skill", "probe-action", "probe-type",
                  "probe-4", scope_status="underived")
        assert excinfo.value.args == ("SCOPE_AUDIT_UNAVAILABLE",)
        with pytest.raises(ValueError) as excinfo:
            audit(probe, "probe-skill", "probe-action", "probe-type",
                  "probe-5", scope_company_ids=["c-1"],
                  scope_status="in_scope")
        assert excinfo.value.args == ("SCOPE_AUDIT_UNAVAILABLE",)
        capsys.readouterr()
        audit_safe(probe, "probe-skill", "probe-action", "probe-type",
                   "probe-6", scope_company_ids=["c-1"],
                   scope_status="in_scope")
        probe.commit()
        warned = [line for line in capsys.readouterr().err.splitlines()
                  if line.startswith("WARN:")]
        assert len(warned) == 1
        assert len(read_all(probe, "audit_log", ["id"])) == before + 4
        sixth = [row for row in read_all(probe, "audit_log", FIRST_COLS)
                 if row["entity_id"] == "probe-6"]
        assert len(sixth) == 1
    finally:
        probe.close()


def test_audit_writes_the_scope_columns_when_present(db_path):
    _scope_columns_case(db_path)


def test_statement_for_every_column_combination():
    from erpclaw_lib import audit as audit_mod
    from erpclaw_lib.audit import migration_audit_statement
    scope_names = {
        "_INSERT_AUDIT_LOG_WITH_SCOPE",
        "_INSERT_AUDIT_LOG_WITH_ACTOR_AND_SCOPE",
        "_INSERT_AUDIT_LOG_WITH_AUTHORIZATION_AND_SCOPE",
        "_INSERT_AUDIT_LOG_WITH_ACTOR_AUTHORIZATION_AND_SCOPE",
    }
    names = [
        "_INSERT_AUDIT_LOG",
        "_INSERT_AUDIT_LOG_WITH_ACTOR",
        "_INSERT_AUDIT_LOG_WITH_AUTHORIZATION",
        "_INSERT_AUDIT_LOG_WITH_SCOPE",
        "_INSERT_AUDIT_LOG_WITH_ACTOR_AND_AUTHORIZATION",
        "_INSERT_AUDIT_LOG_WITH_ACTOR_AND_SCOPE",
        "_INSERT_AUDIT_LOG_WITH_AUTHORIZATION_AND_SCOPE",
        "_INSERT_AUDIT_LOG_WITH_ACTOR_AUTHORIZATION_AND_SCOPE",
    ]
    for name in names:
        sql = getattr(audit_mod, name)
        head = sql.split("VALUES")[0]
        listed = [column.strip() for column in
                  head[head.index("(") + 1:head.rindex(")")].split(",")]
        assert len(listed) == sql.count("?")
        assert len(set(listed)) == len(listed)
        if name in scope_names:
            assert listed[-2:] == ["scope_company_ids", "scope_status"]
    sql, params = migration_audit_statement("047_x", "account", "A1")
    assert sql == audit_mod._INSERT_AUDIT_LOG
    assert len(params) == 9


def test_scope_probe_decided_once_per_connection(db_path, monkeypatch):
    from erpclaw_lib import audit as audit_mod
    from erpclaw_lib.audit import audit
    calls = []
    real_probe = audit_mod._probe_scope_columns

    def _counting(handle):
        calls.append(handle)
        return real_probe(handle)

    monkeypatch.setattr(audit_mod, "_probe_scope_columns", _counting)
    writer = get_connection(db_path)
    try:
        audit(writer, "probe-skill", "probe-action", "probe-type", "Z1")
        writer.commit()
        audit(writer, "probe-skill", "probe-action", "probe-type", "Z2")
        writer.commit()
    finally:
        writer.close()
    assert len(calls) == 1 and calls[0] is writer
    assert audit_mod._has_scope_columns(writer) is True
    from erpclaw_lib.query import P, insert_row
    writer = get_connection(db_path)
    try:
        sql, _cols = insert_row(
            "company", {"id": P(), "name": P(), "abbr": P()})
        writer.execute(sql, ("scope-co-1", "Scope Co", "SCP"))
        audit(writer, "probe-skill", "probe-action", "probe-type",
              "scope-co-1", scope_company_ids=["scope-co-1"],
              scope_status="in_scope")
        writer.rollback()
    finally:
        writer.close()
    reader = get_connection(db_path)
    try:
        assert [row for row in read_all(reader, "company", ["id"])
                if row["id"] == "scope-co-1"] == []
        assert [row for row in read_all(reader, "audit_log", ["entity_id"])
                if row["entity_id"] == "scope-co-1"] == []
    finally:
        reader.close()


_PG_URL = os.environ.get("ERPCLAW_PG_TEST_URL")


@pytest.mark.skipif(
    not _PG_URL,
    reason="ERPCLAW_PG_TEST_URL not set (live PostgreSQL required)")
def test_pg_fresh_install_and_migration(monkeypatch):
    """Expendable database only."""
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
    assert seam.column_names("audit_log", None) == FRESH_COLUMNS
    seam.dispose_engines()
    check = get_connection(None)
    try:
        present = seam.column_names("audit_log", None)
        for statement, name in zip(_DROP_STATEMENTS, SCOPE_COLS):
            if name in present:
                check.execute(statement)
        check.commit()
    finally:
        check.close()
    seam.dispose_engines()
    mig = _load_migration()
    assert mig.run_migration(_PG_URL) == {
        "added": list(mig.SCOPE_COLUMNS), "report_only": False}
    assert mig.run_migration(_PG_URL) == {"added": [], "report_only": False}
    _scope_columns_case(_PG_URL)
