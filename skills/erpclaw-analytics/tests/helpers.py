"""Shared test helpers for erpclaw-analytics tests.

Provides _call_action() to invoke action functions directly and capture
their JSON output (which they print to stdout before calling sys.exit).
"""
import argparse
import io
import json
import os
import sys
import uuid

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

    # Pagination
    "limit": "20",
    "offset": "0",

    # Periodicity
    "periodicity": "monthly",

    # Group by
    "group_by": "account",

    # Aging
    "aging_buckets": "30,60,90,120",

    # Trend
    "metric": None,

    # Comparative
    "periods": None,
    "metrics": None,

    # HR
    "department_id": None,

    # Inventory
    "item_id": None,
    "warehouse_id": None,
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

def create_test_company(conn, name="Stark Industries", abbr="SI"):
    """Insert a test company. Returns company_id."""
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
    """Insert a test fiscal year. Returns fiscal_year_id."""
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
    """Insert a test account. Returns account_id."""
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


def create_test_gl_pair(conn, debit_account_id, credit_account_id,
                        posting_date, amount, voucher_type="journal_entry",
                        voucher_id=None, cost_center_id=None):
    """Insert a balanced GL pair (debit + credit). Returns voucher_id."""
    if voucher_id is None:
        voucher_id = str(uuid.uuid4())
    create_test_gl_entry(conn, debit_account_id, posting_date,
                         amount, "0", voucher_type, voucher_id,
                         cost_center_id=cost_center_id)
    create_test_gl_entry(conn, credit_account_id, posting_date,
                         "0", amount, voucher_type, voucher_id,
                         cost_center_id=cost_center_id)
    return voucher_id


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


def create_test_customer(conn, company_id, name="Test Customer"):
    """Insert a test customer. Returns customer_id."""
    cust_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO customer (id, name, customer_type, company_id)
           VALUES (?, ?, 'company', ?)""",
        (cust_id, name, company_id),
    )
    conn.commit()
    return cust_id


def create_test_sales_invoice(conn, company_id, customer_id, posting_date,
                              grand_total, status="submitted"):
    """Insert a sales_invoice row. Returns invoice_id."""
    inv_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO sales_invoice (id, customer_id, posting_date,
           grand_total, total_amount, status, company_id, currency)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'USD')""",
        (inv_id, customer_id, posting_date, str(grand_total),
         str(grand_total), status, company_id),
    )
    conn.commit()
    return inv_id


def create_test_sales_invoice_item(conn, invoice_id, item_id, qty, rate, amount):
    """Insert a sales_invoice_item row. Returns item_row_id."""
    row_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO sales_invoice_item (id, sales_invoice_id, item_id,
           quantity, rate, amount)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (row_id, invoice_id, item_id, str(qty), str(rate), str(amount)),
    )
    conn.commit()
    return row_id


def create_test_item(conn, company_id, name="Test Item", item_group_id=None):
    """Insert an item. Returns item_id."""
    item_id = str(uuid.uuid4())
    item_code = f"ITEM-{item_id[:8]}"
    conn.execute(
        """INSERT INTO item (id, item_code, item_name, item_group_id, stock_uom)
           VALUES (?, ?, ?, ?, 'Nos')""",
        (item_id, item_code, name, item_group_id),
    )
    conn.commit()
    return item_id


def create_test_employee(conn, company_id, first_name, last_name="",
                         department_id=None, designation_id=None,
                         date_of_joining="2025-01-01", status="active"):
    """Insert an employee. Returns employee_id."""
    emp_id = str(uuid.uuid4())
    full_name = f"{first_name} {last_name}".strip()
    conn.execute(
        """INSERT INTO employee (id, first_name, last_name, full_name, company_id,
           department_id, designation_id, date_of_joining, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (emp_id, first_name, last_name, full_name, company_id,
         department_id, designation_id, date_of_joining, status),
    )
    conn.commit()
    return emp_id


def create_test_department(conn, company_id, name="Engineering"):
    """Insert a department. Returns department_id."""
    dept_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO department (id, name, company_id) VALUES (?, ?, ?)""",
        (dept_id, name, company_id),
    )
    conn.commit()
    return dept_id
