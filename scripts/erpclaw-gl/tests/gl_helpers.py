"""Shared helper functions for ERPClaw GL unit tests.

Reuses the same patterns as erpclaw-setup tests:
  - init_all_tables() from init_schema.py
  - call_action() / ns() / is_error() / is_ok()
  - Seed functions for company, accounts, fiscal years
"""
import argparse
import importlib.util
import io
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

# ──────────────────────────────────────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────────────────────────────────────

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
MODULE_DIR = os.path.dirname(TESTS_DIR)  # erpclaw-gl/
SCRIPTS_DIR = MODULE_DIR                  # db_query.py lives here
# init_schema.py is in erpclaw-setup (sibling module)
SETUP_DIR = os.path.join(os.path.dirname(MODULE_DIR), "erpclaw-setup")
INIT_SCHEMA_PATH = os.path.join(SETUP_DIR, "init_schema.py")

# Make scripts importable (for db_query)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)


def load_db_query():
    """Load this module's db_query.py explicitly to avoid sys.path collisions."""
    db_query_path = os.path.join(SCRIPTS_DIR, "db_query.py")
    spec = importlib.util.spec_from_file_location("db_query_gl", db_query_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Make erpclaw_lib importable
# M54: bind erpclaw_lib to the tree under test, never the deployed
# ~/.openclaw/erpclaw/lib symlink — the last install to run wins that symlink,
# so with several worktrees in flight it resolves to a tree nobody is testing
# (and DANGLES once that worktree is removed). The deployed install stays as
# the fallback for a published module repo, which ships no source/erpclaw/.
_IN_TREE_LIB = os.path.join(SETUP_DIR, "lib")
ERPCLAW_LIB = (_IN_TREE_LIB if os.path.isdir(os.path.join(_IN_TREE_LIB, "erpclaw_lib"))
               else os.path.join(os.path.expanduser(
                   os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))
if ERPCLAW_LIB not in sys.path:
    if importlib.util.find_spec("erpclaw_lib") is None:
        sys.path.insert(0, ERPCLAW_LIB)

from erpclaw_lib.db import setup_pragmas, get_dialect, get_connection
from erpclaw_lib.query import insert_or_ignore


# ──────────────────────────────────────────────────────────────────────────────
# DB helpers
# ──────────────────────────────────────────────────────────────────────────────

def _reset_pg_schema():
    """Drop and recreate the shared ``public`` schema on PostgreSQL.

    Per-test isolation for the PostgreSQL branch: ``DROP SCHEMA public
    CASCADE`` clears every table, index, sequence and the ``decimal_sum``
    aggregate left by the previous test in one statement, so no test ever
    sees another test's rows. The fresh schema is re-provisioned by
    ``init_schema.init_db(None)`` (which re-registers ``decimal_sum`` via
    the ``get_connection`` it calls). Same shape as the L3 smoke suite's
    ``_provision_pg_schema``.
    """
    conn = get_connection()
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
    finally:
        conn.close()


def init_all_tables(db_path=None):
    """Create all ERPClaw core tables using init_schema.init_db().

    SQLite (default): ``db_path`` is the file to build — unchanged.
    PostgreSQL: the shared schema is reset (see ``_reset_pg_schema``) and
    ``init_db`` receives ``None`` so the seam resolves the configured
    target (``ERPCLAW_DB_URL``) instead of being handed a file path.
    """
    if get_dialect() == "postgresql":
        _reset_pg_schema()
        spec = importlib.util.spec_from_file_location("init_schema", INIT_SCHEMA_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.init_db(None)
        return
    spec = importlib.util.spec_from_file_location("init_schema", INIT_SCHEMA_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.init_db(db_path)


class _DecimalSum:
    """Custom SQLite aggregate: SUM using Python Decimal for precision."""
    def __init__(self):
        self.total = Decimal("0")
    def step(self, value):
        if value is not None:
            self.total += Decimal(str(value))
    def finalize(self):
        return str(self.total)


def get_conn(db_path=None):
    """Return a database connection with FK enabled and Row-style access.

    SQLite (default): a ``sqlite3.Connection`` on ``db_path`` — unchanged.
    PostgreSQL: the foundation ``get_connection()`` (a ``PgConnectionWrapper``
    with ``?`` → ``%s`` translation and the persistent ``decimal_sum``
    aggregate); ``db_path`` is ignored because the seam resolves the
    configured target.
    """
    if get_dialect() == "postgresql":
        return get_connection()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    setup_pragmas(conn)
    conn.create_aggregate("decimal_sum", 1, _DecimalSum)
    return conn


# ──────────────────────────────────────────────────────────────────────────────
# Action invocation helpers
# ──────────────────────────────────────────────────────────────────────────────

def call_action(fn, conn, args) -> dict:
    """Invoke a domain function, capture stdout JSON, return parsed dict."""
    buf = io.StringIO()

    def _fake_exit(code=0):
        raise SystemExit(code)

    try:
        with patch("sys.stdout", buf), patch("sys.exit", side_effect=_fake_exit):
            fn(conn, args)
    except SystemExit:
        pass

    output = buf.getvalue().strip()
    if not output:
        return {"status": "error", "message": "no output captured"}
    return json.loads(output)


def ns(**kwargs) -> argparse.Namespace:
    """Build an argparse.Namespace from keyword args (mimics CLI flags)."""
    return argparse.Namespace(**kwargs)


def is_error(result: dict) -> bool:
    """Check if a call_action result is an error response."""
    return result.get("status") == "error"


def is_ok(result: dict) -> bool:
    """Check if a call_action result is a success response."""
    return result.get("status") == "ok"


# ──────────────────────────────────────────────────────────────────────────────
# Utility
# ──────────────────────────────────────────────────────────────────────────────

def _uuid() -> str:
    return str(uuid.uuid4())


# ──────────────────────────────────────────────────────────────────────────────
# Seed helpers
# ──────────────────────────────────────────────────────────────────────────────

def seed_company(conn, name="Test Co", abbr="TC") -> str:
    """Insert a test company via direct SQL and return its ID."""
    cid = _uuid()
    conn.execute(
        """INSERT INTO company (id, name, abbr, default_currency, country,
           fiscal_year_start_month)
           VALUES (?, ?, ?, 'USD', 'United States', 1)""",
        (cid, f"{name} {cid[:6]}", f"{abbr}{cid[:4]}")
    )
    conn.commit()
    return cid


def seed_account(conn, company_id: str, name="Test Account",
                 root_type="asset", account_type=None,
                 account_number=None, is_group=0) -> str:
    """Insert a GL account and return its ID."""
    aid = _uuid()
    direction = "debit_normal" if root_type in ("asset", "expense") else "credit_normal"
    conn.execute(
        """INSERT INTO account (id, name, account_number, root_type, account_type,
           balance_direction, company_id, depth, is_group)
           VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)""",
        (aid, name, account_number or f"ACC-{aid[:6]}", root_type,
         account_type, direction, company_id, is_group)
    )
    conn.commit()
    return aid


def seed_fiscal_year(conn, company_id: str, name=None,
                     start="2026-01-01", end="2026-12-31") -> str:
    """Insert a fiscal year and return its ID."""
    fid = _uuid()
    conn.execute(
        """INSERT INTO fiscal_year (id, name, start_date, end_date, company_id)
           VALUES (?, ?, ?, ?, ?)""",
        (fid, name or f"FY-{fid[:6]}", start, end, company_id)
    )
    conn.commit()
    return fid


def seed_cost_center(conn, company_id: str, name="Main CC") -> str:
    """Insert a cost center and return its ID."""
    ccid = _uuid()
    conn.execute(
        """INSERT INTO cost_center (id, name, company_id, is_group)
           VALUES (?, ?, ?, 0)""",
        (ccid, name, company_id)
    )
    conn.commit()
    return ccid


def seed_customer(conn, company_id: str, name="Test Customer") -> str:
    """Insert a customer and return its ID."""
    cid = _uuid()
    conn.execute(
        """INSERT INTO customer (id, name, company_id)
           VALUES (?, ?, ?)""",
        (cid, name, company_id)
    )
    conn.commit()
    return cid


def seed_supplier(conn, company_id: str, name="Test Supplier") -> str:
    """Insert a supplier and return its ID."""
    sid = _uuid()
    conn.execute(
        """INSERT INTO supplier (id, name, company_id)
           VALUES (?, ?, ?)""",
        (sid, name, company_id)
    )
    conn.commit()
    return sid


def seed_currency(conn, code: str, name: str) -> None:
    """Insert a currency row, ignoring duplicates, on either backend.

    SQLite spells it INSERT OR IGNORE; PostgreSQL has no such verb and
    needs ON CONFLICT DO NOTHING. Routed through
    erpclaw_lib.query.insert_or_ignore() so no fixture carries a
    dialect-specific verb to the driver.
    """
    conn.execute(insert_or_ignore(
        "INSERT OR IGNORE INTO currency (code, name) VALUES (?, ?)"),
        (code, name))
    conn.commit()
