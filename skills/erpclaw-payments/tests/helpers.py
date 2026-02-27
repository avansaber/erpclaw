"""Shared test helpers for erpclaw-payments tests.

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

    # Payment entry fields
    "payment_entry_id": None,
    "company_id": None,
    "payment_type": None,
    "posting_date": None,
    "party_type": None,
    "party_id": None,
    "paid_from_account": None,
    "paid_to_account": None,
    "paid_amount": None,
    "payment_currency": "USD",
    "exchange_rate": "1",
    "reference_number": None,
    "reference_date": None,
    "allocations": None,

    # Allocation
    "voucher_type": None,
    "voucher_id": None,
    "allocated_amount": None,

    # PLE
    "ple_amount": None,   # --amount dest="ple_amount"
    "account_id": None,
    "against_voucher_type": None,
    "against_voucher_id": None,

    # Bank reconciliation
    "bank_account_id": None,

    # List filters
    "pe_status": None,    # --status dest="pe_status"
    "from_date": None,
    "to_date": None,
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


def force_payment_status(conn, payment_entry_id, status):
    """Set payment_entry status directly (for testing guard-condition paths
    without the full submit-payment GL workflow).  Not for production use."""
    conn.execute(
        "UPDATE payment_entry SET status = ? WHERE id = ?",
        (status, payment_entry_id),
    )
    conn.commit()


def create_test_payment_allocation(conn, payment_entry_id, voucher_id, amount,
                                   voucher_type="sales_invoice"):
    """Insert a payment allocation and reduce unallocated_amount on the
    parent payment entry.  Returns allocation_id."""
    from decimal import Decimal
    alloc_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO payment_allocation
           (id, payment_entry_id, voucher_type, voucher_id, allocated_amount)
           VALUES (?, ?, ?, ?, ?)""",
        (alloc_id, payment_entry_id, voucher_type, voucher_id, str(amount)),
    )
    # Reduce unallocated_amount on the payment entry
    row = conn.execute(
        "SELECT unallocated_amount FROM payment_entry WHERE id = ?",
        (payment_entry_id,),
    ).fetchone()
    new_unallocated = str(Decimal(row["unallocated_amount"]) - Decimal(str(amount)))
    conn.execute(
        "UPDATE payment_entry SET unallocated_amount = ? WHERE id = ?",
        (new_unallocated, payment_entry_id),
    )
    conn.commit()
    return alloc_id


def create_test_payment_entry(conn, company_id, payment_type="receive",
                               posting_date="2026-06-15",
                               party_type="customer", party_id=None,
                               paid_from_account=None, paid_to_account=None,
                               paid_amount="1000.00", status="draft",
                               reference_number=None, reference_date=None):
    """Insert a draft payment entry directly via SQL.

    Returns (pe_id, naming_series).
    """
    from erpclaw_lib.naming import get_next_name

    if party_id is None:
        party_id = str(uuid.uuid4())

    pe_id = str(uuid.uuid4())
    naming = get_next_name(conn, "payment_entry", company_id=company_id)

    conn.execute(
        """INSERT INTO payment_entry
           (id, naming_series, payment_type, posting_date, party_type, party_id,
            paid_from_account, paid_to_account, paid_amount, received_amount,
            payment_currency, exchange_rate, reference_number, reference_date,
            status, unallocated_amount, company_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'USD', '1', ?, ?, ?, ?, ?)""",
        (pe_id, naming, payment_type, posting_date,
         party_type, party_id, paid_from_account, paid_to_account,
         paid_amount, paid_amount,  # received = paid (exchange rate 1)
         reference_number, reference_date,
         status, paid_amount,  # unallocated = paid_amount
         company_id),
    )
    conn.commit()
    return pe_id, naming
