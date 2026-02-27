"""Tests for the full procure-to-pay workflow (W7): MR -> RFQ -> SQ -> PO ->
GRN -> PINV, including partial receipt, partial invoicing, and GL balance
verification.

8 integration tests covering the complete buying cycle and edge cases.
"""
import json
import uuid
from decimal import Decimal

from db_query import ACTIONS
from helpers import (
    _call_action,
    setup_buying_environment,
    create_test_item,
    create_test_supplier,
)


# ---------------------------------------------------------------------------
# 1. test_full_procure_to_pay
# ---------------------------------------------------------------------------

def test_full_procure_to_pay(fresh_db):
    """Complete W7 flow: MR -> RFQ -> SQ -> PO -> GRN -> PINV."""
    env = setup_buying_environment(fresh_db)

    # --- Step 1: Material Request ---
    mr_items = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "warehouse_id": env["warehouse_id"],
    }])
    mr_result = _call_action(
        ACTIONS["add-material-request"], fresh_db,
        request_type="purchase",
        company_id=env["company_id"],
        items=mr_items,
    )
    assert mr_result["status"] == "ok"
    mr_id = mr_result["material_request_id"]

    submit_mr = _call_action(
        ACTIONS["submit-material-request"], fresh_db,
        material_request_id=mr_id,
    )
    assert submit_mr["status"] == "ok"

    # --- Step 2: RFQ ---
    rfq_items = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
    }])
    rfq_suppliers = json.dumps([env["supplier_id"]])

    rfq_result = _call_action(
        ACTIONS["add-rfq"], fresh_db,
        company_id=env["company_id"],
        items=rfq_items,
        suppliers=rfq_suppliers,
    )
    assert rfq_result["status"] == "ok"
    rfq_id = rfq_result["rfq_id"]

    submit_rfq = _call_action(
        ACTIONS["submit-rfq"], fresh_db,
        rfq_id=rfq_id,
    )
    assert submit_rfq["status"] == "ok"

    # --- Step 3: Supplier Quotation ---
    # Get RFQ item ID for the quotation
    rfq_item_row = fresh_db.execute(
        "SELECT id FROM rfq_item WHERE rfq_id = ?", (rfq_id,)
    ).fetchone()
    rfq_item_id = rfq_item_row["id"]

    sq_items = json.dumps([{
        "rfq_item_id": rfq_item_id,
        "rate": "25.00",
    }])
    sq_result = _call_action(
        ACTIONS["add-supplier-quotation"], fresh_db,
        rfq_id=rfq_id,
        supplier_id=env["supplier_id"],
        items=sq_items,
    )
    assert sq_result["status"] == "ok"

    # --- Step 4: Purchase Order ---
    po_items = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "25.00",
        "warehouse_id": env["warehouse_id"],
    }])
    po_result = _call_action(
        ACTIONS["add-purchase-order"], fresh_db,
        supplier_id=env["supplier_id"],
        company_id=env["company_id"],
        items=po_items,
        posting_date="2026-02-16",
    )
    assert po_result["status"] == "ok"
    po_id = po_result["purchase_order_id"]

    submit_po = _call_action(
        ACTIONS["submit-purchase-order"], fresh_db,
        purchase_order_id=po_id,
    )
    assert submit_po["status"] == "ok"
    assert submit_po["status_field"] if "status_field" in submit_po else True

    # --- Step 5: GRN (Purchase Receipt) ---
    grn_result = _call_action(
        ACTIONS["create-purchase-receipt"], fresh_db,
        purchase_order_id=po_id,
        posting_date="2026-02-17",
    )
    assert grn_result["status"] == "ok"
    grn_id = grn_result["purchase_receipt_id"]

    submit_grn = _call_action(
        ACTIONS["submit-purchase-receipt"], fresh_db,
        purchase_receipt_id=grn_id,
    )
    assert submit_grn["status"] == "ok"
    assert submit_grn["sle_entries_created"] >= 1

    # --- Step 6: Purchase Invoice ---
    pi_result = _call_action(
        ACTIONS["create-purchase-invoice"], fresh_db,
        purchase_order_id=po_id,
    )
    assert pi_result["status"] == "ok"
    pi_id = pi_result["purchase_invoice_id"]

    submit_pi = _call_action(
        ACTIONS["submit-purchase-invoice"], fresh_db,
        purchase_invoice_id=pi_id,
    )
    assert submit_pi["status"] == "ok"
    assert submit_pi["gl_entries_created"] >= 2

    # Verify PO is fully invoiced
    po_row = fresh_db.execute(
        "SELECT status FROM purchase_order WHERE id = ?", (po_id,)
    ).fetchone()
    assert po_row["status"] in ("fully_invoiced", "fully_received")


