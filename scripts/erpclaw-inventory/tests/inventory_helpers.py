"""Shared helper functions for ERPClaw Inventory unit tests."""
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

ERPCLAW_LIB = os.path.expanduser("~/.openclaw/erpclaw/lib")
if ERPCLAW_LIB not in sys.path:
    sys.path.insert(0, ERPCLAW_LIB)


def load_db_query():
    """Load this module's db_query.py explicitly to avoid sys.path collisions."""
    db_query_path = os.path.join(SCRIPTS_DIR, "db_query.py")
    spec = importlib.util.spec_from_file_location("db_query_inventory", db_query_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def init_all_tables(db_path: str):
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


def get_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
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


def seed_item(conn, name="Test Item", stock_uom="Each",
              item_type="stock", standard_rate="0") -> str:
    iid = _uuid()
    is_stock = 1 if item_type == "stock" else 0
    conn.execute(
        """INSERT INTO item (id, item_name, item_code, stock_uom,
           is_stock_item, item_type, standard_rate, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'active')""",
        (iid, name, f"ITEM-{iid[:6]}", stock_uom, is_stock, item_type, standard_rate)
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


def seed_stock_entry_sle(conn, item_id, warehouse_id, qty="100", valuation_rate="10.00"):
    """Seed stock via direct SLE insert (for tests needing existing stock)."""
    sle_id = _uuid()
    stock_value = str(Decimal(qty) * Decimal(valuation_rate))
    conn.execute(
        """INSERT INTO stock_ledger_entry
           (id, item_id, warehouse_id, posting_date, actual_qty,
            qty_after_transaction, valuation_rate, stock_value,
            stock_value_difference, voucher_type, voucher_id, is_cancelled)
           VALUES (?, ?, ?, '2026-01-01', ?, ?, ?, ?, ?, 'stock_entry', ?, 0)""",
        (sle_id, item_id, warehouse_id, qty, qty,
         valuation_rate, stock_value, stock_value, f"INIT-{sle_id[:8]}"))
    conn.commit()
    return sle_id


def seed_naming_series(conn, company_id):
    series = [
        ("stock_entry", "SE-", 0),
        ("stock_reconciliation", "SR-", 0),
        ("stock_revaluation", "SRV-", 0),
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


def build_inventory_env(conn) -> dict:
    """Create full inventory test environment."""
    cid = seed_company(conn)
    fyid = seed_fiscal_year(conn, cid)
    ccid = seed_cost_center(conn, cid, "Main CC")

    cash = seed_account(conn, cid, "Cash", "asset", "cash", "1000")
    stock_acct = seed_account(conn, cid, "Stock In Hand", "asset", "stock", "1200")
    stock_adj = seed_account(conn, cid, "Stock Adjustment", "expense", "stock_adjustment", "5200")
    expense = seed_account(conn, cid, "Expenses", "expense", "expense", "5000")

    wh = seed_warehouse(conn, cid, "Main Warehouse", stock_acct)
    wh2 = seed_warehouse(conn, cid, "Secondary Warehouse", stock_acct)

    conn.execute(
        """UPDATE company SET
           default_cost_center_id = ?,
           default_warehouse_id = ?
           WHERE id = ?""",
        (ccid, wh, cid)
    )
    conn.commit()

    item1 = seed_item(conn, "Widget A", "Each", "stock", "50.00")
    item2 = seed_item(conn, "Widget B", "Kg", "stock", "100.00")

    # Seed some initial stock for item1
    seed_stock_entry_sle(conn, item1, wh, "100", "50.00")

    seed_naming_series(conn, cid)

    return {
        "company_id": cid, "fiscal_year_id": fyid, "cc": ccid,
        "cash": cash, "stock_acct": stock_acct, "stock_adj": stock_adj,
        "expense": expense,
        "warehouse": wh, "warehouse2": wh2,
        "item1": item1, "item2": item2,
    }
