"""Shared test helpers for erpclaw-projects tests.

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
# All argparse flags from Projects db_query.py main() function.
# Dashes in flag names become underscores in the namespace.
# ---------------------------------------------------------------------------

_DEFAULT_ARGS = {
    # Meta
    "db_path": None,

    # Entity IDs
    "company_id": None,
    "project_id": None,
    "task_id": None,
    "milestone_id": None,
    "timesheet_id": None,
    "employee_id": None,
    "customer_id": None,
    "cost_center_id": None,

    # Common fields
    "name": None,
    "description": None,

    # Project fields
    "project_type": None,
    "billing_type": None,
    "estimated_cost": None,
    "actual_cost": None,
    "total_billed": None,
    "percent_complete": None,

    # Task fields
    "assigned_to": None,
    "estimated_hours": None,
    "actual_hours": None,
    "depends_on": None,
    "parent_task_id": None,

    # Milestone fields
    "target_date": None,
    "completion_date": None,

    # Timesheet fields
    "items": None,

    # Dates
    "start_date": None,
    "end_date": None,

    # Filters
    "status": None,
    "priority": None,
    "from_date": None,
    "to_date": None,
    "search": None,
    "limit": "20",
    "offset": "0",
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
    """Create naming series for project entity types.

    Covers: project, task, timesheet.
    """
    series = [
        ("project", "PROJ-"),
        ("task", "TASK-"),
        ("timesheet", "TS-"),
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


def create_test_employee(conn, company_id, first_name="John", last_name="Doe",
                         date_of_joining="2026-01-15"):
    """Insert a test employee directly via SQL. Returns employee_id.

    The employee table schema uses 'full_name' (NOT NULL).
    The projects db_query.py references 'employee_name' in some queries --
    which maps to 'full_name' in the actual schema.
    """
    employee_id = str(uuid.uuid4())
    full_name = f"{first_name} {last_name}".strip()
    conn.execute(
        """INSERT INTO employee (id, first_name, last_name, full_name,
           date_of_joining, company_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (employee_id, first_name, last_name, full_name,
         date_of_joining, company_id),
    )
    conn.commit()
    return employee_id


def create_test_customer(conn, company_id, name="Acme Corp"):
    """Insert a test customer directly via SQL. Returns customer_id."""
    customer_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO customer (id, name, customer_type, default_currency,
           company_id) VALUES (?, ?, 'company', 'USD', ?)""",
        (customer_id, name, company_id),
    )
    conn.commit()
    return customer_id


# ---------------------------------------------------------------------------
# Projects-specific helpers (use actions where available)
# ---------------------------------------------------------------------------

def create_test_project(conn, company_id, name="Test Project",
                        project_type="external", billing_type="time_and_material",
                        estimated_cost="50000", start_date="2026-01-01",
                        end_date="2026-12-31", customer_id=None,
                        cost_center_id=None):
    """Create a project via the add-project action. Returns project_id."""
    from db_query import ACTIONS
    result = _call_action(
        ACTIONS["add-project"], conn,
        company_id=company_id,
        name=name,
        project_type=project_type,
        billing_type=billing_type,
        estimated_cost=estimated_cost,
        start_date=start_date,
        end_date=end_date,
        customer_id=customer_id,
        cost_center_id=cost_center_id,
    )
    assert result["status"] == "ok", f"create_test_project failed: {result}"
    return result["project"]["id"]


def create_test_task(conn, project_id, name="Test Task",
                     start_date="2026-01-15", end_date="2026-03-15",
                     estimated_hours="40", assigned_to=None, depends_on=None):
    """Create a task via the add-task action. Returns task_id."""
    from db_query import ACTIONS
    result = _call_action(
        ACTIONS["add-task"], conn,
        project_id=project_id,
        name=name,
        start_date=start_date,
        end_date=end_date,
        estimated_hours=estimated_hours,
        assigned_to=assigned_to,
        depends_on=depends_on,
    )
    assert result["status"] == "ok", f"create_test_task failed: {result}"
    return result["task"]["id"]


def create_test_milestone(conn, project_id, name="Alpha Release",
                          target_date="2026-06-30"):
    """Create a milestone via the add-milestone action. Returns milestone_id."""
    from db_query import ACTIONS
    result = _call_action(
        ACTIONS["add-milestone"], conn,
        project_id=project_id,
        name=name,
        target_date=target_date,
    )
    assert result["status"] == "ok", f"create_test_milestone failed: {result}"
    return result["milestone"]["id"]


def create_test_timesheet(conn, company_id, employee_id, project_id,
                          start_date="2026-02-01", end_date="2026-02-07",
                          items=None, task_id=None):
    """Create a timesheet via the add-timesheet action. Returns timesheet_id."""
    from db_query import ACTIONS
    if items is None:
        items = [
            {
                "project_id": project_id,
                "task_id": task_id,
                "activity_type": "development",
                "hours": "8",
                "billing_rate": "150",
                "billable": 1,
                "date": "2026-02-03",
                "description": "Coding",
            },
        ]
    result = _call_action(
        ACTIONS["add-timesheet"], conn,
        company_id=company_id,
        employee_id=employee_id,
        start_date=start_date,
        end_date=end_date,
        items=json.dumps(items),
    )
    assert result["status"] == "ok", f"create_test_timesheet failed: {result}"
    return result["timesheet"]["id"]


# ---------------------------------------------------------------------------
# Full projects environment setup
# ---------------------------------------------------------------------------

def setup_projects_environment(conn):
    """Create a complete environment for Projects testing.

    Returns a dict with:
        company_id, fy_id, cost_center_id,
        customer_id, employee_id
    """
    company_id = create_test_company(conn)
    fy_id = create_test_fiscal_year(conn, company_id)
    create_test_naming_series(conn, company_id)
    cost_center_id = create_test_cost_center(conn, company_id)
    customer_id = create_test_customer(conn, company_id)
    employee_id = create_test_employee(conn, company_id)

    return {
        "company_id": company_id,
        "fy_id": fy_id,
        "cost_center_id": cost_center_id,
        "customer_id": customer_id,
        "employee_id": employee_id,
    }
