"""Shared test helpers for erpclaw-inventory tests.

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
# All argparse flags from db_query.py main() function.
# Dashes in flag names become underscores in the namespace.
# ---------------------------------------------------------------------------

_DEFAULT_ARGS = {
    # Meta
    "db_path": None,

    # Item fields
    "item_id": None,
    "item_code": None,
    "item_name": None,
    "item_group": None,
    "item_type": None,
    "stock_uom": None,
    "valuation_method": None,
    "has_batch": None,
    "has_serial": None,
    "standard_rate": None,
    "reorder_level": None,
    "reorder_qty": None,
    "item_status": None,

    # Item group
    "parent_id": None,
    "name": None,

    # Warehouse
    "warehouse_id": None,
    "warehouse_type": None,
    "account_id": None,
    "is_group": None,
    "company_id": None,

    # Stock entry
    "stock_entry_id": None,
    "entry_type": None,
    "posting_date": None,
    "items": None,

    # Stock entry list filters
    "se_status": None,

    # Cross-skill SLE
    "voucher_type": None,
    "voucher_id": None,
    "entries": None,

    # Batch
    "batch_name": None,
    "batch_id": None,
    "expiry_date": None,
    "manufacturing_date": None,

    # Serial number
    "serial_no": None,
    "sn_status": None,

    # Pricing
    "price_list_id": None,
    "rate": None,
    "min_qty": None,
    "max_qty": None,
    "valid_from": None,
    "valid_to": None,
    "qty": None,
    "party_id": None,
    "currency": None,
    "is_buying": None,
    "is_selling": None,

    # Pricing rule
    "applies_to": None,
    "entity_id": None,
    "discount_percentage": None,
    "pr_rate": None,
    "priority": None,

    # Stock reconciliation
    "stock_reconciliation_id": None,

    # Stock revaluation
    "revaluation_id": None,
    "new_rate": None,
    "reason": None,

    # CSV import
    "csv_path": None,

    # Search / filters
    "search": None,
    "from_date": None,
    "to_date": None,
    "limit": "20",
    "offset": "0",
}


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


def _run_init_db(db_path: str):
    """Execute init_db.py to create all tables."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "init_db", os.path.join(PROJECT_ROOT, "init_db.py")
    )
    init_db = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(init_db)
    init_db.init_db(db_path)


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
                     valuation_method="moving_average", standard_rate="25.00",
                     has_batch=0, has_serial=0):
    """Create an item via the add-item action. Returns item_id."""
    from db_query import ACTIONS
    result = _call_action(
        ACTIONS["add-item"], conn,
        item_code=item_code,
        item_name=item_name,
        item_type=item_type,
        stock_uom=stock_uom,
        valuation_method=valuation_method,
        standard_rate=standard_rate,
        has_batch=str(has_batch) if has_batch else None,
        has_serial=str(has_serial) if has_serial else None,
    )
    assert result["status"] == "ok", f"create_test_item failed: {result}"
    return result["item_id"]


def create_test_warehouse(conn, company_id, name="Main Warehouse",
                          warehouse_type="stores", account_id=None):
    """Create a warehouse via the add-warehouse action. Returns warehouse_id."""
    from db_query import ACTIONS
    result = _call_action(
        ACTIONS["add-warehouse"], conn,
        name=name,
        company_id=company_id,
        warehouse_type=warehouse_type,
        account_id=account_id,
    )
    assert result["status"] == "ok", f"create_test_warehouse failed: {result}"
    return result["warehouse_id"]


def create_test_stock_entry(conn, company_id, entry_type, items_json,
                             posting_date="2026-02-16"):
    """Create a draft stock entry via the add-stock-entry action.
    Returns stock_entry_id."""
    from db_query import ACTIONS
    result = _call_action(
        ACTIONS["add-stock-entry"], conn,
        entry_type=entry_type,
        company_id=company_id,
        posting_date=posting_date,
        items=items_json,
    )
    assert result["status"] == "ok", f"create_test_stock_entry failed: {result}"
    return result["stock_entry_id"]


def submit_test_stock_entry(conn, stock_entry_id):
    """Submit a stock entry. Returns result dict."""
    from db_query import ACTIONS
    return _call_action(
        ACTIONS["submit-stock-entry"], conn,
        stock_entry_id=stock_entry_id,
    )


def create_test_naming_series(conn, company_id):
    """Create naming series for stock_entry and stock_reconciliation."""
    for entity_type, prefix in [("stock_entry", "STE-"),
                                 ("stock_reconciliation", "SR-"),
                                 ("stock_revaluation", "SREVAL-")]:
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


def setup_inventory_environment(conn):
    """Create a complete environment for inventory testing.
    Returns dict with company_id, fy_id, warehouse_id, item_id,
    stock_in_hand_id, cogs_id, stock_received_id, cost_center_id.
    """
    company_id = create_test_company(conn)
    fy_id = create_test_fiscal_year(conn, company_id)
    create_test_naming_series(conn, company_id)
    cost_center_id = create_test_cost_center(conn, company_id)

    # Create accounts for perpetual inventory GL
    stock_in_hand = create_test_account(
        conn, company_id, "Stock In Hand", "asset",
        account_type="stock", account_number="1400",
    )
    cogs = create_test_account(
        conn, company_id, "Cost of Goods Sold", "expense",
        account_type="cost_of_goods_sold", account_number="5100",
    )
    stock_received = create_test_account(
        conn, company_id, "Stock Received But Not Billed", "liability",
        account_type="stock_received_not_billed", account_number="2200",
    )
    stock_adjustment = create_test_account(
        conn, company_id, "Stock Adjustment", "expense",
        account_type="stock_adjustment", account_number="5200",
    )

    warehouse_id = create_test_warehouse(
        conn, company_id, "Main Warehouse", account_id=stock_in_hand,
    )
    item_id = create_test_item(conn)

    return {
        "company_id": company_id,
        "fy_id": fy_id,
        "warehouse_id": warehouse_id,
        "item_id": item_id,
        "stock_in_hand_id": stock_in_hand,
        "cogs_id": cogs,
        "stock_received_id": stock_received,
        "stock_adjustment_id": stock_adjustment,
        "cost_center_id": cost_center_id,
    }
