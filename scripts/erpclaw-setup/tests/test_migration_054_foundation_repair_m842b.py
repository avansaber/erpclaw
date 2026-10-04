"""m842b: PostgreSQL installs that skipped 001/002 regain their objects via 054.

An install migrated by the pre-m842 code carries 001 and 002 as applied while
still missing what they add. This plants exactly that older shape on a fresh
install (leaving both ledger rows applied), runs migrate, and checks 054 puts
every object back without touching the 001/002 ledger rows. The SQLite leg
proves 054 is a no-op off PostgreSQL, and the refusal legs prove 054 never
records itself applied with an object still absent.
"""
import hashlib
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import redirect_stdout

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SETUP_DIR = os.path.dirname(_TESTS_DIR)
_DB_QUERY = os.path.join(_SETUP_DIR, "db_query.py")
_MIGRATION_001 = os.path.join(_SETUP_DIR, "migrations", "001_registry_tables.py")
_MIGRATION_002 = os.path.join(_SETUP_DIR, "migrations", "002_credit_dunning.py")
_MIGRATION_054 = os.path.join(_SETUP_DIR, "migrations", "054_foundation_001_002_on_postgresql.py")
_STEM_054 = "054_foundation_001_002_on_postgresql"

if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
import setup_helpers  # noqa: F401  (binds erpclaw_lib to this tree)

from setup_helpers import seed_company, seed_customer  # noqa: E402
from erpclaw_lib import seam, authority_gate  # noqa: E402
from erpclaw_lib.db import get_connection, db_integrity_error  # noqa: E402
import erpclaw_lib.db as db_lib  # noqa: E402

_PG_REASON = "PG lane runs on the gate's database leg"

_PRECHECK_MESSAGE = ("054: a type registry is absent; this install did not complete "
                     "migration 008 and cannot be repaired by 054")

_SQLITE_LINE = "  SQLite: nothing to repair (001 and 002 never skipped on SQLite)."

_REGISTRIES = ("voucher_type_registry", "party_type_registry", "account_type_registry")
_DESCRIBE_TABLES = ("gl_entry", "payment_entry", "customer", "dunning_level",
                    "dunning_run") + _REGISTRIES

_WANT_VOUCHER = [
    ("journal_entry", "erpclaw-journals", "Journal Entry", "gl_entry"),
    ("sales_invoice", "erpclaw-selling", "Sales Invoice", "gl_entry"),
    ("elimination_entry", "erpclaw-gl", "Elimination Entry", "gl_entry"),
    ("exchange_rate_revaluation", "erpclaw-gl", "Exchange Rate Revaluation", "gl_entry"),
    ("stock_revaluation", "erpclaw-inventory", "Stock Revaluation", "gl_entry"),
    ("stock_entry", "erpclaw-inventory", "Stock Entry", "stock_ledger_entry"),
    ("purchase_receipt", "erpclaw-buying", "Purchase Receipt", "stock_ledger_entry"),
    ("credit_note", "erpclaw-selling", "Credit Note", "stock_ledger_entry"),
]
_WANT_PARTY = [
    ("customer", "erpclaw-selling", "Customer"),
    ("supplier", "erpclaw-buying", "Supplier"),
    ("employee", "erpclaw-hr", "Employee"),
]
_WANT_ACCOUNT = [
    ("bank", "erpclaw-gl", "Bank"),
    ("receivable", "erpclaw-selling", "Receivable"),
    ("payable", "erpclaw-buying", "Payable"),
    ("expense", "erpclaw-gl", "Expense"),
    ("trust", "erpclaw-gl", "Trust"),
]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pg_reset_schema(url):
    from urllib.parse import urlparse
    from erpclaw_lib.db import _resolve_pg_url, get_connection as _gc
    expected_db = urlparse(url).path.strip("/")
    if not expected_db:
        raise RuntimeError("pg target names no database")
    db_url = _resolve_pg_url(url)
    conn = _gc(url)
    try:
        resolved_db = conn.execute(
            "SELECT current_database()").fetchone()[0]
        if resolved_db != expected_db:
            raise RuntimeError("pg database mismatch")
        test_parts = urlparse(url)
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


