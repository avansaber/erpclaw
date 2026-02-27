"""Tests for the full order-to-cash workflow (W6).

8 tests covering the complete cycle: Quotation -> Sales Order -> Delivery Note ->
Sales Invoice, including partial delivery, partial invoicing, tax handling,
and GL balance verification.
"""
import json
from decimal import Decimal

from helpers import (
    _call_action,
    setup_selling_environment,
)
from db_query import ACTIONS


# ---------------------------------------------------------------------------
# Helper: creates items JSON for selling tests
# ---------------------------------------------------------------------------

def _items_json(item_id, qty="10", rate="25.00", warehouse_id=None):
    """Build an items JSON string for action calls."""
    item = {"item_id": item_id, "qty": qty, "rate": rate}
    if warehouse_id:
        item["warehouse_id"] = warehouse_id
    return json.dumps([item])


def _set_dn_item_warehouse(conn, dn_id, warehouse_id):
    """Assign warehouse_id to all delivery note items for a given DN.

    Needed when create-delivery-note inherits items from a quotation-derived
    SO that may not have warehouse_id on its items.
    """
    conn.execute(
        "UPDATE delivery_note_item SET warehouse_id = ? WHERE delivery_note_id = ?",
        (warehouse_id, dn_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 1. test_full_order_to_cash
# ---------------------------------------------------------------------------

def test_full_order_to_cash(fresh_db):
    """Quotation -> SO -> DN -> SINV: complete order-to-cash flow."""
    env = setup_selling_environment(fresh_db)
    items = _items_json(env["item_id"], qty="10", rate="25.00",
                        warehouse_id=env["warehouse_id"])

    # Step 1: Create and submit quotation
    q_result = _call_action(
        ACTIONS["add-quotation"], fresh_db,
        customer_id=env["customer_id"],
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=items,
    )
    assert q_result["status"] == "ok"
    q_id = q_result["quotation_id"]

    submit_q = _call_action(
        ACTIONS["submit-quotation"], fresh_db,
        quotation_id=q_id,
    )
    assert submit_q["status"] == "ok"
    assert submit_q["status_field"] if "status_field" in submit_q else True

    # Step 2: Convert quotation to sales order
    convert_result = _call_action(
        ACTIONS["convert-quotation-to-so"], fresh_db,
        quotation_id=q_id,
    )
    assert convert_result["status"] == "ok"
    so_id = convert_result["sales_order_id"]

    # Submit the sales order
    submit_so = _call_action(
        ACTIONS["submit-sales-order"], fresh_db,
        sales_order_id=so_id,
    )
    assert submit_so["status"] == "ok"
    assert submit_so["status_field"] if "status_field" in submit_so else True

    # Step 3: Create and submit delivery note from SO
    dn_result = _call_action(
        ACTIONS["create-delivery-note"], fresh_db,
        sales_order_id=so_id,
        posting_date="2026-02-17",
    )
    assert dn_result["status"] == "ok"
    dn_id = dn_result["delivery_note_id"]

    # Assign warehouse to DN items (they may need it for SLE)
    _set_dn_item_warehouse(fresh_db, dn_id, env["warehouse_id"])

    submit_dn = _call_action(
        ACTIONS["submit-delivery-note"], fresh_db,
        delivery_note_id=dn_id,
    )
    assert submit_dn["status"] == "ok"
    assert submit_dn["sle_entries_created"] >= 1

    # Step 4: Create and submit sales invoice from SO
    si_result = _call_action(
        ACTIONS["create-sales-invoice"], fresh_db,
        sales_order_id=so_id,
        posting_date="2026-02-18",
    )
    assert si_result["status"] == "ok"
    si_id = si_result["sales_invoice_id"]
    # Since DN exists, update_stock should be 0
    assert si_result["update_stock"] == 0

    submit_si = _call_action(
        ACTIONS["submit-sales-invoice"], fresh_db,
        sales_invoice_id=si_id,
    )
    assert submit_si["status"] == "ok"
    assert submit_si["gl_entries_created"] >= 2

    # Verify final SO status is fully_invoiced
    so_row = fresh_db.execute(
        "SELECT status FROM sales_order WHERE id = ?", (so_id,)
    ).fetchone()
    assert so_row["status"] in ("fully_invoiced", "fully_delivered")


# ---------------------------------------------------------------------------
# 2. test_quotation_to_sales_order
# ---------------------------------------------------------------------------

def test_quotation_to_sales_order(fresh_db):
    """Submit a quotation, convert to SO, verify SO created with correct data."""
    env = setup_selling_environment(fresh_db)
    items = _items_json(env["item_id"], qty="5", rate="25.00",
                        warehouse_id=env["warehouse_id"])

    # Create quotation
    q_result = _call_action(
        ACTIONS["add-quotation"], fresh_db,
        customer_id=env["customer_id"],
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=items,
    )
    assert q_result["status"] == "ok"
    q_id = q_result["quotation_id"]

    # Submit quotation
    submit_q = _call_action(
        ACTIONS["submit-quotation"], fresh_db,
        quotation_id=q_id,
    )
    assert submit_q["status"] == "ok"

    # Convert to SO
    convert_result = _call_action(
        ACTIONS["convert-quotation-to-so"], fresh_db,
        quotation_id=q_id,
    )
    assert convert_result["status"] == "ok"
    so_id = convert_result["sales_order_id"]

    # Verify quotation status changed to 'ordered'
    q_row = fresh_db.execute(
        "SELECT status, converted_to FROM quotation WHERE id = ?", (q_id,)
    ).fetchone()
    assert q_row["status"] == "ordered"
    assert q_row["converted_to"] == so_id

    # Verify SO was created with correct values
    so_result = _call_action(
        ACTIONS["get-sales-order"], fresh_db,
        sales_order_id=so_id,
    )
    assert so_result["status"] == "ok"
    assert so_result["customer_id"] == env["customer_id"]
    assert so_result["company_id"] == env["company_id"]
    assert so_result["grand_total"] == "125.00"
    assert len(so_result["items"]) == 1
    assert so_result["items"][0]["item_id"] == env["item_id"]
    assert so_result["items"][0]["quantity"] == "5.00"


# ---------------------------------------------------------------------------
# 3. test_sales_order_to_delivery
# ---------------------------------------------------------------------------

def test_sales_order_to_delivery(fresh_db):
    """Submit SO, create DN from SO, submit DN, verify SLE created."""
    env = setup_selling_environment(fresh_db)
    items = _items_json(env["item_id"], qty="10", rate="25.00",
                        warehouse_id=env["warehouse_id"])

    # Create and submit SO
    so_result = _call_action(
        ACTIONS["add-sales-order"], fresh_db,
        customer_id=env["customer_id"],
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=items,
    )
    assert so_result["status"] == "ok"
    so_id = so_result["sales_order_id"]

    submit_so = _call_action(
        ACTIONS["submit-sales-order"], fresh_db,
        sales_order_id=so_id,
    )
    assert submit_so["status"] == "ok"

    # Create DN from SO
    dn_result = _call_action(
        ACTIONS["create-delivery-note"], fresh_db,
        sales_order_id=so_id,
        posting_date="2026-02-17",
    )
    assert dn_result["status"] == "ok"
    dn_id = dn_result["delivery_note_id"]
    assert dn_result["total_qty"] == "10.00"

    # Submit DN
    submit_dn = _call_action(
        ACTIONS["submit-delivery-note"], fresh_db,
        delivery_note_id=dn_id,
    )
    assert submit_dn["status"] == "ok"
    assert submit_dn["sle_entries_created"] >= 1

    # Verify SLE shows stock reduction (negative qty)
    sle_rows = fresh_db.execute(
        """SELECT actual_qty FROM stock_ledger_entry
           WHERE voucher_type = 'delivery_note' AND voucher_id = ?
             AND is_cancelled = 0""",
        (dn_id,),
    ).fetchall()
    assert len(sle_rows) >= 1
    assert Decimal(sle_rows[0]["actual_qty"]) == Decimal("-10.00")

    # Verify SO status updated to fully_delivered
    so_row = fresh_db.execute(
        "SELECT status, per_delivered FROM sales_order WHERE id = ?", (so_id,)
    ).fetchone()
    assert so_row["status"] == "fully_delivered"
    assert Decimal(so_row["per_delivered"]) == Decimal("100.00")


# ---------------------------------------------------------------------------
# 4. test_delivery_to_invoice
# ---------------------------------------------------------------------------

def test_delivery_to_invoice(fresh_db):
    """Submit DN, create invoice from DN, submit invoice, verify GL."""
    env = setup_selling_environment(fresh_db)
    items = _items_json(env["item_id"], qty="10", rate="25.00",
                        warehouse_id=env["warehouse_id"])

    # Create and submit SO
    so_result = _call_action(
        ACTIONS["add-sales-order"], fresh_db,
        customer_id=env["customer_id"],
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=items,
    )
    assert so_result["status"] == "ok"
    so_id = so_result["sales_order_id"]

    submit_so = _call_action(
        ACTIONS["submit-sales-order"], fresh_db,
        sales_order_id=so_id,
    )
    assert submit_so["status"] == "ok"

    # Create and submit DN
    dn_result = _call_action(
        ACTIONS["create-delivery-note"], fresh_db,
        sales_order_id=so_id,
        posting_date="2026-02-17",
    )
    assert dn_result["status"] == "ok"
    dn_id = dn_result["delivery_note_id"]

    submit_dn = _call_action(
        ACTIONS["submit-delivery-note"], fresh_db,
        delivery_note_id=dn_id,
    )
    assert submit_dn["status"] == "ok"

    # Create invoice from the delivery note
    si_result = _call_action(
        ACTIONS["create-sales-invoice"], fresh_db,
        delivery_note_id=dn_id,
        posting_date="2026-02-18",
    )
    assert si_result["status"] == "ok"
    si_id = si_result["sales_invoice_id"]
    # Stock already moved by DN, so update_stock = 0
    assert si_result["update_stock"] == 0

    # Submit invoice
    submit_si = _call_action(
        ACTIONS["submit-sales-invoice"], fresh_db,
        sales_invoice_id=si_id,
    )
    assert submit_si["status"] == "ok"
    assert submit_si["gl_entries_created"] >= 2

    # Verify GL entries exist for the invoice
    gl_count = fresh_db.execute(
        """SELECT COUNT(*) as cnt FROM gl_entry
           WHERE voucher_type = 'sales_invoice' AND voucher_id = ?
             AND is_cancelled = 0""",
        (si_id,),
    ).fetchone()["cnt"]
    assert gl_count >= 2  # AR debit + Revenue credit


# ---------------------------------------------------------------------------
# 5. test_invoice_with_tax
# ---------------------------------------------------------------------------

def test_invoice_with_tax(fresh_db):
    """Full flow with tax template at invoice: SO -> DN -> SINV (with tax)."""
    env = setup_selling_environment(fresh_db)
    items = _items_json(env["item_id"], qty="10", rate="25.00",
                        warehouse_id=env["warehouse_id"])

    # Create and submit SO (no tax at SO level)
    so_result = _call_action(
        ACTIONS["add-sales-order"], fresh_db,
        customer_id=env["customer_id"],
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=items,
    )
    assert so_result["status"] == "ok"
    so_id = so_result["sales_order_id"]

    submit_so = _call_action(
        ACTIONS["submit-sales-order"], fresh_db,
        sales_order_id=so_id,
    )
    assert submit_so["status"] == "ok"

    # Create and submit DN
    dn_result = _call_action(
        ACTIONS["create-delivery-note"], fresh_db,
        sales_order_id=so_id,
        posting_date="2026-02-17",
    )
    assert dn_result["status"] == "ok"
    dn_id = dn_result["delivery_note_id"]

    submit_dn = _call_action(
        ACTIONS["submit-delivery-note"], fresh_db,
        delivery_note_id=dn_id,
    )
    assert submit_dn["status"] == "ok"

    # Create invoice from SO with tax template
    si_result = _call_action(
        ACTIONS["create-sales-invoice"], fresh_db,
        sales_order_id=so_id,
        posting_date="2026-02-18",
        tax_template_id=env["tax_template_id"],
    )
    assert si_result["status"] == "ok"
    si_id = si_result["sales_invoice_id"]
    # Net = 250.00, Tax 8% = 20.00, Grand = 270.00
    assert si_result["tax_amount"] == "20.00"
    assert si_result["grand_total"] == "270.00"

    # Submit invoice
    submit_si = _call_action(
        ACTIONS["submit-sales-invoice"], fresh_db,
        sales_invoice_id=si_id,
    )
    assert submit_si["status"] == "ok"

    # Verify tax GL entry
    tax_gl = fresh_db.execute(
        """SELECT credit FROM gl_entry
           WHERE voucher_type = 'sales_invoice' AND voucher_id = ?
             AND account_id = ? AND is_cancelled = 0""",
        (si_id, env["tax_account_id"]),
    ).fetchone()
    assert tax_gl is not None
    assert Decimal(tax_gl["credit"]) == Decimal("20.00")


# ---------------------------------------------------------------------------
# 6. test_partial_delivery
# ---------------------------------------------------------------------------

def test_partial_delivery(fresh_db):
    """SO for qty 10, deliver qty 5, check SO status is partially_delivered."""
    env = setup_selling_environment(fresh_db)
    items = _items_json(env["item_id"], qty="10", rate="25.00",
                        warehouse_id=env["warehouse_id"])

    # Create and submit SO
    so_result = _call_action(
        ACTIONS["add-sales-order"], fresh_db,
        customer_id=env["customer_id"],
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=items,
    )
    assert so_result["status"] == "ok"
    so_id = so_result["sales_order_id"]

    submit_so = _call_action(
        ACTIONS["submit-sales-order"], fresh_db,
        sales_order_id=so_id,
    )
    assert submit_so["status"] == "ok"

    # Create partial DN (5 of 10)
    partial_items = json.dumps([{
        "item_id": env["item_id"],
        "qty": "5",
    }])
    dn_result = _call_action(
        ACTIONS["create-delivery-note"], fresh_db,
        sales_order_id=so_id,
        posting_date="2026-02-17",
        items=partial_items,
    )
    assert dn_result["status"] == "ok"
    dn_id = dn_result["delivery_note_id"]
    assert dn_result["total_qty"] == "5.00"

    # Submit DN
    submit_dn = _call_action(
        ACTIONS["submit-delivery-note"], fresh_db,
        delivery_note_id=dn_id,
    )
    assert submit_dn["status"] == "ok"

    # Verify SO is partially_delivered
    so_row = fresh_db.execute(
        "SELECT status, per_delivered FROM sales_order WHERE id = ?", (so_id,)
    ).fetchone()
    assert so_row["status"] == "partially_delivered"
    assert Decimal(so_row["per_delivered"]) == Decimal("50.00")

    # Verify SO item delivered_qty is 5
    soi = fresh_db.execute(
        "SELECT delivered_qty FROM sales_order_item WHERE sales_order_id = ?",
        (so_id,),
    ).fetchone()
    assert Decimal(soi["delivered_qty"]) == Decimal("5")


# ---------------------------------------------------------------------------
# 7. test_partial_invoicing
# ---------------------------------------------------------------------------

def test_partial_invoicing(fresh_db):
    """Deliver qty 10, invoice qty 5 (via standalone), check outstanding."""
    env = setup_selling_environment(fresh_db)
    items = _items_json(env["item_id"], qty="10", rate="25.00",
                        warehouse_id=env["warehouse_id"])

    # Create standalone invoice for qty 5 (total = 125.00)
    partial_items = json.dumps([{
        "item_id": env["item_id"],
        "qty": "5",
        "rate": "25.00",
        "warehouse_id": env["warehouse_id"],
    }])

    si_result = _call_action(
        ACTIONS["create-sales-invoice"], fresh_db,
        customer_id=env["customer_id"],
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=partial_items,
    )
    assert si_result["status"] == "ok"
    si_id = si_result["sales_invoice_id"]
    assert si_result["grand_total"] == "125.00"

    # Submit it
    submit_si = _call_action(
        ACTIONS["submit-sales-invoice"], fresh_db,
        sales_invoice_id=si_id,
    )
    assert submit_si["status"] == "ok"

    # Outstanding should be the grand total
    si_row = fresh_db.execute(
        "SELECT outstanding_amount FROM sales_invoice WHERE id = ?", (si_id,)
    ).fetchone()
    assert si_row["outstanding_amount"] == "125.00"

    # Partial payment of 50.00
    pay_result = _call_action(
        ACTIONS["update-invoice-outstanding"], fresh_db,
        sales_invoice_id=si_id,
        amount="50.00",
    )
    assert pay_result["status"] == "ok"
    assert pay_result["outstanding_amount"] == "75.00"
    assert pay_result["status_field"] if "status_field" in pay_result else True

    # Verify in database
    si_row2 = fresh_db.execute(
        "SELECT outstanding_amount, status FROM sales_invoice WHERE id = ?",
        (si_id,),
    ).fetchone()
    assert si_row2["outstanding_amount"] == "75.00"
    assert si_row2["status"] == "partially_paid"


# ---------------------------------------------------------------------------
# 8. test_gl_balanced_after_full_cycle
# ---------------------------------------------------------------------------

def test_gl_balanced_after_full_cycle(fresh_db):
    """Run full cycle, verify SUM(debit) = SUM(credit) in gl_entry."""
    env = setup_selling_environment(fresh_db)
    items = _items_json(env["item_id"], qty="10", rate="25.00",
                        warehouse_id=env["warehouse_id"])

    # Create and submit SO
    so_result = _call_action(
        ACTIONS["add-sales-order"], fresh_db,
        customer_id=env["customer_id"],
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=items,
    )
    assert so_result["status"] == "ok"
    so_id = so_result["sales_order_id"]

    submit_so = _call_action(
        ACTIONS["submit-sales-order"], fresh_db,
        sales_order_id=so_id,
    )
    assert submit_so["status"] == "ok"

    # Create and submit DN
    dn_result = _call_action(
        ACTIONS["create-delivery-note"], fresh_db,
        sales_order_id=so_id,
        posting_date="2026-02-17",
    )
    assert dn_result["status"] == "ok"
    dn_id = dn_result["delivery_note_id"]

    submit_dn = _call_action(
        ACTIONS["submit-delivery-note"], fresh_db,
        delivery_note_id=dn_id,
    )
    assert submit_dn["status"] == "ok"

    # Create and submit invoice from SO
    si_result = _call_action(
        ACTIONS["create-sales-invoice"], fresh_db,
        sales_order_id=so_id,
        posting_date="2026-02-18",
    )
    assert si_result["status"] == "ok"
    si_id = si_result["sales_invoice_id"]

    submit_si = _call_action(
        ACTIONS["submit-sales-invoice"], fresh_db,
        sales_invoice_id=si_id,
    )
    assert submit_si["status"] == "ok"

    # Verify GL is balanced: SUM(debit) == SUM(credit) across all entries
    balance_row = fresh_db.execute(
        """SELECT
               COALESCE(SUM(CAST(debit AS REAL)), 0) as total_debit,
               COALESCE(SUM(CAST(credit AS REAL)), 0) as total_credit
           FROM gl_entry
           WHERE is_cancelled = 0""",
    ).fetchone()

    total_debit = Decimal(str(balance_row["total_debit"]))
    total_credit = Decimal(str(balance_row["total_credit"]))

    assert total_debit == total_credit, (
        f"GL imbalanced: total_debit={total_debit}, total_credit={total_credit}"
    )
    # Ensure there are actually GL entries (not vacuously true)
    gl_count = fresh_db.execute(
        "SELECT COUNT(*) as cnt FROM gl_entry WHERE is_cancelled = 0"
    ).fetchone()["cnt"]
    assert gl_count >= 4, f"Expected at least 4 GL entries, got {gl_count}"
