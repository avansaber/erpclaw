"""Procure-to-Pay cross-skill integration tests (XS-18 through XS-24).

These tests verify the complete buying cycle across erpclaw-buying,
erpclaw-payments, erpclaw-gl, and erpclaw-inventory skills.
"""
import json
from decimal import Decimal

from helpers import (
    _call_action,
    setup_phase2_environment,
    create_test_item,
)


# ---------------------------------------------------------------------------
# Shared helper: set default_expense_account_id on company
# ---------------------------------------------------------------------------

def _set_default_expense_account(conn, company_id, expense_account_id):
    """Set the default expense account on the company for invoice posting."""
    conn.execute(
        "UPDATE company SET default_expense_account_id = ? WHERE id = ?",
        (expense_account_id, company_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# XS-18: Full procure-to-pay cycle
# ---------------------------------------------------------------------------

def test_XS18_full_procure_to_pay(fresh_db):
    """Complete P2P: PO -> receipt -> invoice -> GL balanced, PLE created."""
    conn = fresh_db
    env = setup_phase2_environment(conn)
    _set_default_expense_account(conn, env["company_id"], env["expense_id"])

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "50.00",
        "warehouse_id": env["warehouse_id"],
    }])

    # Step 1: Create purchase order (draft)
    result = _call_action("erpclaw-buying", "add-purchase-order", conn,
                          supplier_id=env["supplier_id"],
                          company_id=env["company_id"],
                          items=items_json,
                          posting_date="2026-03-01")
    assert result["status"] == "ok"
    po_id = result["purchase_order_id"]
    assert Decimal(result["grand_total"]) == Decimal("500.00")

    # Step 2: Submit purchase order
    result = _call_action("erpclaw-buying", "submit-purchase-order", conn,
                          purchase_order_id=po_id)
    assert result["status"] == "ok"
    assert result["naming_series"].startswith("PO-")

    # Verify PO status in DB is 'confirmed'
    po_row = conn.execute(
        "SELECT status FROM purchase_order WHERE id = ?", (po_id,)
    ).fetchone()
    assert po_row["status"] == "confirmed"

    # Step 3: Create purchase receipt from PO
    result = _call_action("erpclaw-buying", "create-purchase-receipt", conn,
                          purchase_order_id=po_id,
                          posting_date="2026-03-05")
    assert result["status"] == "ok"
    pr_id = result["purchase_receipt_id"]

    # Step 4: Submit purchase receipt
    result = _call_action("erpclaw-buying", "submit-purchase-receipt", conn,
                          purchase_receipt_id=pr_id)
    assert result["status"] == "ok"
    assert result["naming_series"].startswith("PR-")
    assert result["sle_entries_created"] > 0

    # Verify receipt status in DB
    pr_row = conn.execute(
        "SELECT status FROM purchase_receipt WHERE id = ?", (pr_id,)
    ).fetchone()
    assert pr_row["status"] == "submitted"

    # Verify PO is fully received
    po_row = conn.execute(
        "SELECT status FROM purchase_order WHERE id = ?", (po_id,)
    ).fetchone()
    assert po_row["status"] == "fully_received"

    # Step 5: Create purchase invoice from PO
    result = _call_action("erpclaw-buying", "create-purchase-invoice", conn,
                          purchase_order_id=po_id,
                          posting_date="2026-03-10",
                          due_date="2026-04-10")
    assert result["status"] == "ok"
    pi_id = result["purchase_invoice_id"]
    assert Decimal(result["grand_total"]) == Decimal("500.00")
    # Since PO has submitted receipts, update_stock should be 0
    assert result["update_stock"] == 0

    # Step 6: Submit purchase invoice
    result = _call_action("erpclaw-buying", "submit-purchase-invoice", conn,
                          purchase_invoice_id=pi_id)
    assert result["status"] == "ok"
    assert result["naming_series"].startswith("PINV-")
    assert result["gl_entries_created"] >= 2

    # Verify invoice status in DB
    pi_row = conn.execute(
        "SELECT status FROM purchase_invoice WHERE id = ?", (pi_id,)
    ).fetchone()
    assert pi_row["status"] == "submitted"

    # Verify supplier still exists
    supplier_row = conn.execute(
        "SELECT status FROM supplier WHERE id = ?", (env["supplier_id"],)
    ).fetchone()
    assert supplier_row is not None
    assert supplier_row["status"] == "active"

    # Verify GL is balanced for this invoice
    gl_rows = conn.execute(
        """SELECT * FROM gl_entry
           WHERE voucher_type = 'purchase_invoice' AND voucher_id = ?
             AND is_cancelled = 0""",
        (pi_id,),
    ).fetchall()
    total_debit = sum(Decimal(r["debit"]) for r in gl_rows)
    total_credit = sum(Decimal(r["credit"]) for r in gl_rows)
    assert total_debit == total_credit

    # Verify PLE was created
    ple_rows = conn.execute(
        """SELECT * FROM payment_ledger_entry
           WHERE voucher_type = 'purchase_invoice' AND voucher_id = ?""",
        (pi_id,),
    ).fetchall()
    assert len(ple_rows) == 1
    assert ple_rows[0]["party_type"] == "supplier"
    assert Decimal(ple_rows[0]["amount"]) == Decimal("500.00")


