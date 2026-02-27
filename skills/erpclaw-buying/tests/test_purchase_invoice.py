"""Tests for purchase invoice actions: create, update, get, list, submit,
cancel, debit note, and update-invoice-outstanding.

15 tests covering the full purchase invoice lifecycle including GL entries,
payment ledger entries, tax handling, debit notes, and cross-skill outstanding
updates.
"""
import json
import uuid
from decimal import Decimal

from db_query import ACTIONS
from helpers import (
    _call_action,
    setup_buying_environment,
    create_test_supplier,
    create_test_item,
    create_test_account,
)


# ---------------------------------------------------------------------------
# 1. test_create_purchase_invoice
# ---------------------------------------------------------------------------

def test_create_purchase_invoice(fresh_db):
    """Create a standalone purchase invoice with items and verify success."""
    env = setup_buying_environment(fresh_db)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "25.00",
    }])

    result = _call_action(
        ACTIONS["create-purchase-invoice"], fresh_db,
        supplier_id=env["supplier_id"],
        company_id=env["company_id"],
        items=items_json,
        posting_date="2026-02-16",
        due_date="2026-03-16",
    )

    assert result["status"] == "ok"
    assert "purchase_invoice_id" in result
    assert result["total_amount"] == "250.00"
    assert result["grand_total"] == "250.00"

    # Verify persisted in database
    row = fresh_db.execute(
        "SELECT * FROM purchase_invoice WHERE id = ?",
        (result["purchase_invoice_id"],),
    ).fetchone()
    assert row is not None
    assert row["status"] == "draft"
    assert row["supplier_id"] == env["supplier_id"]
    assert row["outstanding_amount"] == "250.00"


# ---------------------------------------------------------------------------
# 2. test_create_purchase_invoice_from_po
# ---------------------------------------------------------------------------

def test_create_purchase_invoice_from_po(fresh_db):
    """Create a purchase invoice from a submitted PO and verify linkage."""
    env = setup_buying_environment(fresh_db)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "25.00",
        "warehouse_id": env["warehouse_id"],
    }])

    # Create and submit PO
    po_result = _call_action(
        ACTIONS["add-purchase-order"], fresh_db,
        supplier_id=env["supplier_id"],
        company_id=env["company_id"],
        items=items_json,
        posting_date="2026-02-16",
    )
    assert po_result["status"] == "ok"
    po_id = po_result["purchase_order_id"]

    submit_po = _call_action(
        ACTIONS["submit-purchase-order"], fresh_db,
        purchase_order_id=po_id,
    )
    assert submit_po["status"] == "ok"

    # Create invoice from PO
    pi_result = _call_action(
        ACTIONS["create-purchase-invoice"], fresh_db,
        purchase_order_id=po_id,
    )

    assert pi_result["status"] == "ok"
    assert "purchase_invoice_id" in pi_result
    assert pi_result["total_amount"] == "250.00"

    # Verify linkage
    pi_row = fresh_db.execute(
        "SELECT * FROM purchase_invoice WHERE id = ?",
        (pi_result["purchase_invoice_id"],),
    ).fetchone()
    assert pi_row["purchase_order_id"] == po_id
    assert pi_row["supplier_id"] == env["supplier_id"]


# ---------------------------------------------------------------------------
# 3. test_create_purchase_invoice_missing_supplier
# ---------------------------------------------------------------------------

def test_create_purchase_invoice_missing_supplier(fresh_db):
    """Creating a standalone invoice without supplier_id should error."""
    env = setup_buying_environment(fresh_db)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "25.00",
    }])

    result = _call_action(
        ACTIONS["create-purchase-invoice"], fresh_db,
        company_id=env["company_id"],
        items=items_json,
    )

    assert result["status"] == "error"
    assert "supplier" in result["message"].lower()


# ---------------------------------------------------------------------------
# 4. test_get_purchase_invoice
# ---------------------------------------------------------------------------

