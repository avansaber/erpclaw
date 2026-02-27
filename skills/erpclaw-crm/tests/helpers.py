"""Shared test helpers for erpclaw-crm tests.

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
# All argparse flags from CRM db_query.py main() function.
# Dashes in flag names become underscores in the namespace.
# ---------------------------------------------------------------------------

_DEFAULT_ARGS = {
    # Meta
    "db_path": None,

    # Entity IDs
    "lead_id": None,
    "opportunity_id": None,
    "campaign_id": None,
    "activity_id": None,
    "customer_id": None,

    # Lead fields
    "lead_name": None,
    "company_name": None,
    "email": None,
    "phone": None,
    "source": None,
    "territory": None,
    "industry": None,
    "assigned_to": None,
    "notes": None,

    # Opportunity fields
    "opportunity_name": None,
    "opportunity_type": None,
    "expected_closing_date": None,
    "probability": None,
    "expected_revenue": None,
    "stage": None,
    "lost_reason": None,
    "next_follow_up_date": None,

    # Campaign fields
    "name": None,
    "campaign_type": None,
    "start_date": None,
    "end_date": None,
    "budget": None,
    "actual_spend": None,
    "description": None,

    # Activity fields
    "activity_type": None,
    "subject": None,
    "activity_date": None,
    "created_by": None,
    "next_action_date": None,

    # Cross-skill: convert-opportunity-to-quotation
    "items": None,  # JSON array

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


def create_test_customer(conn, company_id, customer_name="Acme Corp",
                         customer_type="company", email=None):
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


# ---------------------------------------------------------------------------
# CRM-specific helpers (use actions where available)
# ---------------------------------------------------------------------------

def create_test_lead(conn, lead_name="John Doe", company_name="Acme Corp",
                     email="john@example.com", phone="555-0100",
                     source="website", territory=None, industry=None):
    """Create a lead via the add-lead action. Returns lead_id."""
    from db_query import ACTIONS
    kwargs = {
        "lead_name": lead_name,
        "email": email,
        "phone": phone,
        "source": source,
    }
    if company_name:
        kwargs["company_name"] = company_name
    if territory:
        kwargs["territory"] = territory
    if industry:
        kwargs["industry"] = industry
    result = _call_action(ACTIONS["add-lead"], conn, **kwargs)
    assert result["status"] == "ok", f"create_test_lead failed: {result}"
    return result["lead"]["id"]


def create_test_opportunity(conn, opportunity_name="Widget Deal",
                            lead_id=None, customer_id=None,
                            opportunity_type="sales",
                            probability="50", expected_revenue="10000.00",
                            expected_closing_date="2026-06-30"):
    """Create an opportunity via the add-opportunity action. Returns opportunity_id."""
    from db_query import ACTIONS
    kwargs = {
        "opportunity_name": opportunity_name,
        "opportunity_type": opportunity_type,
        "probability": probability,
        "expected_revenue": expected_revenue,
        "expected_closing_date": expected_closing_date,
    }
    if lead_id:
        kwargs["lead_id"] = lead_id
    if customer_id:
        kwargs["customer_id"] = customer_id
    result = _call_action(ACTIONS["add-opportunity"], conn, **kwargs)
    assert result["status"] == "ok", f"create_test_opportunity failed: {result}"
    return result["opportunity"]["id"]


def create_test_campaign(conn, name="Summer Promo", campaign_type="email",
                         budget="5000.00", start_date="2026-03-01",
                         end_date="2026-06-30", description=None):
    """Create a campaign via the add-campaign action. Returns campaign_id."""
    from db_query import ACTIONS
    kwargs = {
        "name": name,
        "campaign_type": campaign_type,
        "budget": budget,
        "start_date": start_date,
        "end_date": end_date,
    }
    if description:
        kwargs["description"] = description
    result = _call_action(ACTIONS["add-campaign"], conn, **kwargs)
    assert result["status"] == "ok", f"create_test_campaign failed: {result}"
    return result["campaign"]["id"]


def create_test_activity(conn, activity_type="call", subject="Follow-up call",
                         activity_date="2026-02-16", lead_id=None,
                         opportunity_id=None, customer_id=None,
                         description=None, created_by=None,
                         next_action_date=None):
    """Create a CRM activity via the add-activity action. Returns activity_id."""
    from db_query import ACTIONS
    kwargs = {
        "activity_type": activity_type,
        "subject": subject,
        "activity_date": activity_date,
    }
    if lead_id:
        kwargs["lead_id"] = lead_id
    if opportunity_id:
        kwargs["opportunity_id"] = opportunity_id
    if customer_id:
        kwargs["customer_id"] = customer_id
    if description:
        kwargs["description"] = description
    if created_by:
        kwargs["created_by"] = created_by
    if next_action_date:
        kwargs["next_action_date"] = next_action_date
    result = _call_action(ACTIONS["add-activity"], conn, **kwargs)
    assert result["status"] == "ok", f"create_test_activity failed: {result}"
    return result["activity"]["id"]


# ---------------------------------------------------------------------------
# Full CRM environment setup
# ---------------------------------------------------------------------------

def setup_crm_environment(conn):
    """Create a complete environment for CRM testing.

    Returns a dict with:
        company_id, customer_id, lead_id, opportunity_id
    """
    company_id = create_test_company(conn)
    customer_id = create_test_customer(conn, company_id)
    lead_id = create_test_lead(conn)
    opportunity_id = create_test_opportunity(
        conn, lead_id=lead_id, customer_id=customer_id,
    )

    return {
        "company_id": company_id,
        "customer_id": customer_id,
        "lead_id": lead_id,
        "opportunity_id": opportunity_id,
    }
