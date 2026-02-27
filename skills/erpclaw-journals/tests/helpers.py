"""Shared test helpers for erpclaw-journals tests.

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

    # Journal entry fields
    "journal_entry_id": None,
    "company_id": None,
    "posting_date": None,
    "entry_type": None,
    "remark": None,
    "lines": None,
    "amended_from": None,

    # Intercompany fields
    "source_company_id": None,
    "target_company_id": None,
    "amount": None,
    "description": None,

    # Recurring template fields
    "template_id": None,
    "template_name": None,
    "frequency": None,
    "start_date": None,
    "end_date": None,
    "auto_submit": None,
    "as_of_date": None,
    "template_status": None,

    # List filters
    "je_status": None,       # --status dest="je_status"
    "account_id": None,
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


def create_test_journal_entry(conn, company_id, lines_data, posting_date="2026-06-15",
                              entry_type="journal", remark=None, status="draft"):
    """Insert a draft journal entry with lines directly via SQL.

    lines_data: list of dicts with account_id, debit, credit
    Returns (je_id, naming_series).
    """
    from erpclaw_lib.naming import get_next_name

    je_id = str(uuid.uuid4())
    naming = get_next_name(conn, "journal_entry", company_id=company_id)

    total_debit = sum(
        __import__("decimal").Decimal(str(l.get("debit", "0"))) for l in lines_data
    )
    total_credit = sum(
        __import__("decimal").Decimal(str(l.get("credit", "0"))) for l in lines_data
    )

    conn.execute(
        """INSERT INTO journal_entry
           (id, naming_series, posting_date, entry_type, total_debit, total_credit,
            remark, status, company_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (je_id, naming, posting_date, entry_type,
         str(total_debit), str(total_credit),
         remark, status, company_id),
    )

    for line in lines_data:
        line_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO journal_entry_line
               (id, journal_entry_id, account_id, party_type, party_id,
                debit, credit, cost_center_id, project_id, remark)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (line_id, je_id,
             line["account_id"],
             line.get("party_type"),
             line.get("party_id"),
             str(line.get("debit", "0")),
             str(line.get("credit", "0")),
             line.get("cost_center_id"),
             line.get("project_id"),
             line.get("remark")),
        )

    conn.commit()
    return je_id, naming
