"""m842: an older schema regains what 001 and 002 add, on either backend.

Fresh installs already carry these objects, so this plants the older shape
(missing columns, index, tables and one registry row), migrates, and checks
everything is back. One helper does the work; two thin tests call it.
"""
import importlib.util
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SETUP_DIR = os.path.dirname(_TESTS_DIR)
_DB_QUERY = os.path.join(_SETUP_DIR, "db_query.py")
_MIGRATION_001 = os.path.join(_SETUP_DIR, "migrations", "001_registry_tables.py")
_MIGRATION_002 = os.path.join(_SETUP_DIR, "migrations", "002_credit_dunning.py")

if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
import setup_helpers  # noqa: F401  (binds erpclaw_lib to this tree)

from setup_helpers import seed_company, seed_customer  # noqa: E402
from erpclaw_lib import seam, authority_gate  # noqa: E402
from erpclaw_lib.db import get_connection, db_integrity_error  # noqa: E402
import erpclaw_lib.db as db_lib  # noqa: E402

_PG_REASON = "PG lane runs on the gate's database leg"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pg_reset_schema(url=None):
    from urllib.parse import urlparse
    from erpclaw_lib.db import _resolve_pg_url, get_connection as _gc
    test_url = url or os.environ.get("ERPCLAW_PG_TEST_URL")
    if not test_url:
        raise RuntimeError("pg target unset")
    expected_db = urlparse(test_url).path.strip("/")
    if not expected_db:
        raise RuntimeError("pg target names no database")
    db_url = _resolve_pg_url(test_url)
    conn = _gc(test_url)
    try:
        resolved_db = conn.execute(
            "SELECT current_database()").fetchone()[0]
        if resolved_db != expected_db:
            raise RuntimeError("pg database mismatch")
        test_parts = urlparse(test_url)
        db_parts = urlparse(db_url)
        if (test_parts.hostname != db_parts.hostname
                or test_parts.port != db_parts.port):
            raise RuntimeError("pg host/port mismatch")
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
    finally:
        conn.close()


def _pg_identity(url):
    from urllib.parse import urlparse
    from erpclaw_lib.db import get_connection as _gc
    conn = _gc(url)
    try:
        want_dir = os.environ.get("AUTHORITY_PGDATA_CHECK")
        data_dir = None
        if want_dir:
            data_dir = conn.execute(
                "SELECT current_setting('data_directory')").fetchone()[0]
            listen = conn.execute(
                "SELECT current_setting('listen_addresses')").fetchone()[0]
            assert data_dir == want_dir
            assert listen == ""
        server_addr = conn.execute(
            "SELECT inet_server_addr()").fetchone()[0]
        current_db = conn.execute(
            "SELECT current_database()").fetchone()[0]
    finally:
        conn.close()
    assert server_addr is None
    assert current_db == urlparse(url).path.strip("/")
    return data_dir


def _subprocess_env(tmp_path, use_pg, url=None):
    home = tmp_path / "home"
    (home / ".openclaw" / "erpclaw").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["ERPCLAW_HOME"] = str(home / ".openclaw" / "erpclaw")
    if use_pg:
        env["ERPCLAW_DB_DIALECT"] = "postgresql"
        env["ERPCLAW_DB_URL"] = url
        env.pop("ERPCLAW_DB_PATH", None)
    else:
        env["ERPCLAW_DB_DIALECT"] = "sqlite"
        env.pop("ERPCLAW_DB_URL", None)
    return env


def _run_action(env, action, db_arg=None):
    cmd = [sys.executable, _DB_QUERY, "--action", action]
    if db_arg is not None:
        cmd += ["--db-path", db_arg]
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)


def _direct(target, mod):
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = mod.run_migration(target)
    return result, buf.getvalue()


def _assert_no_failed(target):
    conn = get_connection(target)
    try:
        failed = conn.execute(
            "SELECT id FROM erpclaw_schema_migration WHERE status = 'failed'").fetchall()
        assert [row[0] for row in failed] == [], [tuple(row) for row in failed]
    finally:
        conn.close()


def _older_schema_roundtrip(tmp_path, monkeypatch, target, use_pg, url=None):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ERPCLAW_HOME", str(home / ".openclaw" / "erpclaw"))
    monkeypatch.setattr(db_lib, "DEFAULT_DB_PATH", str(tmp_path / "never.sqlite"))
    monkeypatch.setattr(seam, "DEFAULT_DB_PATH", str(tmp_path / "never.sqlite"))
    if use_pg:
        monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
        monkeypatch.setenv("ERPCLAW_DB_URL", url)
        monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
        target = url
    else:
        monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
        monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
        monkeypatch.setenv("ERPCLAW_DB_PATH", target)
    seam.dispose_engines()
    try:
        _older_schema_roundtrip_inner(tmp_path, target, use_pg, url)
    finally:
        seam.dispose_engines()


