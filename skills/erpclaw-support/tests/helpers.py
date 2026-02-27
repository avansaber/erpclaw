"""Shared test helpers for erpclaw-support tests.

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
    "issue_id": None,
    "sla_id": None,
    "warranty_claim_id": None,
    "schedule_id": None,
    "customer_id": None,
    "item_id": None,
    "serial_number_id": None,
    "company_id": None,

    # Issue fields
    "subject": None,
    "description": None,
    "priority": None,
    "issue_type": None,
    "assigned_to": None,
    "resolution_notes": None,
    "reason": None,

    # SLA fields
    "name": None,
    "priorities": None,  # JSON
    "working_hours": None,
    "is_default": None,

    # Issue comment fields
    "comment": None,
    "comment_by": None,
    "is_internal": None,

    # Warranty fields
    "warranty_expiry_date": None,
    "complaint_description": None,
    "resolution": None,
    "resolution_date": None,
    "cost": None,

    # Maintenance fields
    "schedule_frequency": None,
    "start_date": None,
    "end_date": None,
    "visit_date": None,
    "completed_by": None,
    "observations": None,
    "work_done": None,

    # Filters
    "status": None,
    "from_date": None,
    "to_date": None,
    "limit": "20",
    "offset": "0",
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
    conn.company_id = company_id
    return company_id


def create_test_customer(conn, company_id, customer_name="Acme Corp",
                         customer_type="company"):
    """Insert a test customer directly via SQL. Returns customer_id."""
    customer_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO customer (id, name, customer_type,
           territory, customer_group, company_id, status)
           VALUES (?, ?, ?, 'United States', 'Commercial', ?, 'active')""",
        (customer_id, customer_name, customer_type, company_id),
    )
    conn.commit()
    return customer_id


def create_test_item(conn, item_name="Widget Pro"):
    """Insert a test item directly via SQL. Returns item_id."""
    item_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO item (id, item_code, item_name, stock_uom, is_stock_item)
           VALUES (?, ?, ?, 'Nos', 1)""",
        (item_id, f"ITEM-{item_id[:8]}", item_name),
    )
    conn.commit()
    return item_id


# ---------------------------------------------------------------------------
# Support-specific helpers (use actions where available)
# ---------------------------------------------------------------------------

def create_test_sla(conn, name="Standard SLA",
                    response_times=None, resolution_times=None,
                    working_hours="9-17", is_default=False):
    """Create an SLA via the add-sla action. Returns sla_id."""
    from db_query import ACTIONS
    if response_times is None:
        response_times = {"low": "48", "medium": "24", "high": "8", "critical": "4"}
    if resolution_times is None:
        resolution_times = {"low": "120", "medium": "72", "high": "24", "critical": "8"}
    priorities = {
        "response_times": response_times,
        "resolution_times": resolution_times,
    }
    kwargs = {
        "name": name,
        "priorities": json.dumps(priorities),
        "working_hours": working_hours,
    }
    if is_default:
        kwargs["is_default"] = "1"
    result = _call_action(ACTIONS["add-sla"], conn, **kwargs)
    assert result["status"] == "ok", f"create_test_sla failed: {result}"
    return result["sla"]["id"]


def create_test_issue(conn, subject="Printer not working",
                      customer_id=None, priority="medium",
                      issue_type="bug", description=None,
                      sla_id=None, assigned_to=None):
    """Create an issue via the add-issue action. Returns issue_id."""
    from db_query import ACTIONS
    kwargs = {
        "subject": subject,
        "priority": priority,
        "issue_type": issue_type,
    }
    if customer_id:
        kwargs["customer_id"] = customer_id
    if description:
        kwargs["description"] = description
    if sla_id:
        kwargs["sla_id"] = sla_id
    if assigned_to:
        kwargs["assigned_to"] = assigned_to
    result = _call_action(ACTIONS["add-issue"], conn, **kwargs)
    assert result["status"] == "ok", f"create_test_issue failed: {result}"
    return result["issue"]["id"]


def create_test_warranty_claim(conn, customer_id,
                               complaint_description="Screen cracked",
                               item_id=None, warranty_expiry_date="2027-12-31"):
    """Create a warranty claim via add-warranty-claim action. Returns claim_id."""
    from db_query import ACTIONS
    kwargs = {
        "customer_id": customer_id,
        "complaint_description": complaint_description,
        "warranty_expiry_date": warranty_expiry_date,
    }
    if item_id:
        kwargs["item_id"] = item_id
    result = _call_action(ACTIONS["add-warranty-claim"], conn, **kwargs)
    assert result["status"] == "ok", f"create_test_warranty_claim failed: {result}"
    return result["warranty_claim"]["id"]


def create_test_maintenance_schedule(conn, customer_id,
                                     schedule_frequency="quarterly",
                                     start_date="2026-01-01",
                                     end_date="2026-12-31",
                                     item_id=None, assigned_to=None):
    """Create a maintenance schedule via add-maintenance-schedule action. Returns schedule_id."""
    from db_query import ACTIONS
    kwargs = {
        "customer_id": customer_id,
        "schedule_frequency": schedule_frequency,
        "start_date": start_date,
        "end_date": end_date,
    }
    if item_id:
        kwargs["item_id"] = item_id
    if assigned_to:
        kwargs["assigned_to"] = assigned_to
    result = _call_action(ACTIONS["add-maintenance-schedule"], conn, **kwargs)
    assert result["status"] == "ok", f"create_test_maintenance_schedule failed: {result}"
    return result["maintenance_schedule"]["id"]


# ---------------------------------------------------------------------------
# Full support environment setup
# ---------------------------------------------------------------------------

def setup_support_environment(conn):
    """Create a complete environment for support testing.

    Returns a dict with:
        company_id, customer_id, sla_id, issue_id
    """
    company_id = create_test_company(conn)
    customer_id = create_test_customer(conn, company_id)
    sla_id = create_test_sla(conn, is_default=True)
    issue_id = create_test_issue(conn, customer_id=customer_id)

    return {
        "company_id": company_id,
        "customer_id": customer_id,
        "sla_id": sla_id,
        "issue_id": issue_id,
    }
