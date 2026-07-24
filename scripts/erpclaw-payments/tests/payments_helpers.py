"""Shared helper functions for ERPClaw Payments unit tests (WS2 D3).

Provides:
  - DB bootstrap via init_schema.init_db()
  - call_action() / ns() / is_error() / is_ok()
  - Seed functions for company, accounts, FY, CC, parties, invoices (+ voucher PLE)
  - load_db_query() for explicit module loading (avoids sys.path collisions)

Mirrors the selling/buying test-helper shape so the payments suite runs the
same way (`pytest source/erpclaw/scripts/erpclaw-payments/tests/`).
"""
import argparse
import importlib.util
import io
import json
import os
import sqlite3
import sys
import uuid
from decimal import Decimal
from unittest.mock import patch

# ──────────────────────────────────────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────────────────────────────────────

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
MODULE_DIR = os.path.dirname(TESTS_DIR)  # erpclaw-payments/
SETUP_DIR = os.path.join(os.path.dirname(MODULE_DIR), "erpclaw-setup")
INIT_SCHEMA_PATH = os.path.join(SETUP_DIR, "init_schema.py")

# Make erpclaw_lib importable
ERPCLAW_LIB = os.path.expanduser("~/.openclaw/erpclaw/lib")
if ERPCLAW_LIB not in sys.path:
    sys.path.insert(0, ERPCLAW_LIB)

from erpclaw_lib.db import setup_pragmas


def load_db_query():
    """Load this module's db_query.py explicitly to avoid sys.path collisions."""
    db_query_path = os.path.join(MODULE_DIR, "db_query.py")
    spec = importlib.util.spec_from_file_location("db_query_payments", db_query_path)
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
    return result.get("status") == "error"


def is_ok(result: dict) -> bool:
    return result.get("status") == "ok"


def _uuid() -> str:
    return str(uuid.uuid4())


# ──────────────────────────────────────────────────────────────────────────────
# Seed helpers
# ──────────────────────────────────────────────────────────────────────────────

