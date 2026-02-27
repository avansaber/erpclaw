"""Tests for intercompany invoice mirroring (V4).

10 tests:
- Mirror PI created from submitted SI (1)
- Amounts match exactly (1)
- Account map CRUD (1)
- SI marked as intercompany with cross-reference (1)
- Same-company rejection (1)
- Draft SI rejection (1)
- Already-intercompany rejection (1)
- Cancel cascade deletes draft PI (1)
- Cancel cascade reverses submitted PI GL (1)
- List intercompany invoices (1)
"""
import importlib.util
import json
import os
import sys
import uuid

import db_query
from helpers import (
    _call_action,
    create_test_company,
    create_test_fiscal_year,
    create_test_account,
    create_test_cost_center,
    create_test_naming_series,
    create_test_item,
    create_test_warehouse,
    create_test_customer,
    create_test_tax_template,
    seed_stock_for_item,
    set_company_defaults,
    setup_selling_environment,
)
from decimal import Decimal
from erpclaw_lib.decimal_utils import to_decimal


# ---------------------------------------------------------------------------
# Intercompany environment setup
# ---------------------------------------------------------------------------

def _create_supplier(conn, company_id, name="Source Company (Supplier)"):
    """Insert a supplier directly via SQL. Returns supplier_id."""
    supplier_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO supplier (id, name, supplier_type, default_currency,
           company_id)
           VALUES (?, ?, 'company', 'USD', ?)""",
        (supplier_id, name, company_id),
    )
    conn.commit()
    return supplier_id


def _setup_target_company(conn):
    """Create a second company (target) with all necessary accounts.
    Returns dict with target company's IDs.
    """
    company_id = create_test_company(conn, name="Target Company", abbr="TGT")
    fy_id = create_test_fiscal_year(conn, company_id, name="FY 2026 TGT")

    # Create buying naming series in target company
    for entity_type, prefix in [
        ("purchase_invoice", "PINV-"),
        ("debit_note", "DBN-"),
        ("purchase_order", "PO-"),
        ("purchase_receipt", "GRN-"),
    ]:
        ns_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO naming_series (id, entity_type, prefix, current_value,
               company_id) VALUES (?, ?, ?, 0, ?)""",
            (ns_id, entity_type, prefix, company_id),
        )
    conn.commit()

    cost_center_id = create_test_cost_center(conn, company_id, name="Main - TGT")

    payable_id = create_test_account(
        conn, company_id, "Accounts Payable - TGT", "liability",
        account_type="payable", account_number="T2000",
    )
    expense_id = create_test_account(
        conn, company_id, "IC Expense - TGT", "expense",
        account_type=None, account_number="T6000",
    )
    stock_in_hand_id = create_test_account(
        conn, company_id, "Stock In Hand - TGT", "asset",
        account_type="stock", account_number="T1400",
    )

    # Set company defaults
    set_company_defaults(conn, company_id,
                         default_payable_account_id=payable_id)

    # Create supplier representing source company
    supplier_id = _create_supplier(conn, company_id)

    return {
        "company_id": company_id,
        "fy_id": fy_id,
        "supplier_id": supplier_id,
        "payable_id": payable_id,
        "expense_id": expense_id,
        "stock_in_hand_id": stock_in_hand_id,
        "cost_center_id": cost_center_id,
    }


def _create_and_submit_si(conn, env, items_json=None, posting_date="2026-02-20"):
    """Create and submit a standalone sales invoice. Returns (si_id, result)."""
    if items_json is None:
        items_json = json.dumps([{
            "item_id": env["item_id"],
            "qty": "10",
            "rate": "100.00",
        }])
    # Create draft SI
    r = _call_action(
        db_query.ACTIONS["create-sales-invoice"], conn,
        customer_id=env["customer_id"],
        company_id=env["company_id"],
        posting_date=posting_date,
        items=items_json,
    )
    assert r["status"] == "ok", f"create SI failed: {r}"
    si_id = r["sales_invoice_id"]

    # Submit
    r2 = _call_action(
        db_query.ACTIONS["submit-sales-invoice"], conn,
        sales_invoice_id=si_id,
    )
    assert r2["status"] == "ok", f"submit SI failed: {r2}"
    return si_id, r2


def _setup_intercompany_env(conn):
    """Create full intercompany environment: 2 companies, accounts, mappings."""
    src = setup_selling_environment(conn)
    tgt = _setup_target_company(conn)
    return src, tgt


# ---------------------------------------------------------------------------
# 1. Mirror PI created from submitted SI
# ---------------------------------------------------------------------------

