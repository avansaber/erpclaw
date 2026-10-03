"""Wrapper seam: every general-ledger write is checked on its own handle."""

import logging
import os
import sys
import uuid

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import setup_helpers  # noqa: E402  (binds erpclaw_lib to this tree)
import authority_fixtures as fx  # noqa: E402
from erpclaw_lib import authority_gate  # noqa: E402
from erpclaw_lib import authority_readiness  # noqa: E402
from erpclaw_lib import authority_sink  # noqa: E402
from erpclaw_lib import db as dbmod  # noqa: E402
from erpclaw_lib.db import get_connection  # noqa: E402
from erpclaw_lib.query import Field, P, Q, Table  # noqa: E402

STAMP = "2026-01-02 03:04:05"

_PG_URL = os.environ.get("ERPCLAW_PG_TEST_URL")


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


def _companies(db_path):
    first = fx.seed_company(db_path)
    second = fx.seed_company(db_path, name="Second Co", abbr="SC")
    return first, second


def _head_insert_sql():
    table = Table("gl_chain_head")
    return Q.into(table).columns(
        "company_id", "last_sequence", "last_checksum",
        "updated_at").insert(P(), P(), P(), P()).get_sql()


def _read_heads(conn):
    table = Table("gl_chain_head")
    query = Q.from_(table).select(
        Field("company_id"), Field("last_sequence"),
        Field("last_checksum"), Field("updated_at")).get_sql()
    return [dict(row) for row in conn.execute(query).fetchall()]


def _set_install_empty(conn):
    table = Table("authority_install")
    query = Q.update(table).set(
        Field("install_id"), P()).get_sql()
    conn.execute(query, ("",))


def _write_one(conn, company_id):
    conn.execute(_head_insert_sql(),
                 (company_id, 0, "GENESIS", STAMP))


def _write_two(conn, first, second):
    conn.executemany(_head_insert_sql(), [
        (first, 0, "GENESIS", STAMP),
        (second, 0, "GENESIS", STAMP),
    ])


def test_staged_write_runs(db_path):
    first, second = _companies(db_path)
    conn = get_connection(db_path)
    try:
        _write_one(conn, first)
        conn.commit()
    finally:
        conn.close()
    reader = get_connection(db_path)
    try:
        rows = _read_heads(reader)
    finally:
        reader.close()
    assert len(rows) == 1
    assert rows[0]["company_id"] == first
    assert str(rows[0]["last_sequence"]) == "0"
    wiper = get_connection(db_path)
    try:
        table = Table("gl_chain_head")
        wiper.execute(Q.from_(table).delete().get_sql())
        wiper.commit()
    finally:
        wiper.close()
    conn2 = get_connection(db_path)
    try:
        _write_two(conn2, first, second)
        conn2.commit()
    finally:
        conn2.close()
    reader2 = get_connection(db_path)
    try:
        rows2 = _read_heads(reader2)
    finally:
        reader2.close()
    assert sorted(row["company_id"] for row in rows2) == sorted([first, second])


def test_staged_without_install_table_runs(db_path):
    first, second = _companies(db_path)
    dropper = get_connection(db_path)
    try:
        dropper.execute("DROP TABLE authority_install")
        dropper.commit()
    finally:
        dropper.close()
    conn = get_connection(db_path)
    try:
        _write_one(conn, first)
        conn.commit()
    finally:
        conn.close()
    reader = get_connection(db_path)
    try:
        assert len(_read_heads(reader)) == 1
    finally:
        reader.close()
    wiper = get_connection(db_path)
    try:
        table = Table("gl_chain_head")
        wiper.execute(Q.from_(table).delete().get_sql())
        wiper.commit()
    finally:
        wiper.close()
    conn2 = get_connection(db_path)
    try:
        _write_two(conn2, first, second)
        conn2.commit()
    finally:
        conn2.close()
    reader2 = get_connection(db_path)
    try:
        rows2 = _read_heads(reader2)
    finally:
        reader2.close()
    assert sorted(row["company_id"] for row in rows2) == sorted([first, second])


def test_malformed_install_refuses(db_path):
    first, second = _companies(db_path)
    bad = get_connection(db_path)
    try:
        _set_install_empty(bad)
        bad.commit()
    finally:
        bad.close()
    conn = get_connection(db_path)
    try:
        with pytest.raises(authority_sink.LedgerWriteRefused) as excinfo:
            _write_one(conn, first)
        assert excinfo.value.args == ("AUTHORITY_NOT_READY",)
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
    conn2 = get_connection(db_path)
    try:
        with pytest.raises(authority_sink.LedgerWriteRefused) as excinfo2:
            _write_two(conn2, first, second)
        assert excinfo2.value.args == ("AUTHORITY_NOT_READY",)
    finally:
        try:
            conn2.rollback()
        except Exception:
            pass
        conn2.close()
    reader = get_connection(db_path)
    try:
        assert _read_heads(reader) == []
    finally:
        reader.close()


