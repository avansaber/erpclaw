"""Shared test helpers for erpclaw-ai-engine tests.

Provides _call_action() to invoke action functions directly and capture
their JSON output. Also provides cross-module entity creators for test setup.
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
# Connection wrapper
# ---------------------------------------------------------------------------

class ConnectionWrapper:
    """Wrapper around sqlite3.Connection that supports arbitrary attributes."""

    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        if name == "_conn":
            object.__setattr__(self, name, value)
        else:
            object.__setattr__(self, name, value)


# ---------------------------------------------------------------------------
# Default argument namespace for _call_action
# ---------------------------------------------------------------------------

_DEFAULT_ARGS = {
    # Meta
    "db_path": None,

    # Entity IDs
    "anomaly_id": None,
    "context_id": None,
    "company_id": None,

    # Detection / filter
    "from_date": None,
    "to_date": None,
    "severity": None,
    "status": None,

    # Forecast
    "horizon_days": None,

    # Scenario
    "scenario_type": None,
    "assumptions": None,
    "name": None,

    # Business rules
    "rule_text": None,
    "is_active": None,
    "action_type": None,
    "action_data": None,

    # Categorization
    "pattern": None,
    "account_id": None,
    "description": None,
    "amount": None,
    "source": None,
    "cost_center_id": None,

    # Relationship
    "party_type": None,
    "party_id": None,

    # Context / Decision
    "context_data": None,
    "decision_type": None,
    "options": None,

    # Audit
    "action_name": None,
    "details": None,
    "result": None,

    # Correlation
    "min_strength": None,

    # General
    "limit": "20",
    "offset": "0",
    "reason": None,
}


# ---------------------------------------------------------------------------
# Core test utility: call an action and capture JSON response
# ---------------------------------------------------------------------------

def _call_action(action_fn, conn, **kwargs):
    """Call an action function and return the parsed JSON output."""
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


# ---------------------------------------------------------------------------
# Database initialisation
# ---------------------------------------------------------------------------

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
# Cross-module entity creation helpers (direct SQL)
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
    conn.company_id = company_id
    return company_id


def create_test_fiscal_year(conn, company_id, year=2026):
    """Insert a test fiscal year. Returns fiscal_year_id."""
    fy_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO fiscal_year (id, name, company_id, start_date, end_date,
           is_closed)
           VALUES (?, ?, ?, ?, ?, 0)""",
        (fy_id, f"FY {year}", company_id, f"{year}-01-01", f"{year}-12-31"),
    )
    conn.commit()
    return fy_id


def create_test_account(conn, company_id, name, account_type, root_type,
                        account_number=None, is_group=0):
    """Insert a test account. Returns account_id."""
    acct_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO account (id, name, company_id, account_type, root_type,
           account_number, is_group, balance_direction)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (acct_id, name, company_id, account_type, root_type,
         account_number or str(uuid.uuid4())[:8],
         is_group, "debit_normal" if root_type in ("asset", "expense") else "credit_normal"),
    )
    conn.commit()
    return acct_id


def create_test_cost_center(conn, company_id, name="Main"):
    """Insert a test cost center. Returns cost_center_id."""
    cc_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO cost_center (id, name, company_id, is_group)
           VALUES (?, ?, ?, 0)""",
        (cc_id, name, company_id),
    )
    conn.commit()
    return cc_id


def create_test_customer(conn, company_id, name="Acme Corp"):
    """Insert a test customer. Returns customer_id."""
    cust_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO customer (id, name, customer_type, territory,
           customer_group, company_id, status)
           VALUES (?, ?, 'company', 'United States', 'Commercial', ?, 'active')""",
        (cust_id, name, company_id),
    )
    conn.commit()
    return cust_id


def create_test_supplier(conn, company_id, name="WidgetCo"):
    """Insert a test supplier. Returns supplier_id."""
    sup_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO supplier (id, name, supplier_type, supplier_group,
           company_id, status)
           VALUES (?, ?, 'company', 'General', ?, 'active')""",
        (sup_id, name, company_id),
    )
    conn.commit()
    return sup_id


def create_test_gl_entry(conn, account_id, debit="0", credit="0",
                         posting_date="2026-01-15", voucher_type="journal_entry",
                         voucher_id=None, cost_center_id=None):
    """Insert a GL entry. Returns gl_entry_id."""
    ge_id = str(uuid.uuid4())
    v_id = voucher_id or str(uuid.uuid4())
    conn.execute(
        """INSERT INTO gl_entry (id, posting_date, account_id, debit, credit,
           voucher_type, voucher_id, cost_center_id, is_cancelled)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)""",
        (ge_id, posting_date, account_id, debit, credit,
         voucher_type, v_id, cost_center_id),
    )
    conn.commit()
    return ge_id


def create_test_sales_invoice(conn, company_id, customer_id,
                              grand_total="1000", outstanding="1000",
                              posting_date="2026-01-01", due_date="2026-01-31",
                              status="submitted"):
    """Insert a test sales invoice. Returns invoice_id."""
    inv_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO sales_invoice (id, company_id, customer_id, posting_date,
           due_date, grand_total, outstanding_amount, status, naming_series)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'SINV-')""",
        (inv_id, company_id, customer_id, posting_date, due_date,
         grand_total, outstanding, status),
    )
    conn.commit()
    return inv_id