def test_mirror_pi_created(fresh_db):
    src, tgt = _setup_intercompany_env(fresh_db)
    si_id, _ = _create_and_submit_si(fresh_db, src)

    r = _call_action(
        db_query.ACTIONS["create-intercompany-invoice"], fresh_db,
        sales_invoice_id=si_id,
        target_company_id=tgt["company_id"],
        supplier_id=tgt["supplier_id"],
    )
    assert r["status"] == "ok"
    assert r["purchase_invoice_id"]
    assert r["items_mirrored"] == 1

    # Verify PI exists in DB as draft
    pi = fresh_db.execute(
        "SELECT * FROM purchase_invoice WHERE id = ?",
        (r["purchase_invoice_id"],),
    ).fetchone()
    assert pi is not None
    assert pi["status"] == "draft"
    assert pi["company_id"] == tgt["company_id"]
    assert pi["is_intercompany"] == 1
    assert pi["intercompany_reference_id"] == si_id


# ---------------------------------------------------------------------------
# 2. Amounts match exactly
# ---------------------------------------------------------------------------

def test_amounts_match(fresh_db):
    src, tgt = _setup_intercompany_env(fresh_db)
    si_id, _ = _create_and_submit_si(fresh_db, src)

    r = _call_action(
        db_query.ACTIONS["create-intercompany-invoice"], fresh_db,
        sales_invoice_id=si_id,
        target_company_id=tgt["company_id"],
        supplier_id=tgt["supplier_id"],
    )
    pi_id = r["purchase_invoice_id"]

    # Compare amounts
    si = fresh_db.execute("SELECT * FROM sales_invoice WHERE id = ?", (si_id,)).fetchone()
    pi = fresh_db.execute("SELECT * FROM purchase_invoice WHERE id = ?", (pi_id,)).fetchone()

    assert to_decimal(si["total_amount"]) == to_decimal(pi["total_amount"])
    assert to_decimal(si["tax_amount"]) == to_decimal(pi["tax_amount"])
    assert to_decimal(si["grand_total"]) == to_decimal(pi["grand_total"])
    assert si["currency"] == pi["currency"]

    # Compare line items
    si_items = fresh_db.execute(
        "SELECT * FROM sales_invoice_item WHERE sales_invoice_id = ? ORDER BY item_id",
        (si_id,),
    ).fetchall()
    pi_items = fresh_db.execute(
        "SELECT * FROM purchase_invoice_item WHERE purchase_invoice_id = ? ORDER BY item_id",
        (pi_id,),
    ).fetchall()

    assert len(si_items) == len(pi_items)
    for si_item, pi_item in zip(si_items, pi_items):
        assert si_item["item_id"] == pi_item["item_id"]
        assert to_decimal(si_item["quantity"]) == to_decimal(pi_item["quantity"])
        assert to_decimal(si_item["rate"]) == to_decimal(pi_item["rate"])
        assert to_decimal(si_item["amount"]) == to_decimal(pi_item["amount"])


# ---------------------------------------------------------------------------
# 3. Account map CRUD
# ---------------------------------------------------------------------------

def test_account_map_crud(fresh_db):
    src, tgt = _setup_intercompany_env(fresh_db)

    # Add mapping: source income → target expense
    r = _call_action(
        db_query.ACTIONS["add-intercompany-account-map"], fresh_db,
        company_id=src["company_id"],
        target_company_id=tgt["company_id"],
        source_account_id=src["income_id"],
        target_account_id=tgt["expense_id"],
    )
    assert r["status"] == "ok"
    assert r["map_id"]

    # List mappings
    r2 = _call_action(
        db_query.ACTIONS["list-intercompany-account-maps"], fresh_db,
        company_id=src["company_id"],
        target_company_id=tgt["company_id"],
    )
    assert r2["status"] == "ok"
    assert r2["total"] == 1
    assert r2["mappings"][0]["source_account_name"] == "Sales Revenue"
    assert r2["mappings"][0]["target_account_name"] == "IC Expense - TGT"

    # Duplicate mapping should fail
    r3 = _call_action(
        db_query.ACTIONS["add-intercompany-account-map"], fresh_db,
        company_id=src["company_id"],
        target_company_id=tgt["company_id"],
        source_account_id=src["income_id"],
        target_account_id=tgt["expense_id"],
    )
    assert r3["status"] == "error"


# ---------------------------------------------------------------------------
# 4. SI marked as intercompany with cross-reference
# ---------------------------------------------------------------------------