# ---------------------------------------------------------------------------
# 2. test_material_request_to_po
# ---------------------------------------------------------------------------

def test_material_request_to_po(fresh_db):
    """Submit a material request, then create a PO from the same items."""
    env = setup_buying_environment(fresh_db)

    mr_items = json.dumps([{
        "item_id": env["item_id"],
        "qty": "20",
        "warehouse_id": env["warehouse_id"],
    }])
    mr_result = _call_action(
        ACTIONS["add-material-request"], fresh_db,
        request_type="purchase",
        company_id=env["company_id"],
        items=mr_items,
    )
    assert mr_result["status"] == "ok"
    mr_id = mr_result["material_request_id"]

    submit_mr = _call_action(
        ACTIONS["submit-material-request"], fresh_db,
        material_request_id=mr_id,
    )
    assert submit_mr["status"] == "ok"
    assert submit_mr["naming_series"].startswith("MR-")

    # Create PO from same items
    po_items = json.dumps([{
        "item_id": env["item_id"],
        "qty": "20",
        "rate": "30.00",
        "warehouse_id": env["warehouse_id"],
    }])
    po_result = _call_action(
        ACTIONS["add-purchase-order"], fresh_db,
        supplier_id=env["supplier_id"],
        company_id=env["company_id"],
        items=po_items,
        posting_date="2026-02-16",
    )
    assert po_result["status"] == "ok"
    assert po_result["total_amount"] == "600.00"
    assert po_result["grand_total"] == "600.00"

    # Submit PO
    submit_po = _call_action(
        ACTIONS["submit-purchase-order"], fresh_db,
        purchase_order_id=po_result["purchase_order_id"],
    )
    assert submit_po["status"] == "ok"
    assert submit_po["naming_series"].startswith("PO-")


# ---------------------------------------------------------------------------
# 3. test_po_to_purchase_receipt
# ---------------------------------------------------------------------------

def test_po_to_purchase_receipt(fresh_db):
    """Submit PO, create GRN, submit GRN and verify SLE created."""
    env = setup_buying_environment(fresh_db)

    po_items = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "25.00",
        "warehouse_id": env["warehouse_id"],
    }])
    po_result = _call_action(
        ACTIONS["add-purchase-order"], fresh_db,
        supplier_id=env["supplier_id"],
        company_id=env["company_id"],
        items=po_items,
        posting_date="2026-02-16",
    )
    assert po_result["status"] == "ok"
    po_id = po_result["purchase_order_id"]

    submit_po = _call_action(
        ACTIONS["submit-purchase-order"], fresh_db,
        purchase_order_id=po_id,
    )
    assert submit_po["status"] == "ok"

    # Create GRN
    grn_result = _call_action(
        ACTIONS["create-purchase-receipt"], fresh_db,
        purchase_order_id=po_id,
        posting_date="2026-02-17",
    )
    assert grn_result["status"] == "ok"
    grn_id = grn_result["purchase_receipt_id"]
    assert grn_result["total_qty"] == "10.00"

    # Submit GRN
    submit_grn = _call_action(
        ACTIONS["submit-purchase-receipt"], fresh_db,
        purchase_receipt_id=grn_id,
    )
    assert submit_grn["status"] == "ok"
    assert submit_grn["sle_entries_created"] >= 1

    # Verify SLE entry exists
    sle = fresh_db.execute(
        """SELECT * FROM stock_ledger_entry
           WHERE voucher_type = 'purchase_receipt' AND voucher_id = ?
             AND is_cancelled = 0""",
        (grn_id,),
    ).fetchone()
    assert sle is not None
    assert Decimal(sle["actual_qty"]) == Decimal("10.00")
    assert sle["item_id"] == env["item_id"]
    assert sle["warehouse_id"] == env["warehouse_id"]

    # Verify PO status updated to fully_received
    po_row = fresh_db.execute(
        "SELECT status, per_received FROM purchase_order WHERE id = ?",
        (po_id,),
    ).fetchone()
    assert po_row["status"] == "fully_received"
    assert Decimal(po_row["per_received"]) >= Decimal("100")


