"""Unit tests for the selling PostgreSQL schema-reset guard.

No live server: the helper module's own ``get_connection`` name is replaced
with a stub factory that records every SQL string and answers
``current_database()`` from the database in the URL the real resolution
would open (``helpers._resolve_pg_url`` — never stubbed, it only reads the
environment and never touches a server). The stub never calls the real
``get_connection``.
"""
import os
from urllib.parse import urlparse

import pytest

import selling_helpers as helpers


class _FakeCursor:
    def __init__(self, database):
        self._database = database

    def fetchone(self):
        return (self._database,)


class _FakeConnection:
    def __init__(self, url):
        self.url = url
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append(sql)
        return _FakeCursor(urlparse(self.url).path.strip("/"))

    def commit(self):
        pass

    def close(self):
        pass


def _install_stub(monkeypatch):
    opened = []

    def fake_get_connection(db_path=None):
        url = helpers._resolve_pg_url(db_path)
        conn = _FakeConnection(url)
        opened.append(conn)
        return conn

    monkeypatch.setattr(helpers, "get_connection", fake_get_connection)
    return opened


def test_reset_refuses_when_general_connection_resolves_elsewhere(monkeypatch):
    opened = _install_stub(monkeypatch)
    monkeypatch.setenv("ERPCLAW_PG_TEST_URL",
                       "postgresql://resetguard:5433/reset_alpha")
    monkeypatch.setenv("ERPCLAW_DB_URL",
                       "postgresql://resetguard:5433/reset_beta")
    with pytest.raises(RuntimeError) as excinfo:
        helpers._reset_pg_schema()
    message = str(excinfo.value)
    assert "reset_alpha" in message and "reset_beta" in message, \
        "refusal must name both databases: %s" % message
    assert not any("DROP" in statement
                   for conn in opened for statement in conn.statements), \
        "no DROP may run once the target is refused"


def test_reset_runs_when_urls_match(monkeypatch):
    opened = _install_stub(monkeypatch)
    url = "postgresql://resetguard:5433/reset_same"
    monkeypatch.setenv("ERPCLAW_PG_TEST_URL", url)
    monkeypatch.setenv("ERPCLAW_DB_URL", url)
    helpers._reset_pg_schema()
    statements = [statement for conn in opened for statement in conn.statements]
    assert sum("DROP SCHEMA" in statement for statement in statements) == 1, \
        "expected exactly one DROP SCHEMA: %r" % (statements,)
    assert sum("CREATE SCHEMA" in statement for statement in statements) == 1, \
        "expected exactly one CREATE SCHEMA: %r" % (statements,)


def test_reset_refuses_when_hosts_differ(monkeypatch):
    opened = _install_stub(monkeypatch)
    monkeypatch.setenv("ERPCLAW_PG_TEST_URL",
                       "postgresql://hosta:5433/reset_same")
    monkeypatch.setenv("ERPCLAW_DB_URL",
                       "postgresql://hostb:5433/reset_same")
    with pytest.raises(RuntimeError) as excinfo:
        helpers._reset_pg_schema()
    message = str(excinfo.value)
    assert "hosta" in message and "hostb" in message, \
        "refusal must name both hosts: %s" % message
    assert not any("DROP" in statement
                   for conn in opened for statement in conn.statements), \
        "no DROP may run once the target is refused"


def test_reset_refuses_when_ports_differ(monkeypatch):
    opened = _install_stub(monkeypatch)
    monkeypatch.setenv("ERPCLAW_PG_TEST_URL",
                       "postgresql://hostb:5433/reset_same")
    monkeypatch.setenv("ERPCLAW_DB_URL",
                       "postgresql://hostb:5434/reset_same")
    with pytest.raises(RuntimeError) as excinfo:
        helpers._reset_pg_schema()
    message = str(excinfo.value)
    assert "5433" in message and "5434" in message, \
        "refusal must name both ports: %s" % message
    assert not any("DROP" in statement
                   for conn in opened for statement in conn.statements), \
        "no DROP may run once the target is refused"


def test_reset_checks_the_url_the_connection_resolves_via_db_path(monkeypatch):
    opened = _install_stub(monkeypatch)
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    monkeypatch.setenv("ERPCLAW_DB_PATH",
                       "postgresql://hostb:5433/reset_same")
    monkeypatch.setenv("ERPCLAW_PG_TEST_URL",
                       "postgresql://hosta:5433/reset_same")
    with pytest.raises(RuntimeError) as excinfo:
        helpers._reset_pg_schema()
    message = str(excinfo.value)
    assert "hosta" in message and "hostb" in message, \
        "refusal must name both hosts: %s" % message
    assert not any("DROP" in statement
                   for conn in opened for statement in conn.statements), \
        "no DROP may run once the target is refused"


def test_reset_runs_when_db_path_resolves_to_the_test_url(monkeypatch):
    opened = _install_stub(monkeypatch)
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    url = "postgresql://hostb:5433/reset_same"
    monkeypatch.setenv("ERPCLAW_DB_PATH", url)
    monkeypatch.setenv("ERPCLAW_PG_TEST_URL", url)
    helpers._reset_pg_schema()
    statements = [statement for conn in opened for statement in conn.statements]
    assert sum("DROP SCHEMA" in statement for statement in statements) == 1, \
        "expected exactly one DROP SCHEMA: %r" % (statements,)
    assert sum("CREATE SCHEMA" in statement for statement in statements) == 1, \
        "expected exactly one CREATE SCHEMA: %r" % (statements,)
