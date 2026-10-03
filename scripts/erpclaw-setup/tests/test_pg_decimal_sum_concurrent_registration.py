"""Concurrent first connects register the decimal_sum aggregate exactly once.

Opening a connection must never fail because another process is opening one at
the same moment. On PostgreSQL ``get_connection`` calls
``_ensure_pg_decimal_sum`` on every connect; that helper used to run
``CREATE OR REPLACE FUNCTION`` twice plus a ``DO`` block on every single
connect, so two processes connecting to a fresh schema at once could fail with
``tuple concurrently updated`` (concurrent ``CREATE OR REPLACE`` of the same
function) or a duplicate-object error (both pass the existence check, both
``CREATE AGGREGATE``). A multi-process deployment (a web worker pool, two
agents, a gate) hits this on its first connects.

The helper now has a fast path (one search-path-scoped existence read, no DDL
when the aggregate is already there) and a slow path serialised by a
transaction-scoped advisory lock. These tests pin that behaviour against a live
server:

  1. eight subprocesses connecting to a fresh, empty schema at the same moment
     all succeed and read the same exact sum (repeated over five fresh schemas);
  2. once registered, further connects run no DDL (the support functions'
     ``pg_proc`` rows keep their ``xmin``);
  3. the aggregate still sums exactly, including the empty-set result.

GATED: skipped unless ``ERPCLAW_PG_TEST_URL`` points at a reachable, EXPENDABLE
Postgres database (it creates and drops throwaway schemas named ``m823_*`` and
nothing else). CI has no Postgres, so this stays skipped there; run it where a
live server exists:

    ERPCLAW_PG_TEST_URL='postgresql://erpclaw@localhost/erpclaw_test' \
        pytest source/erpclaw/scripts/erpclaw-setup/tests/test_pg_decimal_sum_concurrent_registration.py
"""
import os
import subprocess
import sys
import time
import urllib.parse
import uuid

import pytest

SETUP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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
    reason="ERPCLAW_PG_TEST_URL not set (live Postgres required for m823)",
)

_PROCS = 8
_ROUNDS = 5
_SUM_SQL = "SELECT decimal_sum(x) AS total FROM (VALUES ('1.10'), ('2.20')) AS t(x)"


def _schema_url(schema):
    """The base test URL pointed at ``schema`` via a connection option, so the
    server sets ``search_path`` before any session SQL runs (in particular
    before ``get_connection`` registers the aggregate)."""
    sep = "&" if "?" in PG_URL else "?"
    return (PG_URL + sep + "options="
            + urllib.parse.quote("-csearch_path=" + schema, safe=""))


def _new_schema():
    """Create a fresh, empty schema in the test database and return its name."""
    import psycopg2
    schema = "m823_" + uuid.uuid4().hex[:12]
    setup = psycopg2.connect(PG_URL)
    try:
        setup.autocommit = True
        cur = setup.cursor()
        try:
            cur.execute('CREATE SCHEMA "%s"' % schema)
        finally:
            cur.close()
    finally:
        setup.close()
    return schema


def _drop_schema(schema):
    """Drop a schema this module created, with everything it registered."""
    import psycopg2
    setup = psycopg2.connect(PG_URL)
    try:
        setup.autocommit = True
        cur = setup.cursor()
        try:
            cur.execute('DROP SCHEMA "%s" CASCADE' % schema)
        finally:
            cur.close()
    finally:
        setup.close()


_WORKER = (
    "import os, sys, time\n"
    "sys.path.insert(0, %r)\n"
    "ready = os.environ['M823_READY']\n"
    "barrier = os.environ['M823_BARRIER']\n"
    "open(ready, 'w').write('ready')\n"
    "deadline = time.time() + 60\n"
    "while not os.path.exists(barrier):\n"
    "    if time.time() > deadline:\n"
    "        sys.stderr.write('barrier wait timed out\\n')\n"
    "        sys.exit(2)\n"
    "    time.sleep(0.02)\n"
    "os.environ['ERPCLAW_DB_DIALECT'] = 'postgresql'\n"
    "from erpclaw_lib.db import get_connection\n"
    "conn = get_connection()\n"
    "try:\n"
    "    row = conn.execute(%r).fetchone()\n"
    "    sys.stdout.write(str(row['total']) + '\\n')\n"
    "finally:\n"
    "    conn.close()\n"
) % (ERPCLAW_LIB, _SUM_SQL)


