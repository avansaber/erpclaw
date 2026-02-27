"""Shared test helpers for erpclaw-selling tests.

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

    # Customer fields
    "customer_id": None,
    "customer_type": None,
    "customer_group": None,
    "payment_terms_id": None,
    "credit_limit": None,
    "tax_id": None,
    "exempt_from_sales_tax": None,
    "primary_address": None,
    "primary_contact": None,

    # Common fields
    "name": None,
    "company_id": None,
    "posting_date": None,
    "items": None,
    "tax_template_id": None,

    # Quotation fields
    "quotation_id": None,
    "valid_till": None,

    # Sales order fields
    "sales_order_id": None,
    "delivery_date": None,

    # Delivery note fields
    "delivery_note_id": None,

    # Sales invoice fields
    "sales_invoice_id": None,
    "due_date": None,

    # Credit note fields
    "against_invoice_id": None,
    "reason": None,

    # Payment / outstanding
    "amount": None,

    # Sales partner
    "commission_rate": None,

    # Recurring template fields
    "template_id": None,
    "frequency": None,
    "start_date": None,
    "end_date": None,
    "as_of_date": None,
    "template_status": None,

    # Intercompany fields
    "target_company_id": None,
    "supplier_id": None,
    "source_account_id": None,
    "target_account_id": None,

    # CSV import
    "csv_path": None,

    # Status filter (for list queries)
    "doc_status": None,

    # Search / pagination
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


def create_test_company(conn, name="Test Company", abbr="TC",
                        default_receivable_account_id=None,
                        default_payable_account_id=None,
                        default_income_account_id=None,
                        default_expense_account_id=None,
                        default_cost_center_id=None,
                        exchange_gain_loss_account_id=None):
    """Insert a test company directly via SQL. Returns company_id.

    Optional kwargs set company default account/cost-center IDs at creation
    time so tests don't need to UPDATE the company table afterwards.
    """
    company_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO company (id, name, abbr, default_currency, country,
           fiscal_year_start_month, default_receivable_account_id,
           default_payable_account_id, default_income_account_id,
           default_expense_account_id, default_cost_center_id,
           exchange_gain_loss_account_id)
           VALUES (?, ?, ?, 'USD', 'United States', 1, ?, ?, ?, ?, ?, ?)""",
        (company_id, name, abbr, default_receivable_account_id,
         default_payable_account_id, default_income_account_id,
         default_expense_account_id, default_cost_center_id,
         exchange_gain_loss_account_id),
    )
    conn.commit()
    return company_id


def set_company_defaults(conn, company_id, **kwargs):
    """Update company default account/cost-center IDs.

    Accepted kwargs: default_receivable_account_id, default_payable_account_id,
    default_income_account_id, default_expense_account_id,
    default_cost_center_id, exchange_gain_loss_account_id.
    """
    allowed = {
        "default_receivable_account_id", "default_payable_account_id",
        "default_income_account_id", "default_expense_account_id",
        "default_cost_center_id", "exchange_gain_loss_account_id",
    }
    updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    if not updates:
        return
    set_clause = ", ".join(f"{col} = ?" for col in updates)
    values = list(updates.values()) + [company_id]
    conn.execute(f"UPDATE company SET {set_clause} WHERE id = ?", values)
    conn.commit()


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


def create_test_naming_series(conn, company_id):
    """Create naming series for selling document types."""
    for entity_type, prefix in [
        ("quotation", "QTN-"),
        ("sales_order", "SO-"),
        ("delivery_note", "DN-"),
        ("sales_invoice", "SINV-"),
        ("credit_note", "CN-"),
    ]:
        ns_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO naming_series (id, entity_type, prefix, current_value,
               company_id) VALUES (?, ?, ?, 0, ?)""",
            (ns_id, entity_type, prefix, company_id),
        )
    conn.commit()


def create_test_item(conn, item_code="SKU-001", item_name="Widget A",
                     item_type="stock", stock_uom="Each",
                     valuation_method="moving_average", standard_rate="25.00",
                     has_batch=0, has_serial=0):
    """Insert a test item directly via SQL. Returns item_id."""
    item_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO item (id, item_code, item_name, item_type, stock_uom,
           valuation_method, standard_rate, has_batch, has_serial, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')""",
        (item_id, item_code, item_name, item_type, stock_uom,
         valuation_method, standard_rate, has_batch, has_serial),
    )
    conn.commit()
    return item_id