# ---------------------------------------------------------------------------
# XS-19: Purchase receipt creates SLE and GL
# ---------------------------------------------------------------------------

def test_XS19_receipt_creates_stock_and_gl(fresh_db):
    """Submitting a purchase receipt creates SLE (positive) and GL entries
    (DR Stock In Hand, CR Stock Received Not Billed)."""
    conn = fresh_db
    env = setup_phase2_environment(conn)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "20",
        "rate": "30.00",
        "warehouse_id": env["warehouse_id"],
    }])

    # Create and submit PO
    result = _call_action("erpclaw-buying", "add-purchase-order", conn,
                          supplier_id=env["supplier_id"],
                          company_id=env["company_id"],
                          items=items_json,
                          posting_date="2026-03-01")
    po_id = result["purchase_order_id"]
    _call_action("erpclaw-buying", "submit-purchase-order", conn,
                 purchase_order_id=po_id)

    # Create receipt
    result = _call_action("erpclaw-buying", "create-purchase-receipt", conn,
                          purchase_order_id=po_id,
                          posting_date="2026-03-05")
    pr_id = result["purchase_receipt_id"]

    # Submit receipt
    result = _call_action("erpclaw-buying", "submit-purchase-receipt", conn,
                          purchase_receipt_id=pr_id)
    assert result["status"] == "ok"
    assert result["sle_entries_created"] > 0
    assert result["gl_entries_created"] > 0

    # Verify SLE: positive actual_qty
    sle_rows = conn.execute(
        """SELECT * FROM stock_ledger_entry
           WHERE voucher_type = 'purchase_receipt' AND voucher_id = ?
             AND is_cancelled = 0""",
        (pr_id,),
    ).fetchall()
    assert len(sle_rows) > 0
    for sle in sle_rows:
        assert Decimal(sle["actual_qty"]) > 0, "SLE actual_qty should be positive for receipt"

    # Verify GL: DR Stock In Hand, CR Stock Received Not Billed
    gl_rows = conn.execute(
        """SELECT * FROM gl_entry
           WHERE voucher_type = 'purchase_receipt' AND voucher_id = ?
             AND is_cancelled = 0""",
        (pr_id,),
    ).fetchall()
    assert len(gl_rows) >= 2

    total_debit = sum(Decimal(r["debit"]) for r in gl_rows)
    total_credit = sum(Decimal(r["credit"]) for r in gl_rows)
    assert total_debit == total_credit, "GL must be balanced"
    assert total_debit == Decimal("600.00")  # 20 * 30.00


# ---------------------------------------------------------------------------
# XS-20: Purchase invoice creates GL and PLE
# ---------------------------------------------------------------------------