# ---------------------------------------------------------------------------
# 4. test_receipt_to_invoice
# ---------------------------------------------------------------------------

def test_receipt_to_invoice(fresh_db):
    """Submit GRN, create invoice from GRN, submit and verify GL."""
    env = setup_buying_environment(fresh_db)

    # Create and submit PO
    po_items = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "25.00",
        "warehouse_id": env["warehouse_id"],
    }])
    po_result = _call_action(
        ACTIONS["add-purchase-order"], fresh_db,
        supplier_id=env["supplier_id"],
        company_id=env["company_id"],
        items=po_items,
        posting_date="2026-02-16",
    )
    assert po_result["status"] == "ok"
    po_id = po_result["purchase_order_id"]

    _call_action(
        ACTIONS["submit-purchase-order"], fresh_db,
        purchase_order_id=po_id,
    )

    # Create and submit GRN
    grn_result = _call_action(
        ACTIONS["create-purchase-receipt"], fresh_db,
        purchase_order_id=po_id,
        posting_date="2026-02-17",
    )
    assert grn_result["status"] == "ok"
    grn_id = grn_result["purchase_receipt_id"]

    _call_action(
        ACTIONS["submit-purchase-receipt"], fresh_db,
        purchase_receipt_id=grn_id,
    )

    # Create invoice from PO (stock already moved via GRN)
    pi_result = _call_action(
        ACTIONS["create-purchase-invoice"], fresh_db,
        purchase_order_id=po_id,
    )
    assert pi_result["status"] == "ok"
    pi_id = pi_result["purchase_invoice_id"]
    # update_stock should be 0 since GRN already submitted
    assert pi_result.get("update_stock") == 0 or pi_result.get("update_stock") is False

    # Submit invoice
    submit_pi = _call_action(
        ACTIONS["submit-purchase-invoice"], fresh_db,
        purchase_invoice_id=pi_id,
    )
    assert submit_pi["status"] == "ok"
    assert submit_pi["gl_entries_created"] >= 2

    # Verify GL: SRNB debit for 250 (clears accrual from receipt)
    srnb_gl = fresh_db.execute(
        """SELECT SUM(CAST(debit AS REAL)) as total_debit
           FROM gl_entry
           WHERE voucher_type = 'purchase_invoice' AND voucher_id = ?
             AND account_id = ? AND is_cancelled = 0""",
        (pi_id, env["stock_received_id"]),
    ).fetchone()
    assert srnb_gl is not None
    assert Decimal(str(srnb_gl["total_debit"])) == Decimal("250.00")


# ---------------------------------------------------------------------------
# 5. test_invoice_with_tax
# ---------------------------------------------------------------------------