def seed_account(conn, company_id: str, name: str, root_type: str,
                 account_type=None) -> str:
    aid = _uuid()
    direction = "debit_normal" if root_type in ("asset", "expense") else "credit_normal"
    conn.execute(
        """INSERT INTO account (id, name, account_number, root_type, account_type,
           balance_direction, company_id, depth)
           VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
        (aid, f"{name}-{aid[:6]}", f"ACC-{aid[:6]}", root_type, account_type,
         direction, company_id))
    conn.commit()
    return aid


def _ple_voucher(conn, *, posting_date, account_id, party_type, party_id,
                 voucher_type, voucher_id, amount):
    """Insert the invoice's voucher-level PLE (+grand_total) as selling/buying do."""
    conn.execute(
        "INSERT INTO payment_ledger_entry "
        "(id, posting_date, account_id, party_type, party_id, voucher_type, "
        " voucher_id, amount, amount_in_account_currency, currency, delinked) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'USD', 0)",
        (_uuid(), posting_date, account_id, party_type, party_id,
         voucher_type, voucher_id, str(amount), str(amount)))
    conn.commit()


def build_ar_env(conn) -> dict:
    """AR side: company + FY + default CC + bank/AR/discount/commission accounts
    + customer."""
    cid = _uuid()
    conn.execute("INSERT INTO company (id, name, abbr) VALUES (?, ?, ?)",
                 (cid, f"AR Co {cid[:6]}", f"AR{cid[:4]}"))
    conn.execute(
        "INSERT INTO fiscal_year (id, name, start_date, end_date, is_closed, company_id) "
        "VALUES (?, ?, '2026-01-01', '2026-12-31', 0, ?)",
        (_uuid(), f"FY-{cid[:6]}", cid))
    ccid = _uuid()
    conn.execute(
        "INSERT INTO cost_center (id, name, company_id, is_group) VALUES (?, ?, ?, 0)",
        (ccid, f"Main CC {cid[:6]}", cid))
    conn.execute("UPDATE company SET default_cost_center_id = ? WHERE id = ?",
                 (ccid, cid))
    bank = seed_account(conn, cid, "Bank", "asset", "bank")
    ar = seed_account(conn, cid, "Debtors", "asset")
    discount = seed_account(conn, cid, "Early Pay Discounts", "expense")
    commission = seed_account(conn, cid, "Commission Expense", "expense")
    cust = _uuid()
    conn.execute(
        "INSERT INTO customer (id, name, customer_type, status, company_id) "
        "VALUES (?, 'Bruce', 'company', 'active', ?)", (cust, cid))
    conn.commit()
    return {"company_id": cid, "cc": ccid, "bank": bank, "ar": ar,
            "discount": discount, "commission": commission, "customer": cust}


def build_ap_env(conn) -> dict:
    """AP side: company + FY + default CC + bank/AP/TDS accounts + supplier."""
    cid = _uuid()
    conn.execute("INSERT INTO company (id, name, abbr) VALUES (?, ?, ?)",
                 (cid, f"AP Co {cid[:6]}", f"AP{cid[:4]}"))
    conn.execute(
        "INSERT INTO fiscal_year (id, name, start_date, end_date, is_closed, company_id) "
        "VALUES (?, ?, '2026-01-01', '2026-12-31', 0, ?)",
        (_uuid(), f"FY-{cid[:6]}", cid))
    ccid = _uuid()
    conn.execute(
        "INSERT INTO cost_center (id, name, company_id, is_group) VALUES (?, ?, ?, 0)",
        (ccid, f"Main CC {cid[:6]}", cid))
    conn.execute("UPDATE company SET default_cost_center_id = ? WHERE id = ?",
                 (ccid, cid))
    bank = seed_account(conn, cid, "Bank", "asset", "bank")
    ap = seed_account(conn, cid, "Creditors", "liability")
    tds = seed_account(conn, cid, "TDS Payable", "liability")
    supp = _uuid()
    conn.execute(
        "INSERT INTO supplier (id, name, supplier_type, status, company_id) "
        "VALUES (?, 'Gotham Steel', 'company', 'active', ?)", (supp, cid))
    conn.commit()
    return {"company_id": cid, "cc": ccid, "bank": bank, "ap": ap,
            "tds": tds, "supplier": supp}


def seed_sales_invoice(conn, env, grand_total, status="submitted") -> str:
    """Submitted sales invoice + one line item + its voucher-level PLE."""
    si_id = _uuid()
    conn.execute(
        "INSERT INTO sales_invoice (id, customer_id, posting_date, grand_total, "
        " total_amount, tax_amount, rounding_adjustment, outstanding_amount, "
        " status, company_id) "
        "VALUES (?, ?, '2026-06-01', ?, ?, '0', '0', ?, ?, ?)",
        (si_id, env["customer"], str(grand_total), str(grand_total),
         str(grand_total), status, env["company_id"]))
    conn.execute(
        "INSERT INTO sales_invoice_item (id, sales_invoice_id, item_id, quantity, "
        " rate, amount, net_amount) VALUES (?, ?, 'ITEM-1', '1', ?, ?, ?)",
        (_uuid(), si_id, str(grand_total), str(grand_total), str(grand_total)))
    _ple_voucher(conn, posting_date="2026-06-01", account_id=env["ar"],
                 party_type="customer", party_id=env["customer"],
                 voucher_type="sales_invoice", voucher_id=si_id,
                 amount=grand_total)
    return si_id


def seed_purchase_invoice(conn, env, grand_total, status="submitted") -> str:
    """Submitted purchase invoice + one line item + its voucher-level PLE."""
    pi_id = _uuid()
    conn.execute(
        "INSERT INTO purchase_invoice (id, supplier_id, posting_date, grand_total, "
        " total_amount, tax_amount, rounding_adjustment, outstanding_amount, "
        " status, company_id) "
        "VALUES (?, ?, '2026-06-01', ?, ?, '0', '0', ?, ?, ?)",
        (pi_id, env["supplier"], str(grand_total), str(grand_total),
         str(grand_total), status, env["company_id"]))
    conn.execute(
        "INSERT INTO purchase_invoice_item (id, purchase_invoice_id, item_id, "
        " quantity, rate, amount) VALUES (?, ?, 'ITEM-1', '1', ?, ?)",
        (_uuid(), pi_id, str(grand_total), str(grand_total)))
    _ple_voucher(conn, posting_date="2026-06-01", account_id=env["ap"],
                 party_type="supplier", party_id=env["supplier"],
                 voucher_type="purchase_invoice", voucher_id=pi_id,
                 amount=grand_total)
    return pi_id
