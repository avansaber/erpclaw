"""Wave G F11 — the PostgreSQL connection URL never prints its password.

On the Postgres branch `db_path` IS the connection URL (erpclaw_lib.db resolves
`postgresql://user:password@host/db`), and `_init_db_postgres` interpolated it
straight into a stderr line while `initialize-database` echoed it again in its
JSON response. Both wrote the database password into terminal scrollback, CI
logs, and any captured install transcript.

Everything except the credential is deliberately KEPT: an operator still needs
host, port, user and database name to act on the message. So each pin asserts
both halves — the password is gone AND the context is still there.

Offline: the failure pin points at a socket directory that cannot exist, and the
success pin drives `_init_db_postgres` against a stub connection. No server, no
network, and it runs on the Postgres branch by construction.
"""
import importlib.util
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stderr

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SETUP_DIR = os.path.dirname(_TESTS_DIR)
_INIT_SCHEMA_PATH = os.path.join(_SETUP_DIR, "init_schema.py")
_DB_QUERY_PATH = os.path.join(_SETUP_DIR, "db_query.py")

_PASSWORD = "s3cr3t-p4ssw0rd"
_URL = f"postgresql://erpclaw:{_PASSWORD}@127.0.0.1:5433/erpclaw_wave_g"


def _init_schema():
    spec = importlib.util.spec_from_file_location("init_schema_f11", _INIT_SCHEMA_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# The redactor itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    # The shipped shape.
    (_URL, "postgresql://erpclaw:***@127.0.0.1:5433/erpclaw_wave_g"),
    # A password containing ':' is masked WHOLE, never half-printed.
    ("postgresql://erpclaw:pa:ss@db.internal/erp", "postgresql://erpclaw:***@db.internal/erp"),
    # No credential in the URL: unchanged.
    ("postgresql://erpclaw@db.internal:5432/erp", "postgresql://erpclaw@db.internal:5432/erp"),
    # Key/value DSN and query-parameter forms.
    ("host=db.internal user=erpclaw password=s3cr3t dbname=erp",
     "host=db.internal user=erpclaw password=*** dbname=erp"),
    ("postgresql://db.internal/erp?sslmode=require&password=s3cr3t",
     "postgresql://db.internal/erp?sslmode=require&password=***"),
    # SQLite: a filesystem path carries no credential and must pass through.
    ("/home/ubuntu/.openclaw/erpclaw/data.sqlite", "/home/ubuntu/.openclaw/erpclaw/data.sqlite"),
])
def test_redact_db_url(raw, expected):
    assert _init_schema().redact_db_url(raw) == expected


def test_redact_db_url_masks_a_url_embedded_in_driver_text():
    """Driver messages can echo the connection string; redact those too."""
    mod = _init_schema()
    out = mod.redact_db_url(f'could not connect using "{_URL}" after 4 attempts')
    assert _PASSWORD not in out
    assert "erpclaw:***@127.0.0.1:5433/erpclaw_wave_g" in out


# ---------------------------------------------------------------------------
# The two real print sites
# ---------------------------------------------------------------------------

class _StubCursor:
    def fetchone(self):
        return (1,)  # truthy: every idempotent seed probe reads "already present"


class _StubConn:
    """Minimal PgConnectionWrapper stand-in: execute()/commit()/close()."""

    def execute(self, sql, params=None):
        return _StubCursor()

    def commit(self):
        pass

    def close(self):
        pass


def test_postgres_success_line_is_redacted(monkeypatch):
    """The success print (the shipped leak site) carries the masked URL only."""
    mod = _init_schema()
    import erpclaw_lib.db as db

    monkeypatch.setattr(db, "get_connection", lambda *a, **k: _StubConn())
    buf = io.StringIO()
    with redirect_stderr(buf):
        mod._init_db_postgres(_URL)
    out = buf.getvalue()

    assert _PASSWORD not in out
    assert "erpclaw:***@127.0.0.1:5433/erpclaw_wave_g" in out
    assert "Backend: PostgreSQL" in out


def test_connection_failure_keeps_context_and_drops_the_password(tmp_path):
    """End-to-end: a real `initialize-database` against a Postgres URL whose
    server cannot exist. Neither stream may carry the password, and both the
    masked URL and the failure cause must still be there."""
    home = tmp_path / "home"
    (home / ".openclaw" / "erpclaw").mkdir(parents=True)
    # A socket directory that cannot exist → an immediate, non-retried failure.
    url = f"postgresql://erpclaw:{_PASSWORD}@/erpclaw_wave_g?host={tmp_path}/no-such-socket-dir"

    env = {**os.environ,
           "HOME": str(home),
           "ERPCLAW_HOME": str(home / ".openclaw" / "erpclaw"),
           "ERPCLAW_DB_DIALECT": "postgresql"}
    proc = subprocess.run(
        [sys.executable, _DB_QUERY_PATH, "--action", "initialize-database",
         "--db-path", url],
        env=env, capture_output=True, text=True, timeout=120)

    both = proc.stdout + proc.stderr
    assert proc.returncode != 0, both
    assert _PASSWORD not in both, "the database password reached the operator's console"
    assert url not in both

    payload = json.loads(proc.stdout)
    assert payload["status"] == "error"
    # Context preserved: which URL, which user, which database, and why.
    assert "erpclaw:***@" in payload["message"]
    assert "erpclaw_wave_g" in payload["message"]
    assert "PostgreSQL connection failed" in payload["message"]
