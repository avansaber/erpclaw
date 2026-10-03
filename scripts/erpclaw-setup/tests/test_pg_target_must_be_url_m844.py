"""m844 - install and upgrade refuse a non-URL target before any write."""
import importlib.util
import json
import os
import subprocess
import sys
import types
import urllib.parse

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SETUP_DIR = os.path.dirname(_TESTS_DIR)
_INIT_SCHEMA_PATH = os.path.join(_SETUP_DIR, "init_schema.py")
_RUNNER_PATH = os.path.join(_SETUP_DIR, "migration_runner.py")
_DB_QUERY_PATH = os.path.join(_SETUP_DIR, "db_query.py")

_IN_TREE_LIB = os.path.join(_SETUP_DIR, "lib")
if _IN_TREE_LIB not in sys.path:
    sys.path.insert(0, _IN_TREE_LIB)

PG_URL = os.environ.get("ERPCLAW_PG_TEST_URL")


def _expected(source):
    return (
        "PostgreSQL target from %s is not a postgresql:// URL; use the URL form,"
        " for example postgresql:///<database>?host=<socket directory>."
        " A libpq keyword string (dbname=... host=...) is not accepted here." % (source,)
    )


_NO_TARGET_RUNNER = (
    "ERPCLAW_DB_DIALECT=postgresql but the migration runner has no target: "
    "set ERPCLAW_DB_URL, pass a postgresql:// URL, or set ERPCLAW_DB_PATH to a postgresql:// URL."
)

_NO_TARGET_INIT = (
    "PostgreSQL connection failed for the configured target: "
    "ERPCLAW_DB_DIALECT=postgresql but no connection URL (set ERPCLAW_DB_URL or pass db_path)."
)