def test_si_marked_intercompany(fresh_db):
    src, tgt = _setup_intercompany_env(fresh_db)
    si_id, _ = _create_and_submit_si(fresh_db, src)

    # Before: not intercompany
    si = fresh_db.execute("SELECT * FROM sales_invoice WHERE id = ?", (si_id,)).fetchone()
    assert si["is_intercompany"] == 0

    r = _call_action(
        db_query.ACTIONS["create-intercompany-invoice"], fresh_db,
        sales_invoice_id=si_id,
        target_company_id=tgt["company_id"],
        supplier_id=tgt["supplier_id"],
    )
    pi_id = r["purchase_invoice_id"]

    # After: is_intercompany = 1, intercompany_reference_id = PI id
    si = fresh_db.execute("SELECT * FROM sales_invoice WHERE id = ?", (si_id,)).fetchone()
    assert si["is_intercompany"] == 1
    assert si["intercompany_reference_id"] == pi_id


# ---------------------------------------------------------------------------
# 5. Same-company rejection
# ---------------------------------------------------------------------------

def test_same_company_rejection(fresh_db):
    src, tgt = _setup_intercompany_env(fresh_db)
    si_id, _ = _create_and_submit_si(fresh_db, src)

    r = _call_action(
        db_query.ACTIONS["create-intercompany-invoice"], fresh_db,
        sales_invoice_id=si_id,
        target_company_id=src["company_id"],  # Same company!
        supplier_id=tgt["supplier_id"],
    )
    assert r["status"] == "error"
    assert "different" in r["message"].lower()


# ---------------------------------------------------------------------------
# 6. Draft SI rejection
# ---------------------------------------------------------------------------

def test_draft_si_rejection(fresh_db):
    src, tgt = _setup_intercompany_env(fresh_db)

    # Create draft SI only (don't submit)
    items_json = json.dumps([{
        "item_id": src["item_id"],
        "qty": "5",
        "rate": "50.00",
    }])
    r = _call_action(
        db_query.ACTIONS["create-sales-invoice"], fresh_db,
        customer_id=src["customer_id"],
        company_id=src["company_id"],
        posting_date="2026-02-20",
        items=items_json,
    )
    si_id = r["sales_invoice_id"]

    # Try to create IC invoice from draft
    r2 = _call_action(
        db_query.ACTIONS["create-intercompany-invoice"], fresh_db,
        sales_invoice_id=si_id,
        target_company_id=tgt["company_id"],
        supplier_id=tgt["supplier_id"],
    )
    assert r2["status"] == "error"
    assert "submitted" in r2["message"].lower()


# ---------------------------------------------------------------------------
# 7. Already-intercompany rejection
# ---------------------------------------------------------------------------

def test_already_intercompany_rejection(fresh_db):
    src, tgt = _setup_intercompany_env(fresh_db)
    si_id, _ = _create_and_submit_si(fresh_db, src)

    # First IC invoice creation
    r = _call_action(
        db_query.ACTIONS["create-intercompany-invoice"], fresh_db,
        sales_invoice_id=si_id,
        target_company_id=tgt["company_id"],
        supplier_id=tgt["supplier_id"],
    )
    assert r["status"] == "ok"

    # Second attempt should fail
    r2 = _call_action(
        db_query.ACTIONS["create-intercompany-invoice"], fresh_db,
        sales_invoice_id=si_id,
        target_company_id=tgt["company_id"],
        supplier_id=tgt["supplier_id"],
    )
    assert r2["status"] == "error"
    assert "already" in r2["message"].lower()


# ---------------------------------------------------------------------------
# 8. Cancel cascade deletes draft PI
# ---------------------------------------------------------------------------

def test_cancel_cascade_draft_pi(fresh_db):
    src, tgt = _setup_intercompany_env(fresh_db)
    si_id, _ = _create_and_submit_si(fresh_db, src)

    r = _call_action(
        db_query.ACTIONS["create-intercompany-invoice"], fresh_db,
        sales_invoice_id=si_id,
        target_company_id=tgt["company_id"],
        supplier_id=tgt["supplier_id"],
    )
    pi_id = r["purchase_invoice_id"]

    # Cancel the IC invoice
    r2 = _call_action(
        db_query.ACTIONS["cancel-intercompany-invoice"], fresh_db,
        sales_invoice_id=si_id,
    )
    assert r2["status"] == "ok"
    assert r2["si_status"] == "cancelled"

    # SI should be cancelled
    si = fresh_db.execute("SELECT status FROM sales_invoice WHERE id = ?", (si_id,)).fetchone()
    assert si["status"] == "cancelled"

    # Draft PI should be deleted
    pi = fresh_db.execute("SELECT id FROM purchase_invoice WHERE id = ?", (pi_id,)).fetchone()
    assert pi is None


