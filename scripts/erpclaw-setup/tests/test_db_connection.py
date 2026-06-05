"""Tests for the dialect-aware connection layer (Wave 0 — cross-DB add-on A).

Covers:
  - the qmark→pyformat placeholder translator (exact-value),
  - the SQLite path is unchanged (real connection: PRAGMAs, decimal_sum,
    sqlite3.Row dual access, custom attributes, fresh-file 0600),
  - the PostgreSQL branch of get_connection() with psycopg2.connect patched to
    a fake connection (no live Postgres needed): URL resolution, DictCursor
    cursor_factory, lock_timeout/statement_timeout SETs, post-setup commit,
    and that PgConnectionWrapper.execute translates placeholders and proxies
    through a cursor while custom attributes round-trip.

No real Postgres server is required; the psycopg2 seam is patched.
"""
import os
import sqlite3
import stat
import sys

import pytest

ERPCLAW_LIB = os.path.expanduser("~/.openclaw/erpclaw/lib")
if ERPCLAW_LIB not in sys.path:
    sys.path.insert(0, ERPCLAW_LIB)

from erpclaw_lib import db as dbmod
from erpclaw_lib.db import (
    _qmark_to_pyformat,
    _resolve_pg_url,
    get_connection,
    PgConnectionWrapper,
    ConnectionWrapper,
)


# ── Placeholder translator ────────────────────────────────────────────────

@pytest.mark.parametrize("sql,expected", [
    # bare placeholders become %s
    ("SELECT * FROM account WHERE id = ?", "SELECT * FROM account WHERE id = %s"),
    ("INSERT INTO t (a, b) VALUES (?, ?)", "INSERT INTO t (a, b) VALUES (%s, %s)"),
    # literal % is doubled so psycopg2 does not treat it as a format marker
    ("SELECT * FROM t WHERE name LIKE '%foo%'",
     "SELECT * FROM t WHERE name LIKE '%%foo%%'"),
    # a ? INSIDE a string literal is data, not a placeholder
    ("SELECT * FROM t WHERE q = 'why?' AND id = ?",
     "SELECT * FROM t WHERE q = 'why?' AND id = %s"),
    # combined: LIKE wildcard + real placeholder
    ("SELECT * FROM t WHERE name LIKE ? AND tag LIKE '%x%'",
     "SELECT * FROM t WHERE name LIKE %s AND tag LIKE '%%x%%'"),
    # doubled '' escape inside a literal is preserved
    ("UPDATE t SET note = 'it''s ok' WHERE id = ?",
     "UPDATE t SET note = 'it''s ok' WHERE id = %s"),
    # no placeholders, no percent — unchanged
    ("SELECT 1", "SELECT 1"),
])
def test_qmark_to_pyformat(sql, expected):
    assert _qmark_to_pyformat(sql) == expected


# ── SQLite path is unchanged ──────────────────────────────────────────────

def test_sqlite_connection_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("ERPCLAW_DB_DIALECT", raising=False)
    path = str(tmp_path / "fresh.sqlite")
    conn = get_connection(path)
    try:
        assert isinstance(conn, ConnectionWrapper)
        # PRAGMAs applied
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        # decimal_sum aggregate registered and exact
        conn.execute("CREATE TABLE m (amt TEXT)")
        conn.executemany("INSERT INTO m (amt) VALUES (?)", [("0.10",), ("0.20",)])
        assert conn.execute("SELECT decimal_sum(amt) FROM m").fetchone()[0] == "0.30"
        # sqlite3.Row dual access (positional + by-name)
        row = conn.execute("SELECT amt AS amt FROM m ORDER BY amt LIMIT 1").fetchone()
        assert row[0] == "0.10" and row["amt"] == "0.10"
        # custom attribute round-trips (the whole reason for the wrapper)
        conn.company_id = "co-1"
        assert conn.company_id == "co-1"
    finally:
        conn.close()
    # fresh file got 0600
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


# ── PostgreSQL branch — psycopg2 patched, no live server ──────────────────

class _FakeCursor:
    def __init__(self, log):
        self._log = log
        self.rows = []

    def execute(self, sql, params=None):
        self._log.append(("execute", sql, params))
        return self

    def executemany(self, sql, seq):
        self._log.append(("executemany", sql, list(seq)))
        return self

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def close(self):
        self._log.append(("cursor_close",))


class _FakeConn:
    """Minimal psycopg2-connection stand-in. __slots__ makes it reject unknown
    attribute assignment, exercising PgConnectionWrapper's fallback storage."""
    __slots__ = ("_log", "cursor_factory", "_committed", "_closed")

    def __init__(self, log):
        object.__setattr__(self, "_log", log)
        object.__setattr__(self, "cursor_factory", None)
        object.__setattr__(self, "_committed", 0)
        object.__setattr__(self, "_closed", False)

    def cursor(self):
        return _FakeCursor(self._log)

    def commit(self):
        object.__setattr__(self, "_committed", self._committed + 1)
        self._log.append(("commit",))

    def rollback(self):
        self._log.append(("rollback",))

    def close(self):
        object.__setattr__(self, "_closed", True)


