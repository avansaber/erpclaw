"""Shared test helpers for erpclaw-tax tests.

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
# All argparse flags from db_query.py main() function.
# Dashes in flag names become underscores in the namespace.
# ---------------------------------------------------------------------------

_DEFAULT_ARGS = {
    # Meta
    "db_path": None,

    # Tax template
    "tax_template_id": None,
    "name": None,
    "tax_type": None,
    "is_default": None,
    "lines": None,
    "description": None,

    # Tax category
    "tax_category_id": None,

    # Tax rule
    "priority": None,
    "customer_id": None,
    "customer_group": None,
    "supplier_id": None,
    "shipping_state": None,

    # Resolve
    "party_type": None,
    "party_id": None,
    "company_id": None,
    "transaction_type": None,
    "shipping_address": None,

    # Calculate
    "items": None,
    "item_overrides": None,

    # Item tax template
    "item_id": None,
    "tax_rate": None,

    # Withholding
    "wh_rate": None,
    "threshold_amount": None,
    "form_type": None,
    "tax_year": None,
    "withholding_amount": None,
    "voucher_type": None,
    "voucher_id": None,

    # 1099
    "ple_amount": None,

    # Common
    "limit": "20",
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
                        account_number=None, balance_direction=None,
                        is_group=0, parent_id=None):
    """Insert a test account directly via SQL. Returns account_id."""
    acct_id = str(uuid.uuid4())
    if balance_direction is None:
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


def create_test_tax_template(conn, company_id, tax_acct_id,
                             name="Sales Tax 8%", tax_type="sales",
                             is_default=False, rate="8.0",
                             charge_type="on_net_total",
                             add_deduct="add"):
    """Create a tax template with one line directly via SQL.

    Returns (template_id, line_id).
    """
    from db_query import ACTIONS
    lines = json.dumps([{
        "tax_account_id": tax_acct_id,
        "rate": rate,
        "charge_type": charge_type,
        "add_deduct": add_deduct,
    }])
    result = _call_action(
        ACTIONS["add-tax-template"], conn,
        name=name,
        tax_type=tax_type,
        company_id=company_id,
        lines=lines,
        is_default=is_default,
    )
    assert result["status"] == "ok", f"create_test_tax_template failed: {result}"
    template_id = result["tax_template_id"]
    # Fetch the line ID
    line = conn.execute(
        "SELECT id FROM tax_template_line WHERE tax_template_id = ?",
        (template_id,),
    ).fetchone()
    line_id = line["id"] if line else None
    return template_id, line_id


def create_test_tax_category(conn, name="Standard"):
    """Create a tax category via the action. Returns category_id."""
    from db_query import ACTIONS
    result = _call_action(
        ACTIONS["add-tax-category"], conn,
        name=name,
    )
    assert result["status"] == "ok", f"create_test_tax_category failed: {result}"
    return result["tax_category_id"]


def create_test_customer(conn, company_id, name="Test Customer",
                         customer_group=None, exempt=False):
    """Insert a test customer directly via SQL. Returns customer_id."""
    cust_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO customer (id, name, customer_type, customer_group,
           exempt_from_sales_tax, company_id)
           VALUES (?, ?, 'company', ?, ?, ?)""",
        (cust_id, name, customer_group, 1 if exempt else 0, company_id),
    )
    conn.commit()
    return cust_id


def create_test_supplier(conn, company_id, name="Test Supplier",
                         tax_id=None, is_1099=False, w9_on_file=False,
                         wh_category_id=None):
    """Insert a test supplier directly via SQL. Returns supplier_id."""
    sup_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO supplier (id, name, supplier_type, tax_id,
           is_1099_vendor, w9_on_file, tax_withholding_category_id, company_id)
           VALUES (?, ?, 'company', ?, ?, ?, ?, ?)""",
        (sup_id, name, tax_id, 1 if is_1099 else 0,
         1 if w9_on_file else 0, wh_category_id, company_id),
    )
    conn.commit()
    return sup_id