def test_active_not_ready_refuses(db_path):
    first, second = _companies(db_path)
    fx.make_active(db_path)
    conn = get_connection(db_path)
    try:
        with pytest.raises(authority_sink.LedgerWriteRefused) as excinfo:
            _write_one(conn, first)
        assert excinfo.value.args == ("AUTHORITY_NOT_READY",)
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
    conn2 = get_connection(db_path)
    try:
        with pytest.raises(authority_sink.LedgerWriteRefused) as excinfo2:
            _write_two(conn2, first, second)
        assert excinfo2.value.args == ("AUTHORITY_NOT_READY",)
    finally:
        try:
            conn2.rollback()
        except Exception:
            pass
        conn2.close()
    reader = get_connection(db_path)
    try:
        assert _read_heads(reader) == []
    finally:
        reader.close()


def test_active_ready_refuses_without_context(db_path, monkeypatch, caplog):
    """not qualification: patched readiness below proves nothing."""
    first, second = _companies(db_path)
    fx.make_active(db_path)
    monkeypatch.setattr(authority_readiness, "is_ready", lambda c: True)
    conn = get_connection(db_path)
    try:
        with caplog.at_level(logging.WARNING, logger="erpclaw.authority"):
            with pytest.raises(authority_sink.LedgerWriteRefused) as excinfo:
                _write_one(conn, first)
        assert excinfo.value.args == ("LEDGER_WRITE_UNAUTHORIZED",)
        assert isinstance(excinfo.value, authority_gate.AuthorityRefusal)
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
    conn2 = get_connection(db_path)
    try:
        with caplog.at_level(logging.WARNING, logger="erpclaw.authority"):
            with pytest.raises(authority_sink.LedgerWriteRefused) as excinfo2:
                _write_two(conn2, first, second)
        assert excinfo2.value.args == ("LEDGER_WRITE_UNAUTHORIZED",)
        assert isinstance(excinfo2.value, authority_gate.AuthorityRefusal)
    finally:
        try:
            conn2.rollback()
        except Exception:
            pass
        conn2.close()
    wanted = ("ledger write refused: code=LEDGER_WRITE_UNAUTHORIZED"
              " table=gl_chain_head action=-")
    hits = [record for record in caplog.records
            if record.name == "erpclaw.authority"
            and record.levelno == logging.WARNING
            and record.getMessage() == wanted]
    assert len(hits) == 2
    reader = get_connection(db_path)
    try:
        assert _read_heads(reader) == []
    finally:
        reader.close()


def test_unenforced_family_passes_at_active(db_path, monkeypatch):
    """not qualification: patched readiness below proves nothing."""
    _companies(db_path)
    fx.make_active(db_path)
    monkeypatch.setattr(authority_readiness, "is_ready", lambda c: True)
    conn = get_connection(db_path)
    try:
        conn.execute("DELETE FROM stock_fifo_layer WHERE 1 = 0")
        table = Table("payment_ledger_entry")
        query = Q.update(table).set(
            Field("remarks"), P()).where(
            Field("id") == P()).get_sql()
        conn.execute(query, ("x", "no-such-id"))
        conn.commit()
    finally:
        conn.close()


def test_non_ledger_statements_never_probe(db_path, monkeypatch):
    def _fail(conn):
        raise AssertionError("install_phase must not run here")
    monkeypatch.setattr(authority_gate, "install_phase", _fail)
    conn = get_connection(db_path)
    try:
        rows = conn.execute("SELECT * FROM gl_entry").fetchall()
        assert list(rows) == []
        cid = str(uuid.uuid4())
        table = Table("company")
        query = Q.into(table).columns(
            "id", "name", "abbr", "default_currency", "country",
            "fiscal_year_start_month").insert(
            P(), P(), P(), P(), P(), P()).get_sql()
        conn.execute(query, (cid, "Probe Co", "PB", "USD",
                             "United States", 1))
        conn.commit()
        conn.execute("SAVEPOINT probe_check")
        conn.execute("RELEASE SAVEPOINT probe_check")
    finally:
        conn.close()
    assert set(dbmod.LEDGER_SINK_NAMES) == set(authority_sink.LEDGER_SINKS)


def test_arguments_forwarded_unchanged(db_path, monkeypatch):
    conn = get_connection(db_path)
    try:
        raw = object.__getattribute__(conn, "_conn")
        for args in (("SELECT 1",), ("SELECT ?", (1,)),
                     ("SELECT 1", None)):
            try:
                want_cur = raw.execute(*args)
                want_rows = [tuple(row) for row in want_cur.fetchall()]
                want_exc = None
            except Exception as exc:
                want_rows = None
                want_exc = (type(exc), str(exc))
            try:
                got_cur = conn.execute(*args)
                got_rows = [tuple(row) for row in got_cur.fetchall()]
                got_exc = None
            except Exception as exc:
                got_rows = None
                got_exc = (type(exc), str(exc))
            assert got_exc == want_exc
            assert got_rows == want_rows
        seen = []
        orig = authority_sink.check_statement
        def _recorder(wrapper, sql):
            seen.append(sql)
            return orig(wrapper, sql)
        monkeypatch.setattr(authority_sink, "check_statement", _recorder)
        first, _second = _companies(db_path)
        text = _head_insert_sql()
        params = (first, 0, "GENESIS", STAMP)
        conn.execute(text, params)
        conn.commit()
        assert seen and seen[-1] == text
        assert seen[-1] is text
    finally:
        conn.close()