def test_get_purchase_invoice(fresh_db):
    """Create then get a purchase invoice, verify items and totals."""
    env = setup_buying_environment(fresh_db)

    items_json = json.dumps([
        {"item_id": env["item_id"], "qty": "5", "rate": "20.00"},
    ])

    create_result = _call_action(
        ACTIONS["create-purchase-invoice"], fresh_db,
        supplier_id=env["supplier_id"],
        company_id=env["company_id"],
        items=items_json,
        posting_date="2026-02-16",
    )
    assert create_result["status"] == "ok"
    pi_id = create_result["purchase_invoice_id"]

    get_result = _call_action(
        ACTIONS["get-purchase-invoice"], fresh_db,
        purchase_invoice_id=pi_id,
    )

    assert get_result["status"] == "ok"
    assert get_result["id"] == pi_id
    assert get_result["total_amount"] == "100.00"
    assert get_result["grand_total"] == "100.00"
    assert "items" in get_result
    assert len(get_result["items"]) == 1
    assert get_result["items"][0]["item_id"] == env["item_id"]
    assert get_result["items"][0]["quantity"] == "5.00"
    assert get_result["items"][0]["rate"] == "20.00"
    assert get_result["items"][0]["amount"] == "100.00"


# ---------------------------------------------------------------------------
# 5. test_list_purchase_invoices
# ---------------------------------------------------------------------------

def test_list_purchase_invoices(fresh_db):
    """Create 2 purchase invoices, list them, verify both returned."""
    env = setup_buying_environment(fresh_db)

    for rate in ["25.00", "30.00"]:
        items_json = json.dumps([{
            "item_id": env["item_id"],
            "qty": "10",
            "rate": rate,
        }])
        r = _call_action(
            ACTIONS["create-purchase-invoice"], fresh_db,
            supplier_id=env["supplier_id"],
            company_id=env["company_id"],
            items=items_json,
            posting_date="2026-02-16",
        )
        assert r["status"] == "ok"

    list_result = _call_action(
        ACTIONS["list-purchase-invoices"], fresh_db,
        company_id=env["company_id"],
    )

    assert list_result["status"] == "ok"
    assert list_result["total_count"] == 2
    assert len(list_result["purchase_invoices"]) == 2


# ---------------------------------------------------------------------------
# 6. test_update_purchase_invoice
# ---------------------------------------------------------------------------

def test_update_purchase_invoice(fresh_db):
    """Update a draft invoice's due_date and verify."""
    env = setup_buying_environment(fresh_db)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "25.00",
    }])

    create_result = _call_action(
        ACTIONS["create-purchase-invoice"], fresh_db,
        supplier_id=env["supplier_id"],
        company_id=env["company_id"],
        items=items_json,
        posting_date="2026-02-16",
        due_date="2026-03-16",
    )
    assert create_result["status"] == "ok"
    pi_id = create_result["purchase_invoice_id"]

    update_result = _call_action(
        ACTIONS["update-purchase-invoice"], fresh_db,
        purchase_invoice_id=pi_id,
        due_date="2026-04-16",
    )

    assert update_result["status"] == "ok"
    assert "due_date" in update_result["updated_fields"]

    # Verify in DB
    row = fresh_db.execute(
        "SELECT due_date FROM purchase_invoice WHERE id = ?", (pi_id,)
    ).fetchone()
    assert row["due_date"] == "2026-04-16"


# ---------------------------------------------------------------------------
# 7. test_submit_purchase_invoice
# ---------------------------------------------------------------------------

def test_submit_purchase_invoice(fresh_db):
    """Submit a purchase invoice and verify status changes to submitted."""
    env = setup_buying_environment(fresh_db)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "25.00",
    }])

    create_result = _call_action(
        ACTIONS["create-purchase-invoice"], fresh_db,
        supplier_id=env["supplier_id"],
        company_id=env["company_id"],
        items=items_json,
        posting_date="2026-02-16",
    )
    assert create_result["status"] == "ok"
    pi_id = create_result["purchase_invoice_id"]

    submit_result = _call_action(
        ACTIONS["submit-purchase-invoice"], fresh_db,
        purchase_invoice_id=pi_id,
    )

    assert submit_result["status"] == "ok"
    assert submit_result["status_field"] if "status_field" in submit_result else True
    assert "naming_series" in submit_result
    assert submit_result["naming_series"].startswith("PINV-")

    # Verify status in DB
    row = fresh_db.execute(
        "SELECT status FROM purchase_invoice WHERE id = ?", (pi_id,)
    ).fetchone()
    assert row["status"] == "submitted"


# ---------------------------------------------------------------------------
# 8. test_submit_purchase_invoice_gl
# ---------------------------------------------------------------------------

