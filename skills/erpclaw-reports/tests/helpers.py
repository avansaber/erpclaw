"""Shared test helpers for erpclaw-reports tests.

Provides _call_action() to invoke action functions directly and capture
their JSON output (which they print to stdout before calling sys.exit).
"""
import argparse
import io
import json
import os
import sys
import uuid
from datetime import datetime

# Add scripts to path for importing db_query
SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

# Add shared lib
LIB_DIR = os.path.expanduser("~/.openclaw/erpclaw/lib")
if LIB_DIR not in sys.path:
    sys.path.insert(0, LIB_DIR)

# Monorepo root (contains init_db.py)
_MONOREPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../.."))
_SERVER_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "erpclaw-setup", "scripts")
if os.path.exists(os.path.join(_MONOREPO_ROOT, "init_db.py")):
    PROJECT_ROOT = _MONOREPO_ROOT
elif os.path.exists(os.path.join(_SERVER_ROOT, "init_db.py")):
    PROJECT_ROOT = _SERVER_ROOT
else:
    PROJECT_ROOT = _MONOREPO_ROOT


# ---------------------------------------------------------------------------
# Default argument namespace for _call_action
# All argparse flags from db_query.py main() function.
# Dashes in flag names become underscores in the namespace.
# ---------------------------------------------------------------------------

_DEFAULT_ARGS = {
    # Meta
    "db_path": None,

    # Common filters
    "company_id": None,
    "from_date": None,
    "to_date": None,
    "as_of_date": None,
    "account_id": None,
    "cost_center_id": None,
    "project_id": None,

    # General ledger
    "party_type": None,
    "party_id": None,
    "voucher_type": None,

    # Aging
    "customer_id": None,
    "supplier_id": None,
    "aging_buckets": "30,60,90,120",

    # Budget
    "fiscal_year_id": None,

    # P&L periodicity
    "periodicity": "annual",

    # Comparative
    "periods": None,

    # Elimination
    "name": None,
    "target_company_id": None,
    "source_account_id": None,
    "target_account_id": None,
    "posting_date": None,

    # Pagination
    "limit": "100",
    "offset": "0",
}


def _call_action(action_fn, conn, **kwargs):
    """Call an action function and return the parsed JSON output.

    Intercepts sys.stdout and catches SystemExit (raised by _ok / _err).
    Returns a dict with the parsed JSON response.
    """
    merged = {**_DEFAULT_ARGS, **kwargs}
    args = argparse.Namespace(**merged)

    captured = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = captured
    try:
        action_fn(conn, args)
    except SystemExit:
        pass
    finally:
        sys.stdout = old_stdout

    output = captured.getvalue()
    return json.loads(output)


def _run_init_db(db_path: str):
    """Execute init_db.py to create all tables."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "init_db", os.path.join(PROJECT_ROOT, "init_db.py")
    )
    init_db = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(init_db)
    init_db.init_db(db_path)


# ---------------------------------------------------------------------------
# Test data creation helpers
# ---------------------------------------------------------------------------

def create_test_company(conn, name="Test Company", abbr="TC"):
    """Insert a test company directly via SQL. Returns company_id."""
    company_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO company (id, name, abbr, default_currency, country,
           fiscal_year_start_month)
           VALUES (?, ?, ?, 'USD', 'United States', 1)""",
        (company_id, name, abbr),
    )
    conn.commit()
    return company_id