# ---------------------------------------------------------------------------
# 9. Cancel cascade reverses submitted PI GL
# ---------------------------------------------------------------------------

def test_cancel_cascade_submitted_pi(fresh_db):
    """Cancel IC invoice when mirror PI has been submitted — reverses both GL."""
    src, tgt = _setup_intercompany_env(fresh_db)
    si_id, _ = _create_and_submit_si(fresh_db, src)

    r = _call_action(
        db_query.ACTIONS["create-intercompany-invoice"], fresh_db,
        sales_invoice_id=si_id,
        target_company_id=tgt["company_id"],
        supplier_id=tgt["supplier_id"],
    )
    pi_id = r["purchase_invoice_id"]

    # Submit the PI via buying's submit action
    _submit_purchase_invoice(fresh_db, pi_id, tgt)

    # Verify PI has GL entries
    pi_gl_count = fresh_db.execute(
        """SELECT COUNT(*) as cnt FROM gl_entry
           WHERE voucher_type = 'purchase_invoice' AND voucher_id = ?
             AND is_cancelled = 0""",
        (pi_id,),
    ).fetchone()["cnt"]
    assert pi_gl_count >= 2

    # Cancel the IC invoice
    r2 = _call_action(
        db_query.ACTIONS["cancel-intercompany-invoice"], fresh_db,
        sales_invoice_id=si_id,
    )
    assert r2["status"] == "ok"
    assert r2["si_gl_reversals"] > 0
    assert r2["pi_gl_reversals"] > 0

    # Both should be cancelled
    si = fresh_db.execute("SELECT status FROM sales_invoice WHERE id = ?", (si_id,)).fetchone()
    pi = fresh_db.execute("SELECT status FROM purchase_invoice WHERE id = ?", (pi_id,)).fetchone()
    assert si["status"] == "cancelled"
    assert pi["status"] == "cancelled"

    # All GL should be balanced (reversals cancel out)
    gl_totals = fresh_db.execute(
        """SELECT decimal_sum(debit) as d, decimal_sum(credit) as c
           FROM gl_entry WHERE is_cancelled = 0"""
    ).fetchone()
    assert to_decimal(gl_totals["d"]) == to_decimal(gl_totals["c"])


def _submit_purchase_invoice(conn, pi_id, tgt_env):
    """Submit a purchase invoice by importing buying's db_query module."""
    _monorepo = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../../../skills/erpclaw-buying/scripts"))
    _server = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "erpclaw-buying", "scripts")
    buying_scripts = _monorepo if os.path.isdir(_monorepo) else _server
    spec = importlib.util.spec_from_file_location(
        "buying_db_query",
        os.path.join(buying_scripts, "db_query.py"),
    )
    buying_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(buying_mod)

    # Build a minimal namespace for the buying action
    import argparse
    args = argparse.Namespace(
        purchase_invoice_id=pi_id,
        db_path=None,
    )

    # Capture output and call submit
    import io
    captured = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = captured
    try:
        buying_mod.ACTIONS["submit-purchase-invoice"](conn, args)
    except SystemExit:
        pass
    finally:
        sys.stdout = old_stdout

    output = captured.getvalue()
    result = json.loads(output)
    assert result["status"] == "ok", f"submit PI failed: {result}"
    return result


# ---------------------------------------------------------------------------
# 10. List intercompany invoices
# ---------------------------------------------------------------------------

def test_list_intercompany_invoices(fresh_db):
    src, tgt = _setup_intercompany_env(fresh_db)
    si_id, _ = _create_and_submit_si(fresh_db, src)

    _call_action(
        db_query.ACTIONS["create-intercompany-invoice"], fresh_db,
        sales_invoice_id=si_id,
        target_company_id=tgt["company_id"],
        supplier_id=tgt["supplier_id"],
    )

    # List from source company perspective (should show SI)
    r = _call_action(
        db_query.ACTIONS["list-intercompany-invoices"], fresh_db,
        company_id=src["company_id"],
    )
    assert r["status"] == "ok"
    assert r["total"] >= 1
    directions = [inv["direction"] for inv in r["invoices"]]
    assert "sales" in directions

    # List from target company perspective (should show PI)
    r2 = _call_action(
        db_query.ACTIONS["list-intercompany-invoices"], fresh_db,
        company_id=tgt["company_id"],
    )
    assert r2["status"] == "ok"
    assert r2["total"] >= 1
    directions = [inv["direction"] for inv in r2["invoices"]]
    assert "purchase" in directions