def create_test_warehouse(conn, company_id, name="Main Warehouse",
                          warehouse_type="stores", account_id=None):
    """Insert a test warehouse directly via SQL. Returns warehouse_id."""
    wh_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO warehouse (id, name, warehouse_type, account_id,
           company_id, is_group)
           VALUES (?, ?, ?, ?, ?, 0)""",
        (wh_id, name, warehouse_type, account_id, company_id),
    )
    conn.commit()
    return wh_id


def create_test_customer(conn, company_id, name="Acme Corp",
                         customer_type="company", credit_limit=None):
    """Create a customer via the add-customer action. Returns customer_id."""
    from db_query import ACTIONS
    kwargs = {
        "name": name,
        "company_id": company_id,
        "customer_type": customer_type,
    }
    if credit_limit is not None:
        kwargs["credit_limit"] = str(credit_limit)
    result = _call_action(ACTIONS["add-customer"], conn, **kwargs)
    assert result["status"] == "ok", f"create_test_customer failed: {result}"
    return result["customer_id"]


def create_test_tax_template(conn, company_id, name="Sales Tax 8%",
                             tax_type="sales"):
    """Insert a test tax template with a single line. Returns template_id."""
    template_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO tax_template (id, name, tax_type, company_id)
           VALUES (?, ?, ?, ?)""",
        (template_id, name, tax_type, company_id),
    )
    # Create a tax account
    tax_account_id = create_test_account(
        conn, company_id, "Sales Tax Payable", "liability",
        account_type="tax", account_number="2100",
    )
    # Add a single tax line at 8%
    line_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO tax_template_line (id, tax_template_id, tax_account_id,
           charge_type, rate, add_deduct, row_order)
           VALUES (?, ?, ?, 'on_net_total', '8.00', 'add', 1)""",
        (line_id, template_id, tax_account_id),
    )
    conn.commit()
    return template_id, tax_account_id


def seed_stock_for_item(conn, item_id, warehouse_id, qty="100", rate="25.00"):
    """Insert a stock ledger entry to seed stock for testing.
    This creates stock balance without going through the full stock entry flow.
    """
    sle_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO stock_ledger_entry
           (id, item_id, warehouse_id, posting_date, posting_time,
            actual_qty, valuation_rate, qty_after_transaction,
            stock_value, stock_value_difference,
            voucher_type, voucher_id, is_cancelled)
           VALUES (?, ?, ?, '2026-01-01', '00:00:00',
                   ?, ?, ?, ?, ?,
                   'stock_entry', ?, 0)""",
        (sle_id, item_id, warehouse_id,
         qty, rate, qty, str(float(qty) * float(rate)),
         str(float(qty) * float(rate)),
         str(uuid.uuid4())),
    )
    conn.commit()


def setup_selling_environment(conn):
    """Create a complete environment for selling tests.
    Returns dict with company_id, fy_id, customer_id, item_id, warehouse_id,
    receivable_id, income_id, cogs_id, stock_in_hand_id, cost_center_id,
    tax_template_id, tax_account_id.
    """
    company_id = create_test_company(conn)
    fy_id = create_test_fiscal_year(conn, company_id)
    create_test_naming_series(conn, company_id)
    cost_center_id = create_test_cost_center(conn, company_id)

    # Create accounts
    receivable_id = create_test_account(
        conn, company_id, "Accounts Receivable", "asset",
        account_type="receivable", account_number="1200",
    )
    income_id = create_test_account(
        conn, company_id, "Sales Revenue", "income",
        account_type="revenue", account_number="4000",
    )
    cogs_id = create_test_account(
        conn, company_id, "Cost of Goods Sold", "expense",
        account_type="cost_of_goods_sold", account_number="5100",
    )
    stock_in_hand_id = create_test_account(
        conn, company_id, "Stock In Hand", "asset",
        account_type="stock", account_number="1400",
    )
    stock_received_id = create_test_account(
        conn, company_id, "Stock Received But Not Billed", "liability",
        account_type="stock_received_not_billed", account_number="2200",
    )
    stock_adjustment_id = create_test_account(
        conn, company_id, "Stock Adjustment", "expense",
        account_type="stock_adjustment", account_number="5200",
    )

    # Set company defaults
    set_company_defaults(conn, company_id,
                         default_receivable_account_id=receivable_id,
                         default_income_account_id=income_id)

    # Create item and warehouse
    item_id = create_test_item(conn)
    warehouse_id = create_test_warehouse(
        conn, company_id, "Main Warehouse", account_id=stock_in_hand_id,
    )

    # Seed stock so delivery notes can work
    seed_stock_for_item(conn, item_id, warehouse_id, qty="100", rate="25.00")

    # Create customer
    customer_id = create_test_customer(conn, company_id)

    # Create tax template
    tax_template_id, tax_account_id = create_test_tax_template(conn, company_id)

    return {
        "company_id": company_id,
        "fy_id": fy_id,
        "customer_id": customer_id,
        "item_id": item_id,
        "warehouse_id": warehouse_id,
        "receivable_id": receivable_id,
        "income_id": income_id,
        "cogs_id": cogs_id,
        "stock_in_hand_id": stock_in_hand_id,
        "stock_received_id": stock_received_id,
        "stock_adjustment_id": stock_adjustment_id,
        "cost_center_id": cost_center_id,
        "tax_template_id": tax_template_id,
        "tax_account_id": tax_account_id,
    }
