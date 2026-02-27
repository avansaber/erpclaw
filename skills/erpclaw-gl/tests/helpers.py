"""Shared test helpers for erpclaw-gl tests.

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
# store_true flags default to False; lambda-parsed booleans default to None.
# ---------------------------------------------------------------------------

_DEFAULT_ARGS = {
    # Meta
    "db_path": None,

    # Chart of accounts
    "template": None,
    "company_id": None,

    # Account
    "account_id": None,
    "name": None,
    "account_number": None,
    "parent_id": None,
    "root_type": None,
    "account_type": None,
    "currency": None,
    "is_group": False,           # store_true
    "is_frozen": None,           # lambda parsed boolean
    "include_frozen": False,     # store_true
    "search": None,

    # GL entries
    "voucher_type": None,
    "voucher_id": None,
    "posting_date": None,
    "entries": None,
    "is_cancelled": None,        # lambda parsed boolean
    "party_type": None,
    "party_id": None,

    # Fiscal year
    "start_date": None,
    "end_date": None,
    "fiscal_year_id": None,
    "closing_account_id": None,

    # Budget
    "budget_amount": None,
    "action_if_exceeded": None,
    "cost_center_id": None,

    # Naming series
    "entity_type": None,

    # Balance / dates / pagination
    "as_of_date": None,
    "from_date": None,
    "to_date": None,
    "limit": None,
    "offset": None,
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


def create_test_company(conn, name="Test Company", abbr="TC",
                        default_receivable_account_id=None,
                        default_payable_account_id=None,
                        default_income_account_id=None,
                        default_expense_account_id=None,
                        default_cost_center_id=None,
                        exchange_gain_loss_account_id=None):
    """Insert a test company directly via SQL. Returns company_id.

    Optional kwargs set company default account/cost-center IDs at creation
    time so tests don't need to UPDATE the company table afterwards.
    """
    company_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO company (id, name, abbr, default_currency, country,
           fiscal_year_start_month, default_receivable_account_id,
           default_payable_account_id, default_income_account_id,
           default_expense_account_id, default_cost_center_id,
           exchange_gain_loss_account_id)
           VALUES (?, ?, ?, 'USD', 'United States', 1, ?, ?, ?, ?, ?, ?)""",
        (company_id, name, abbr, default_receivable_account_id,
         default_payable_account_id, default_income_account_id,
         default_expense_account_id, default_cost_center_id,
         exchange_gain_loss_account_id),
    )
    conn.commit()
    return company_id


def set_company_defaults(conn, company_id, **kwargs):
    """Update company default account/cost-center IDs.

    Accepted kwargs: default_receivable_account_id, default_payable_account_id,
    default_income_account_id, default_expense_account_id,
    default_cost_center_id, exchange_gain_loss_account_id.
    """
    allowed = {
        "default_receivable_account_id", "default_payable_account_id",
        "default_income_account_id", "default_expense_account_id",
        "default_cost_center_id", "exchange_gain_loss_account_id",
    }
    updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    if not updates:
        return
    set_clause = ", ".join(f"{col} = ?" for col in updates)
    values = list(updates.values()) + [company_id]
    conn.execute(f"UPDATE company SET {set_clause} WHERE id = ?", values)
    conn.commit()


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


def create_test_cost_center(conn, company_id, name="Main", is_group=0,
                            parent_id=None):
    """Insert a test cost center directly via SQL. Returns cost_center_id."""
    cc_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO cost_center (id, name, parent_id, company_id, is_group)
           VALUES (?, ?, ?, ?, ?)""",
        (cc_id, name, parent_id, company_id, is_group),
    )
    conn.commit()
    return cc_id


def post_test_gl_entries(conn, company_id, entries, voucher_type="journal_entry",
                         voucher_id=None, posting_date="2026-06-15",
                         cost_center_id=None):
    """Post GL entries directly via SQL for testing.

    entries: list of dicts with account_id, debit, credit
    Returns list of gl_entry IDs.
    """
    if voucher_id is None:
        voucher_id = str(uuid.uuid4())
    gl_ids = []
    for entry in entries:
        gl_id = str(uuid.uuid4())
        gl_ids.append(gl_id)
        conn.execute(
            """INSERT INTO gl_entry (id, posting_date, account_id, debit, credit,
               debit_base, credit_base, currency, exchange_rate, voucher_type,
               voucher_id, cost_center_id, is_cancelled)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'USD', '1', ?, ?, ?, 0)""",
            (gl_id, posting_date, entry["account_id"],
             entry.get("debit", "0"), entry.get("credit", "0"),
             entry.get("debit", "0"), entry.get("credit", "0"),
             voucher_type, voucher_id,
             entry.get("cost_center_id", cost_center_id)),
        )
    conn.commit()
    return gl_ids