def _older_schema_roundtrip_inner(tmp_path, target, use_pg, url=None):
    mig001 = _load("m842_001_direct", _MIGRATION_001)
    mig002 = _load("m842_002_direct", _MIGRATION_002)
    env = _subprocess_env(tmp_path, use_pg, url)
    db_arg = None if use_pg else target

    proc = _run_action(env, "initialize-database", db_arg)
    assert proc.returncode == 0, proc.stderr[-2000:] + proc.stdout[-2000:]
    proc = _run_action(env, "migrate", db_arg)
    assert proc.returncode == 0, proc.stderr[-2000:] + proc.stdout[-2000:]
    payload = json.loads(proc.stdout)
    assert payload["status"] == "ok"
    _assert_no_failed(target)

    _, out001 = _direct(target, mig001)
    _, out002 = _direct(target, mig002)
    if use_pg:
        combined = out001 + out002
        assert "Database not found" not in combined
        lines = combined.splitlines()
        for name in ("voucher_type_registry", "party_type_registry",
                     "account_type_registry", "gl_entry.dimensions_json",
                     "payment_entry.payment_method", "idx_gl_entry_project",
                     "customer.credit_status", "dunning_level", "dunning_run"):
            assert "  PostgreSQL: %s: already present" % name in lines, \
                "fresh-run output lacks the already-present line for %s:\n%s" % (name, combined)
        for line in lines:
            assert not line.rstrip().endswith("created."), "unexpected creation line: %r" % line
            assert not line.rstrip().endswith("added."), "unexpected addition line: %r" % line
        assert "already present" in combined
        for line in lines:
            if not line.strip():
                continue
            if line.startswith("  Seeded type registries:"):
                continue
            assert line.startswith("  PostgreSQL:"), "line without backend tag: %r" % line

    conn = get_connection(target)
    try:
        phase = authority_gate.install_phase(conn)[0]
        assert phase == "STAGED", phase
        company_id = seed_company(conn, "Older Co", "OC")
        customer_id = seed_customer(conn, company_id, "Older Customer")
        skipped = []

        def _drop(label, sql, params=None):
            try:
                if params is None:
                    conn.execute(sql)
                else:
                    conn.execute(sql, params)
            except Exception as exc:
                skipped.append((label, str(exc)))
                return False
            return True

        _drop("drop index", "DROP INDEX idx_gl_entry_project")
        _drop("drop dimensions", "ALTER TABLE gl_entry DROP COLUMN dimensions_json")
        _drop("drop pay method", "ALTER TABLE payment_entry DROP COLUMN payment_method")
        _drop("delete registry row",
              "DELETE FROM voucher_type_registry WHERE voucher_type = ? AND target_table = ?",
              ("elimination_entry", "gl_entry"))
        _drop("drop dunning_run", "DROP TABLE dunning_run")
        _drop("drop dunning_level", "DROP TABLE dunning_level")
        _drop("drop credit_status", "ALTER TABLE customer DROP COLUMN credit_status")
        try:
            conn.execute("DELETE FROM erpclaw_schema_migration WHERE id IN "
                         "('001_registry_tables', '002_credit_dunning')")
        except Exception as exc:
            skipped.append(("delete ledger rows", str(exc)))
        conn.commit()
        if use_pg:
            assert skipped == [], skipped
        else:
            assert [label for label, _ in skipped] == ["drop dimensions"], skipped
        if use_pg:
            assert "dimensions_json" not in seam.column_names("gl_entry", target)
        else:
            assert "dimensions_json" in seam.column_names("gl_entry", target)
        assert "payment_method" not in seam.column_names("payment_entry", target)
        assert "credit_status" not in seam.column_names("customer", target)
        assert not seam.table_exists("dunning_level", target)
        assert not seam.table_exists("dunning_run", target)
        assert "idx_gl_entry_project" not in seam.index_names("gl_entry", target)
        row = conn.execute(
            "SELECT 1 FROM voucher_type_registry WHERE voucher_type = ? "
            "AND target_table = ?",
            ("elimination_entry", "gl_entry")).fetchone()
        assert row is None
    finally:
        conn.close()

    proc = _run_action(env, "migrate", db_arg)
    assert proc.returncode == 0, proc.stderr[-2000:] + proc.stdout[-2000:]
    payload = json.loads(proc.stdout)
    assert payload["status"] == "ok"
    assert payload["applied"] == ["001_registry_tables", "002_credit_dunning"]

    _assert_no_failed(target)
    check = get_connection(target)
    try:
        rows = check.execute(
            "SELECT id, status FROM erpclaw_schema_migration WHERE id IN "
            "('001_registry_tables', '002_credit_dunning')").fetchall()
        by_id = {}
        for row in rows:
            by_id.setdefault(row[0], []).append(row[1])
        assert by_id.get("001_registry_tables") == ["applied"], by_id
        assert by_id.get("002_credit_dunning") == ["applied"], by_id
    finally:
        check.close()

    assert "dimensions_json" in seam.column_names("gl_entry", target)
    assert "payment_method" in seam.column_names("payment_entry", target)
    assert "credit_status" in seam.column_names("customer", target)
    assert "idx_gl_entry_project" in seam.index_names("gl_entry", target)
    assert seam.table_exists("dunning_level", target)
    assert seam.table_exists("dunning_run", target)
    verify = get_connection(target)
    try:
        reg = verify.execute(
            "SELECT skill_name, label FROM voucher_type_registry "
            "WHERE voucher_type = ? AND target_table = ?",
            ("elimination_entry", "gl_entry")).fetchone()
        assert reg is not None, "elimination registry row missing"
        assert reg[0] == "erpclaw-gl", reg[0]
        assert reg[1] == "Elimination Entry", reg[1]
        cust = verify.execute(
            "SELECT credit_status FROM customer WHERE id = ?",
            (customer_id,)).fetchone()
        assert cust is not None, "planted customer missing"
        assert cust[0] == "active", cust[0]
        if use_pg:
            exc_type = db_integrity_error(verify)
            verify.execute("SAVEPOINT m842_bogus_check")
            try:
                verify.execute("UPDATE customer SET credit_status = ? WHERE id = ?",
                               ("bogus", customer_id))
            except exc_type:
                verify.execute("ROLLBACK TO SAVEPOINT m842_bogus_check")
                verify.execute("RELEASE SAVEPOINT m842_bogus_check")
            else:
                verify.execute("RELEASE SAVEPOINT m842_bogus_check")
                raise AssertionError("bogus credit_status was accepted")
    finally:
        verify.close()

    proc = _run_action(env, "migrate", db_arg)
    assert proc.returncode == 0, proc.stderr[-2000:] + proc.stdout[-2000:]
    payload = json.loads(proc.stdout)
    assert payload["status"] == "ok"
    assert payload["applied"] == []
    _assert_no_failed(target)
    five = ("gl_entry", "payment_entry", "customer", "dunning_level", "dunning_run")
    before = {name: seam.describe_table(name, target) for name in five}
    _direct(target, mig001)
    _direct(target, mig002)
    after = {name: seam.describe_table(name, target) for name in five}
    assert before == after

    handle = get_connection(target)
    try:
        handle.execute("DROP TABLE party_type_registry")
        handle.commit()
    finally:
        handle.close()
    _direct(target, mig001)
    assert seam.table_exists("party_type_registry", target)
    reader = get_connection(target)
    try:
        got = reader.execute(
            "SELECT party_type, skill_name, label FROM party_type_registry "
            "ORDER BY party_type").fetchall()
        assert [tuple(r) for r in got] == [
            ("customer", "erpclaw-selling", "Customer"),
            ("employee", "erpclaw-hr", "Employee"),
            ("supplier", "erpclaw-buying", "Supplier"),
        ], got
    finally:
        reader.close()


def test_older_schema_sqlite(tmp_path, monkeypatch):
    target = str(tmp_path / "older.sqlite")
    try:
        _older_schema_roundtrip(tmp_path, monkeypatch, target, use_pg=False)
    finally:
        assert not (tmp_path / "never.sqlite").exists()
        assert not (tmp_path / "home" / ".openclaw" / "erpclaw" / "data.sqlite").exists()


@pytest.mark.skipif(not os.environ.get("ERPCLAW_PG_TEST_URL"), reason=_PG_REASON)
def test_older_schema_postgresql(tmp_path, monkeypatch):
    url = os.environ.get("ERPCLAW_PG_TEST_URL")
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", url)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    seam.dispose_engines()
    try:
        _pg_identity(url)
        _pg_reset_schema(url)
        _older_schema_roundtrip(tmp_path, monkeypatch, None, use_pg=True, url=url)
    finally:
        seam.dispose_engines()
        assert not (tmp_path / "never.sqlite").exists()
        assert not (tmp_path / "home" / ".openclaw" / "erpclaw" / "data.sqlite").exists()
