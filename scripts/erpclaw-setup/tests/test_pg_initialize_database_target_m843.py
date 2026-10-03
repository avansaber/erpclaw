"""m843 — initialize-database verifies the PostgreSQL target it was given.

On the PostgreSQL branch `initialize_database` built the full schema at the
`--db-path` target (`init_db` prints success) and then verified a DIFFERENT
target: it opened `get_connection()` and called `table_names()` with no
target, so both resolved only the environment. With `ERPCLAW_DB_DIALECT`
set to `postgresql` and no `ERPCLAW_DB_URL` in the environment the action
exited 1 even though the schema it was asked to build was complete:

    ERPCLAW_DB_DIALECT=postgresql but no connection URL (set ERPCLAW_DB_URL or pass db_path).

(Fail-before output on the base, reproduced against a disposable
PostgreSQL 16 cluster: `init_db` prints success — 229 tables — then the
action exits 1 with the message above.)

The fix passes the action's own target down: `get_connection(db_path)` and
`table_names(db_path)`. When `db_path` is None the behaviour is unchanged
(environment resolution).

GATED: skipped unless ``ERPCLAW_PG_TEST_URL`` points at a reachable,
EXPENDABLE Postgres database. The guard below is copied from the other live
PostgreSQL tests in this directory (e.g.
``test_migration_039_purchase_line_discount.py``): the server must be a
local-socket-only PostgreSQL 16 and the connected database must be the one
named by the URL. ``initialize-database`` builds the full schema in
``public``, so the test resets that schema first (as
``test_migration_pg_drop_constraint.py`` does) and never points at shared
data. CI has no Postgres, so this stays skipped there; run it where a live
server exists:

    ERPCLAW_PG_TEST_URL='postgresql://postgres@/erpclaw_verify?host=/tmp/pg&port=5433' \
        pytest source/erpclaw/scripts/erpclaw-setup/tests/test_pg_initialize_database_target_m843.py
"""
import json
import os
import subprocess
import sys
import urllib.parse

import pytest

SETUP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB_QUERY_PATH = os.path.join(SETUP_DIR, "db_query.py")

# Bind erpclaw_lib to the tree under test, never the deployed
# ~/.openclaw/erpclaw/lib symlink — same reason as test_cross_db_addon_c.py.
_IN_TREE_LIB = os.path.join(SETUP_DIR, "lib")
ERPCLAW_LIB = (_IN_TREE_LIB if os.path.isdir(os.path.join(_IN_TREE_LIB, "erpclaw_lib"))
               else os.path.join(os.path.expanduser(
                   os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))
if ERPCLAW_LIB not in sys.path:
    import importlib.util
    if importlib.util.find_spec("erpclaw_lib") is None:
        sys.path.insert(0, ERPCLAW_LIB)

PG_URL = os.environ.get("ERPCLAW_PG_TEST_URL")

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="ERPCLAW_PG_TEST_URL not set (live Postgres required for m843)",
)


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


def test_initialize_database_verifies_given_pg_target(monkeypatch, tmp_path):
    """`initialize-database --db-path <pg URL>` succeeds with no URL in env."""
    _guard_expendable_target(monkeypatch)
    _reset_public_schema()

    home = tmp_path / "home"
    (home / ".openclaw" / "erpclaw").mkdir(parents=True)

    env = {**os.environ,
           "HOME": str(home),
           "ERPCLAW_HOME": str(home / ".openclaw" / "erpclaw"),
           "ERPCLAW_DB_DIALECT": "postgresql"}
    # The whole point: the action must verify the --db-path target, not the
    # environment. (Copied from test_pg_init_redaction.py, which also
    # re-points $ERPCLAW_HOME/lib under tmp_path.)
    env.pop("ERPCLAW_DB_URL", None)
    env.pop("ERPCLAW_DB_PATH", None)

    proc = subprocess.run(
        [sys.executable, _DB_QUERY_PATH, "--action", "initialize-database",
         "--db-path", PG_URL],
        env=env, capture_output=True, text=True, timeout=300)

    both = proc.stdout + proc.stderr
    assert proc.returncode == 0, both
    payload = json.loads(proc.stdout)
    assert payload["status"] == "ok", both
    assert payload["dialect"] == "postgresql", both

    from erpclaw_lib.seam import table_names
    assert payload["table_count"] == len(table_names(PG_URL)), both
    assert payload["table_count"] > 0, both

    password = urllib.parse.urlsplit(PG_URL).password
    if password:
        assert password not in proc.stdout, (
            "the database password reached stdout")
        assert password not in proc.stderr, (
            "the database password reached stderr")
