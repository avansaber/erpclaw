"""Shared test helpers for erpclaw-manufacturing tests.

Provides _call_action() to invoke action functions directly and capture
their JSON output (which they print to stdout before calling sys.exit).
"""
import argparse
import io
import json
import os
import sys
import uuid
from decimal import Decimal

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
# Ensure naming prefixes exist for all manufacturing entity types.
# The shared naming module may not yet include job_card, production_plan,
# or subcontracting_order.  We patch them in at import time so that
# get_next_name() works during tests.
# ---------------------------------------------------------------------------
from erpclaw_lib.naming import ENTITY_PREFIXES  # noqa: E402

_EXTRA_PREFIXES = {
    "job_card": "JC-",
    "production_plan": "PP-",
    "subcontracting_order": "SCO-",
}
for _k, _v in _EXTRA_PREFIXES.items():
    if _k not in ENTITY_PREFIXES:
        ENTITY_PREFIXES[_k] = _v


# ---------------------------------------------------------------------------
# Default argument namespace for _call_action
# All argparse flags from manufacturing db_query.py main() function.
# Dashes in flag names become underscores in the namespace.
# ---------------------------------------------------------------------------

_DEFAULT_ARGS = {
    # Meta
    "db_path": None,

    # Entity IDs
    "company_id": None,
    "item_id": None,
    "bom_id": None,
    "work_order_id": None,
    "job_card_id": None,
    "production_plan_id": None,

    # Master data
    "name": None,
    "description": None,

    # Quantities
    "quantity": None,
    "produced_qty": None,
    "for_quantity": None,
    "completed_qty": None,

    # JSON payloads
    "items": None,
    "operations": None,

    # Operation / workstation / routing references
    "routing_id": None,
    "operation_id": None,
    "workstation_id": None,

    # Workstation fields
    "hour_rate": None,
    "time_in_mins": None,
    "actual_time_in_mins": None,
    "workstation_type": None,
    "working_hours_per_day": None,
    "production_capacity": None,
    "holiday_list_id": None,

    # Dates
    "planned_start_date": None,
    "planned_end_date": None,
    "posting_date": None,

    # Warehouse references
    "source_warehouse_id": None,
    "target_warehouse_id": None,
    "wip_warehouse_id": None,

    # Sales / supplier / subcontracting
    "sales_order_id": None,
    "supplier_id": None,
    "service_item_id": None,
    "supplier_warehouse_id": None,

    # Production planning
    "planning_horizon_days": None,

    # BOM flags
    "is_active": None,
    "is_default": None,
    "uom": None,

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


def create_test_item(conn, item_code="SKU-001", item_name="Widget A",
                     item_type="stock", stock_uom="Each",
                     valuation_method="moving_average", standard_rate="25.00"):
    """Insert a test item directly via SQL. Returns item_id.

    Uses direct SQL INSERT rather than the inventory skill's add-item action
    to avoid cross-skill dependency in manufacturing tests.
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


def create_test_warehouse(conn, company_id, name="Main Warehouse",
                          warehouse_type="stores", account_id=None):
    """Insert a test warehouse directly via SQL. Returns warehouse_id."""
    wh_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO warehouse (id, name, warehouse_type, company_id, account_id)
           VALUES (?, ?, ?, ?, ?)""",
        (wh_id, name, warehouse_type, company_id, account_id),
    )
    conn.commit()
    return wh_id