@pytest.fixture
def fake_psycopg2(monkeypatch):
    """Patch the psycopg2 module that get_connection lazily imports."""
    import types
    log = []
    captured = {}

    fake = types.ModuleType("psycopg2")
    fake_extras = types.ModuleType("psycopg2.extras")
    fake_extras.DictCursor = object()  # sentinel; we only assert identity

    def fake_connect(url, cursor_factory=None):
        captured["url"] = url
        captured["cursor_factory"] = cursor_factory
        conn = _FakeConn(log)
        conn.cursor_factory = cursor_factory
        return conn

    fake.connect = fake_connect
    fake.extras = fake_extras
    monkeypatch.setitem(sys.modules, "psycopg2", fake)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", fake_extras)
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    return {"log": log, "captured": captured, "DictCursor": fake_extras.DictCursor}


def test_pg_get_connection_configures_session(fake_psycopg2, monkeypatch):
    monkeypatch.setenv("ERPCLAW_DB_URL", "postgresql://u@h/erpclaw_test")
    monkeypatch.delenv("ERPCLAW_PG_LOCK_TIMEOUT", raising=False)
    monkeypatch.delenv("ERPCLAW_PG_STATEMENT_TIMEOUT", raising=False)

    conn = get_connection()

    assert isinstance(conn, PgConnectionWrapper)
    cap = fake_psycopg2["captured"]
    assert cap["url"] == "postgresql://u@h/erpclaw_test"
    assert cap["cursor_factory"] is fake_psycopg2["DictCursor"]

    log = fake_psycopg2["log"]
    sets = [(sql, params) for kind, sql, params in
            [e for e in log if e[0] == "execute"]]
    assert ("SET lock_timeout = %s", ("5s",)) in sets
    assert ("SET statement_timeout = %s", ("0",)) in sets
    # session settings were committed so the handed-back conn is idle
    assert ("commit",) in log


def test_pg_timeouts_are_env_overridable(fake_psycopg2, monkeypatch):
    monkeypatch.setenv("ERPCLAW_DB_URL", "postgresql://u@h/db")
    monkeypatch.setenv("ERPCLAW_PG_LOCK_TIMEOUT", "10s")
    monkeypatch.setenv("ERPCLAW_PG_STATEMENT_TIMEOUT", "30s")

    get_connection()

    sets = [(e[1], e[2]) for e in fake_psycopg2["log"] if e[0] == "execute"]
    assert ("SET lock_timeout = %s", ("10s",)) in sets
    assert ("SET statement_timeout = %s", ("30s",)) in sets


def test_pg_execute_translates_placeholders(fake_psycopg2, monkeypatch):
    monkeypatch.setenv("ERPCLAW_DB_URL", "postgresql://u@h/db")
    conn = get_connection()
    fake_psycopg2["log"].clear()

    conn.execute("SELECT * FROM account WHERE id = ? AND name LIKE '%x%'", ("a-1",))

    assert fake_psycopg2["log"] == [
        ("execute",
         "SELECT * FROM account WHERE id = %s AND name LIKE '%%x%%'",
         ("a-1",)),
    ]


def test_pg_execute_no_params_passes_through(fake_psycopg2, monkeypatch):
    monkeypatch.setenv("ERPCLAW_DB_URL", "postgresql://u@h/db")
    conn = get_connection()
    fake_psycopg2["log"].clear()

    # No params → no %-processing by psycopg2 → SQL must be untouched.
    conn.execute("SELECT now()")

    assert fake_psycopg2["log"] == [("execute", "SELECT now()", None)]


def test_pg_custom_attribute_round_trips(fake_psycopg2, monkeypatch):
    monkeypatch.setenv("ERPCLAW_DB_URL", "postgresql://u@h/db")
    conn = get_connection()
    # _FakeConn has __slots__ so this assignment can't land on the underlying
    # connection — it must fall back to wrapper storage, then read back.
    conn.company_id = "co-42"
    assert conn.company_id == "co-42"


def test_pg_missing_url_raises(monkeypatch):
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    with pytest.raises(RuntimeError, match="no connection URL"):
        _resolve_pg_url(None)


def test_resolve_pg_url_precedence(monkeypatch):
    monkeypatch.setenv("ERPCLAW_DB_URL", "postgresql://env-url/db")
    monkeypatch.setenv("ERPCLAW_DB_PATH", "postgresql://env-path/db")
    # explicit arg wins
    assert _resolve_pg_url("postgresql://arg/db") == "postgresql://arg/db"
    # then ERPCLAW_DB_URL
    assert _resolve_pg_url(None) == "postgresql://env-url/db"
    # then ERPCLAW_DB_PATH
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    assert _resolve_pg_url(None) == "postgresql://env-path/db"