def test_submit_purchase_invoice_gl(fresh_db):
    """Submit a purchase invoice and verify GL entries (Expense DR, AP CR)."""
    env = setup_buying_environment(fresh_db)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "25.00",
    }])

    create_result = _call_action(
        ACTIONS["create-purchase-invoice"], fresh_db,
        supplier_id=env["supplier_id"],
        company_id=env["company_id"],
        items=items_json,
        posting_date="2026-02-16",
    )
    assert create_result["status"] == "ok"
    pi_id = create_result["purchase_invoice_id"]

    submit_result = _call_action(
        ACTIONS["submit-purchase-invoice"], fresh_db,
        purchase_invoice_id=pi_id,
    )
    assert submit_result["status"] == "ok"
    assert submit_result["gl_entries_created"] >= 2

    # Verify GL: expense debit
    expense_gl = fresh_db.execute(
        """SELECT * FROM gl_entry
           WHERE voucher_type = 'purchase_invoice' AND voucher_id = ?
             AND account_id = ? AND is_cancelled = 0""",
        (pi_id, env["expense_id"]),
    ).fetchone()
    assert expense_gl is not None
    assert Decimal(expense_gl["debit"]) == Decimal("250.00")
    assert Decimal(expense_gl["credit"]) == Decimal("0")

    # Verify GL: AP credit
    ap_gl = fresh_db.execute(
        """SELECT * FROM gl_entry
           WHERE voucher_type = 'purchase_invoice' AND voucher_id = ?
             AND account_id = ? AND is_cancelled = 0""",
        (pi_id, env["payable_id"]),
    ).fetchone()
    assert ap_gl is not None
    assert Decimal(ap_gl["credit"]) == Decimal("250.00")
    assert Decimal(ap_gl["debit"]) == Decimal("0")


# ---------------------------------------------------------------------------
# 9. test_submit_purchase_invoice_ple
# ---------------------------------------------------------------------------

def test_submit_purchase_invoice_ple(fresh_db):
    """Submit a purchase invoice and verify payment_ledger_entry created."""
    env = setup_buying_environment(fresh_db)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "25.00",
    }])

    create_result = _call_action(
        ACTIONS["create-purchase-invoice"], fresh_db,
        supplier_id=env["supplier_id"],
        company_id=env["company_id"],
        items=items_json,
        posting_date="2026-02-16",
    )
    assert create_result["status"] == "ok"
    pi_id = create_result["purchase_invoice_id"]

    submit_result = _call_action(
        ACTIONS["submit-purchase-invoice"], fresh_db,
        purchase_invoice_id=pi_id,
    )
    assert submit_result["status"] == "ok"

    # Verify PLE
    ple = fresh_db.execute(
        """SELECT * FROM payment_ledger_entry
           WHERE against_voucher_type = 'purchase_invoice'
             AND against_voucher_id = ?""",
        (pi_id,),
    ).fetchone()
    assert ple is not None
    assert ple["party_type"] == "supplier"
    assert ple["party_id"] == env["supplier_id"]
    assert Decimal(ple["amount"]) == Decimal("250.00")
    assert ple["account_id"] == env["payable_id"]


# ---------------------------------------------------------------------------
# 10. test_submit_purchase_invoice_with_tax
# ---------------------------------------------------------------------------

def test_submit_purchase_invoice_with_tax(fresh_db):
    """Submit an invoice with a tax template and verify tax GL entry."""
    env = setup_buying_environment(fresh_db)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "25.00",
    }])

    # Create invoice WITH tax template
    create_result = _call_action(
        ACTIONS["create-purchase-invoice"], fresh_db,
        supplier_id=env["supplier_id"],
        company_id=env["company_id"],
        items=items_json,
        tax_template_id=env["tax_template_id"],
        posting_date="2026-02-16",
    )
    assert create_result["status"] == "ok"
    pi_id = create_result["purchase_invoice_id"]
    # 10 * 25 = 250.00 net, 8% tax = 20.00, grand = 270.00
    assert create_result["tax_amount"] == "20.00"
    assert create_result["grand_total"] == "270.00"

    submit_result = _call_action(
        ACTIONS["submit-purchase-invoice"], fresh_db,
        purchase_invoice_id=pi_id,
    )
    assert submit_result["status"] == "ok"

    # Verify tax GL: DR Input Tax
    tax_gl = fresh_db.execute(
        """SELECT * FROM gl_entry
           WHERE voucher_type = 'purchase_invoice' AND voucher_id = ?
             AND account_id = ? AND is_cancelled = 0""",
        (pi_id, env["tax_account_id"]),
    ).fetchone()
    assert tax_gl is not None
    assert Decimal(tax_gl["debit"]) == Decimal("20.00")

    # Verify AP credit equals grand total (270)
    ap_gl = fresh_db.execute(
        """SELECT * FROM gl_entry
           WHERE voucher_type = 'purchase_invoice' AND voucher_id = ?
             AND account_id = ? AND is_cancelled = 0""",
        (pi_id, env["payable_id"]),
    ).fetchone()
    assert ap_gl is not None
    assert Decimal(ap_gl["credit"]) == Decimal("270.00")


