"""m841: a fresh PostgreSQL install migrates to the end.

Migration 037's split queries filtered with ``HAVING marked > 0 AND marked <
total``. SQLite accepts an output-column alias in ``HAVING``; PostgreSQL raises
``column "marked" does not exist``, so ``migrate`` stopped at 037 and every
later migration never applied. The queries now repeat the aggregate
expressions, and ``run_migration(None)`` resolves the same environment chain as
``get_connection`` instead of handing the seam the SQLite file path.

One test, three steps, on one fresh schema: fresh migrate to the end, retry
after a recorded 037 failure, then the planted split through the migration's
own entry points.
"""
import importlib.util
import json
import os
import subprocess
import sys
import uuid

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SETUP_DIR = os.path.dirname(_TESTS_DIR)
_DB_QUERY = os.path.join(_SETUP_DIR, "db_query.py")
_MIGRATION = os.path.join(
    _SETUP_DIR, "migrations", "037_cancellation_marking_symmetry.py")
_RUNNER = os.path.join(_SETUP_DIR, "migration_runner.py")

_MIGRATION_ID = "037_cancellation_marking_symmetry"
_AUDIT_ACTION = "migration:037_cancellation_marking_symmetry"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pg_reset_schema():
    from urllib.parse import urlparse
    from erpclaw_lib.db import _resolve_pg_url, get_connection as _gc
    test_url = os.environ.get("ERPCLAW_PG_TEST_URL")
    if not test_url:
        raise RuntimeError("pg target unset")
    expected_db = urlparse(test_url).path.strip("/")
    if not expected_db:
        raise RuntimeError("pg target names no database")
    db_url = _resolve_pg_url(None)
    conn = _gc()
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
    conn = _gc()
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


def _pg_env(url, home):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["ERPCLAW_HOME"] = str(home / ".openclaw" / "erpclaw")
    env["ERPCLAW_DB_DIALECT"] = "postgresql"
    env["ERPCLAW_DB_URL"] = url
    env.pop("ERPCLAW_DB_PATH", None)
    return env


def _discover_stems():
    runner = _load("migration_runner_m841", _RUNNER)
    return [stem for stem, _ in runner.discover()]


def _run_action(env, action):
    proc = subprocess.run(
        [sys.executable, _DB_QUERY, "--action", action],
        env=env, capture_output=True, text=True, timeout=600)
    return proc


def _migrate(env):
    proc = _run_action(env, "migrate")
    assert proc.returncode == 0, proc.stderr[-2000:] + proc.stdout[-2000:]
    payload = json.loads(proc.stdout)
    assert payload["status"] == "ok"
    return payload


def _ledger_statuses():
    from erpclaw_lib.db import get_connection
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, status FROM erpclaw_schema_migration").fetchall()
    finally:
        conn.close()
    by_id = {}
    for row in rows:
        by_id.setdefault(row[0], []).append(row[1])
    return by_id


def _plant_gl(conn, voucher, debit_cancelled=1, credit_cancelled=0):
    conn.execute(
        "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
        " voucher_type, voucher_id, is_cancelled) VALUES (?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, "2026-08-01", "ar-account", "850.00", "0.00",
         "sales_invoice", voucher, debit_cancelled))
    conn.execute(
        "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
        " voucher_type, voucher_id, is_cancelled) VALUES (?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, "2026-08-01", "ar-account", "0.00", "850.00",
         "sales_invoice", voucher, credit_cancelled))


def _flags(conn, voucher):
    return sorted(row[0] for row in conn.execute(
        "SELECT is_cancelled FROM gl_entry WHERE voucher_id = ?",
        (voucher,)).fetchall())


def _audit_count(conn, voucher):
    return conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action = ? AND entity_id = ?",
        (_AUDIT_ACTION, voucher)).fetchone()[0]


@pytest.mark.skipif(not os.environ.get("ERPCLAW_PG_TEST_URL"),
                    reason="PG lane runs on the gate's PostgreSQL leg")
