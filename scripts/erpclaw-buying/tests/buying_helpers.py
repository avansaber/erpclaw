"""Shared helper functions for ERPClaw Buying unit tests."""
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

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
MODULE_DIR = os.path.dirname(TESTS_DIR)
SCRIPTS_DIR = MODULE_DIR
SETUP_DIR = os.path.join(os.path.dirname(MODULE_DIR), "erpclaw-setup")
INIT_SCHEMA_PATH = os.path.join(SETUP_DIR, "init_schema.py")

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


def load_db_query():
    """Load this module's db_query.py explicitly to avoid sys.path collisions."""
    db_query_path = os.path.join(SCRIPTS_DIR, "db_query.py")
    spec = importlib.util.spec_from_file_location("db_query_buying", db_query_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PG_TEST_DB_PREFIX = "erpclaw_muse_t_"
PG_TEST_DB_ALLOWLIST = ("erpclaw_integration", "erpclaw_muse_test_b")


def assert_pg_test_database(test_url=None) -> str:
    """Refuse unless the PostgreSQL test database name is expendable.

    Returns the database named by ``ERPCLAW_PG_TEST_URL`` when it starts
    with ``erpclaw_muse_t_`` or is one of the allowlisted integration
    databases; raises ``RuntimeError`` otherwise. Runs before any
    connection is opened, so a refused target issues no SQL at all.
    """
    from urllib.parse import urlparse
    url = test_url if test_url is not None else os.environ.get("ERPCLAW_PG_TEST_URL")
    if not url:
        raise RuntimeError(
            "refusing to reset the shared schema: ERPCLAW_PG_TEST_URL is "
            "not set, so the reset target is unknown")
    dbname = urlparse(url).path.strip("/")
    if not dbname:
        raise RuntimeError(
            "refusing to reset the shared schema: ERPCLAW_PG_TEST_URL "
            "names no database")
    if not (dbname.startswith(PG_TEST_DB_PREFIX)
            or dbname in PG_TEST_DB_ALLOWLIST):
        raise RuntimeError(
            "refusing to reset the shared schema: database %r is not an "
            "expendable test database (must start with %r or be one of %r)"
            % (dbname, PG_TEST_DB_PREFIX, list(PG_TEST_DB_ALLOWLIST)))
    return dbname


def _reset_pg_schema():
    """Drop and recreate the shared ``public`` schema on PostgreSQL.

    Per-test isolation for the PostgreSQL branch: ``DROP SCHEMA public
    CASCADE`` clears every table left by the previous test in one
    statement. The guard above runs first and refuses any database whose
    name is not an expendable test database; no host/port comparison is
    made (the fixture sets ``ERPCLAW_DB_URL`` from ``ERPCLAW_PG_TEST_URL``
    itself, so comparing the two would prove nothing).
    """
    assert_pg_test_database()
    conn = get_connection()
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
    finally:
        conn.close()


def _init_pg_schema():
    """Provision the schema on the configured PostgreSQL target.

    Reset the shared schema, then run ``init_schema.init_db(None)`` so
    the seam resolves the configured target (``ERPCLAW_DB_URL``) instead
    of a file path.
    """
    _reset_pg_schema()
    spec = importlib.util.spec_from_file_location("init_schema", INIT_SCHEMA_PATH)
    schema_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(schema_mod)
    schema_mod.init_db(None)


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
    PostgreSQL: the foundation ``get_connection()``; ``db_path`` is
    ignored because the seam resolves the configured target.
    """
    if get_dialect() == "postgresql":
        return get_connection()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    setup_pragmas(conn)
    conn.create_aggregate("decimal_sum", 1, _DecimalSum)
    return conn


def call_action(fn, conn, args) -> dict:
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
    return argparse.Namespace(**kwargs)


def is_error(result: dict) -> bool:
    return result.get("status") == "error"


def is_ok(result: dict) -> bool:
    return result.get("status") == "ok"


def _uuid() -> str:
    return str(uuid.uuid4())


# ── Seed helpers ──

def seed_company(conn, name="Test Co", abbr="TC") -> str:
    cid = _uuid()
    conn.execute(
        """INSERT INTO company (id, name, abbr, default_currency, country,
           fiscal_year_start_month)
           VALUES (?, ?, ?, 'USD', 'United States', 1)""",
        (cid, f"{name} {cid[:6]}", f"{abbr}{cid[:4]}")
    )
    conn.commit()
    return cid


def seed_account(conn, company_id, name="Test Account",
                 root_type="asset", account_type=None, account_number=None) -> str:
    aid = _uuid()
    direction = "debit_normal" if root_type in ("asset", "expense") else "credit_normal"
    conn.execute(
        """INSERT INTO account (id, name, account_number, root_type, account_type,
           balance_direction, company_id, depth) VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
        (aid, name, account_number or f"ACC-{aid[:6]}", root_type,
         account_type, direction, company_id)
    )
    conn.commit()
    return aid