def create_test_fiscal_year(conn, company_id, name="FY 2026",
                            start_date="2026-01-01", end_date="2026-12-31"):
    """Insert a test fiscal year directly via SQL. Returns fiscal_year_id."""
    fy_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO fiscal_year (id, name, start_date, end_date, company_id)
           VALUES (?, ?, ?, ?, ?)""",
        (fy_id, name, start_date, end_date, company_id),
    )
    conn.commit()
    return fy_id


def create_test_account(conn, company_id, name, root_type, account_type=None,
                        account_number=None, is_group=0, parent_id=None):
    """Insert a test account directly via SQL. Returns account_id.

    NOTE: account_type values must match the DDL CHECK constraint (lowercase):
    'bank','cash','receivable','payable','stock','fixed_asset',
    'accumulated_depreciation','cost_of_goods_sold','tax','equity',
    'revenue','expense','stock_received_not_billed','stock_adjustment',
    'rounding','exchange_gain_loss','depreciation','payroll_payable',
    'temporary','asset_received_not_billed'
    """
    acct_id = str(uuid.uuid4())
    balance_direction = "debit_normal"
    if root_type in ("liability", "equity", "income"):
        balance_direction = "credit_normal"
    conn.execute(
        """INSERT INTO account (id, name, account_number, parent_id, root_type,
           account_type, currency, is_group, balance_direction, company_id, depth)
           VALUES (?, ?, ?, ?, ?, ?, 'USD', ?, ?, ?, 0)""",
        (acct_id, name, account_number, parent_id, root_type, account_type,
         is_group, balance_direction, company_id),
    )
    conn.commit()
    return acct_id


def create_test_gl_entry(conn, account_id, posting_date, debit, credit,
                         voucher_type="journal_entry", voucher_id=None,
                         party_type=None, party_id=None, remarks=None,
                         cost_center_id=None, is_cancelled=0):
    """Insert a single gl_entry row. Returns the gl_entry id."""
    gl_id = str(uuid.uuid4())
    if voucher_id is None:
        voucher_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO gl_entry (id, posting_date, account_id, party_type,
           party_id, debit, credit, voucher_type, voucher_id, remarks,
           cost_center_id, is_cancelled, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
        (gl_id, posting_date, account_id, party_type, party_id,
         str(debit), str(credit), voucher_type, voucher_id, remarks,
         cost_center_id, is_cancelled),
    )
    conn.commit()
    return gl_id


def create_test_gl_entries(conn, entries):
    """Insert multiple gl_entry rows from a list of dicts.

    Each dict should have: account_id, posting_date, debit, credit.
    Optional keys: voucher_type, voucher_id, party_type, party_id, remarks,
                   cost_center_id, is_cancelled.
    Returns list of gl_entry IDs.
    """
    ids = []
    for e in entries:
        gl_id = create_test_gl_entry(
            conn,
            account_id=e["account_id"],
            posting_date=e["posting_date"],
            debit=e["debit"],
            credit=e["credit"],
            voucher_type=e.get("voucher_type", "journal_entry"),
            voucher_id=e.get("voucher_id"),
            party_type=e.get("party_type"),
            party_id=e.get("party_id"),
            remarks=e.get("remarks"),
            cost_center_id=e.get("cost_center_id"),
            is_cancelled=e.get("is_cancelled", 0),
        )
        ids.append(gl_id)
    return ids


def create_test_customer(conn, company_id, name="Test Customer"):
    """Insert a test customer directly via SQL. Returns customer_id."""
    cust_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO customer (id, name, customer_type, company_id)
           VALUES (?, ?, 'company', ?)""",
        (cust_id, name, company_id),
    )
    conn.commit()
    return cust_id


def create_test_supplier(conn, company_id, name="Test Supplier"):
    """Insert a test supplier directly via SQL. Returns supplier_id."""
    sup_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO supplier (id, name, supplier_type, company_id)
           VALUES (?, ?, 'company', ?)""",
        (sup_id, name, company_id),
    )
    conn.commit()
    return sup_id


def create_test_payment_entry(conn, company_id, payment_type, posting_date,
                              paid_from_account, paid_to_account,
                              paid_amount, party_type=None, party_id=None,
                              status="submitted"):
    """Insert a payment_entry row. Returns payment_entry id."""
    pe_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO payment_entry (id, payment_type, posting_date,
           party_type, party_id, paid_from_account, paid_to_account,
           paid_amount, received_amount, status, company_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (pe_id, payment_type, posting_date, party_type, party_id,
         paid_from_account, paid_to_account,
         str(paid_amount), str(paid_amount), status, company_id),
    )
    conn.commit()
    return pe_id


def create_test_ple(conn, account_id, party_type, party_id, posting_date,
                    amount, voucher_type="sales_invoice", voucher_id=None,
                    against_voucher_type=None, against_voucher_id=None,
                    delinked=0):
    """Insert a payment_ledger_entry row. Returns PLE id."""
    ple_id = str(uuid.uuid4())
    if voucher_id is None:
        voucher_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO payment_ledger_entry (id, posting_date, account_id,
           party_type, party_id, voucher_type, voucher_id,
           against_voucher_type, against_voucher_id,
           amount, amount_in_account_currency, currency, delinked, remarks)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'USD', ?, NULL)""",
        (ple_id, posting_date, account_id, party_type, party_id,
         voucher_type, voucher_id, against_voucher_type, against_voucher_id,
         str(amount), str(amount), delinked),
    )
    conn.commit()
    return ple_id


def create_test_budget(conn, fiscal_year_id, company_id, account_id,
                       budget_amount, cost_center_id=None,
                       action_if_exceeded="warn"):
    """Insert a budget row. Returns budget id."""
    budget_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO budget (id, fiscal_year_id, account_id, cost_center_id,
           budget_amount, company_id, action_if_exceeded)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (budget_id, fiscal_year_id, account_id, cost_center_id,
         str(budget_amount), company_id, action_if_exceeded),
    )
    conn.commit()
    return budget_id


def create_test_cost_center(conn, company_id, name="Main Cost Center"):
    """Insert a cost_center row. Returns cost_center id."""
    cc_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO cost_center (id, name, company_id, is_group)
           VALUES (?, ?, ?, 0)""",
        (cc_id, name, company_id),
    )
    conn.commit()
    return cc_id
