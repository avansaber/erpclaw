"""Migration 041: the audit table gains actor columns; no write needs them.

Fresh installs carry the five columns from the start. Older installs gain them
through the migration, which rewrites nothing. Until the migration runs, every
write keeps its nine-column shape on both old and new code.
"""
import importlib.util
import os
import pwd
import sys

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SETUP_DIR = os.path.dirname(_TESTS_DIR)
_MIGRATION = os.path.join(_SETUP_DIR, "migrations",
                          "041_audit_actor_columns.py")

if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import setup_helpers  # noqa: E402  (binds erpclaw_lib to this tree)
from setup_helpers import init_all_tables, open_reader, read_all  # noqa: E402
from erpclaw_lib import seam  # noqa: E402  (after the lib binding)


def _account():
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return "uid:%d" % os.geteuid()


ACCOUNT = _account()
VAR = "ERPCLAW_ACTOR_CONTEXT"
ACTOR_COLS = ["actor_os_account", "actor_channel", "actor_principal_claim",
              "actor_status", "actor_hop"]
FIRST_COLS = ["id", "timestamp", "user_id", "skill", "action", "entity_type",
              "entity_id", "old_values", "new_values", "description"]
FRESH_COLUMNS = FIRST_COLS + ACTOR_COLS + ["authorization_id",
               "authorization_status", "scope_company_ids", "scope_status",
               "actor_session_digest"]
# Re-upgraded order differs: the fixture drops only the actor columns,
# so migration 041 re-adds them after the authorization and scope columns.
REUPGRADED_COLUMNS = FIRST_COLS + ["authorization_id",
                    "authorization_status", "scope_company_ids",
                    "scope_status", "actor_session_digest"] + ACTOR_COLS