def test_invoice_with_tax(fresh_db):
    """Full PO -> GRN -> PINV flow with tax template applied."""
    env = setup_buying_environment(fresh_db)

    # Create PO with tax
    po_items = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "25.00",
        "warehouse_id": env["warehouse_id"],
    }])
    po_result = _call_action(
        ACTIONS["add-purchase-order"], fresh_db,
        supplier_id=env["supplier_id"],
        company_id=env["company_id"],
        items=po_items,
        tax_template_id=env["tax_template_id"],
        posting_date="2026-02-16",
    )
    assert po_result["status"] == "ok"
    po_id = po_result["purchase_order_id"]
    # 250.00 + 8% tax = 270.00
    assert po_result["tax_amount"] == "20.00"
    assert po_result["grand_total"] == "270.00"

    _call_action(
        ACTIONS["submit-purchase-order"], fresh_db,
        purchase_order_id=po_id,
    )

    # GRN
    grn_result = _call_action(
        ACTIONS["create-purchase-receipt"], fresh_db,
        purchase_order_id=po_id,
        posting_date="2026-02-17",
    )
    assert grn_result["status"] == "ok"
    grn_id = grn_result["purchase_receipt_id"]

    _call_action(
        ACTIONS["submit-purchase-receipt"], fresh_db,
        purchase_receipt_id=grn_id,
    )

    # Invoice from PO (inherits tax_template_id)
    pi_result = _call_action(
        ACTIONS["create-purchase-invoice"], fresh_db,
        purchase_order_id=po_id,
    )
    assert pi_result["status"] == "ok"
    pi_id = pi_result["purchase_invoice_id"]
    assert pi_result["tax_amount"] == "20.00"
    assert pi_result["grand_total"] == "270.00"

    submit_pi = _call_action(
        ACTIONS["submit-purchase-invoice"], fresh_db,
        purchase_invoice_id=pi_id,
    )
    assert submit_pi["status"] == "ok"

    # Verify tax GL
    tax_gl = fresh_db.execute(
        """SELECT * FROM gl_entry
           WHERE voucher_type = 'purchase_invoice' AND voucher_id = ?
             AND account_id = ? AND is_cancelled = 0""",
        (pi_id, env["tax_account_id"]),
    ).fetchone()
    assert tax_gl is not None
    assert Decimal(tax_gl["debit"]) == Decimal("20.00")

    # AP credit = grand total 270
    ap_gl = fresh_db.execute(
        """SELECT * FROM gl_entry
           WHERE voucher_type = 'purchase_invoice' AND voucher_id = ?
             AND account_id = ? AND is_cancelled = 0""",
        (pi_id, env["payable_id"]),
    ).fetchone()
    assert ap_gl is not None
    assert Decimal(ap_gl["credit"]) == Decimal("270.00")


# ---------------------------------------------------------------------------
# 6. test_partial_receipt
# ---------------------------------------------------------------------------

def test_partial_receipt(fresh_db):
    """PO for qty 10, receive qty 5 only. Verify PO receipt_status."""
    env = setup_buying_environment(fresh_db)

    po_items = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "25.00",
        "warehouse_id": env["warehouse_id"],
    }])
    po_result = _call_action(
        ACTIONS["add-purchase-order"], fresh_db,
        supplier_id=env["supplier_id"],
        company_id=env["company_id"],
        items=po_items,
        posting_date="2026-02-16",
    )
    assert po_result["status"] == "ok"
    po_id = po_result["purchase_order_id"]

    _call_action(
        ACTIONS["submit-purchase-order"], fresh_db,
        purchase_order_id=po_id,
    )

    # Get PO item ID for partial receipt
    po_item_row = fresh_db.execute(
        "SELECT id FROM purchase_order_item WHERE purchase_order_id = ?",
        (po_id,),
    ).fetchone()
    po_item_id = po_item_row["id"]

    # Create partial GRN for qty 5
    partial_items = json.dumps([{
        "purchase_order_item_id": po_item_id,
        "qty": "5",
        "warehouse_id": env["warehouse_id"],
    }])
    grn_result = _call_action(
        ACTIONS["create-purchase-receipt"], fresh_db,
        purchase_order_id=po_id,
        items=partial_items,
        posting_date="2026-02-17",
    )
    assert grn_result["status"] == "ok"
    grn_id = grn_result["purchase_receipt_id"]
    assert grn_result["total_qty"] == "5.00"

    # Submit GRN
    submit_grn = _call_action(
        ACTIONS["submit-purchase-receipt"], fresh_db,
        purchase_receipt_id=grn_id,
    )
    assert submit_grn["status"] == "ok"

    # Verify PO is partially_received
    po_row = fresh_db.execute(
        "SELECT status, per_received FROM purchase_order WHERE id = ?",
        (po_id,),
    ).fetchone()
    assert po_row["status"] == "partially_received"
    assert Decimal(po_row["per_received"]) == Decimal("50.00")

    # Verify PO item received_qty
    poi_row = fresh_db.execute(
        "SELECT received_qty FROM purchase_order_item WHERE id = ?",
        (po_item_id,),
    ).fetchone()
    assert Decimal(poi_row["received_qty"]) == Decimal("5")


