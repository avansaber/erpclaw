"""Shared test helpers for erpclaw-assets tests.

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
# All argparse flags from Assets db_query.py main() function.
# Dashes in flag names become underscores in the namespace.
# ---------------------------------------------------------------------------

_DEFAULT_ARGS = {
    # Meta
    "db_path": None,

    # Entity IDs
    "company_id": None,
    "asset_id": None,
    "asset_category_id": None,
    "item_id": None,
    "depreciation_schedule_id": None,
    "maintenance_id": None,

    # Asset category fields
    "name": None,
    "depreciation_method": None,
    "useful_life_years": None,
    "asset_account_id": None,
    "depreciation_account_id": None,
    "accumulated_depreciation_account_id": None,

    # Asset fields
    "gross_value": None,
    "salvage_value": None,
    "purchase_date": None,
    "purchase_invoice_id": None,
    "depreciation_start_date": None,
    "location": None,
    "custodian_employee_id": None,
    "warranty_expiry_date": None,

    # Movement fields
    "movement_type": None,
    "movement_date": None,
    "from_location": None,
    "to_location": None,
    "from_employee_id": None,
    "to_employee_id": None,
    "reason": None,

    # Maintenance fields
    "maintenance_type": None,
    "scheduled_date": None,
    "actual_date": None,
    "cost": None,
    "performed_by": None,
    "description": None,
    "next_due_date": None,

    # Disposal fields
    "disposal_date": None,
    "disposal_method": None,
    "sale_amount": None,
    "buyer_details": None,

    # GL / posting fields
    "posting_date": None,
    "cost_center_id": None,

    # Report fields
    "as_of_date": None,

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
    """Create naming series for asset entity types.

    Covers: asset, asset_disposal.
    """
    series = [
        ("asset", "AST-"),
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
# Asset-specific helpers (use actions where available)
# ---------------------------------------------------------------------------

def create_test_asset_category(conn, company_id, name="Office Equipment",
                               depreciation_method="straight_line",
                               useful_life_years="5",
                               asset_account_id=None,
                               depreciation_account_id=None,
                               accumulated_depreciation_account_id=None):
    """Create an asset category via the add-asset-category action.
    Returns category_id.
    """
    from db_query import ACTIONS
    result = _call_action(
        ACTIONS["add-asset-category"], conn,
        company_id=company_id,
        name=name,
        depreciation_method=depreciation_method,
        useful_life_years=useful_life_years,
        asset_account_id=asset_account_id,
        depreciation_account_id=depreciation_account_id,
        accumulated_depreciation_account_id=accumulated_depreciation_account_id,
    )
    assert result["status"] == "ok", f"create_test_asset_category failed: {result}"
    return result["asset_category_id"]


def create_test_asset(conn, company_id, category_id, name="Laptop Dell XPS",
                      gross_value="12000.00", salvage_value="2000.00",
                      depreciation_method=None, useful_life_years=None,
                      purchase_date="2026-01-15",
                      depreciation_start_date="2026-02-01",
                      location="HQ Office"):
    """Create an asset via the add-asset action. Returns asset_id."""
    from db_query import ACTIONS
    result = _call_action(
        ACTIONS["add-asset"], conn,
        company_id=company_id,
        name=name,
        asset_category_id=category_id,
        gross_value=gross_value,
        salvage_value=salvage_value,
        depreciation_method=depreciation_method,
        useful_life_years=useful_life_years,
        purchase_date=purchase_date,
        depreciation_start_date=depreciation_start_date,
        location=location,
    )
    assert result["status"] == "ok", f"create_test_asset failed: {result}"
    return result["asset_id"]


def submit_asset(conn, asset_id):
    """Submit an asset via the update-asset action. Returns result."""
    from db_query import ACTIONS
    result = _call_action(
        ACTIONS["update-asset"], conn,
        asset_id=asset_id,
        status="submitted",
    )
    assert result["status"] == "ok", f"submit_asset failed: {result}"
    return result


def create_submitted_asset(conn, company_id, category_id, name="Laptop Dell XPS",
                           gross_value="12000.00", salvage_value="2000.00",
                           depreciation_method=None, useful_life_years=None,
                           purchase_date="2026-01-15",
                           depreciation_start_date="2026-02-01",
                           location="HQ Office"):
    """Create and submit an asset. Returns asset_id."""
    asset_id = create_test_asset(
        conn, company_id, category_id, name=name,
        gross_value=gross_value, salvage_value=salvage_value,
        depreciation_method=depreciation_method,
        useful_life_years=useful_life_years,
        purchase_date=purchase_date,
        depreciation_start_date=depreciation_start_date,
        location=location,
    )
    submit_asset(conn, asset_id)
    return asset_id


def force_asset_status(conn, asset_id, status):
    """Set asset status directly (for testing guard-condition paths without
    running the full disposal or depreciation workflow).  Not for production
    use."""
    conn.execute(
        "UPDATE asset SET status = ? WHERE id = ?",
        (status, asset_id),
    )
    conn.commit()


def generate_schedule(conn, asset_id):
    """Generate depreciation schedule for an asset. Returns result."""
    from db_query import ACTIONS
    result = _call_action(
        ACTIONS["generate-depreciation-schedule"], conn,
        asset_id=asset_id,
    )
    assert result["status"] == "ok", f"generate_schedule failed: {result}"
    return result


# ---------------------------------------------------------------------------
# Full asset environment setup
# ---------------------------------------------------------------------------

def setup_asset_environment(conn):
    """Create a complete environment for asset testing.

    Returns a dict with:
        company_id, fy_id, cost_center_id,
        asset_account_id, depreciation_account_id,
        accumulated_depreciation_account_id,
        category_id
    """
    company_id = create_test_company(conn)
    fy_id = create_test_fiscal_year(conn, company_id)
    create_test_naming_series(conn, company_id)
    cost_center_id = create_test_cost_center(conn, company_id)

    # Accounts needed for asset management with GL posting
    asset_account_id = create_test_account(
        conn, company_id, "Fixed Assets", "asset",
        account_type="fixed_asset", account_number="1500",
    )
    depreciation_account_id = create_test_account(
        conn, company_id, "Depreciation Expense", "expense",
        account_type="depreciation", account_number="6200",
    )
    accumulated_depreciation_account_id = create_test_account(
        conn, company_id, "Accumulated Depreciation", "asset",
        account_type="accumulated_depreciation", account_number="1550",
        balance_direction="credit_normal",
    )

    # Create asset category with all accounts linked
    category_id = create_test_asset_category(
        conn, company_id,
        name="Office Equipment",
        depreciation_method="straight_line",
        useful_life_years="5",
        asset_account_id=asset_account_id,
        depreciation_account_id=depreciation_account_id,
        accumulated_depreciation_account_id=accumulated_depreciation_account_id,
    )

    return {
        "company_id": company_id,
        "fy_id": fy_id,
        "cost_center_id": cost_center_id,
        "asset_account_id": asset_account_id,
        "depreciation_account_id": depreciation_account_id,
        "accumulated_depreciation_account_id": accumulated_depreciation_account_id,
        "category_id": category_id,
    }