def _init_schema_mod(name="init_schema_m844"):
    spec = importlib.util.spec_from_file_location(name, _INIT_SCHEMA_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _runner_mod(name="migration_runner_m844"):
    spec = importlib.util.spec_from_file_location(name, _RUNNER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_require_pg_url_accepts_postgresql_scheme():
    from erpclaw_lib.db import require_pg_url
    target = "postgresql:///books?host=/run/pg"
    assert require_pg_url(target, source="--db-path") == target


def test_require_pg_url_accepts_postgres_scheme():
    from erpclaw_lib.db import require_pg_url
    target = "postgres://db.internal:5432/books"
    assert require_pg_url(target, source="ERPCLAW_DB_URL") == target


@pytest.mark.parametrize("source", ["--db-path", "ERPCLAW_DB_URL", "ERPCLAW_DB_PATH"])
@pytest.mark.parametrize("bad", ["dbname=books", "host=/tmp dbname=books", "", "/tmp/books.sqlite"])
def test_require_pg_url_refuses_non_url(monkeypatch, source, bad):
    from erpclaw_lib.db import require_pg_url
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    with pytest.raises(RuntimeError) as excinfo:
        require_pg_url(bad, source=source)
    assert str(excinfo.value) == _expected(source)


@pytest.mark.parametrize("source", ["--db-path", "ERPCLAW_DB_URL", "ERPCLAW_DB_PATH"])
def test_require_pg_url_refuses_driver_scheme(monkeypatch, source):
    from erpclaw_lib.db import require_pg_url
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    with pytest.raises(RuntimeError) as excinfo:
        require_pg_url("postgresql+psycopg2://h/db", source=source)
    assert str(excinfo.value) == _expected(source)


@pytest.mark.parametrize("source", ["--db-path", "ERPCLAW_DB_URL", "ERPCLAW_DB_PATH"])
def test_require_pg_url_refuses_uppercase_scheme(monkeypatch, source):
    from erpclaw_lib.db import require_pg_url
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    with pytest.raises(RuntimeError) as excinfo:
        require_pg_url("POSTGRESQL://h/db", source=source)
    assert str(excinfo.value) == _expected(source)


def test_require_pg_url_never_echoes_target(monkeypatch):
    from erpclaw_lib.db import require_pg_url
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    secret = "s3cr3t-pw-xyz"
    with pytest.raises(RuntimeError) as excinfo:
        require_pg_url("dbname=books host=/run/pg password=%s" % secret, source="--db-path")
    assert str(excinfo.value) == _expected("--db-path")
    assert secret not in str(excinfo.value)


def test_init_db_postgres_no_target_message(monkeypatch):
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    mod = _init_schema_mod("init_schema_m844_no_target")
    with pytest.raises(RuntimeError) as excinfo:
        mod._init_db_postgres(None)
    assert str(excinfo.value) == _NO_TARGET_INIT


def test_init_db_postgres_keyword_db_path_refused_before_connect(monkeypatch):
    import erpclaw_lib.db as db
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    calls = []
    def _recorder(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("must not connect")
    monkeypatch.setattr(db, "get_connection", _recorder)
    mod = _init_schema_mod("init_schema_m844_kw_path")
    with pytest.raises(RuntimeError) as excinfo:
        mod._init_db_postgres("dbname=books host=/run/pg")
    assert str(excinfo.value) == _expected("--db-path")
    assert calls == []


def test_init_db_postgres_keyword_env_url_refused_before_connect(monkeypatch):
    import erpclaw_lib.db as db
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", "dbname=books host=/run/pg")
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    calls = []
    def _recorder(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("must not connect")
    monkeypatch.setattr(db, "get_connection", _recorder)
    mod = _init_schema_mod("init_schema_m844_kw_env")
    with pytest.raises(RuntimeError) as excinfo:
        mod._init_db_postgres(None)
    assert str(excinfo.value) == _expected("ERPCLAW_DB_URL")
    assert calls == []


def test_resolve_target_refuses_env_url_keyword(monkeypatch):
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", "dbname=books")
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    runner = _runner_mod("migration_runner_m844_env_kw")
    with pytest.raises(RuntimeError) as excinfo:
        runner._resolve_target(None)
    assert str(excinfo.value) == _expected("ERPCLAW_DB_URL")


def test_resolve_target_returns_url_unchanged(monkeypatch):
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", "postgresql:///books?host=/run/pg")
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    runner = _runner_mod("migration_runner_m844_env_ok")
    assert runner._resolve_target(None) == "postgresql:///books?host=/run/pg"


def test_resolve_target_keyword_db_path_keeps_no_target_message(monkeypatch):
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    runner = _runner_mod("migration_runner_m844_no_target")
    with pytest.raises(RuntimeError) as excinfo:
        runner._resolve_target("dbname=books host=/run/pg")
    assert str(excinfo.value) == _NO_TARGET_RUNNER


def test_router_refuses_before_link(monkeypatch):
    import erpclaw_lib.db as db
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    spec = importlib.util.spec_from_file_location("db_query_m844", _DB_QUERY_PATH)
    dq = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dq)
    link_calls = []
    monkeypatch.setattr(dq, "_link_shared_library", lambda: link_calls.append(1))
    def _boom(*args, **kwargs):
        raise AssertionError("must not connect")
    monkeypatch.setattr(db, "get_connection", _boom)
    args = types.SimpleNamespace(db_path="dbname=books host=/run/pg", force=False, force_reinit=False)
    with pytest.raises(RuntimeError) as excinfo:
        dq.initialize_database(None, args)
    assert str(excinfo.value) == _expected("--db-path")
    assert link_calls == []


def _guard_expendable_target(monkeypatch):
    """Refuse any database that is not an expendable local test target."""
    from urllib.parse import unquote, urlparse

    from erpclaw_lib.db import get_connection

    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", PG_URL)
    expected_db = unquote(urlparse(PG_URL).path.lstrip("/"))
    guard = get_connection(PG_URL)
    try:
        current_db = guard.execute("SELECT current_database()").fetchone()[0]
        server_addr = guard.execute("SELECT inet_server_addr()").fetchone()[0]
        listen = guard.execute(
            "SELECT current_setting('listen_addresses')").fetchone()[0]
        version_num = guard.execute(
            "SELECT current_setting('server_version_num')").fetchone()[0]
        version = guard.execute("SELECT version()").fetchone()[0]
    finally:
        guard.close()
    print("version(): %s" % version)
    print("current_database(): %s" % current_db)
    if current_db != expected_db:
        pytest.fail("refusing: connected database is not the one in the URL")
    if server_addr is not None:
        pytest.fail("refusing: server is reachable over the network")
    if listen != "":
        pytest.fail("refusing: server listens on an address")
    if not str(version_num).startswith("16"):
        pytest.fail("refusing: server is not PostgreSQL 16")


def _reset_public_schema():
    """Drop and recreate the shared ``public`` schema (as
    ``test_migration_pg_drop_constraint.py`` does) so the table count the
    action reports is the count of a fresh full-schema build."""
    import psycopg2
    setup = psycopg2.connect(PG_URL)
    try:
        setup.autocommit = True
        cur = setup.cursor()
        try:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        finally:
            cur.close()
    finally:
        setup.close()


def _keyword_for_url(url):
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parts.query)
    dbname = urllib.parse.unquote(parts.path.lstrip("/"))
    host = query.get("host", [""])[0]
    port = query.get("port", [""])[0]
    user = urllib.parse.unquote(parts.username) if parts.username else ""
    bits = ["dbname=%s" % dbname]
    if host:
        bits.append("host=%s" % host)
    if port:
        bits.append("port=%s" % port)
    if user:
        bits.append("user=%s" % user)
    return " ".join(bits)


@pytest.mark.skipif(not PG_URL, reason="ERPCLAW_PG_TEST_URL not set (live Postgres required for m844)")
def test_live_keyword_refused_before_any_write(monkeypatch, tmp_path):
    _guard_expendable_target(monkeypatch)
    keyword = _keyword_for_url(PG_URL)
    assert "dbname=" in keyword
    _reset_public_schema()

    home = tmp_path / "home"
    (home / ".openclaw" / "erpclaw").mkdir(parents=True)
    erpclaw_home = str(home / ".openclaw" / "erpclaw")

    env = {**os.environ,
           "HOME": str(home),
           "ERPCLAW_HOME": erpclaw_home,
           "ERPCLAW_DB_DIALECT": "postgresql"}
    env.pop("ERPCLAW_DB_URL", None)
    env.pop("ERPCLAW_DB_PATH", None)

    proc = subprocess.run(
        [sys.executable, _DB_QUERY_PATH, "--action", "initialize-database",
         "--db-path", keyword],
        env=env, capture_output=True, text=True, timeout=300,
        cwd=str(tmp_path))
    both = proc.stdout + proc.stderr
    assert proc.returncode == 1, both
    payload = json.loads(proc.stdout)
    assert payload["status"] == "error", both
    assert payload["message"] == _expected("--db-path"), both
    assert not os.path.exists(os.path.join(erpclaw_home, "lib")), both
    assert sorted(p.name for p in tmp_path.iterdir()) == ["home"], both

    from erpclaw_lib.seam import table_names
    assert len(table_names(PG_URL)) == 0, both

    proc_ok = subprocess.run(
        [sys.executable, _DB_QUERY_PATH, "--action", "initialize-database",
         "--db-path", PG_URL],
        env=env, capture_output=True, text=True, timeout=300,
        cwd=str(tmp_path))
    both_ok = proc_ok.stdout + proc_ok.stderr
    assert proc_ok.returncode == 0, both_ok
    payload_ok = json.loads(proc_ok.stdout)
    assert payload_ok["status"] == "ok", both_ok