@pytest.mark.skipif(not os.environ.get("ERPCLAW_PG_TEST_URL"),
                    reason="live Postgres required")
def test_pg_legs(monkeypatch, caplog):
    """not qualification: patched readiness below proves nothing."""
    from erpclaw_lib import seam
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", _PG_URL)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    seam.dispose_engines()
    try:
        _pg_identity(_PG_URL)
        from setup_helpers import init_all_tables as _init_all
        target = None

        def _fresh_two():
            _pg_reset_schema()
            _init_all(None)
            first = fx.seed_company(target)
            second = fx.seed_company(target, name="Second Co", abbr="SC")
            return first, second

        first, second = _fresh_two()
        conn = get_connection(target)
        try:
            _write_one(conn, first)
            conn.commit()
        finally:
            conn.close()
        reader = get_connection(target)
        try:
            assert len(_read_heads(reader)) == 1
        finally:
            reader.close()
        wiper = get_connection(target)
        try:
            table = Table("gl_chain_head")
            wiper.execute(Q.from_(table).delete().get_sql())
            wiper.commit()
        finally:
            wiper.close()
        conn2 = get_connection(target)
        try:
            _write_two(conn2, first, second)
            conn2.commit()
        finally:
            conn2.close()
        reader2 = get_connection(target)
        try:
            rows2 = _read_heads(reader2)
        finally:
            reader2.close()
        assert sorted(row["company_id"] for row in rows2) == sorted(
            [first, second])

        first, second = _fresh_two()
        dropper = get_connection(target)
        try:
            dropper.execute("DROP TABLE authority_install CASCADE")
            dropper.commit()
        finally:
            dropper.close()
        conn = get_connection(target)
        try:
            _write_one(conn, first)
            conn.commit()
        finally:
            conn.close()
        reader = get_connection(target)
        try:
            assert len(_read_heads(reader)) == 1
        finally:
            reader.close()

        first, second = _fresh_two()
        bad = get_connection(target)
        try:
            _set_install_empty(bad)
            bad.commit()
        finally:
            bad.close()
        conn = get_connection(target)
        try:
            with pytest.raises(authority_sink.LedgerWriteRefused) as excinfo:
                _write_one(conn, first)
            assert excinfo.value.args == ("AUTHORITY_NOT_READY",)
        finally:
            try:
                conn.rollback()
            except Exception:
                pass
            conn.close()
        conn2 = get_connection(target)
        try:
            with pytest.raises(authority_sink.LedgerWriteRefused) as excinfo2:
                _write_two(conn2, first, second)
            assert excinfo2.value.args == ("AUTHORITY_NOT_READY",)
        finally:
            try:
                conn2.rollback()
            except Exception:
                pass
            conn2.close()

        first, second = _fresh_two()
        fx.make_active(target)
        conn = get_connection(target)
        try:
            with pytest.raises(authority_sink.LedgerWriteRefused) as excinfo:
                _write_one(conn, first)
            assert excinfo.value.args == ("AUTHORITY_NOT_READY",)
        finally:
            try:
                conn.rollback()
            except Exception:
                pass
            conn.close()

        first, second = _fresh_two()
        fx.make_active(target)
        monkeypatch.setattr(authority_readiness, "is_ready", lambda c: True)
        conn = get_connection(target)
        try:
            with caplog.at_level(logging.WARNING,
                                 logger="erpclaw.authority"):
                with pytest.raises(
                        authority_sink.LedgerWriteRefused) as excinfo:
                    _write_one(conn, first)
            assert excinfo.value.args == ("LEDGER_WRITE_UNAUTHORIZED",)
        finally:
            try:
                conn.rollback()
            except Exception:
                pass
            conn.close()
        monkeypatch.setattr(authority_readiness, "is_ready",
                            lambda c: False)

        first, second = _fresh_two()
        conn = get_connection(target)
        try:
            try:
                conn.execute("SELECT * FROM no_such_table").fetchall()
            except Exception:
                pass
            calls = []
            orig_phase = authority_gate.install_phase
            def _recorder_phase(handle):
                calls.append(True)
                return orig_phase(handle)
            monkeypatch.setattr(authority_gate, "install_phase",
                                _recorder_phase)
            import psycopg2.errors as _pg_errors
            with pytest.raises(_pg_errors.InFailedSqlTransaction):
                _write_one(conn, first)
            assert calls == []
        finally:
            try:
                conn.rollback()
            except Exception:
                pass
            conn.close()
        check = get_connection(target)
        try:
            check.execute("SELECT 1").fetchone()
            check.commit()
        finally:
            check.close()
    finally:
        seam.dispose_engines()