def test_XS20_purchase_invoice_creates_gl_and_ple(fresh_db):
    """Submitting a purchase invoice creates GL entries
    (DR Expense, CR Payable) and PLE entry."""
    conn = fresh_db
    env = setup_phase2_environment(conn)
    _set_default_expense_account(conn, env["company_id"], env["expense_id"])

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "5",
        "rate": "100.00",
        "warehouse_id": env["warehouse_id"],
    }])

    # Create standalone invoice (no PO/receipt)
    result = _call_action("erpclaw-buying", "create-purchase-invoice", conn,
                          supplier_id=env["supplier_id"],
                          company_id=env["company_id"],
                          items=items_json,
                          posting_date="2026-03-15",
                          due_date="2026-04-15")
    assert result["status"] == "ok"
    pi_id = result["purchase_invoice_id"]
    assert Decimal(result["grand_total"]) == Decimal("500.00")

    # Submit invoice
    result = _call_action("erpclaw-buying", "submit-purchase-invoice", conn,
                          purchase_invoice_id=pi_id)
    assert result["status"] == "ok"
    assert result["gl_entries_created"] >= 2

    # Verify GL entries: DR Expense, CR Payable
    gl_rows = conn.execute(
        """SELECT * FROM gl_entry
           WHERE voucher_type = 'purchase_invoice' AND voucher_id = ?
             AND is_cancelled = 0""",
        (pi_id,),
    ).fetchall()

    total_debit = sum(Decimal(r["debit"]) for r in gl_rows)
    total_credit = sum(Decimal(r["credit"]) for r in gl_rows)
    assert total_debit == total_credit, "GL must be balanced"
    assert total_credit == Decimal("500.00")

    # Verify PLE exists with correct amount
    ple_rows = conn.execute(
        """SELECT * FROM payment_ledger_entry
           WHERE voucher_type = 'purchase_invoice' AND voucher_id = ?""",
        (pi_id,),
    ).fetchall()
    assert len(ple_rows) == 1
    assert ple_rows[0]["party_type"] == "supplier"
    assert ple_rows[0]["party_id"] == env["supplier_id"]
    assert Decimal(ple_rows[0]["amount"]) == Decimal("500.00")


# ---------------------------------------------------------------------------
# XS-21: Purchase invoice with tax
# ---------------------------------------------------------------------------

def test_XS21_purchase_invoice_with_tax(fresh_db):
    """Submitting a purchase invoice with tax_template_id includes tax in
    GL (DR includes tax to input tax account) and grand_total = net + tax."""
    conn = fresh_db
    env = setup_phase2_environment(conn)
    _set_default_expense_account(conn, env["company_id"], env["expense_id"])

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "100.00",
        "warehouse_id": env["warehouse_id"],
    }])

    # Create invoice with purchase tax template (8%)
    result = _call_action("erpclaw-buying", "create-purchase-invoice", conn,
                          supplier_id=env["supplier_id"],
                          company_id=env["company_id"],
                          items=items_json,
                          tax_template_id=env["purchase_tax_id"],
                          posting_date="2026-03-15",
                          due_date="2026-04-15")
    assert result["status"] == "ok"
    pi_id = result["purchase_invoice_id"]

    # Verify tax calculation: net=1000, tax=80 (8%), grand=1080
    assert Decimal(result["total_amount"]) == Decimal("1000.00")
    assert Decimal(result["tax_amount"]) == Decimal("80.00")
    assert Decimal(result["grand_total"]) == Decimal("1080.00")

    # Submit
    result = _call_action("erpclaw-buying", "submit-purchase-invoice", conn,
                          purchase_invoice_id=pi_id)
    assert result["status"] == "ok"

    # Verify GL includes tax account debit
    gl_rows = conn.execute(
        """SELECT * FROM gl_entry
           WHERE voucher_type = 'purchase_invoice' AND voucher_id = ?
             AND is_cancelled = 0""",
        (pi_id,),
    ).fetchall()

    # Should have: DR Expense 1000, DR Input Tax 80, CR Payable 1080
    total_debit = sum(Decimal(r["debit"]) for r in gl_rows)
    total_credit = sum(Decimal(r["credit"]) for r in gl_rows)
    assert total_debit == total_credit == Decimal("1080.00")

    # Check that the tax account has a debit entry
    tax_gl = [r for r in gl_rows if r["account_id"] == env["purchase_tax_acct"]]
    assert len(tax_gl) == 1
    assert Decimal(tax_gl[0]["debit"]) == Decimal("80.00")


# ---------------------------------------------------------------------------
# XS-22: Debit note reverses GL
# ---------------------------------------------------------------------------