def _isolate(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ERPCLAW_HOME", str(home / ".openclaw" / "erpclaw"))
    monkeypatch.setattr(db_lib, "DEFAULT_DB_PATH", str(tmp_path / "never.sqlite"))
    monkeypatch.setattr(seam, "DEFAULT_DB_PATH", str(tmp_path / "never.sqlite"))


def _check_no_leak(tmp_path):
    assert not (tmp_path / "never.sqlite").exists()
    assert not (tmp_path / "home" / ".openclaw" / "erpclaw" / "data.sqlite").exists()


def _pg_leg(monkeypatch, tmp_path):
    url = os.environ.get("ERPCLAW_PG_TEST_URL")
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", url)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    seam.dispose_engines()
    _pg_identity(url)
    _pg_reset_schema(url)
    return url


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


def _fresh_install(tmp_path, url):
    env = _subprocess_env(tmp_path, True, url)
    proc = _run_action(env, "initialize-database")
    assert proc.returncode == 0, proc.stderr[-2000:] + proc.stdout[-2000:]
    proc = _run_action(env, "migrate")
    assert proc.returncode == 0, proc.stderr[-2000:] + proc.stdout[-2000:]
    payload = json.loads(proc.stdout)
    assert payload["status"] == "ok"
    return payload


def _ledger_full(target, ids):
    conn = get_connection(target)
    try:
        rows = conn.execute(
            "SELECT id, module_name, migration_type, ddl_statements, status, "
            "previous_schema, planned_at, applied_at, rolled_back_at, applied_by "
            "FROM erpclaw_schema_migration WHERE id IN (%s) ORDER BY id"
            % ", ".join("?" * len(ids)), tuple(ids)).fetchall()
        return [tuple(row) for row in rows]
    finally:
        conn.close()


def _registry_counts(target):
    conn = get_connection(target)
    try:
        return {name: conn.execute(
            "SELECT COUNT(*) FROM %s" % name).fetchone()[0] for name in _REGISTRIES}
    finally:
        conn.close()


def _assert_no_failed(target):
    conn = get_connection(target)
    try:
        failed = conn.execute(
            "SELECT id FROM erpclaw_schema_migration WHERE status = 'failed'").fetchall()
        assert [row[0] for row in failed] == [], [tuple(row) for row in failed]
    finally:
        conn.close()


def _assert_seeds_present(target):
    conn = get_connection(target)
    try:
        for voucher_type, skill, label, table in _WANT_VOUCHER:
            row = conn.execute(
                "SELECT 1 FROM voucher_type_registry WHERE voucher_type = ? "
                "AND skill_name = ? AND label = ? AND target_table = ?",
                (voucher_type, skill, label, table)).fetchone()
            assert row is not None, (voucher_type, table)
        for party_type, skill, label in _WANT_PARTY:
            row = conn.execute(
                "SELECT 1 FROM party_type_registry WHERE party_type = ? "
                "AND skill_name = ? AND label = ?",
                (party_type, skill, label)).fetchone()
            assert row is not None, party_type
        for account_type, skill, label in _WANT_ACCOUNT:
            row = conn.execute(
                "SELECT 1 FROM account_type_registry WHERE account_type = ? "
                "AND skill_name = ? AND label = ?",
                (account_type, skill, label)).fetchone()
            assert row is not None, account_type
    finally:
        conn.close()


def _catalog_snapshot(target):
    described = {name: seam.describe_table(name, target) for name in _DESCRIBE_TABLES}
    indexes = seam.index_names("gl_entry", target)
    conn = get_connection(target)
    try:
        rows = {}
        for name in _REGISTRIES:
            got = conn.execute("SELECT * FROM %s" % name).fetchall()
            rows[name] = sorted(tuple(row) for row in got)
    finally:
        conn.close()
    return described, indexes, rows


def _assert_direct_repair_clean(target, mod054):
    before = _catalog_snapshot(target)
    _, out = _direct(target, mod054)
    for line in out.splitlines():
        if not line.strip():
            continue
        assert (line.endswith(": already present")
                or line.startswith("  Seeded type registries: ")
                or line == "  PostgreSQL: registries seeded (missing rows only)."), repr(line)
    assert before == _catalog_snapshot(target)


@pytest.mark.skipif(not os.environ.get("ERPCLAW_PG_TEST_URL"), reason=_PG_REASON)
def test_repaired_state_postgresql(tmp_path, monkeypatch):
    url = _pg_leg(monkeypatch, tmp_path)
    try:
        mod054 = _load("m842b_054_repair", _MIGRATION_054)
        payload = _fresh_install(tmp_path, url)
        assert _STEM_054 in payload["applied"]

        before_ledger = _ledger_full(url, ["001_registry_tables", "002_credit_dunning"])
        assert len(before_ledger) == 2
        before_counts = _registry_counts(url)

        conn = get_connection(url)
        try:
            phase = authority_gate.install_phase(conn)[0]
            assert phase == "STAGED", phase
            company_id = seed_company(conn, "Repair Co", "RC")
            customer_id = seed_customer(conn, company_id, "Repair Customer")
            conn.execute("DROP INDEX idx_gl_entry_project")
            conn.execute("ALTER TABLE gl_entry DROP COLUMN dimensions_json")
            conn.execute("ALTER TABLE payment_entry DROP COLUMN payment_method")
            conn.execute("DELETE FROM voucher_type_registry WHERE voucher_type = ? "
                         "AND target_table = ?",
                         ("elimination_entry", "gl_entry"))
            conn.execute("DROP TABLE dunning_run")
            conn.execute("DROP TABLE dunning_level")
            conn.execute("ALTER TABLE customer DROP COLUMN credit_status")
            conn.execute("DELETE FROM erpclaw_schema_migration WHERE id = ?", (_STEM_054,))
            conn.commit()
        finally:
            conn.close()
        assert "dimensions_json" not in seam.column_names("gl_entry", url)
        assert "payment_method" not in seam.column_names("payment_entry", url)
        assert "credit_status" not in seam.column_names("customer", url)
        assert "idx_gl_entry_project" not in seam.index_names("gl_entry", url)
        assert not seam.table_exists("dunning_level", url)
        assert not seam.table_exists("dunning_run", url)

        env = _subprocess_env(tmp_path, True, url)
        proc = _run_action(env, "migrate")
        assert proc.returncode == 0, proc.stderr[-2000:] + proc.stdout[-2000:]
        payload = json.loads(proc.stdout)
        assert payload["status"] == "ok"

        assert "dimensions_json" in seam.column_names("gl_entry", url)
        assert "payment_method" in seam.column_names("payment_entry", url)
        assert "credit_status" in seam.column_names("customer", url)
        assert "idx_gl_entry_project" in seam.index_names("gl_entry", url)
        assert seam.table_exists("dunning_level", url)
        assert seam.table_exists("dunning_run", url)

        check = get_connection(url)
        try:
            reg = check.execute(
                "SELECT skill_name, label, target_table, is_active "
                "FROM voucher_type_registry WHERE voucher_type = ? AND target_table = ?",
                ("elimination_entry", "gl_entry")).fetchone()
            assert reg is not None, "elimination registry row missing"
            assert tuple(reg) == ("erpclaw-gl", "Elimination Entry", "gl_entry", 1), tuple(reg)
        finally:
            check.close()
        _assert_seeds_present(url)
        assert _registry_counts(url) == before_counts

        verify = get_connection(url)
        try:
            cust = verify.execute(
                "SELECT credit_status FROM customer WHERE id = ?", (customer_id,)).fetchone()
            assert cust is not None, "planted customer missing"
            assert cust[0] == "active", cust[0]
            exc_type = db_integrity_error(verify)
            verify.execute("SAVEPOINT m842b_bogus_check")
            try:
                verify.execute("UPDATE customer SET credit_status = ? WHERE id = ?",
                               ("bogus", customer_id))
            except exc_type:
                verify.execute("ROLLBACK TO SAVEPOINT m842b_bogus_check")
                verify.execute("RELEASE SAVEPOINT m842b_bogus_check")
            else:
                verify.execute("RELEASE SAVEPOINT m842b_bogus_check")
                raise AssertionError("bogus credit_status was accepted")
        finally:
            verify.close()

        assert _ledger_full(url, ["001_registry_tables", "002_credit_dunning"]) == before_ledger
        assert payload["applied"] == [_STEM_054]
        _assert_no_failed(url)
    finally:
        seam.dispose_engines()
        _check_no_leak(tmp_path)


@pytest.mark.skipif(not os.environ.get("ERPCLAW_PG_TEST_URL"), reason=_PG_REASON)
def test_idempotent_postgresql(tmp_path, monkeypatch):
    url = _pg_leg(monkeypatch, tmp_path)
    try:
        mod054 = _load("m842b_054_idempotent", _MIGRATION_054)
        _fresh_install(tmp_path, url)
        env = _subprocess_env(tmp_path, True, url)
        proc = _run_action(env, "migrate")
        assert proc.returncode == 0, proc.stderr[-2000:] + proc.stdout[-2000:]
        assert json.loads(proc.stdout)["applied"] == []
        _assert_direct_repair_clean(url, mod054)
        _assert_no_failed(url)
    finally:
        seam.dispose_engines()
        _check_no_leak(tmp_path)


@pytest.mark.skipif(not os.environ.get("ERPCLAW_PG_TEST_URL"), reason=_PG_REASON)
def test_fresh_install_postgresql(tmp_path, monkeypatch):
    url = _pg_leg(monkeypatch, tmp_path)
    try:
        mod054 = _load("m842b_054_fresh", _MIGRATION_054)
        payload = _fresh_install(tmp_path, url)
        assert _STEM_054 in payload["applied"]
        conn = get_connection(url)
        try:
            row = conn.execute(
                "SELECT status FROM erpclaw_schema_migration WHERE id = ?",
                (_STEM_054,)).fetchone()
            assert row is not None and row[0] == "applied", row
        finally:
            conn.close()
        _assert_direct_repair_clean(url, mod054)
        _assert_no_failed(url)
    finally:
        seam.dispose_engines()
        _check_no_leak(tmp_path)


def test_sqlite_nothing_to_repair(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    target = str(tmp_path / "leg.sqlite")
    with open(target, "wb") as handle:
        handle.write(b"m842b-sqlite-leg-marker" * 64)
    with open(target, "rb") as handle:
        digest_before = hashlib.sha256(handle.read()).hexdigest()

    def _refuse(*args, **kwargs):
        raise AssertionError("repair must not touch a backend on SQLite")

    monkeypatch.setattr(db_lib, "get_connection", _refuse)
    monkeypatch.setattr(seam, "table_exists", _refuse)
    monkeypatch.setattr(seam, "get_engine", _refuse)
    monkeypatch.setattr(sqlite3, "connect", _refuse)

    mod054 = _load("m842b_054_sqlite", _MIGRATION_054)
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = mod054.run_migration(target)
    assert result is None
    assert buf.getvalue() == _SQLITE_LINE + "\n"
    with open(target, "rb") as handle:
        assert hashlib.sha256(handle.read()).hexdigest() == digest_before
    assert not os.path.exists(target + "-wal")
    assert not os.path.exists(target + "-shm")
    _check_no_leak(tmp_path)


@pytest.mark.skipif(not os.environ.get("ERPCLAW_PG_TEST_URL"), reason=_PG_REASON)
def test_failure_recorded_postgresql(tmp_path, monkeypatch):
    url = _pg_leg(monkeypatch, tmp_path)
    try:
        _fresh_install(tmp_path, url)
        before_ledger = _ledger_full(url, ["001_registry_tables", "002_credit_dunning"])
        conn = get_connection(url)
        try:
            conn.execute("DROP TABLE dunning_run")
            conn.execute("DROP TABLE dunning_level")
            conn.execute("DROP TABLE customer CASCADE")
            conn.execute("DELETE FROM erpclaw_schema_migration WHERE id = ?", (_STEM_054,))
            conn.commit()
        finally:
            conn.close()

        env = _subprocess_env(tmp_path, True, url)
        proc = _run_action(env, "migrate")
        assert proc.returncode == 1, proc.stderr[-2000:] + proc.stdout[-2000:]
        payload = json.loads(proc.stdout)
        assert payload["status"] == "error"
        assert "Migration '%s' failed" % _STEM_054 in payload["message"]

        check = get_connection(url)
        try:
            row = check.execute(
                "SELECT status, applied_at FROM erpclaw_schema_migration WHERE id = ?",
                (_STEM_054,)).fetchone()
            assert row is not None, "054 ledger row missing"
            assert row[0] == "failed", tuple(row)
            assert row[1] is None, tuple(row)
        finally:
            check.close()
        assert not seam.table_exists("dunning_level", url)
        assert "dimensions_json" in seam.column_names("gl_entry", url)
        assert "payment_method" in seam.column_names("payment_entry", url)
        assert "idx_gl_entry_project" in seam.index_names("gl_entry", url)
        assert _ledger_full(url, ["001_registry_tables", "002_credit_dunning"]) == before_ledger
    finally:
        seam.dispose_engines()
        _check_no_leak(tmp_path)


@pytest.mark.skipif(not os.environ.get("ERPCLAW_PG_TEST_URL"), reason=_PG_REASON)
def test_registry_precheck_postgresql(tmp_path, monkeypatch):
    url = _pg_leg(monkeypatch, tmp_path)
    try:
        mod054 = _load("m842b_054_precheck", _MIGRATION_054)
        _fresh_install(tmp_path, url)
        conn = get_connection(url)
        try:
            conn.execute("DROP TABLE party_type_registry CASCADE")
            conn.commit()
        finally:
            conn.close()
        with pytest.raises(RuntimeError) as excinfo:
            mod054.run_migration(url)
        assert str(excinfo.value) == _PRECHECK_MESSAGE
        assert not seam.table_exists("party_type_registry", url)
    finally:
        seam.dispose_engines()
        _check_no_leak(tmp_path)


@pytest.mark.skipif(not os.environ.get("ERPCLAW_PG_TEST_URL"), reason=_PG_REASON)
def test_absent_parent_postgresql(tmp_path, monkeypatch):
    url = _pg_leg(monkeypatch, tmp_path)
    try:
        mod001 = _load("m842b_001_absent", _MIGRATION_001)
        mod054 = _load("m842b_054_absent", _MIGRATION_054)
        _fresh_install(tmp_path, url)
        conn = get_connection(url)
        try:
            conn.execute("DROP TABLE payment_entry CASCADE")
            conn.commit()
        finally:
            conn.close()
        _, out = _direct(url, mod001)
        assert ("  PostgreSQL: payment_entry absent; "
                "payment_entry.payment_method not added") in out.splitlines()
        with pytest.raises(RuntimeError) as excinfo:
            mod054.run_migration(url)
        assert "payment_entry.payment_method" in str(excinfo.value)
    finally:
        seam.dispose_engines()
        _check_no_leak(tmp_path)


@pytest.mark.parametrize("filename", ["001_registry_tables.py",
                                      "002_credit_dunning.py",
                                      "054_foundation_001_002_on_postgresql.py"])
def test_cli_default(tmp_path, monkeypatch, filename):
    _isolate(monkeypatch, tmp_path)
    mod = _load("m842b_cli_" + filename[:-3], os.path.join(_SETUP_DIR, "migrations", filename))
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    assert mod._build_parser().parse_args([]).db_path is None
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
    assert mod._build_parser().parse_args([]).db_path == str(
        tmp_path / "home" / ".openclaw" / "erpclaw" / "data.sqlite")
    _check_no_leak(tmp_path)