def test_fresh_postgresql_migrates_to_the_end(monkeypatch, tmp_path):
    url = os.environ.get("ERPCLAW_PG_TEST_URL")
    from erpclaw_lib import seam
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", url)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    seam.dispose_engines()
    try:
        _pg_identity(url)
        _pg_reset_schema()
        env = _pg_env(url, tmp_path / "home")
        stems = _discover_stems()

        # 1. A fresh install migrates to the end.
        proc = _run_action(env, "initialize-database")
        assert proc.returncode == 0, proc.stderr[-2000:] + proc.stdout[-2000:]
        payload = _migrate(env)
        assert payload["applied"] == stems
        by_id = _ledger_statuses()
        assert set(by_id) == set(stems)
        assert all(statuses == ["applied"] for statuses in by_id.values())
        assert (tmp_path / "home" / ".openclaw" / "erpclaw" / "lib").is_symlink()

        # 2. A recorded 037 failure retries 037, then applies 038 onward.
        position = stems.index(_MIGRATION_ID)
        later = stems[position + 1:]
        from erpclaw_lib.db import get_connection
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE erpclaw_schema_migration "
                "SET status = 'failed', applied_at = NULL WHERE id = ?",
                (_MIGRATION_ID,))
            for stem in later:
                conn.execute(
                    "DELETE FROM erpclaw_schema_migration WHERE id = ?",
                    (stem,))
            conn.commit()
        finally:
            conn.close()
        payload = _migrate(env)
        assert payload["applied"] == [_MIGRATION_ID] + later
        by_id = _ledger_statuses()
        assert set(by_id) == set(stems)
        assert by_id[_MIGRATION_ID] == ["applied"]
        assert all(statuses == ["applied"] for statuses in by_id.values())

        # 3. The planted split heals through the migration's own entry points.
        mig = _load("migration_037_m841", _MIGRATION)
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO company (id, name, abbr, default_currency) "
                "VALUES ('co1', 'Split Co', 'SC', 'USD')")
            conn.execute(
                "INSERT INTO account (id, name, root_type, is_group, company_id) "
                "VALUES ('ar-account', 'Trade Receivables', 'asset', 0, 'co1')")
            _plant_gl(conn, "inv1")
            conn.execute(
                "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
                " voucher_type, voucher_id, is_cancelled) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, "2026-08-01", "ar-account", "100.00", "0.00",
                 "sales_invoice", "odd", 1))
            conn.execute(
                "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
                " voucher_type, voucher_id, is_cancelled) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, "2026-08-01", "ar-account", "0.00", "40.00",
                 "sales_invoice", "odd", 0))
            conn.execute(
                "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
                " voucher_type, voucher_id, is_cancelled) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, "2026-08-01", "ar-account", "50.00", "0.00",
                 "sales_invoice", "bal0", 0))
            conn.execute(
                "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
                " voucher_type, voucher_id, is_cancelled) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, "2026-08-01", "ar-account", "0.00", "50.00",
                 "sales_invoice", "bal0", 0))
            conn.execute(
                "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
                " voucher_type, voucher_id, is_cancelled) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, "2026-08-01", "ar-account", "50.00", "0.00",
                 "sales_invoice", "bal1", 1))
            conn.execute(
                "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
                " voucher_type, voucher_id, is_cancelled) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, "2026-08-01", "ar-account", "0.00", "50.00",
                 "sales_invoice", "bal1", 1))
            conn.commit()
        finally:
            conn.close()

        mig.run_migration(None)
        fresh = get_connection()
        try:
            assert _flags(fresh, "inv1") == [1, 1]
            assert _audit_count(fresh, "inv1") == 1
            assert _flags(fresh, "bal1") == [1, 1]
            assert _audit_count(fresh, "bal1") == 0
        finally:
            fresh.close()

        conn = get_connection()
        try:
            _plant_gl(conn, "inv2")
            conn.commit()
        finally:
            conn.close()
        mig.run_migration(url)
        fresh = get_connection()
        try:
            assert _flags(fresh, "inv2") == [1, 1]
            assert _audit_count(fresh, "inv2") == 1
            assert _flags(fresh, "odd") == [0, 1]
            assert _flags(fresh, "bal0") == [0, 0]
            assert _flags(fresh, "inv1") == [1, 1]
            assert _audit_count(fresh, "inv1") == 1
            assert _audit_count(fresh, "bal0") == 0
            assert _audit_count(fresh, "odd") == 0
            assert _flags(fresh, "bal1") == [1, 1]
            assert _audit_count(fresh, "bal1") == 0
        finally:
            fresh.close()
    finally:
        seam.dispose_engines()