def test_XS22_debit_note_reverses_gl(fresh_db):
    """Creating a debit note records the return with negative amounts,
    and update-invoice-outstanding reduces the original invoice outstanding.
    The original invoice GL is then cancelled via cancel-purchase-invoice
    which produces reversal GL entries."""
    conn = fresh_db
    env = setup_phase2_environment(conn)
    _set_default_expense_account(conn, env["company_id"], env["expense_id"])

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "50.00",
        "warehouse_id": env["warehouse_id"],
    }])

    # Create and submit purchase invoice (grand_total = 500.00)
    result = _call_action("erpclaw-buying", "create-purchase-invoice", conn,
                          supplier_id=env["supplier_id"],
                          company_id=env["company_id"],
                          items=items_json,
                          posting_date="2026-03-15",
                          due_date="2026-04-15")
    pi_id = result["purchase_invoice_id"]
    original_grand = Decimal(result["grand_total"])
    assert original_grand == Decimal("500.00")

    result = _call_action("erpclaw-buying", "submit-purchase-invoice", conn,
                          purchase_invoice_id=pi_id)
    assert result["status"] == "ok"

    # Verify original GL entries exist and are balanced
    orig_gl = conn.execute(
        """SELECT * FROM gl_entry
           WHERE voucher_type = 'purchase_invoice' AND voucher_id = ?
             AND is_cancelled = 0""",
        (pi_id,),
    ).fetchall()
    assert len(orig_gl) >= 2
    orig_debit = sum(Decimal(r["debit"]) for r in orig_gl)
    orig_credit = sum(Decimal(r["credit"]) for r in orig_gl)
    assert orig_debit == orig_credit == Decimal("500.00")

    # Create debit note for partial return: 3 items @ 50 = 150
    dn_items = json.dumps([{
        "item_id": env["item_id"],
        "qty": "3",
        "rate": "50.00",
    }])
    result = _call_action("erpclaw-buying", "create-debit-note", conn,
                          against_invoice_id=pi_id,
                          items=dn_items,
                          reason="Defective goods",
                          posting_date="2026-03-20")
    assert result["status"] == "ok"
    dn_id = result["debit_note_id"]

    # Verify debit note has negative total (return)
    assert Decimal(result["total_amount"]) == Decimal("-150.00")

    # Verify the debit note record in DB
    dn_row = conn.execute(
        "SELECT * FROM purchase_invoice WHERE id = ?", (dn_id,)
    ).fetchone()
    assert dn_row["is_return"] == 1
    assert dn_row["return_against"] == pi_id
    assert Decimal(dn_row["grand_total"]) == Decimal("-150.00")

    # Use update-invoice-outstanding to reduce the original invoice
    result = _call_action("erpclaw-buying", "update-invoice-outstanding", conn,
                          purchase_invoice_id=pi_id,
                          amount="150.00")
    assert result["status"] == "ok"
    assert Decimal(result["outstanding_amount"]) == Decimal("350.00")

    # Verify original invoice outstanding is reduced in DB
    pi_row = conn.execute(
        "SELECT outstanding_amount, status FROM purchase_invoice WHERE id = ?",
        (pi_id,),
    ).fetchone()
    assert Decimal(pi_row["outstanding_amount"]) == Decimal("350.00")
    assert pi_row["status"] == "partially_paid"

    # Now cancel the original invoice to verify GL reversal works
    result = _call_action("erpclaw-buying", "cancel-purchase-invoice", conn,
                          purchase_invoice_id=pi_id)
    assert result["status"] == "ok"

    # Verify original GL entries are now cancelled
    cancelled_gl = conn.execute(
        """SELECT COUNT(*) as cnt FROM gl_entry
           WHERE voucher_type = 'purchase_invoice' AND voucher_id = ?
             AND is_cancelled = 1""",
        (pi_id,),
    ).fetchone()["cnt"]
    assert cancelled_gl >= 2

    # Verify reversal GL entries are created (balanced)
    reversal_gl = conn.execute(
        """SELECT * FROM gl_entry
           WHERE voucher_type = 'purchase_invoice' AND voucher_id = ?
             AND is_cancelled = 0""",
        (pi_id,),
    ).fetchall()
    assert len(reversal_gl) >= 2
    rev_debit = sum(Decimal(r["debit"]) for r in reversal_gl)
    rev_credit = sum(Decimal(r["credit"]) for r in reversal_gl)
    assert rev_debit == rev_credit, "Reversal GL must be balanced"


# ---------------------------------------------------------------------------
# XS-23: Payment closes purchase invoice
# ---------------------------------------------------------------------------