def create_test_purchase_invoice(conn, company_id, supplier_id,
                                 grand_total="500", outstanding="500",
                                 posting_date="2026-01-01", due_date="2026-01-31",
                                 status="submitted"):
    """Insert a test purchase invoice. Returns invoice_id."""
    inv_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO purchase_invoice (id, company_id, supplier_id, posting_date,
           due_date, grand_total, outstanding_amount, status, naming_series)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PINV-')""",
        (inv_id, company_id, supplier_id, posting_date, due_date,
         grand_total, outstanding, status),
    )
    conn.commit()
    return inv_id


def create_test_payment_entry(conn, company_id, party_type, party_id,
                              amount="1000", posting_date="2026-01-15",
                              payment_type="receive",
                              paid_from_account=None, paid_to_account=None):
    """Insert a test payment entry. Returns payment_id.

    paid_from_account and paid_to_account are required by schema (NOT NULL).
    If not provided, dummy account UUIDs are created inline.
    """
    pay_id = str(uuid.uuid4())
    from_acct = paid_from_account or str(uuid.uuid4())
    to_acct = paid_to_account or str(uuid.uuid4())

    # Ensure dummy accounts exist if not provided
    for acct_id in (from_acct, to_acct):
        if acct_id not in (paid_from_account, paid_to_account):
            conn.execute(
                """INSERT OR IGNORE INTO account (id, name, company_id,
                   account_type, root_type, account_number, is_group,
                   balance_direction)
                   VALUES (?, 'Dummy', ?, 'bank', 'asset', ?, 0,
                   'debit_normal')""",
                (acct_id, company_id, acct_id[:8]),
            )

    conn.execute(
        """INSERT INTO payment_entry (id, company_id, payment_type, party_type,
           party_id, paid_from_account, paid_to_account, paid_amount,
           posting_date, status, naming_series)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'submitted', 'PAY-')""",
        (pay_id, company_id, payment_type, party_type, party_id,
         from_acct, to_acct, amount, posting_date),
    )
    conn.commit()
    return pay_id


def create_test_budget(conn, company_id, fiscal_year_id, account_id,
                       budget_amount="10000", cost_center_id=None):
    """Insert a test budget. Returns budget_id.

    Schema: budget has account_id, cost_center_id, budget_amount directly
    (no separate budget_line table).
    """
    budget_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO budget (id, company_id, fiscal_year_id, account_id,
           cost_center_id, budget_amount, action_if_exceeded)
           VALUES (?, ?, ?, ?, ?, ?, 'warn')""",
        (budget_id, company_id, fiscal_year_id, account_id,
         cost_center_id, budget_amount),
    )
    conn.commit()
    return budget_id


def create_test_categorization_rule(conn, pattern, target_account_id,
                                    confidence="0.5", source="bank_feed",
                                    target_cost_center_id=None):
    """Insert a categorization rule with custom confidence.

    The add-categorization-rule action always defaults confidence to '0.5'.
    Use this helper when a specific confidence value is needed for testing
    priority ordering.  Returns rule_id.
    """
    from datetime import datetime
    rule_id = str(uuid.uuid4())
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """INSERT INTO categorization_rule (id, pattern, source,
           target_account_id, target_cost_center_id, confidence,
           times_applied, times_overridden, created_by, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 0, 0, 'user', ?, ?)""",
        (rule_id, pattern, source, target_account_id,
         target_cost_center_id, confidence, now, now),
    )
    conn.commit()
    return rule_id


def deactivate_business_rule(conn, rule_id):
    """Set a business rule to inactive (active=0).

    No deactivate-business-rule action exists in the AI engine skill,
    so this helper encapsulates the direct SQL.  Returns None.
    """
    conn.execute(
        "UPDATE business_rule SET active = 0 WHERE id = ?", (rule_id,)
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Full AI engine test environment setup
# ---------------------------------------------------------------------------

def setup_ai_environment(conn):
    """Create a complete environment for AI engine testing.

    Returns a dict with:
        company_id, customer_id, supplier_id, expense_account_id,
        revenue_account_id, bank_account_id, cost_center_id, fiscal_year_id
    """
    company_id = create_test_company(conn)
    fiscal_year_id = create_test_fiscal_year(conn, company_id)
    bank_acct = create_test_account(conn, company_id, "Bank", "bank", "asset")
    revenue_acct = create_test_account(conn, company_id, "Revenue", "revenue", "income")
    expense_acct = create_test_account(conn, company_id, "Office Expenses", "expense", "expense")
    cost_center = create_test_cost_center(conn, company_id)
    customer_id = create_test_customer(conn, company_id)
    supplier_id = create_test_supplier(conn, company_id)

    return {
        "company_id": company_id,
        "customer_id": customer_id,
        "supplier_id": supplier_id,
        "bank_account_id": bank_acct,
        "revenue_account_id": revenue_acct,
        "expense_account_id": expense_acct,
        "cost_center_id": cost_center,
        "fiscal_year_id": fiscal_year_id,
    }