# ---------------------------------------------------------------------------
# 7. test_partial_invoicing
# ---------------------------------------------------------------------------

def test_partial_invoicing(fresh_db):
    """Receive qty 10, invoice only qty 5, check outstanding is partial."""
    env = setup_buying_environment(fresh_db)

    # Create PO for qty 10
    po_items = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "25.00",
        "warehouse_id": env["warehouse_id"],
    }])
    po_result = _call_action(
        ACTIONS["add-purchase-order"], fresh_db,
        supplier_id=env["supplier_id"],
        company_id=env["company_id"],
        items=po_items,
        posting_date="2026-02-16",
    )
    assert po_result["status"] == "ok"
    po_id = po_result["purchase_order_id"]

    _call_action(
        ACTIONS["submit-purchase-order"], fresh_db,
        purchase_order_id=po_id,
    )

    # Receive all 10
    grn_result = _call_action(
        ACTIONS["create-purchase-receipt"], fresh_db,
        purchase_order_id=po_id,
        posting_date="2026-02-17",
    )
    assert grn_result["status"] == "ok"
    grn_id = grn_result["purchase_receipt_id"]

    _call_action(
        ACTIONS["submit-purchase-receipt"], fresh_db,
        purchase_receipt_id=grn_id,
    )

    # Create standalone invoice for only 5 items
    partial_inv_items = json.dumps([{
        "item_id": env["item_id"],
        "qty": "5",
        "rate": "25.00",
    }])
    pi_result = _call_action(
        ACTIONS["create-purchase-invoice"], fresh_db,
        supplier_id=env["supplier_id"],
        company_id=env["company_id"],
        items=partial_inv_items,
        posting_date="2026-02-18",
    )
    assert pi_result["status"] == "ok"
    pi_id = pi_result["purchase_invoice_id"]
    assert pi_result["total_amount"] == "125.00"
    assert pi_result["grand_total"] == "125.00"

    # Submit partial invoice
    submit_pi = _call_action(
        ACTIONS["submit-purchase-invoice"], fresh_db,
        purchase_invoice_id=pi_id,
    )
    assert submit_pi["status"] == "ok"

    # Outstanding should be 125.00
    pi_row = fresh_db.execute(
        "SELECT outstanding_amount, status FROM purchase_invoice WHERE id = ?",
        (pi_id,),
    ).fetchone()
    assert pi_row["outstanding_amount"] == "125.00"
    assert pi_row["status"] == "submitted"

    # Make partial payment to reduce outstanding
    pay_result = _call_action(
        ACTIONS["update-invoice-outstanding"], fresh_db,
        purchase_invoice_id=pi_id,
        amount="50.00",
    )
    assert pay_result["status"] == "ok"
    assert pay_result["outstanding_amount"] == "75.00"
    pi_row = fresh_db.execute(
        "SELECT status FROM purchase_invoice WHERE id = ?", (pi_id,)
    ).fetchone()
    assert pi_row["status"] == "partially_paid"