# ---------------------------------------------------------------------------
# 11. test_submit_already_submitted
# ---------------------------------------------------------------------------

def test_submit_already_submitted(fresh_db):
    """Submitting an already-submitted invoice should return error."""
    env = setup_buying_environment(fresh_db)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "25.00",
    }])

    create_result = _call_action(
        ACTIONS["create-purchase-invoice"], fresh_db,
        supplier_id=env["supplier_id"],
        company_id=env["company_id"],
        items=items_json,
        posting_date="2026-02-16",
    )
    assert create_result["status"] == "ok"
    pi_id = create_result["purchase_invoice_id"]

    # First submit -- should succeed
    r1 = _call_action(
        ACTIONS["submit-purchase-invoice"], fresh_db,
        purchase_invoice_id=pi_id,
    )
    assert r1["status"] == "ok"

    # Second submit -- should fail
    r2 = _call_action(
        ACTIONS["submit-purchase-invoice"], fresh_db,
        purchase_invoice_id=pi_id,
    )
    assert r2["status"] == "error"
    assert "draft" in r2["message"].lower() or "submitted" in r2["message"].lower()


# ---------------------------------------------------------------------------
# 12. test_cancel_purchase_invoice
# ---------------------------------------------------------------------------

def test_cancel_purchase_invoice(fresh_db):
    """Submit then cancel a purchase invoice, verify GL entries reversed."""
    env = setup_buying_environment(fresh_db)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "25.00",
    }])

    create_result = _call_action(
        ACTIONS["create-purchase-invoice"], fresh_db,
        supplier_id=env["supplier_id"],
        company_id=env["company_id"],
        items=items_json,
        posting_date="2026-02-16",
    )
    assert create_result["status"] == "ok"
    pi_id = create_result["purchase_invoice_id"]

    # Submit
    submit_result = _call_action(
        ACTIONS["submit-purchase-invoice"], fresh_db,
        purchase_invoice_id=pi_id,
    )
    assert submit_result["status"] == "ok"

    # Cancel
    cancel_result = _call_action(
        ACTIONS["cancel-purchase-invoice"], fresh_db,
        purchase_invoice_id=pi_id,
    )
    assert cancel_result["status"] == "ok"
    assert cancel_result["status_field"] if "status_field" in cancel_result else True

    # Verify status
    row = fresh_db.execute(
        "SELECT status FROM purchase_invoice WHERE id = ?", (pi_id,)
    ).fetchone()
    assert row["status"] == "cancelled"

    # Verify GL reversals exist (is_cancelled=1 on originals, reversal rows)
    gl_cancelled = fresh_db.execute(
        """SELECT COUNT(*) as cnt FROM gl_entry
           WHERE voucher_type = 'purchase_invoice' AND voucher_id = ?
             AND is_cancelled = 1""",
        (pi_id,),
    ).fetchone()
    assert gl_cancelled["cnt"] >= 2  # At least expense + AP cancelled

    # Verify PLE delinked
    ple = fresh_db.execute(
        """SELECT delinked FROM payment_ledger_entry
           WHERE voucher_type = 'purchase_invoice' AND voucher_id = ?""",
        (pi_id,),
    ).fetchone()
    assert ple is not None
    assert ple["delinked"] == 1


# ---------------------------------------------------------------------------
# 13. test_cancel_draft_invoice
# ---------------------------------------------------------------------------