def _worker_env(url, ready, barrier):
    env = dict(os.environ)
    env["ERPCLAW_DB_DIALECT"] = "postgresql"
    env["ERPCLAW_DB_URL"] = url
    env.pop("ERPCLAW_DB_PATH", None)
    env.pop("ERPCLAW_DB_READONLY", None)
    env["M823_READY"] = ready
    env["M823_BARRIER"] = barrier
    return env


def _run_first_connect_race(tmp_path, procs=_PROCS):
    """One race round: fresh empty schema, ``procs`` subprocesses released
    together, every one must exit 0 printing ``3.30``. The schema is always
    dropped again, including on failure."""
    schema = _new_schema()
    url = _schema_url(schema)
    tag = "%s_%s" % (schema, uuid.uuid4().hex[:6])
    barrier = str(tmp_path / ("m823_go_%s" % tag))
    children = []
    try:
        for i in range(procs):
            ready = str(tmp_path / ("m823_ready_%s_%d" % (tag, i)))
            children.append(subprocess.Popen(
                [sys.executable, "-c", _WORKER],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=_worker_env(url, ready, barrier)))
        deadline = time.time() + 60
        while time.time() < deadline:
            if all(os.path.exists(str(tmp_path / ("m823_ready_%s_%d" % (tag, i))))
                   for i in range(procs)):
                break
            time.sleep(0.05)
        else:
            raise AssertionError("workers did not all reach the barrier in 60s")
        with open(barrier, "w") as fh:
            fh.write("go")
        for child in children:
            try:
                out, err = child.communicate(timeout=120)
            except subprocess.TimeoutExpired:
                child.kill()
                out, err = child.communicate()
                raise AssertionError("worker hung: %s" % err)
            assert child.returncode == 0, (
                "worker exited %s: %s" % (child.returncode, err))
            assert out.strip() == "3.30", (
                "worker printed %r (stderr: %s)" % (out, err))
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
        _drop_schema(schema)


def test_concurrent_first_connects_all_succeed(tmp_path):
    """Eight processes connecting to an empty schema at once all succeed."""
    for _ in range(_ROUNDS):
        _run_first_connect_race(tmp_path)


def _support_function_xmins(url, schema):
    """Map each support-function name to its pg_proc xmin in ``schema``."""
    import psycopg2
    probe = psycopg2.connect(url)
    try:
        cur = probe.cursor()
        try:
            cur.execute(
                "SELECT p.proname, p.xmin::text FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = %s AND p.proname IN "
                "('erpclaw_decimal_sum_sfunc', 'erpclaw_decimal_sum_ffunc')",
                (schema,))
            rows = dict(cur.fetchall())
        finally:
            cur.close()
    finally:
        probe.close()
    return rows


def test_fast_path_runs_no_ddl(monkeypatch):
    """After registration, further connects rewrite no catalog rows."""
    schema = _new_schema()
    try:
        url = _schema_url(schema)
        monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
        monkeypatch.setenv("ERPCLAW_DB_URL", url)
        monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
        monkeypatch.delenv("ERPCLAW_DB_READONLY", raising=False)
        from erpclaw_lib.db import get_connection
        get_connection().close()
        before = _support_function_xmins(url, schema)
        assert set(before) == {
            "erpclaw_decimal_sum_sfunc", "erpclaw_decimal_sum_ffunc"}, before
        for _ in range(3):
            get_connection().close()
        assert _support_function_xmins(url, schema) == before
    finally:
        _drop_schema(schema)


def test_decimal_sum_still_correct(monkeypatch):
    """Exact sums, including the empty-set result pinned from the base."""
    schema = _new_schema()
    try:
        url = _schema_url(schema)
        monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
        monkeypatch.setenv("ERPCLAW_DB_URL", url)
        monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
        monkeypatch.delenv("ERPCLAW_DB_READONLY", raising=False)
        from erpclaw_lib.db import get_connection
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT decimal_sum(x) AS total "
                "FROM (VALUES ('0.10'), ('0.20')) AS t(x)").fetchone()
            assert str(row["total"]) == "0.30"
            row = conn.execute(
                "SELECT decimal_sum(x) AS total "
                "FROM (VALUES ('0.10'), ('0.20')) AS t(x) WHERE false").fetchone()
            assert str(row["total"]) == "0"
        finally:
            conn.close()
    finally:
        _drop_schema(schema)
