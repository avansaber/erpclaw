"""Shared helper functions for ERPClaw Payroll unit tests.

Provides:
  - DB bootstrap via init_schema.init_db()
  - call_action() / ns() / is_error() / is_ok()
  - Seed functions for company, accounts, fiscal years, employees
  - build_payroll_env() for full payroll test environment
  - load_db_query() for explicit module loading (avoids sys.path collisions)
"""
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

# ──────────────────────────────────────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────────────────────────────────────

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
MODULE_DIR = os.path.dirname(TESTS_DIR)  # erpclaw-payroll/
SCRIPTS_DIR = MODULE_DIR                  # db_query.py lives here
# init_schema.py is in erpclaw-setup (sibling module)
SETUP_DIR = os.path.join(os.path.dirname(MODULE_DIR), "erpclaw-setup")
INIT_SCHEMA_PATH = os.path.join(SETUP_DIR, "init_schema.py")

# Make erpclaw_lib importable
ERPCLAW_LIB = os.path.expanduser("~/.openclaw/erpclaw/lib")
if ERPCLAW_LIB not in sys.path:
    sys.path.insert(0, ERPCLAW_LIB)


def load_db_query():
    """Load this module's db_query.py explicitly to avoid sys.path collisions."""
    db_query_path = os.path.join(SCRIPTS_DIR, "db_query.py")
    spec = importlib.util.spec_from_file_location("db_query_payroll", db_query_path)
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


class _ConnWrapper:
    """Wrapper around sqlite3.Connection that allows setting arbitrary attributes.

    Some payroll functions may set attributes on the connection (e.g., conn.skill_name).
    Native sqlite3.Connection objects reject arbitrary attribute assignment,
    so this wrapper delegates to the real connection but allows dynamic attrs.
    """
    def __init__(self, conn):
        object.__setattr__(self, '_conn', conn)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, '_conn'), name)

    def __setattr__(self, name, value):
        try:
            setattr(object.__getattribute__(self, '_conn'), name, value)
        except AttributeError:
            object.__setattr__(self, name, value)


def get_conn(db_path: str):
    """Return a wrapped sqlite3.Connection with FK enabled and Row factory."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.create_aggregate("decimal_sum", 1, _DecimalSum)
    return _ConnWrapper(conn)


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
    """Check if a call_action result is an error response."""
    return result.get("status") == "error"


def is_ok(result: dict) -> bool:
    """Check if a call_action result is a success response."""
    return result.get("status") == "ok"


# ──────────────────────────────────────────────────────────────────────────────
# Utility
# ──────────────────────────────────────────────────────────────────────────────

def _uuid() -> str:
    return str(uuid.uuid4())


# ──────────────────────────────────────────────────────────────────────────────
# Seed helpers
# ──────────────────────────────────────────────────────────────────────────────

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
                 root_type="asset", account_type=None,
                 account_number=None, is_group=0, disabled=0) -> str:
    """Insert a GL account and return its ID."""
    aid = _uuid()
    direction = "debit_normal" if root_type in ("asset", "expense") else "credit_normal"
    conn.execute(
        """INSERT INTO account (id, name, account_number, root_type, account_type,
           balance_direction, company_id, depth, is_group, disabled)
           VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
        (aid, name, account_number or f"ACC-{aid[:6]}", root_type,
         account_type, direction, company_id, is_group, disabled)
    )
    conn.commit()
    return aid


def seed_fiscal_year(conn, company_id: str, name=None,
                     start="2026-01-01", end="2026-12-31") -> str:
    """Insert a fiscal year and return its ID."""
    fid = _uuid()
    conn.execute(
        """INSERT INTO fiscal_year (id, name, start_date, end_date, company_id)
           VALUES (?, ?, ?, ?, ?)""",
        (fid, name or f"FY-{fid[:6]}", start, end, company_id)
    )
    conn.commit()
    return fid


def seed_cost_center(conn, company_id: str, name="Main CC") -> str:
    """Insert a cost center and return its ID."""
    ccid = _uuid()
    conn.execute(
        """INSERT INTO cost_center (id, name, company_id, is_group)
           VALUES (?, ?, ?, 0)""",
        (ccid, name, company_id)
    )
    conn.commit()
    return ccid


def seed_employee(conn, company_id: str, first_name="John", last_name="Doe",
                  federal_filing_status="single",
                  employee_401k_rate="0", hsa_contribution="0") -> str:
    """Insert a test employee and return its ID."""
    eid = _uuid()
    full_name = f"{first_name} {last_name}"
    conn.execute(
        """INSERT INTO employee (id, first_name, last_name, full_name,
           date_of_joining, employment_type, status, company_id,
           federal_filing_status, employee_401k_rate, hsa_contribution)
           VALUES (?, ?, ?, ?, '2025-01-15', 'full_time', 'active', ?, ?, ?, ?)""",
        (eid, first_name, last_name, full_name, company_id,
         federal_filing_status, employee_401k_rate, hsa_contribution)
    )
    conn.commit()
    return eid


def seed_naming_series(conn, company_id: str):
    """Seed naming series for payroll entity types."""
    series = [
        ("payroll_run", "PAY-", 0),
        ("salary_slip", "SS-", 0),
        ("journal_entry", "JE-", 0),
        ("payroll_entry", "PE-", 0),
    ]
    for entity_type, prefix, current in series:
        conn.execute(
            """INSERT OR IGNORE INTO naming_series
               (id, entity_type, prefix, current_value, company_id)
               VALUES (?, ?, ?, ?, ?)""",
            (_uuid(), entity_type, prefix, current, company_id)
        )
    conn.commit()


def build_payroll_env(conn) -> dict:
    """Create a full payroll test environment.

    Creates: company, fiscal year, cost center, GL accounts (salary expense,
    payroll payable, cash, federal tax payable, SS payable, medicare payable),
    employee, and naming series.

    Returns a dict with all IDs needed for payroll tests.
    """
    cid = seed_company(conn)
    fyid = seed_fiscal_year(conn, cid)
    ccid = seed_cost_center(conn, cid)

    # GL accounts needed for payroll posting
    cash = seed_account(conn, cid, "Cash", root_type="asset", account_type="cash")
    salary_expense = seed_account(conn, cid, "Salary Expense",
                                  root_type="expense", account_type=None)
    payroll_payable = seed_account(conn, cid, "Payroll Payable",
                                   root_type="liability", account_type="payable")
    federal_tax_payable = seed_account(conn, cid, "Federal Income Tax Withheld",
                                       root_type="liability", account_type=None)
    ss_payable = seed_account(conn, cid, "Social Security Payable",
                              root_type="liability", account_type=None)
    medicare_payable = seed_account(conn, cid, "Medicare Payable",
                                    root_type="liability", account_type=None)
    employer_tax_expense = seed_account(conn, cid, "Employer Tax Expense",
                                        root_type="expense", account_type=None)

    # Employee
    emp_id = seed_employee(conn, cid)

    # Naming series
    seed_naming_series(conn, cid)

    return {
        "company_id": cid,
        "fiscal_year_id": fyid,
        "cost_center_id": ccid,
        "cash": cash,
        "salary_expense": salary_expense,
        "payroll_payable": payroll_payable,
        "federal_tax_payable": federal_tax_payable,
        "ss_payable": ss_payable,
        "medicare_payable": medicare_payable,
        "employer_tax_expense": employer_tax_expense,
        "employee_id": emp_id,
    }
