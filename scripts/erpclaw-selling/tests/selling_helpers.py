"""Shared helper functions for ERPClaw Selling unit tests.

Provides:
  - DB bootstrap via init_schema.init_db()
  - call_action() / ns() / is_error() / is_ok()
  - Seed functions for company, accounts, items, warehouses, customers
  - load_db_query() for explicit module loading (avoids sys.path collisions)
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
MODULE_DIR = os.path.dirname(TESTS_DIR)  # erpclaw-selling/
SCRIPTS_DIR = MODULE_DIR                  # db_query.py lives here
# init_schema.py is in erpclaw-setup (sibling module)
SETUP_DIR = os.path.join(os.path.dirname(MODULE_DIR), "erpclaw-setup")
INIT_SCHEMA_PATH = os.path.join(SETUP_DIR, "init_schema.py")

# Make erpclaw_lib importable
ERPCLAW_LIB = os.path.expanduser("~/.openclaw/erpclaw/lib")
if ERPCLAW_LIB not in sys.path:
    sys.path.insert(0, ERPCLAW_LIB)

from erpclaw_lib.db import setup_pragmas


def load_db_query():
    """Load this module's db_query.py explicitly to avoid sys.path collisions."""
    db_query_path = os.path.join(SCRIPTS_DIR, "db_query.py")
    spec = importlib.util.spec_from_file_location("db_query_selling", db_query_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────────────────────────────────────────────────────────────
# DB helpers
# ──────────────────────────────────────────────────────────────────────────────

def init_all_tables(db_path: str):
    """Create all ERPClaw core tables using init_schema.init_db()."""
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


def get_conn(db_path: str) -> sqlite3.Connection:
    """Return a sqlite3.Connection with FK enabled and Row factory."""
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
                 account_number=None) -> str:
    """Insert a GL account and return its ID."""
    aid = _uuid()
    direction = "debit_normal" if root_type in ("asset", "expense") else "credit_normal"
    conn.execute(
        """INSERT INTO account (id, name, account_number, root_type, account_type,
           balance_direction, company_id, depth)
           VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
        (aid, name, account_number or f"ACC-{aid[:6]}", root_type,
         account_type, direction, company_id)
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
        """INSERT INTO customer (id, name, company_id, customer_type, status, credit_limit)
           VALUES (?, ?, ?, 'company', 'active', '0')""",
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


def seed_item(conn, name="Test Item", stock_uom="Each") -> str:
    """Insert an item and return its ID."""
    iid = _uuid()
    conn.execute(
        """INSERT INTO item (id, item_name, item_code, stock_uom, is_stock_item)
           VALUES (?, ?, ?, ?, 1)""",
        (iid, name, f"ITEM-{iid[:6]}", stock_uom)
    )
    conn.commit()
    return iid


def seed_warehouse(conn, company_id: str, name="Main Warehouse",
                   account_id=None) -> str:
    """Insert a warehouse and return its ID."""
    wid = _uuid()
    conn.execute(
        """INSERT INTO warehouse (id, name, company_id, account_id)
           VALUES (?, ?, ?, ?)""",
        (wid, name, company_id, account_id)
    )
    conn.commit()
    return wid


def seed_naming_series(conn, company_id: str):
    """Seed naming series for common entity types."""
    series = [
        ("quotation", "QTN-", 0),
        ("sales_order", "SO-", 0),
        ("delivery_note", "DN-", 0),
        ("sales_invoice", "SI-", 0),
        ("credit_note", "CN-", 0),
        ("purchase_order", "PO-", 0),
        ("purchase_invoice", "PI-", 0),
        ("journal_entry", "JE-", 0),
    ]
    for entity_type, prefix, current in series:
        conn.execute(
            """INSERT OR IGNORE INTO naming_series
               (id, entity_type, prefix, current_value, company_id)
               VALUES (?, ?, ?, ?, ?)""",
            (_uuid(), entity_type, prefix, current, company_id)
        )
    conn.commit()


def seed_stock_entry(conn, item_id: str, warehouse_id: str,
                     qty="100", valuation_rate="10.00"):
    """Insert SLE entry to establish stock balance for an item."""
    sle_id = _uuid()
    stock_value = str(Decimal(qty) * Decimal(valuation_rate))
    conn.execute(
        """INSERT INTO stock_ledger_entry
           (id, item_id, warehouse_id, posting_date, actual_qty,
            qty_after_transaction, valuation_rate, stock_value,
            stock_value_difference, voucher_type, voucher_id, is_cancelled)
           VALUES (?, ?, ?, '2026-01-01', ?, ?, ?, ?, ?, 'stock_entry', ?, 0)""",
        (sle_id, item_id, warehouse_id, qty, qty,
         valuation_rate, stock_value, stock_value,
         f"INIT-{sle_id[:8]}")
    )
    conn.commit()


def build_selling_env(conn) -> dict:
    """Create a full selling test environment: company + accounts + FY + CC + item + WH + customer.

    Returns dict with all IDs needed for order-to-cash tests.
    """
    cid = seed_company(conn)
    fyid = seed_fiscal_year(conn, cid)
    ccid = seed_cost_center(conn, cid, "Main CC")

    # Key GL accounts
    cash = seed_account(conn, cid, "Cash", "asset", "cash", "1000")
    ar = seed_account(conn, cid, "Accounts Receivable", "asset", "receivable", "1100")
    revenue = seed_account(conn, cid, "Sales Revenue", "income", "revenue", "4000")
    cogs = seed_account(conn, cid, "COGS", "expense", "cost_of_goods_sold", "5000")
    stock_acct = seed_account(conn, cid, "Stock In Hand", "asset", "stock", "1200")

    # Set company defaults
    # Warehouse first (needed for company default)
    wh = seed_warehouse(conn, cid, "Main Warehouse", stock_acct)

    conn.execute(
        """UPDATE company SET
           default_receivable_account_id = ?,
           default_income_account_id = ?,
           default_cost_center_id = ?,
           default_warehouse_id = ?
           WHERE id = ?""",
        (ar, revenue, ccid, wh, cid)
    )
    conn.commit()
    item1 = seed_item(conn, "Widget A")
    item2 = seed_item(conn, "Widget B")
    seed_stock_entry(conn, item1, wh, "100", "10.00")
    seed_stock_entry(conn, item2, wh, "50", "20.00")

    # Customer
    cust = seed_customer(conn, cid, "Acme Corp")

    # Naming series
    seed_naming_series(conn, cid)

    return {
        "company_id": cid, "fiscal_year_id": fyid, "cc": ccid,
        "cash": cash, "ar": ar, "revenue": revenue, "cogs": cogs,
        "stock_acct": stock_acct, "warehouse": wh,
        "item1": item1, "item2": item2,
        "customer": cust,
    }
