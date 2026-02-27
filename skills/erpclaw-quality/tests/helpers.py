"""Shared test helpers for erpclaw-quality tests.

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
# Connection wrapper — sqlite3.Connection is a C extension type that does
# not support arbitrary attribute assignment.  The shared naming module's
# get_next_name() expects conn.company_id to be set.  This thin wrapper
# delegates all standard connection methods while also allowing attribute
# storage (e.g. conn.company_id = "...").
# ---------------------------------------------------------------------------

class ConnectionWrapper:
    """Wrapper around sqlite3.Connection that supports arbitrary attributes."""

    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)

    # Attribute delegation: first check instance dict, then underlying conn
    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        if name == "_conn":
            object.__setattr__(self, name, value)
        else:
            object.__setattr__(self, name, value)


# ---------------------------------------------------------------------------
# Default argument namespace for _call_action
# All argparse flags from quality db_query.py main() function.
# Dashes in flag names become underscores in the namespace.
# ---------------------------------------------------------------------------

_DEFAULT_ARGS = {
    # Meta
    "db_path": None,

    # Entity IDs
    "template_id": None,
    "quality_inspection_id": None,
    "non_conformance_id": None,
    "quality_goal_id": None,
    "item_id": None,
    "batch_id": None,

    # Template fields
    "name": None,
    "description": None,
    "inspection_type": None,
    "parameters": None,  # JSON array

    # Inspection fields
    "inspection_date": None,
    "inspected_by": None,
    "sample_size": None,
    "reference_type": None,
    "reference_id": None,
    "remarks": None,

    # Readings JSON
    "readings": None,  # JSON array

    # Non-conformance fields
    "severity": None,
    "root_cause": None,
    "corrective_action": None,
    "preventive_action": None,
    "responsible_employee_id": None,
    "resolution_date": None,

    # Quality goal fields
    "measurable": None,
    "current_value": None,
    "target_value": None,
    "monitoring_frequency": None,
    "review_date": None,

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
    """Insert a test company directly via SQL. Returns company_id.

    Also sets conn.company_id so that get_next_name() can resolve it
    automatically without requiring an explicit company_id parameter.
    """
    company_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO company (id, name, abbr, default_currency, country,
           fiscal_year_start_month)
           VALUES (?, ?, ?, 'USD', 'United States', 1)""",
        (company_id, name, abbr),
    )
    conn.commit()
    # Store on connection so get_next_name() can find it
    conn.company_id = company_id
    return company_id


def create_test_item(conn, company_id=None, item_code="ITEM-001",
                     item_name="Test Widget", item_type="stock",
                     stock_uom="Each", valuation_method="moving_average",
                     standard_rate="25.00"):
    """Insert a test item directly via SQL. Returns item_id.

    Uses direct SQL INSERT rather than the inventory skill's add-item action
    to avoid cross-skill dependency in quality tests.
    """
    item_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO item (id, item_code, item_name, item_type, stock_uom,
           valuation_method, standard_rate, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'active')""",
        (item_id, item_code, item_name, item_type, stock_uom,
         valuation_method, standard_rate),
    )
    conn.commit()
    return item_id


# ---------------------------------------------------------------------------
# Quality-specific helpers (use actions where available)
# ---------------------------------------------------------------------------

def create_test_inspection_template(conn, name="Dimensional Check",
                                    inspection_type="incoming",
                                    item_id=None, description=None,
                                    parameters=None):
    """Create an inspection template via the add-inspection-template action.

    Returns template_id.
    """
    from db_query import ACTIONS
    kwargs = {
        "name": name,
        "inspection_type": inspection_type,
    }
    if item_id:
        kwargs["item_id"] = item_id
    if description:
        kwargs["description"] = description
    if parameters:
        kwargs["parameters"] = json.dumps(parameters)
    result = _call_action(ACTIONS["add-inspection-template"], conn, **kwargs)
    assert result["status"] == "ok", f"create_test_inspection_template failed: {result}"
    return result["template"]["id"]


def create_test_quality_inspection(conn, item_id, inspection_type="incoming",
                                   inspection_date="2026-02-15",
                                   template_id=None, inspected_by=None,
                                   sample_size=None, remarks=None):
    """Create a quality inspection via the add-quality-inspection action.

    Returns the full result dict (not just the ID) for richer assertions.
    """
    from db_query import ACTIONS
    kwargs = {
        "item_id": item_id,
        "inspection_type": inspection_type,
        "inspection_date": inspection_date,
    }
    if template_id:
        kwargs["template_id"] = template_id
    if inspected_by:
        kwargs["inspected_by"] = inspected_by
    if sample_size:
        kwargs["sample_size"] = sample_size
    if remarks:
        kwargs["remarks"] = remarks
    result = _call_action(ACTIONS["add-quality-inspection"], conn, **kwargs)
    assert result["status"] == "ok", f"create_test_quality_inspection failed: {result}"
    return result


def create_test_non_conformance(conn, description="Scratch on surface",
                                severity="minor", item_id=None,
                                quality_inspection_id=None):
    """Create a non-conformance report via the add-non-conformance action.

    Returns the full result dict.
    """
    from db_query import ACTIONS
    kwargs = {
        "description": description,
        "severity": severity,
    }
    if item_id:
        kwargs["item_id"] = item_id
    if quality_inspection_id:
        kwargs["quality_inspection_id"] = quality_inspection_id
    result = _call_action(ACTIONS["add-non-conformance"], conn, **kwargs)
    assert result["status"] == "ok", f"create_test_non_conformance failed: {result}"
    return result


def create_test_quality_goal(conn, name="Defect Rate < 2%",
                             target_value="2.00",
                             monitoring_frequency="monthly",
                             measurable=None, review_date=None):
    """Create a quality goal via the add-quality-goal action.

    Returns the full result dict.
    """
    from db_query import ACTIONS
    kwargs = {
        "name": name,
        "target_value": target_value,
    }
    if monitoring_frequency:
        kwargs["monitoring_frequency"] = monitoring_frequency
    if measurable:
        kwargs["measurable"] = measurable
    if review_date:
        kwargs["review_date"] = review_date
    result = _call_action(ACTIONS["add-quality-goal"], conn, **kwargs)
    assert result["status"] == "ok", f"create_test_quality_goal failed: {result}"
    return result


# ---------------------------------------------------------------------------
# Full quality environment setup
# ---------------------------------------------------------------------------

def setup_quality_environment(conn):
    """Create a complete environment for quality testing.

    Returns a dict with:
        company_id, item_id, item_id_2
    """
    company_id = create_test_company(conn)
    item_id = create_test_item(
        conn, company_id, item_code="WIDGET-001",
        item_name="Widget A", standard_rate="50.00",
    )
    item_id_2 = create_test_item(
        conn, company_id, item_code="WIDGET-002",
        item_name="Widget B", standard_rate="75.00",
    )

    return {
        "company_id": company_id,
        "item_id": item_id,
        "item_id_2": item_id_2,
    }