_DROP_STATEMENTS = (
    "ALTER TABLE audit_log DROP COLUMN actor_os_account",
    "ALTER TABLE audit_log DROP COLUMN actor_channel",
    "ALTER TABLE audit_log DROP COLUMN actor_principal_claim",
    "ALTER TABLE audit_log DROP COLUMN actor_status",
    "ALTER TABLE audit_log DROP COLUMN actor_hop",
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "migration_041", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _fresh_actor():
    from erpclaw_lib import actor
    actor._reset_cache()
    yield
    actor._reset_cache()
    seam.dispose_engines()


@pytest.fixture
def pre041(conn, db_path):
    """Rewind audit_log to its shape before the actor columns existed."""
    present = seam.column_names("audit_log", db_path)
    for statement in _DROP_STATEMENTS:
        name = statement.rsplit(" ", 1)[-1]
        if name in present:
            conn.execute(statement)
    conn.commit()
    assert all(name not in seam.column_names("audit_log", db_path)
               for name in ACTOR_COLS)
    return db_path


def _counts(conn, db_path):
    return {name: len(read_all(conn, name, seam.column_names(name, db_path)))
            for name in seam.table_names(db_path)}


# ── E: the migration ────────────────────────────────────────────────────────

def test_fresh_install_has_the_actor_columns(db_path):
    assert seam.column_names("audit_log", db_path) == FRESH_COLUMNS


def test_migration_adds_the_columns_and_changes_no_row(conn, db_path, pre041):
    from erpclaw_lib.audit import migration_audit_statement
    mig = _load_migration()
    for entity_id in ("E1", "E2"):
        sql, params = migration_audit_statement(
            "041_probe", "account", entity_id,
            old_values={"name": "before"}, new_values={"name": "after"},
            description="probe row")
        conn.execute(sql, params)
    conn.commit()
    before_counts = _counts(conn, db_path)
    assert before_counts["audit_log"] >= 2
    before_first = read_all(conn, "audit_log", FIRST_COLS + ["entity_id"])
    before_first = [r for r in before_first
                    if r["entity_id"] in ("E1", "E2")]
    assert len(before_first) == 2
    result = mig.run_migration(db_path)
    assert result == {"added": list(mig.ACTOR_COLUMNS), "report_only": False}
    assert seam.column_names("audit_log", db_path) == REUPGRADED_COLUMNS
    assert _counts(conn, db_path) == before_counts
    after = read_all(conn, "audit_log", FRESH_COLUMNS)
    after = [r for r in after if r["entity_id"] in ("E1", "E2")]
    assert len(after) == 2
    for row in after:
        match = [r for r in before_first if r["id"] == row["id"]][0]
        for column in FIRST_COLS:
            assert row[column] == match[column]
        for column in ACTOR_COLS:
            assert row[column] is None


def test_second_run_is_a_no_op(conn, db_path, pre041):
    mig = _load_migration()
    assert mig.run_migration(db_path) == {
        "added": list(mig.ACTOR_COLUMNS), "report_only": False}
    assert mig.run_migration(db_path) == {"added": [], "report_only": False}


def test_report_only_writes_nothing(conn, db_path, pre041):
    mig = _load_migration()
    result = mig.run_migration(db_path, report_only=True)
    assert result == {"would_add": list(mig.ACTOR_COLUMNS),
                      "report_only": True}
    present = seam.column_names("audit_log", db_path)
    assert all(name not in present for name in ACTOR_COLS)


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


# ── F: no write depends on the migration ────────────────────────────────────

def test_f_pre041_audit_writes_nine_columns(conn, db_path, pre041, capsys):
    from erpclaw_lib import audit as audit_mod
    from erpclaw_lib.audit import (
        audit, audit_safe, migration_audit_statement)
    from erpclaw_lib.db import get_connection
    capsys.readouterr()
    before = len(read_all(conn, "audit_log", ["id"]))
    writer = get_connection(db_path)
    try:
        audit(writer, "erpclaw-setup", "create", "company", "C1",
              new_values={"name": "Nine Co"},
              description="nine-column business row")
        writer.commit()
        audit_safe(writer, "erpclaw-setup", "create", "company", "C2",
                   new_values={"name": "Safe Co"},
                   description="nine-column guarded row")
        writer.commit()
    finally:
        writer.close()
    assert len(read_all(conn, "audit_log", ["id"])) == before + 2
    rows = {r["entity_id"]: r for r in
            read_all(conn, "audit_log", FIRST_COLS)}
    first = rows["C1"]
    assert (first["skill"], first["action"], first["entity_type"],
            first["entity_id"]) == (
        "erpclaw-setup", "create", "company", "C1")
    assert first["new_values"] == '{"name": "Nine Co"}'
    assert first["description"] == "nine-column business row"
    assert first["old_values"] is None
    assert first["id"] and first["timestamp"] is not None
    assert capsys.readouterr().err == ""
    sql, params = migration_audit_statement("041_probe2", "account", "A9",
                                            new_values={"name": "x"})
    conn.execute(sql, params)
    conn.commit()
    assert len(read_all(conn, "audit_log", ["id"])) == before + 3
    assert sql == audit_mod._INSERT_AUDIT_LOG


def test_f_after_041_a_new_connection_records_the_actor(
        conn, db_path, pre041, monkeypatch):
    from erpclaw_lib.audit import audit
    from erpclaw_lib.db import get_connection
    mig = _load_migration()
    monkeypatch.delenv(VAR, raising=False)
    assert mig.run_migration(db_path)["added"] == list(mig.ACTOR_COLUMNS)
    writer = get_connection(db_path)
    try:
        audit(writer, "erpclaw-setup", "create", "company", "C3",
              description="first recorded row")
        writer.commit()
    finally:
        writer.close()
    rows = [r for r in read_all(conn, "audit_log", FIRST_COLS + ACTOR_COLS)
            if r["entity_id"] == "C3"]
    assert len(rows) == 1
    assert (rows[0]["actor_os_account"], rows[0]["actor_channel"],
            rows[0]["actor_principal_claim"], rows[0]["actor_status"],
            rows[0]["actor_hop"]) == (ACCOUNT, None, None, "absent", None)


def test_f_decided_once_per_connection(conn, db_path, pre041, monkeypatch):
    from erpclaw_lib import audit as audit_mod
    from erpclaw_lib.audit import audit
    from erpclaw_lib.db import get_connection
    mig = _load_migration()
    monkeypatch.delenv(VAR, raising=False)
    first = get_connection(db_path)
    try:
        audit(first, "erpclaw-setup", "create", "company", "X1")
        first.commit()
    finally:
        pass
    assert mig.run_migration(db_path)["added"] == list(mig.ACTOR_COLUMNS)
    try:
        audit(first, "erpclaw-setup", "create", "company", "X2")
        first.commit()
    finally:
        first.close()
    rows = {r["entity_id"]: r for r in
            read_all(conn, "audit_log", FIRST_COLS + ACTOR_COLS)}
    assert rows["X1"]["actor_status"] is None
    assert rows["X2"]["actor_status"] is None
    second = get_connection(db_path)
    try:
        audit(second, "erpclaw-setup", "create", "company", "Y1")
        second.commit()
    finally:
        second.close()
    rows = {r["entity_id"]: r for r in
            read_all(conn, "audit_log", FIRST_COLS + ACTOR_COLS)}
    assert rows["Y1"]["actor_status"] == "absent"
    calls = []
    real_probe = audit_mod._probe_actor_columns

    def _counting(handle):
        calls.append(handle)
        return real_probe(handle)

    monkeypatch.setattr(audit_mod, "_probe_actor_columns", _counting)
    third = get_connection(db_path)
    try:
        audit(third, "erpclaw-setup", "create", "company", "Z1")
        third.commit()
        audit(third, "erpclaw-setup", "create", "company", "Z2")
        third.commit()
    finally:
        third.close()
    assert len(calls) == 1 and calls[0] is third
    assert audit_mod._has_actor_columns(third) is True


def test_f_probe_leaves_the_caller_transaction_uncommitted(db_path):
    from erpclaw_lib.audit import audit
    from erpclaw_lib.db import get_connection
    from erpclaw_lib.query import P, insert_row
    writer = get_connection(db_path)
    try:
        sql, _cols = insert_row(
            "company", {"id": P(), "name": P(), "abbr": P()})
        writer.execute(sql, ("probe-co-1", "Probe Co", "PRB"))
        audit(writer, "erpclaw-setup", "create", "company", "probe-co-1",
              new_values={"name": "Probe Co"})
        writer.rollback()
    finally:
        writer.close()
    reader = open_reader(db_path)
    try:
        assert [r for r in read_all(reader, "company", ["id"])
                if r["id"] == "probe-co-1"] == []
        assert [r for r in read_all(reader, "audit_log", ["entity_id"])
                if r["entity_id"] == "probe-co-1"] == []
    finally:
        reader.close()


def test_f_audit_first_statement_rolls_back_cleanly(db_path):
    from erpclaw_lib.audit import audit
    from erpclaw_lib.db import get_connection
    reader = open_reader(db_path)
    try:
        before = len(read_all(reader, "audit_log", ["id"]))
    finally:
        reader.close()
    writer = get_connection(db_path)
    try:
        audit(writer, "erpclaw-setup", "create", "company", "first-only")
        writer.rollback()
    finally:
        writer.close()
    reader = open_reader(db_path)
    try:
        rows = read_all(reader, "audit_log", ["id", "entity_id"])
    finally:
        reader.close()
    assert len(rows) == before
    assert [r for r in rows if r["entity_id"] == "first-only"] == []


def test_f_pre041_probe_is_false_and_row_has_nine_values(
        conn, db_path, pre041):
    from erpclaw_lib import audit as audit_mod
    from erpclaw_lib.audit import audit
    from erpclaw_lib.db import get_connection
    writer = get_connection(db_path)
    try:
        assert audit_mod._probe_actor_columns(writer) is False
        audit(writer, "erpclaw-setup", "create", "company", "pre-co-1",
              new_values={"name": "Pre Co"})
        writer.commit()
    finally:
        writer.close()
    assert all(name not in seam.column_names("audit_log", db_path)
               for name in ACTOR_COLS)
    rows = [r for r in read_all(conn, "audit_log", FIRST_COLS)
            if r["entity_id"] == "pre-co-1"]
    assert len(rows) == 1
    assert (rows[0]["skill"], rows[0]["action"], rows[0]["entity_type"],
            rows[0]["new_values"]) == (
        "erpclaw-setup", "create", "company", '{"name": "Pre Co"}')


def test_f_audit_write_imports_no_heavy_orm(tmp_path):
    import subprocess
    db = str(tmp_path / "import-probe.sqlite")
    init_all_tables(db)
    lib = os.path.join(_SETUP_DIR, "lib")
    env = dict(os.environ)
    env["PYTHONPATH"] = lib + os.pathsep + env.get("PYTHONPATH", "")
    env["ERPCLAW_DB_PATH"] = db
    env["ERPCLAW_DB_DIALECT"] = "sqlite"
    env.pop("ERPCLAW_DB_URL", None)
    script = (
        "from erpclaw_lib.db import get_connection;"
        "from erpclaw_lib.audit import audit;"
        "import sys;"
        "handle = get_connection();"
        "audit(handle, 'erpclaw-setup', 'create', 'company', 'sub-c1');"
        "handle.commit();"
        "print('sqlalchemy' in sys.modules)")
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip().splitlines()[-1] == "False"
    reader = open_reader(db)
    try:
        rows = [r for r in
                read_all(reader, "audit_log",
                         ["entity_id", "actor_status"])
                if r["entity_id"] == "sub-c1"]
    finally:
        reader.close()
    assert [r["actor_status"] for r in rows] == ["absent"]


_PG_URL = os.environ.get("ERPCLAW_PG_TEST_URL")


def _reset_pg_schema():
    """Drop and recreate the shared ``public`` schema on PostgreSQL.

    Per-test isolation for the PostgreSQL branch: the drop clears every
    table left by the previous test in one statement, so no test ever
    sees another test's rows. The fresh schema is re-provisioned by
    ``init_schema.init_db(None)``. The reset opens the general connection
    (``get_connection()`` with no argument, which resolves
    ``ERPCLAW_DB_URL`` then ``ERPCLAW_DB_PATH``) and refuses unless the
    database that connection reports matches the database named in
    ``ERPCLAW_PG_TEST_URL`` and the host and port parsed from
    ``ERPCLAW_PG_TEST_URL`` and from the URL the general connection
    actually resolves match (both absent counts as equal, for socket
    URLs). The drop is issued on that same general connection, so the
    database the suite uses is the database that is checked. An unset
    ``ERPCLAW_PG_TEST_URL``, a URL naming no database, or any mismatch
    raises instead of dropping a schema on a database the caller did not
    mean.
    """
    from urllib.parse import urlparse
    from erpclaw_lib.db import _resolve_pg_url, get_connection
    test_url = os.environ.get("ERPCLAW_PG_TEST_URL")
    if not test_url:
        raise RuntimeError(
            "refusing to reset the shared schema: ERPCLAW_PG_TEST_URL is "
            "not set, so the reset target is unknown")
    expected_db = urlparse(test_url).path.strip("/")
    if not expected_db:
        raise RuntimeError(
            "refusing to reset the shared schema: ERPCLAW_PG_TEST_URL "
            "names no database")
    db_url = _resolve_pg_url(None)
    conn = get_connection()
    try:
        resolved_db = conn.execute("SELECT current_database()").fetchone()[0]
        if resolved_db != expected_db:
            raise RuntimeError(
                "refusing to reset the shared schema: ERPCLAW_PG_TEST_URL "
                "names database %r but the connection resolved to %r"
                % (expected_db, resolved_db))
        test_parts = urlparse(test_url)
        db_parts = urlparse(db_url)
        if (test_parts.hostname != db_parts.hostname
                or test_parts.port != db_parts.port):
            raise RuntimeError(
                "refusing to reset the shared schema: host/port mismatch "
                "between ERPCLAW_PG_TEST_URL (%r, %r) and ERPCLAW_DB_URL "
                "(%r, %r)" % (test_parts.hostname, test_parts.port,
                               db_parts.hostname, db_parts.port))
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
    finally:
        conn.close()


@pytest.mark.skipif(not _PG_URL, reason="live Postgres required")
def test_pg_actor_columns_and_migration(monkeypatch):
    """Runs against the database named by ERPCLAW_PG_TEST_URL only."""
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", _PG_URL)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    _reset_pg_schema()
    init_all_tables(None)
    assert seam.column_names("audit_log", _PG_URL) == FRESH_COLUMNS
    from erpclaw_lib.audit import audit
    from erpclaw_lib.db import get_connection
    monkeypatch.delenv(VAR, raising=False)
    writer = get_connection()
    try:
        audit(writer, "erpclaw-setup", "create", "company", "PG1")
        writer.commit()
    finally:
        writer.close()
    reader = get_connection()
    try:
        check = [r for r in read_all(
            reader, "audit_log", FIRST_COLS + ACTOR_COLS)
            if r["entity_id"] == "PG1"][0]
    finally:
        reader.close()
    assert check["actor_status"] == "absent"
    present = seam.column_names("audit_log", _PG_URL)
    dropper = get_connection()
    try:
        for statement in _DROP_STATEMENTS:
            name = statement.rsplit(" ", 1)[-1]
            if name in present:
                dropper.execute(statement)
        dropper.commit()
    finally:
        dropper.close()
    bare = get_connection()
    try:
        audit(bare, "erpclaw-setup", "create", "company", "PG2")
        bare.commit()
    finally:
        bare.close()
    reader = get_connection()
    try:
        pre = [r for r in read_all(reader, "audit_log", FIRST_COLS)
               if r["entity_id"] == "PG2"]
    finally:
        reader.close()
    assert len(pre) == 1
    mig = _load_migration()
    assert mig.run_migration(_PG_URL) == {
        "added": list(mig.ACTOR_COLUMNS), "report_only": False}
    assert seam.column_names("audit_log", _PG_URL) == REUPGRADED_COLUMNS
    again = get_connection()
    try:
        audit(again, "erpclaw-setup", "create", "company", "PG3")
        again.commit()
    finally:
        again.close()
    reader = get_connection()
    try:
        rows = {r["entity_id"]: r for r in read_all(
            reader, "audit_log", FIRST_COLS + ACTOR_COLS)}
    finally:
        reader.close()
    assert rows["PG3"]["actor_status"] == "absent"


def test_f_migration_statement_shape_unchanged():
    from erpclaw_lib import audit
    from erpclaw_lib.audit import migration_audit_statement
    sql, params = migration_audit_statement("035_x", "account", "A1")
    assert sql == audit._INSERT_AUDIT_LOG
    assert len(params) == 9