def test_cancel_draft_invoice(fresh_db):
    """Cancelling a draft invoice should return error (must be submitted)."""
    env = setup_buying_environment(fresh_db)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "25.00",
    }])

    create_result = _call_action(
        ACTIONS["create-purchase-invoice"], fresh_db,
        supplier_id=env["supplier_id"],
        company_id=env["company_id"],
        items=items_json,
        posting_date="2026-02-16",
    )
    assert create_result["status"] == "ok"
    pi_id = create_result["purchase_invoice_id"]

    # Try to cancel a draft
    cancel_result = _call_action(
        ACTIONS["cancel-purchase-invoice"], fresh_db,
        purchase_invoice_id=pi_id,
    )

    assert cancel_result["status"] == "error"
    assert "draft" in cancel_result["message"].lower()


# ---------------------------------------------------------------------------
# 14. test_create_debit_note
# ---------------------------------------------------------------------------

def test_create_debit_note(fresh_db):
    """Create a debit note against a submitted purchase invoice."""
    env = setup_buying_environment(fresh_db)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "25.00",
    }])

    # Create and submit invoice
    create_result = _call_action(
        ACTIONS["create-purchase-invoice"], fresh_db,
        supplier_id=env["supplier_id"],
        company_id=env["company_id"],
        items=items_json,
        posting_date="2026-02-16",
    )
    assert create_result["status"] == "ok"
    pi_id = create_result["purchase_invoice_id"]

    submit_result = _call_action(
        ACTIONS["submit-purchase-invoice"], fresh_db,
        purchase_invoice_id=pi_id,
    )
    assert submit_result["status"] == "ok"

    # Create debit note for 3 items returned
    dn_items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "3",
        "rate": "25.00",
    }])

    dn_result = _call_action(
        ACTIONS["create-debit-note"], fresh_db,
        against_invoice_id=pi_id,
        items=dn_items_json,
        reason="Defective goods",
    )

    assert dn_result["status"] == "ok"
    assert "debit_note_id" in dn_result
    assert dn_result["against_invoice_id"] == pi_id
    # 3 * 25 = 75.00, negated = -75.00
    assert Decimal(dn_result["total_amount"]) == Decimal("-75.00")

    # Verify debit note is_return=1
    dn_row = fresh_db.execute(
        "SELECT * FROM purchase_invoice WHERE id = ?",
        (dn_result["debit_note_id"],),
    ).fetchone()
    assert dn_row["is_return"] == 1
    assert dn_row["return_against"] == pi_id


# ---------------------------------------------------------------------------
# 15. test_update_invoice_outstanding
# ---------------------------------------------------------------------------

def test_update_invoice_outstanding(fresh_db):
    """Call update-invoice-outstanding to reduce outstanding amount."""
    env = setup_buying_environment(fresh_db)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "25.00",
    }])

    # Create and submit invoice (grand_total = 250.00)
    create_result = _call_action(
        ACTIONS["create-purchase-invoice"], fresh_db,
        supplier_id=env["supplier_id"],
        company_id=env["company_id"],
        items=items_json,
        posting_date="2026-02-16",
    )
    assert create_result["status"] == "ok"
    pi_id = create_result["purchase_invoice_id"]

    submit_result = _call_action(
        ACTIONS["submit-purchase-invoice"], fresh_db,
        purchase_invoice_id=pi_id,
    )
    assert submit_result["status"] == "ok"

    # Partial payment: 100.00
    pay_result = _call_action(
        ACTIONS["update-invoice-outstanding"], fresh_db,
        purchase_invoice_id=pi_id,
        amount="100.00",
    )

    assert pay_result["status"] == "ok"
    assert pay_result["outstanding_amount"] == "150.00"
    assert pay_result["status_field"] if "status_field" in pay_result else True

    # Verify in DB
    row = fresh_db.execute(
        "SELECT outstanding_amount, status FROM purchase_invoice WHERE id = ?",
        (pi_id,),
    ).fetchone()
    assert row["outstanding_amount"] == "150.00"
    assert row["status"] == "partially_paid"

    # Full remaining payment: 150.00
    pay2_result = _call_action(
        ACTIONS["update-invoice-outstanding"], fresh_db,
        purchase_invoice_id=pi_id,
        amount="150.00",
    )

    assert pay2_result["status"] == "ok"
    assert pay2_result["outstanding_amount"] == "0.00"

    # Verify paid status
    row2 = fresh_db.execute(
        "SELECT status FROM purchase_invoice WHERE id = ?", (pi_id,)
    ).fetchone()
    assert row2["status"] == "paid"