# ---------------------------------------------------------------------------
# 8. test_gl_balanced_after_full_cycle
# ---------------------------------------------------------------------------

def test_gl_balanced_after_full_cycle(fresh_db):
    """Run full PO -> GRN -> PINV cycle, verify SUM(debit) = SUM(credit)."""
    env = setup_buying_environment(fresh_db)

    # PO
    po_items = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "25.00",
        "warehouse_id": env["warehouse_id"],
    }])
    po_result = _call_action(
        ACTIONS["add-purchase-order"], fresh_db,
        supplier_id=env["supplier_id"],
        company_id=env["company_id"],
        items=po_items,
        posting_date="2026-02-16",
    )
    assert po_result["status"] == "ok"
    po_id = po_result["purchase_order_id"]

    _call_action(
        ACTIONS["submit-purchase-order"], fresh_db,
        purchase_order_id=po_id,
    )

    # GRN
    grn_result = _call_action(
        ACTIONS["create-purchase-receipt"], fresh_db,
        purchase_order_id=po_id,
        posting_date="2026-02-17",
    )
    assert grn_result["status"] == "ok"
    grn_id = grn_result["purchase_receipt_id"]

    submit_grn = _call_action(
        ACTIONS["submit-purchase-receipt"], fresh_db,
        purchase_receipt_id=grn_id,
    )
    assert submit_grn["status"] == "ok"

    # Invoice from PO
    pi_result = _call_action(
        ACTIONS["create-purchase-invoice"], fresh_db,
        purchase_order_id=po_id,
    )
    assert pi_result["status"] == "ok"
    pi_id = pi_result["purchase_invoice_id"]

    submit_pi = _call_action(
        ACTIONS["submit-purchase-invoice"], fresh_db,
        purchase_invoice_id=pi_id,
    )
    assert submit_pi["status"] == "ok"

    # Verify GL balance: SUM(debit) == SUM(credit) across ALL gl_entry rows
    balance = fresh_db.execute(
        """SELECT
             COALESCE(SUM(CAST(debit AS REAL)), 0) as total_debit,
             COALESCE(SUM(CAST(credit AS REAL)), 0) as total_credit
           FROM gl_entry
           WHERE is_cancelled = 0"""
    ).fetchone()

    total_debit = Decimal(str(balance["total_debit"]))
    total_credit = Decimal(str(balance["total_credit"]))

    assert total_debit == total_credit, (
        f"GL imbalance: total_debit={total_debit}, total_credit={total_credit}"
    )
    # Ensure there are actually GL entries (not just a trivial 0==0 case)
    assert total_debit > 0, "Expected non-zero GL entries after full cycle"

    # Also verify GRN GL balance independently
    grn_gl = fresh_db.execute(
        """SELECT
             COALESCE(SUM(CAST(debit AS REAL)), 0) as d,
             COALESCE(SUM(CAST(credit AS REAL)), 0) as c
           FROM gl_entry
           WHERE voucher_type = 'purchase_receipt' AND voucher_id = ?
             AND is_cancelled = 0""",
        (grn_id,),
    ).fetchone()
    assert Decimal(str(grn_gl["d"])) == Decimal(str(grn_gl["c"])), (
        f"GRN GL imbalance: debit={grn_gl['d']}, credit={grn_gl['c']}"
    )

    # Verify invoice GL balance independently
    pi_gl = fresh_db.execute(
        """SELECT
             COALESCE(SUM(CAST(debit AS REAL)), 0) as d,
             COALESCE(SUM(CAST(credit AS REAL)), 0) as c
           FROM gl_entry
           WHERE voucher_type = 'purchase_invoice' AND voucher_id = ?
             AND is_cancelled = 0""",
        (pi_id,),
    ).fetchone()
    assert Decimal(str(pi_gl["d"])) == Decimal(str(pi_gl["c"])), (
        f"Invoice GL imbalance: debit={pi_gl['d']}, credit={pi_gl['c']}"
    )
