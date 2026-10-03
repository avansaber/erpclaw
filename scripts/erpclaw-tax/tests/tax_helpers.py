"""Shared helper functions for ERPClaw Tax unit tests.

Mirrors source/erpclaw/scripts/erpclaw-gl/tests/gl_helpers.py:
  - init_all_tables() from init_schema.py
  - call_action() / ns() / is_error() / is_ok()
  - Direct-SQL seed helpers for company, accounts, customers, suppliers
    (seeding non-tax tables directly follows the GL suite pattern).

Tax-specific additions:
  - ns() carries every flag the tax module's argparse parser defines, so
    handler attribute access never raises AttributeError.
  - TAX_TABLES / snapshot() cover the eight tables the tax module owns, so
    negative tests can prove a refusal wrote nothing.
  - Connections come from erpclaw_lib.db.get_connection(), which also
    registers the decimal_sum aggregate the withholding handlers need.
"""
import argparse
import importlib.util
import io
import json
import os
import sys
import uuid
from unittest.mock import patch

# ──────────────────────────────────────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────────────────────────────────────

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
MODULE_DIR = os.path.dirname(TESTS_DIR)  # erpclaw-tax/
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
    spec = importlib.util.spec_from_file_location("db_query_tax", db_query_path)
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

from erpclaw_lib.db import get_connection


# ──────────────────────────────────────────────────────────────────────────────
# DB helpers
# ──────────────────────────────────────────────────────────────────────────────

def init_all_tables(db_path: str):
    """Create all ERPClaw core tables using init_schema.init_db()."""
    spec = importlib.util.spec_from_file_location("init_schema", INIT_SCHEMA_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.init_db(db_path)


def get_conn(db_path: str):
    """Return an erpclaw_lib connection with FK, Row factory, decimal_sum."""
    return get_connection(db_path)


# ──────────────────────────────────────────────────────────────────────────────
# Action invocation helpers
# ──────────────────────────────────────────────────────────────────────────────

# Every flag the tax module's CLI parser defines, so handlers can read any
# attribute without AttributeError. Mirrors the parser in db_query.py main().
_NS_DEFAULTS = dict(
    name=None, tax_type=None, company_id=None, company_name=None,
    tax_template_id=None, is_default=None, lines=None, description=None,
    tax_category_id=None, priority=None, customer_id=None, customer_group=None,
    supplier_id=None, shipping_state=None, party_type=None, party_id=None,
    transaction_type=None, shipping_address=None, items=None, item_overrides=None,
    item_id=None, tax_rate=None, wh_rate=None, threshold_amount=None,
    form_type=None, tax_year=None, withholding_amount=None, voucher_type=None,
    voucher_id=None, ple_amount=None, limit="20", offset="0",
)


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
    data = dict(_NS_DEFAULTS)
    data.update(kwargs)
    return argparse.Namespace(**data)


def is_error(result: dict) -> bool:
    """Check if a call_action result is an error response."""
    return result.get("status") == "error"


def is_ok(result: dict) -> bool:
    """Check if a call_action result is a success response."""
    return result.get("status") == "ok"


# ──────────────────────────────────────────────────────────────────────────────
# Owned-table snapshot (for proving refusals write nothing)
# ──────────────────────────────────────────────────────────────────────────────

TAX_TABLES = (
    "tax_template",
    "tax_template_line",
    "tax_category",
    "tax_rule",
    "item_tax_template",
    "tax_withholding_category",
    "tax_withholding_group",
    "tax_withholding_entry",
)


def snapshot(conn) -> dict:
    """Row counts for every table the tax module owns."""
    return {
        table: conn.execute(
            "SELECT COUNT(*) AS cnt FROM %s" % table).fetchone()["cnt"]
        for table in TAX_TABLES
    }


# ──────────────────────────────────────────────────────────────────────────────
# Seed helpers (direct SQL for tables owned by other modules — GL pattern)
# ──────────────────────────────────────────────────────────────────────────────

def _uuid() -> str:
    return str(uuid.uuid4())


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
                 root_type="liability", account_type="tax",
                 account_number=None, is_group=0) -> str:
    """Insert a GL account and return its ID.

    Defaults are liability/tax so withholding-category creation finds a
    withholding account; pass explicit types where the test needs otherwise.
    """
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


def seed_customer(conn, company_id: str, name="Test Customer",
                  customer_group=None, exempt=False) -> str:
    """Insert a customer and return its ID."""
    cid = _uuid()
    conn.execute(
        """INSERT INTO customer (id, name, company_id, customer_group,
           exempt_from_sales_tax)
           VALUES (?, ?, ?, ?, ?)""",
        (cid, name, company_id, customer_group, 1 if exempt else 0)
    )
    conn.commit()
    return cid


def seed_supplier(conn, company_id: str, name="Test Supplier",
                  is_1099=False, tax_id=None) -> str:
    """Insert a supplier and return its ID."""
    sid = _uuid()
    conn.execute(
        """INSERT INTO supplier (id, name, company_id, is_1099_vendor, tax_id)
           VALUES (?, ?, ?, ?, ?)""",
        (sid, name, company_id, 1 if is_1099 else 0, tax_id)
    )
    conn.commit()
    return sid


def seed_withholding_entry(conn, supplier_id: str, category_id: str,
                           fiscal_year: str, taxable_amount: str) -> str:
    """Insert a tax_withholding_entry row via direct SQL and return its ID.

    The tax module owns this table, so direct seeding is normally avoided —
    but record-1099-payment cannot write here (see DEFECT-01 in CHANGES.md:
    the supplier table has no withholding-category column, so the handler
    always refuses). Direct seeding is the only way to exercise the 1099 /
    withholding read paths until that defect is fixed.
    """
    eid = _uuid()
    conn.execute(
        """INSERT INTO tax_withholding_entry
           (id, party_type, party_id, category_id, fiscal_year,
            taxable_amount, withheld_amount)
           VALUES (?, 'supplier', ?, ?, ?, ?, '0')""",
        (eid, supplier_id, category_id, fiscal_year, taxable_amount)
    )
    conn.commit()
    return eid


def tl(account_id: str, rate: str, charge_type="on_net_total",
       add_deduct="add", row_order=0) -> dict:
    """Build one tax-template line dict (caller json.dumps a list of these)."""
    return {"tax_account_id": account_id, "rate": rate,
            "charge_type": charge_type, "add_deduct": add_deduct,
            "row_order": row_order}
