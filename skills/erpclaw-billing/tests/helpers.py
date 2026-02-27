"""Shared test helpers for erpclaw-billing tests.

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
# Patch naming prefixes for billing entities
# ---------------------------------------------------------------------------

_EXTRA_PREFIXES = {
    "meter": "MTR-",
}

try:
    from erpclaw_lib.naming import ENTITY_PREFIXES
    for k, v in _EXTRA_PREFIXES.items():
        if k not in ENTITY_PREFIXES:
            ENTITY_PREFIXES[k] = v
except ImportError:
    pass


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
    "meter_id": None,
    "rate_plan_id": None,
    "billing_period_id": None,
    "customer_id": None,
    "company_id": None,
    "item_id": None,
    "serial_number_id": None,

    # Meter fields
    "name": None,
    "meter_type": None,
    "unit": None,
    "install_date": None,
    "address": None,
    "status": None,

    # Reading fields
    "reading_date": None,
    "reading_value": None,
    "reading_type": None,
    "source": None,
    "uom": None,
    "estimated_reason": None,

    # Usage event fields
    "event_date": None,
    "event_type": None,
    "quantity": None,
    "properties": None,
    "idempotency_key": None,
    "events": None,

    # Rate plan fields
    "billing_model": None,
    "tiers": None,
    "base_charge": None,
    "base_charge_period": None,
    "effective_from": None,
    "effective_to": None,
    "minimum_charge": None,
    "minimum_commitment": None,
    "overage_rate": None,
    "service_type": None,
    "consumption": None,

    # Billing period fields
    "from_date": None,
    "to_date": None,
    "billing_date": None,
    "billing_period_ids": None,

    # Adjustment fields
    "amount": None,
    "adjustment_type": None,
    "reason": None,
    "approved_by": None,

    # Prepaid fields
    "valid_until": None,

    # Filters
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


# ---------------------------------------------------------------------------
# Billing-specific helpers (use actions where available)
# ---------------------------------------------------------------------------

def create_test_meter(conn, customer_id, meter_type="electricity",
                      name=None, rate_plan_id=None):
    """Create a meter via the add-meter action. Returns meter_id."""
    from db_query import ACTIONS
    kwargs = {
        "customer_id": customer_id,
        "meter_type": meter_type,
    }
    if name:
        kwargs["name"] = name
    if rate_plan_id:
        kwargs["rate_plan_id"] = rate_plan_id
    result = _call_action(ACTIONS["add-meter"], conn, **kwargs)
    assert result["status"] == "ok", f"create_test_meter failed: {result}"
    return result["meter"]["id"]


def create_test_rate_plan(conn, name="Standard Electric",
                          billing_model="tiered", tiers=None,
                          base_charge=None, effective_from="2026-01-01",
                          minimum_charge=None, service_type=None):
    """Create a rate plan via the add-rate-plan action. Returns rate_plan_id."""
    from db_query import ACTIONS
    if tiers is None:
        tiers = [
            {"tier_start": "0", "tier_end": "100", "rate": "0.10"},
            {"tier_start": "100", "tier_end": "500", "rate": "0.08"},
            {"tier_start": "500", "rate": "0.06"},
        ]
    kwargs = {
        "name": name,
        "billing_model": billing_model,
        "tiers": json.dumps(tiers),
        "effective_from": effective_from,
    }
    if base_charge:
        kwargs["base_charge"] = base_charge
    if minimum_charge:
        kwargs["minimum_charge"] = minimum_charge
    if service_type:
        kwargs["service_type"] = service_type
    result = _call_action(ACTIONS["add-rate-plan"], conn, **kwargs)
    assert result["status"] == "ok", f"create_test_rate_plan failed: {result}"
    return result["rate_plan"]["id"]


def create_test_meter_reading(conn, meter_id, reading_date, reading_value,
                              reading_type=None, source=None, uom=None):
    """Create a meter reading via add-meter-reading action. Returns reading_id."""
    from db_query import ACTIONS
    kwargs = {
        "meter_id": meter_id,
        "reading_date": reading_date,
        "reading_value": reading_value,
    }
    if reading_type:
        kwargs["reading_type"] = reading_type
    if source:
        kwargs["source"] = source
    if uom:
        kwargs["uom"] = uom
    result = _call_action(ACTIONS["add-meter-reading"], conn, **kwargs)
    assert result["status"] == "ok", f"create_test_meter_reading failed: {result}"
    return result["reading"]["id"]


def create_test_usage_event(conn, meter_id, event_date, quantity,
                            event_type="api_call", properties=None,
                            idempotency_key=None):
    """Create a usage event via add-usage-event action. Returns event_id."""
    from db_query import ACTIONS
    kwargs = {
        "meter_id": meter_id,
        "event_date": event_date,
        "quantity": quantity,
        "event_type": event_type,
    }
    if properties:
        kwargs["properties"] = json.dumps(properties) if isinstance(properties, dict) else properties
    if idempotency_key:
        kwargs["idempotency_key"] = idempotency_key
    result = _call_action(ACTIONS["add-usage-event"], conn, **kwargs)
    assert result["status"] == "ok", f"create_test_usage_event failed: {result}"
    return result["usage_event"]["id"]


# ---------------------------------------------------------------------------
# Full billing environment setup
# ---------------------------------------------------------------------------

def setup_billing_environment(conn):
    """Create a complete environment for billing testing.

    Returns a dict with:
        company_id, customer_id, meter_id, rate_plan_id
    """
    company_id = create_test_company(conn)
    customer_id = create_test_customer(conn, company_id)
    rate_plan_id = create_test_rate_plan(conn)
    meter_id = create_test_meter(
        conn, customer_id, rate_plan_id=rate_plan_id
    )

    return {
        "company_id": company_id,
        "customer_id": customer_id,
        "meter_id": meter_id,
        "rate_plan_id": rate_plan_id,
    }