def seed_fiscal_year(conn, company_id, name=None,
                     start="2026-01-01", end="2026-12-31") -> str:
    fid = _uuid()
    conn.execute(
        """INSERT INTO fiscal_year (id, name, start_date, end_date, company_id)
           VALUES (?, ?, ?, ?, ?)""",
        (fid, name or f"FY-{fid[:6]}", start, end, company_id)
    )
    conn.commit()
    return fid


def seed_cost_center(conn, company_id, name="Main CC") -> str:
    ccid = _uuid()
    conn.execute(
        """INSERT INTO cost_center (id, name, company_id, is_group) VALUES (?, ?, ?, 0)""",
        (ccid, name, company_id)
    )
    conn.commit()
    return ccid


def seed_supplier(conn, company_id, name="Test Supplier") -> str:
    sid = _uuid()
    conn.execute(
        """INSERT INTO supplier (id, name, company_id, supplier_type, status)
           VALUES (?, ?, ?, 'company', 'active')""",
        (sid, name, company_id)
    )
    conn.commit()
    return sid


def seed_item(conn, name="Test Item", stock_uom="Each") -> str:
    iid = _uuid()
    conn.execute(
        """INSERT INTO item (id, item_name, item_code, stock_uom, is_stock_item)
           VALUES (?, ?, ?, ?, 1)""",
        (iid, name, f"ITEM-{iid[:6]}", stock_uom)
    )
    conn.commit()
    return iid


def seed_warehouse(conn, company_id, name="Main Warehouse", account_id=None) -> str:
    wid = _uuid()
    conn.execute(
        """INSERT INTO warehouse (id, name, company_id, account_id) VALUES (?, ?, ?, ?)""",
        (wid, name, company_id, account_id)
    )
    conn.commit()
    return wid


def seed_naming_series(conn, company_id):
    series = [
        ("purchase_order", "PO-", 0),
        ("purchase_receipt", "PR-", 0),
        ("purchase_invoice", "PI-", 0),
        ("material_request", "MR-", 0),
        ("request_for_quotation", "RFQ-", 0),
        ("debit_note", "DN-", 0),
        ("journal_entry", "JE-", 0),
    ]
    for entity_type, prefix, current in series:
        conn.execute(insert_or_ignore(
            """INSERT OR IGNORE INTO naming_series
               (id, entity_type, prefix, current_value, company_id)
               VALUES (?, ?, ?, ?, ?)"""),
            (_uuid(), entity_type, prefix, current, company_id)
        )
    conn.commit()


def build_buying_env(conn) -> dict:
    """Create full buying test environment."""
    cid = seed_company(conn)
    fyid = seed_fiscal_year(conn, cid)
    ccid = seed_cost_center(conn, cid, "Main CC")

    cash = seed_account(conn, cid, "Cash", "asset", "cash", "1000")
    ap = seed_account(conn, cid, "Accounts Payable", "liability", "payable", "2000")
    expense = seed_account(conn, cid, "Purchases", "expense", "expense", "5000")
    cogs = seed_account(conn, cid, "COGS", "expense", "cost_of_goods_sold", "5100")
    stock_acct = seed_account(conn, cid, "Stock In Hand", "asset", "stock", "1200")
    srnb = seed_account(conn, cid, "Stock Received Not Billed", "liability",
                        "stock_received_not_billed", "2150")

    wh = seed_warehouse(conn, cid, "Main Warehouse", stock_acct)

    conn.execute(
        """UPDATE company SET
           default_payable_account_id = ?,
           default_expense_account_id = ?,
           default_cost_center_id = ?,
           default_warehouse_id = ?
           WHERE id = ?""",
        (ap, expense, ccid, wh, cid)
    )
    conn.commit()

    item1 = seed_item(conn, "Raw Material A")
    item2 = seed_item(conn, "Raw Material B")
    supplier = seed_supplier(conn, cid, "Acme Supplies")
    seed_naming_series(conn, cid)

    return {
        "company_id": cid, "fiscal_year_id": fyid, "cc": ccid,
        "cash": cash, "ap": ap, "expense": expense,
        "cogs": cogs, "stock_acct": stock_acct, "srnb": srnb, "warehouse": wh,
        "item1": item1, "item2": item2,
        "supplier": supplier,
    }