def create_test_naming_series(conn, company_id):
    """Create naming series for manufacturing entity types.

    Covers: bom, work_order, job_card, production_plan,
    subcontracting_order, stock_entry, stock_reconciliation.
    """
    series = [
        ("bom", "BOM-"),
        ("work_order", "WO-"),
        ("job_card", "JC-"),
        ("production_plan", "PP-"),
        ("subcontracting_order", "SCO-"),
        ("stock_entry", "STE-"),
        ("stock_reconciliation", "SR-"),
    ]
    for entity_type, prefix in series:
        ns_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO naming_series (id, entity_type, prefix, current_value,
               company_id) VALUES (?, ?, ?, 0, ?)""",
            (ns_id, entity_type, prefix, company_id),
        )
    conn.commit()


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
# Manufacturing-specific helpers (use actions where available)
# ---------------------------------------------------------------------------

def create_test_workstation(conn, name="Assembly Line 1", hour_rate="50.00"):
    """Create a workstation via the add-workstation action. Returns workstation_id."""
    from db_query import ACTIONS
    result = _call_action(
        ACTIONS["add-workstation"], conn,
        name=name,
        hour_rate=hour_rate,
    )
    assert result["status"] == "ok", f"create_test_workstation failed: {result}"
    return result["workstation_id"]


def create_test_operation(conn, name="Welding", workstation_id=None):
    """Create an operation via the add-operation action. Returns operation_id."""
    from db_query import ACTIONS
    result = _call_action(
        ACTIONS["add-operation"], conn,
        name=name,
        workstation_id=workstation_id,
    )
    assert result["status"] == "ok", f"create_test_operation failed: {result}"
    return result["operation_id"]


def create_test_bom(conn, item_id_or_env, items_json=None, company_id=None,
                    operations_json=None, operations=None, quantity="1"):
    """Create a BOM via the add-bom action. Returns bom_id.

    Two calling styles:
      create_test_bom(conn, item_id, items_json, company_id)   # explicit
      create_test_bom(conn, env)                                 # convenience
      create_test_bom(conn, env, operations=[...])               # convenience + ops
    """
    from db_query import ACTIONS
    if isinstance(item_id_or_env, dict):
        env = item_id_or_env
        item_id = env["fg_id"]
        company_id = env["company_id"]
        items_json = json.dumps([
            {"item_id": env["rm1_id"], "quantity": "2", "rate": "10.00"},
            {"item_id": env["rm2_id"], "quantity": "3", "rate": "5.00"},
        ])
    else:
        item_id = item_id_or_env

    ops = operations_json or (json.dumps(operations) if operations else None)

    kwargs = {
        "item_id": item_id,
        "items": items_json,
        "company_id": company_id,
        "quantity": quantity,
    }
    if ops is not None:
        kwargs["operations"] = ops
    result = _call_action(ACTIONS["add-bom"], conn, **kwargs)
    assert result["status"] == "ok", f"create_test_bom failed: {result}"
    return result["bom_id"]


# ---------------------------------------------------------------------------
# Full manufacturing environment setup
# ---------------------------------------------------------------------------

def setup_manufacturing_environment(conn):
    """Create a complete environment for manufacturing testing.

    Returns a dict with:
        company_id, fy_id, cost_center_id,
        stock_in_hand_id, cogs_id, stock_received_id, stock_adjustment_id,
            wip_account_id,
        source_wh_id, wip_wh_id, target_wh_id,
        fg_id, rm1_id, rm2_id,
    """
    company_id = create_test_company(conn)
    fy_id = create_test_fiscal_year(conn, company_id)
    create_test_naming_series(conn, company_id)
    cost_center_id = create_test_cost_center(conn, company_id)

    # ---- Accounts --------------------------------------------------------
    stock_in_hand_id = create_test_account(
        conn, company_id, "Stock In Hand", "asset",
        account_type="stock", account_number="1400",
    )
    cogs_id = create_test_account(
        conn, company_id, "Cost of Goods Sold", "expense",
        account_type="cost_of_goods_sold", account_number="5100",
    )
    stock_received_id = create_test_account(
        conn, company_id, "Stock Received But Not Billed", "liability",
        account_type="stock_received_not_billed", account_number="2200",
    )
    stock_adjustment_id = create_test_account(
        conn, company_id, "Stock Adjustment", "expense",
        account_type="stock_adjustment", account_number="5200",
    )
    wip_account_id = create_test_account(
        conn, company_id, "Work In Progress", "asset",
        account_type="stock", account_number="1450",
    )

    # ---- Warehouses ------------------------------------------------------
    source_wh_id = create_test_warehouse(
        conn, company_id, "Stores - TC", warehouse_type="stores",
        account_id=stock_in_hand_id,
    )
    wip_wh_id = create_test_warehouse(
        conn, company_id, "WIP - TC", warehouse_type="production",
        account_id=wip_account_id,
    )
    target_wh_id = create_test_warehouse(
        conn, company_id, "Finished Goods - TC", warehouse_type="stores",
        account_id=stock_in_hand_id,
    )

    # ---- Items -----------------------------------------------------------
    fg_id = create_test_item(
        conn, item_code="FG-001", item_name="Finished Good A",
        standard_rate="100.00",
    )
    rm1_id = create_test_item(
        conn, item_code="RM-001", item_name="Raw Material 1",
        standard_rate="10.00",
    )
    rm2_id = create_test_item(
        conn, item_code="RM-002", item_name="Raw Material 2",
        standard_rate="5.00",
    )

    # ---- Stock in source warehouse (direct SLE inserts) ------------------
    # Receive RM1 (100 qty @ $10) and RM2 (200 qty @ $5) so that
    # transfer-materials has stock to draw from.
    for item_id, qty, rate in [(rm1_id, "100", "10.00"), (rm2_id, "200", "5.00")]:
        sle_id = str(uuid.uuid4())
        stock_value = str(round(Decimal(qty) * Decimal(rate), 2))
        conn.execute(
            """INSERT INTO stock_ledger_entry (
                id, posting_date, item_id, warehouse_id,
                actual_qty, qty_after_transaction, valuation_rate,
                stock_value, stock_value_difference,
                voucher_type, voucher_id, incoming_rate,
                is_cancelled, fiscal_year, created_at)
            VALUES (?, '2026-01-15', ?, ?, ?, ?, ?, ?, ?,
                    'stock_entry', ?, ?, 0, 'FY 2026', datetime('now'))""",
            (sle_id, item_id, source_wh_id, qty, qty, rate,
             stock_value, stock_value, str(uuid.uuid4()), rate),
        )
    conn.commit()

    return {
        "company_id": company_id,
        "fy_id": fy_id,
        "cost_center_id": cost_center_id,
        # Accounts
        "stock_in_hand_id": stock_in_hand_id,
        "cogs_id": cogs_id,
        "stock_received_id": stock_received_id,
        "stock_adjustment_id": stock_adjustment_id,
        "wip_account_id": wip_account_id,
        # Warehouses
        "source_wh_id": source_wh_id,
        "wip_wh_id": wip_wh_id,
        "target_wh_id": target_wh_id,
        # Items
        "fg_id": fg_id,
        "rm1_id": rm1_id,
        "rm2_id": rm2_id,
    }
