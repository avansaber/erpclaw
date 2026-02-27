"""Shared test helpers for erpclaw-hr tests.

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
# All argparse flags from HR db_query.py main() function.
# Dashes in flag names become underscores in the namespace.
# ---------------------------------------------------------------------------

_DEFAULT_ARGS = {
    # Meta
    "db_path": None,

    # Entity IDs
    "company_id": None,
    "employee_id": None,
    "department_id": None,
    "designation_id": None,
    "employee_grade_id": None,
    "leave_type_id": None,
    "leave_application_id": None,
    "expense_claim_id": None,
    "payment_entry_id": None,
    "holiday_list_id": None,
    "payroll_cost_center_id": None,
    "salary_structure_id": None,
    "leave_policy_id": None,
    "shift_id": None,
    "attendance_device_id": None,
    "cost_center_id": None,

    # Employee fields
    "first_name": None,
    "last_name": None,
    "date_of_birth": None,
    "gender": None,
    "date_of_joining": None,
    "date_of_exit": None,
    "employment_type": None,
    "branch": None,
    "reporting_to": None,
    "company_email": None,
    "personal_email": None,
    "cell_phone": None,
    "emergency_contact": None,
    "bank_details": None,

    # Tax / payroll fields
    "federal_filing_status": None,
    "w4_allowances": None,
    "w4_additional_withholding": None,
    "state_filing_status": None,
    "state_withholding_allowances": None,
    "employee_401k_rate": None,
    "hsa_contribution": None,
    "is_exempt_from_fica": None,

    # Department / designation fields
    "name": None,
    "description": None,
    "parent_id": None,

    # Leave fields
    "max_days_allowed": None,
    "is_paid_leave": None,
    "is_carry_forward": None,
    "max_carry_forward_days": None,
    "is_compensatory": None,
    "applicable_after_days": None,
    "total_leaves": None,
    "fiscal_year": None,
    "half_day": None,
    "half_day_date": None,
    "reason": None,
    "approved_by": None,

    # Attendance fields
    "date": None,
    "shift": None,
    "check_in_time": None,
    "check_out_time": None,
    "working_hours": None,
    "late_entry": None,
    "early_exit": None,
    "source": None,

    # Bulk attendance
    "entries": None,

    # Holiday list fields
    "holidays": None,

    # Expense claim fields
    "expense_date": None,
    "items": None,

    # Lifecycle event fields
    "event_type": None,
    "event_date": None,
    "details": None,
    "old_values": None,
    "new_values": None,

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
# Shared entity creation helpers (direct SQL unless action is preferable)
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
    """Create naming series for HR entity types.

    Covers: employee, expense_claim, leave_application, salary_slip.
    """
    series = [
        ("employee", "EMP-"),
        ("expense_claim", "EC-"),
        ("leave_application", "LA-"),
        ("salary_slip", "SS-"),
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


# ---------------------------------------------------------------------------
# HR-specific helpers (use actions where available)
# ---------------------------------------------------------------------------

def create_test_department(conn, company_id, name="Engineering"):
    """Create a department via the add-department action. Returns department_id."""
    from db_query import ACTIONS
    result = _call_action(
        ACTIONS["add-department"], conn,
        name=name,
        company_id=company_id,
    )
    assert result["status"] == "ok", f"create_test_department failed: {result}"
    return result["department_id"]


def create_test_designation(conn, company_id=None, name="Engineer"):
    """Create a designation via the add-designation action. Returns designation_id.

    Note: add-designation does not require company_id, but we accept it for
    API consistency with other helpers.
    """
    from db_query import ACTIONS
    result = _call_action(
        ACTIONS["add-designation"], conn,
        name=name,
    )
    assert result["status"] == "ok", f"create_test_designation failed: {result}"
    return result["designation_id"]


def create_test_employee(conn, company_id, first_name="John", last_name="Doe",
                         date_of_joining="2026-01-15", department_id=None,
                         designation_id=None, gender="male",
                         employment_type="full_time"):
    """Create an employee via the add-employee action. Returns employee_id."""
    from db_query import ACTIONS
    result = _call_action(
        ACTIONS["add-employee"], conn,
        first_name=first_name,
        last_name=last_name,
        date_of_joining=date_of_joining,
        company_id=company_id,
        department_id=department_id,
        designation_id=designation_id,
        gender=gender,
        employment_type=employment_type,
    )
    assert result["status"] == "ok", f"create_test_employee failed: {result}"
    return result["employee_id"]


def create_test_leave_type(conn, name="Annual Leave", max_days_allowed="20",
                           is_paid_leave="1", is_carry_forward="0",
                           max_carry_forward_days=None, is_compensatory="0",
                           applicable_after_days="0"):
    """Create a leave type via the add-leave-type action. Returns leave_type_id."""
    from db_query import ACTIONS
    result = _call_action(
        ACTIONS["add-leave-type"], conn,
        name=name,
        max_days_allowed=max_days_allowed,
        is_paid_leave=is_paid_leave,
        is_carry_forward=is_carry_forward,
        max_carry_forward_days=max_carry_forward_days,
        is_compensatory=is_compensatory,
        applicable_after_days=applicable_after_days,
    )
    assert result["status"] == "ok", f"create_test_leave_type failed: {result}"
    return result["leave_type_id"]


# ---------------------------------------------------------------------------
# Full HR environment setup
# ---------------------------------------------------------------------------

def setup_hr_environment(conn):
    """Create a complete environment for HR testing.

    Returns a dict with:
        company_id, fy_id, cost_center_id,
        department_id, designation_id,
        expense_account_id, payable_account_id,
    """
    company_id = create_test_company(conn)
    fy_id = create_test_fiscal_year(conn, company_id)
    create_test_naming_series(conn, company_id)
    cost_center_id = create_test_cost_center(conn, company_id)

    department_id = create_test_department(conn, company_id, name="Engineering")
    designation_id = create_test_designation(conn, company_id, name="Engineer")

    # Accounts needed for expense claims with GL posting
    expense_account_id = create_test_account(
        conn, company_id, "Employee Expenses", "expense",
        account_type="expense", account_number="6100",
    )
    payable_account_id = create_test_account(
        conn, company_id, "Accounts Payable", "liability",
        account_type="payable", account_number="2100",
    )

    return {
        "company_id": company_id,
        "fy_id": fy_id,
        "cost_center_id": cost_center_id,
        "department_id": department_id,
        "designation_id": designation_id,
        "expense_account_id": expense_account_id,
        "payable_account_id": payable_account_id,
    }