def test_XS23_payment_closes_purchase_invoice(fresh_db):
    """Submitting a payment (pay type) that covers the full invoice
    reduces outstanding to 0."""
    conn = fresh_db
    env = setup_phase2_environment(conn)
    _set_default_expense_account(conn, env["company_id"], env["expense_id"])

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "50.00",
        "warehouse_id": env["warehouse_id"],
    }])

    # Create and submit purchase invoice
    result = _call_action("erpclaw-buying", "create-purchase-invoice", conn,
                          supplier_id=env["supplier_id"],
                          company_id=env["company_id"],
                          items=items_json,
                          posting_date="2026-03-15",
                          due_date="2026-04-15")
    pi_id = result["purchase_invoice_id"]
    grand_total = result["grand_total"]  # "500.00"

    result = _call_action("erpclaw-buying", "submit-purchase-invoice", conn,
                          purchase_invoice_id=pi_id)
    assert result["status"] == "ok"

    # Create payment (pay type: DR payable, CR bank)
    result = _call_action("erpclaw-payments", "add-payment", conn,
                          company_id=env["company_id"],
                          payment_type="pay",
                          posting_date="2026-03-20",
                          party_type="supplier",
                          party_id=env["supplier_id"],
                          paid_from_account=env["bank_id"],
                          paid_to_account=env["payable_id"],
                          paid_amount=grand_total)
    assert result["status"] == "ok"
    pe_id = result["payment_entry_id"]

    # Submit payment
    result = _call_action("erpclaw-payments", "submit-payment", conn,
                          payment_entry_id=pe_id)
    assert result["status"] == "ok"
    assert result["gl_entries_created"] == 2

    # Update invoice outstanding via cross-skill action
    result = _call_action("erpclaw-buying", "update-invoice-outstanding", conn,
                          purchase_invoice_id=pi_id,
                          amount=grand_total)
    assert result["status"] == "ok"

    # Verify outstanding is now 0
    pi_row = conn.execute(
        "SELECT outstanding_amount, status FROM purchase_invoice WHERE id = ?",
        (pi_id,),
    ).fetchone()
    assert Decimal(pi_row["outstanding_amount"]) == Decimal("0")
    assert pi_row["status"] == "paid"


# ---------------------------------------------------------------------------
# XS-24: GL balanced after full buying cycle
# ---------------------------------------------------------------------------

def test_XS24_gl_balanced_after_buying_cycle(fresh_db):
    """After a complete buying cycle (PO -> receipt -> invoice -> payment),
    check-gl-integrity passes."""
    conn = fresh_db
    env = setup_phase2_environment(conn)
    _set_default_expense_account(conn, env["company_id"], env["expense_id"])

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "15",
        "rate": "40.00",
        "warehouse_id": env["warehouse_id"],
    }])

    # Full cycle: PO -> receipt -> invoice -> payment
    # 1. Create and submit PO
    result = _call_action("erpclaw-buying", "add-purchase-order", conn,
                          supplier_id=env["supplier_id"],
                          company_id=env["company_id"],
                          items=items_json,
                          posting_date="2026-04-01")
    po_id = result["purchase_order_id"]
    _call_action("erpclaw-buying", "submit-purchase-order", conn,
                 purchase_order_id=po_id)

    # 2. Create and submit receipt
    result = _call_action("erpclaw-buying", "create-purchase-receipt", conn,
                          purchase_order_id=po_id,
                          posting_date="2026-04-05")
    pr_id = result["purchase_receipt_id"]
    _call_action("erpclaw-buying", "submit-purchase-receipt", conn,
                 purchase_receipt_id=pr_id)

    # 3. Create and submit invoice
    result = _call_action("erpclaw-buying", "create-purchase-invoice", conn,
                          purchase_order_id=po_id,
                          posting_date="2026-04-10",
                          due_date="2026-05-10")
    pi_id = result["purchase_invoice_id"]
    grand_total = result["grand_total"]  # "600.00"
    _call_action("erpclaw-buying", "submit-purchase-invoice", conn,
                 purchase_invoice_id=pi_id)

    # 4. Create and submit payment
    result = _call_action("erpclaw-payments", "add-payment", conn,
                          company_id=env["company_id"],
                          payment_type="pay",
                          posting_date="2026-04-15",
                          party_type="supplier",
                          party_id=env["supplier_id"],
                          paid_from_account=env["bank_id"],
                          paid_to_account=env["payable_id"],
                          paid_amount=grand_total)
    pe_id = result["payment_entry_id"]
    _call_action("erpclaw-payments", "submit-payment", conn,
                 payment_entry_id=pe_id)

    # 5. Verify GL integrity
    result = _call_action("erpclaw-gl", "check-gl-integrity", conn,
                          company_id=env["company_id"])
    assert result["status"] == "ok"
    assert result["balanced"] is True, (
        f"GL not balanced: difference={result['difference']}"
    )
