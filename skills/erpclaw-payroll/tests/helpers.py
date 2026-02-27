"""Shared test helpers for erpclaw-payroll tests.

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
# All argparse flags from payroll db_query.py main() function.
# Dashes in flag names become underscores in the namespace.
# ---------------------------------------------------------------------------

_DEFAULT_ARGS = {
    # Meta
    "db_path": None,

    # Entity IDs
    "company_id": None,
    "employee_id": None,
    "department_id": None,
    "salary_component_id": None,
    "salary_structure_id": None,
    "salary_assignment_id": None,
    "salary_slip_id": None,
    "payroll_run_id": None,
    "cost_center_id": None,

    # Salary component fields
    "name": None,
    "description": None,
    "component_type": None,
    "is_tax_applicable": None,
    "is_statutory": None,
    "is_pre_tax": None,
    "variable_based_on_taxable_salary": None,
    "depends_on_payment_days": None,
    "gl_account_id": None,

    # Salary structure fields
    "payroll_frequency": None,
    "components": None,

    # Salary assignment fields
    "base_amount": None,
    "effective_from": None,
    "effective_to": None,

    # Payroll run fields
    "period_start": None,
    "period_end": None,

    # Tax config fields
    "tax_jurisdiction": None,
    "filing_status": None,
    "state_code": None,
    "standard_deduction": None,
    "rates": None,
    "tax_year": None,
    "ss_wage_base": None,
    "ss_employee_rate": None,
    "ss_employer_rate": None,
    "medicare_employee_rate": None,
    "medicare_employer_rate": None,
    "additional_medicare_threshold": None,
    "additional_medicare_rate": None,
    "wage_base": None,
    "rate": None,
    "employer_rate_override": None,

    # Wage garnishment fields
    "garnishment_id": None,
    "order_number": None,
    "creditor_name": None,
    "garnishment_type": None,
    "amount_or_percentage": None,
    "is_percentage": False,
    "total_owed": None,
    "start_date": None,
    "end_date": None,

    # Filters
    "status": None,
    "from_date": None,
    "to_date": None,
    "limit": "20",
    "offset": "0",
    "search": None,
}


# ---------------------------------------------------------------------------
# Core test utility: call an action and capture JSON response
# ---------------------------------------------------------------------------

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
# Shared entity creation helpers (direct SQL)
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


def create_test_naming_series(conn, company_id):
    """Create naming series for payroll entity types."""
    series = [
        ("employee", "EMP-"),
        ("salary_slip", "SS-"),
        ("payroll_run", "PRUN-"),
    ]
    for entity_type, prefix in series:
        ns_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO naming_series (id, entity_type, prefix, current_value,
               company_id) VALUES (?, ?, ?, 0, ?)""",
            (ns_id, entity_type, prefix, company_id),
        )
    conn.commit()


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


def create_test_cost_center(conn, company_id, name="Main - TC"):
    """Insert a test cost center. Returns cost_center_id."""
    cc_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO cost_center (id, name, company_id, is_group)
           VALUES (?, ?, ?, 0)""",
        (cc_id, name, company_id),
    )
    conn.commit()
    return cc_id


def create_test_department(conn, company_id, name="Engineering"):
    """Insert a test department directly via SQL. Returns department_id."""
    dept_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO department (id, name, company_id)
           VALUES (?, ?, ?)""",
        (dept_id, name, company_id),
    )
    conn.commit()
    return dept_id


def create_test_employee(conn, company_id, first_name="John", last_name="Doe",
                         date_of_joining="2026-01-15", department_id=None,
                         gender="male", employment_type="full_time",
                         federal_filing_status="single",
                         employee_401k_rate="0", hsa_contribution="0",
                         is_exempt_from_fica=0):
    """Insert a test employee directly via SQL. Returns employee_id."""
    emp_id = str(uuid.uuid4())
    full_name = f"{first_name} {last_name}" if last_name else first_name
    conn.execute(
        """INSERT INTO employee (id, first_name, last_name, full_name,
           date_of_joining, gender, employment_type, company_id, department_id,
           federal_filing_status, employee_401k_rate, hsa_contribution,
           is_exempt_from_fica, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')""",
        (emp_id, first_name, last_name, full_name, date_of_joining,
         gender, employment_type, company_id, department_id,
         federal_filing_status, employee_401k_rate, hsa_contribution,
         is_exempt_from_fica),
    )
    conn.commit()
    return emp_id


# ---------------------------------------------------------------------------
# Full payroll environment setup
# ---------------------------------------------------------------------------

def setup_payroll_environment(conn):
    """Create a complete environment for payroll testing.

    Returns a dict with all IDs needed for payroll tests:
        company_id, fy_id, cost_center_id, department_id,
        salary_expense_id, payroll_payable_id, federal_tax_payable_id,
        ss_payable_id, medicare_payable_id, employer_tax_expense_id,
        employee_ids (list of 2 employee IDs)
    """
    company_id = create_test_company(conn)
    fy_id = create_test_fiscal_year(conn, company_id)
    create_test_naming_series(conn, company_id)
    cost_center_id = create_test_cost_center(conn, company_id)
    department_id = create_test_department(conn, company_id, name="Engineering")

    # Accounts needed for payroll GL posting
    salary_expense_id = create_test_account(
        conn, company_id, "Salary Expense", "expense",
        account_type="expense", account_number="5100",
    )
    payroll_payable_id = create_test_account(
        conn, company_id, "Payroll Payable", "liability",
        account_type="payable", account_number="2200",
    )
    federal_tax_payable_id = create_test_account(
        conn, company_id, "Federal IT Withheld", "liability",
        account_type="tax", account_number="2210",
    )
    ss_payable_id = create_test_account(
        conn, company_id, "Social Security Payable", "liability",
        account_type="tax", account_number="2220",
    )
    medicare_payable_id = create_test_account(
        conn, company_id, "Medicare Payable", "liability",
        account_type="tax", account_number="2230",
    )
    employer_tax_expense_id = create_test_account(
        conn, company_id, "Employer Tax Expense", "expense",
        account_type="expense", account_number="5200",
    )
    futa_payable_id = create_test_account(
        conn, company_id, "FUTA Payable", "liability",
        account_type="tax", account_number="2240",
    )
    suta_payable_id = create_test_account(
        conn, company_id, "SUTA Payable", "liability",
        account_type="tax", account_number="2250",
    )

    # Create 2 employees
    emp1_id = create_test_employee(
        conn, company_id, "Alice", "Smith",
        department_id=department_id,
        federal_filing_status="single",
    )
    emp2_id = create_test_employee(
        conn, company_id, "Bob", "Jones",
        department_id=department_id,
        federal_filing_status="married_jointly",
    )

    return {
        "company_id": company_id,
        "fy_id": fy_id,
        "cost_center_id": cost_center_id,
        "department_id": department_id,
        "salary_expense_id": salary_expense_id,
        "payroll_payable_id": payroll_payable_id,
        "federal_tax_payable_id": federal_tax_payable_id,
        "ss_payable_id": ss_payable_id,
        "medicare_payable_id": medicare_payable_id,
        "employer_tax_expense_id": employer_tax_expense_id,
        "futa_payable_id": futa_payable_id,
        "suta_payable_id": suta_payable_id,
        "employee_ids": [emp1_id, emp2_id],
    }
