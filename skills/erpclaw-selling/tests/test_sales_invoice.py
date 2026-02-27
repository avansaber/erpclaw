"""Tests for sales invoice actions: create, update, get, list, submit, cancel,
credit note, and update-invoice-outstanding.

15 tests covering the full sales invoice lifecycle including GL entries,
payment ledger entries, tax handling, credit notes, and outstanding updates.
"""
import json
from decimal import Decimal

from helpers import (
    _call_action,
    setup_selling_environment,
    create_test_customer,
    create_test_item,
    create_test_warehouse,
    seed_stock_for_item,
)
from db_query import ACTIONS


# ---------------------------------------------------------------------------
# 1. test_create_sales_invoice
# ---------------------------------------------------------------------------

def test_create_sales_invoice(fresh_db):
    """Create a standalone SINV with items, assert ok + sales_invoice_id."""
    env = setup_selling_environment(fresh_db)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "25.00",
        "warehouse_id": env["warehouse_id"],
    }])

    result = _call_action(
        ACTIONS["create-sales-invoice"], fresh_db,
        customer_id=env["customer_id"],
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=items_json,
    )

    assert result["status"] == "ok"
    assert "sales_invoice_id" in result
    assert result["total_amount"] == "250.00"
    assert result["grand_total"] == "250.00"

    # Verify persistence in the database
    row = fresh_db.execute(
        "SELECT * FROM sales_invoice WHERE id = ?",
        (result["sales_invoice_id"],),
    ).fetchone()
    assert row is not None
    assert row["status"] == "draft"
    assert row["customer_id"] == env["customer_id"]
    assert row["company_id"] == env["company_id"]


# ---------------------------------------------------------------------------
# 2. test_create_sales_invoice_from_so
# ---------------------------------------------------------------------------

def test_create_sales_invoice_from_so(fresh_db):
    """Create a sales invoice from a submitted sales order."""
    env = setup_selling_environment(fresh_db)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "5",
        "rate": "25.00",
        "warehouse_id": env["warehouse_id"],
    }])

    # Create and submit SO
    so_result = _call_action(
        ACTIONS["add-sales-order"], fresh_db,
        customer_id=env["customer_id"],
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=items_json,
    )
    assert so_result["status"] == "ok"
    so_id = so_result["sales_order_id"]

    submit_result = _call_action(
        ACTIONS["submit-sales-order"], fresh_db,
        sales_order_id=so_id,
    )
    assert submit_result["status"] == "ok"
    assert submit_result["status_field"] if "status_field" in submit_result else True

    # Create invoice from SO
    si_result = _call_action(
        ACTIONS["create-sales-invoice"], fresh_db,
        sales_order_id=so_id,
        posting_date="2026-02-16",
    )

    assert si_result["status"] == "ok"
    assert "sales_invoice_id" in si_result
    assert si_result["total_amount"] == "125.00"
    assert si_result["grand_total"] == "125.00"

    # Verify the invoice links back to the SO
    row = fresh_db.execute(
        "SELECT sales_order_id FROM sales_invoice WHERE id = ?",
        (si_result["sales_invoice_id"],),
    ).fetchone()
    assert row["sales_order_id"] == so_id


# ---------------------------------------------------------------------------
# 3. test_create_sales_invoice_missing_customer
# ---------------------------------------------------------------------------

def test_create_sales_invoice_missing_customer(fresh_db):
    """Creating a standalone invoice without --customer-id should error."""
    env = setup_selling_environment(fresh_db)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "25.00",
        "warehouse_id": env["warehouse_id"],
    }])

    result = _call_action(
        ACTIONS["create-sales-invoice"], fresh_db,
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=items_json,
    )

    assert result["status"] == "error"
    assert "customer" in result["message"].lower()


# ---------------------------------------------------------------------------
# 4. test_get_sales_invoice
# ---------------------------------------------------------------------------

def test_get_sales_invoice(fresh_db):
    """Create a sales invoice, then get it and verify items + totals."""
    env = setup_selling_environment(fresh_db)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "8",
        "rate": "25.00",
        "warehouse_id": env["warehouse_id"],
    }])

    create_result = _call_action(
        ACTIONS["create-sales-invoice"], fresh_db,
        customer_id=env["customer_id"],
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=items_json,
    )
    assert create_result["status"] == "ok"
    si_id = create_result["sales_invoice_id"]

    get_result = _call_action(
        ACTIONS["get-sales-invoice"], fresh_db,
        sales_invoice_id=si_id,
    )

    assert get_result["status"] == "ok"
    assert get_result["id"] == si_id
    assert get_result["total_amount"] == "200.00"
    assert get_result["grand_total"] == "200.00"
    assert get_result["outstanding_amount"] == "200.00"
    assert "items" in get_result
    assert len(get_result["items"]) == 1
    assert get_result["items"][0]["item_id"] == env["item_id"]
    assert get_result["items"][0]["quantity"] == "8.00"
    assert get_result["items"][0]["rate"] == "25.00"


# ---------------------------------------------------------------------------
# 5. test_list_sales_invoices
# ---------------------------------------------------------------------------

def test_list_sales_invoices(fresh_db):
    """Create 2 invoices, list them, verify both appear."""
    env = setup_selling_environment(fresh_db)

    for qty in ["5", "10"]:
        items_json = json.dumps([{
            "item_id": env["item_id"],
            "qty": qty,
            "rate": "25.00",
            "warehouse_id": env["warehouse_id"],
        }])
        r = _call_action(
            ACTIONS["create-sales-invoice"], fresh_db,
            customer_id=env["customer_id"],
            company_id=env["company_id"],
            posting_date="2026-02-16",
            items=items_json,
        )
        assert r["status"] == "ok"

    list_result = _call_action(
        ACTIONS["list-sales-invoices"], fresh_db,
        company_id=env["company_id"],
    )

    assert list_result["status"] == "ok"
    assert list_result["total_count"] == 2
    assert len(list_result["sales_invoices"]) == 2


# ---------------------------------------------------------------------------
# 6. test_update_sales_invoice
# ---------------------------------------------------------------------------

def test_update_sales_invoice(fresh_db):
    """Update a draft invoice's due_date and verify the update."""
    env = setup_selling_environment(fresh_db)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "25.00",
        "warehouse_id": env["warehouse_id"],
    }])

    create_result = _call_action(
        ACTIONS["create-sales-invoice"], fresh_db,
        customer_id=env["customer_id"],
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=items_json,
    )
    assert create_result["status"] == "ok"
    si_id = create_result["sales_invoice_id"]

    update_result = _call_action(
        ACTIONS["update-sales-invoice"], fresh_db,
        sales_invoice_id=si_id,
        due_date="2026-04-30",
    )

    assert update_result["status"] == "ok"
    assert update_result["sales_invoice_id"] == si_id
    assert "due_date" in update_result["updated_fields"]

    # Verify in database
    row = fresh_db.execute(
        "SELECT due_date FROM sales_invoice WHERE id = ?", (si_id,)
    ).fetchone()
    assert row["due_date"] == "2026-04-30"


# ---------------------------------------------------------------------------
# 7. test_submit_sales_invoice
# ---------------------------------------------------------------------------

def test_submit_sales_invoice(fresh_db):
    """Submit a draft invoice and verify status becomes 'submitted'."""
    env = setup_selling_environment(fresh_db)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "25.00",
        "warehouse_id": env["warehouse_id"],
    }])

    create_result = _call_action(
        ACTIONS["create-sales-invoice"], fresh_db,
        customer_id=env["customer_id"],
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=items_json,
    )
    assert create_result["status"] == "ok"
    si_id = create_result["sales_invoice_id"]

    submit_result = _call_action(
        ACTIONS["submit-sales-invoice"], fresh_db,
        sales_invoice_id=si_id,
    )

    assert submit_result["status"] == "ok"
    assert submit_result["sales_invoice_id"] == si_id
    assert submit_result["status_field"] if "status_field" in submit_result else True
    assert "naming_series" in submit_result
    assert submit_result["naming_series"].startswith("INV-")

    # Verify status in database
    row = fresh_db.execute(
        "SELECT status FROM sales_invoice WHERE id = ?", (si_id,)
    ).fetchone()
    assert row["status"] == "submitted"


# ---------------------------------------------------------------------------
# 8. test_submit_sales_invoice_gl
# ---------------------------------------------------------------------------

def test_submit_sales_invoice_gl(fresh_db):
    """Submit an invoice and verify GL entries: AR debit, Revenue credit."""
    env = setup_selling_environment(fresh_db)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "25.00",
        "warehouse_id": env["warehouse_id"],
    }])

    create_result = _call_action(
        ACTIONS["create-sales-invoice"], fresh_db,
        customer_id=env["customer_id"],
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=items_json,
    )
    assert create_result["status"] == "ok"
    si_id = create_result["sales_invoice_id"]

    submit_result = _call_action(
        ACTIONS["submit-sales-invoice"], fresh_db,
        sales_invoice_id=si_id,
    )
    assert submit_result["status"] == "ok"
    assert submit_result["gl_entries_created"] >= 2

    # Check GL entries for this voucher
    gl_rows = fresh_db.execute(
        """SELECT account_id, debit, credit, party_type, party_id
           FROM gl_entry
           WHERE voucher_type = 'sales_invoice' AND voucher_id = ?
             AND is_cancelled = 0""",
        (si_id,),
    ).fetchall()
    gl_list = [dict(r) for r in gl_rows]

    # Find AR (debit) entry
    ar_entries = [g for g in gl_list if g["account_id"] == env["receivable_id"]]
    assert len(ar_entries) >= 1
    ar_entry = ar_entries[0]
    assert Decimal(ar_entry["debit"]) == Decimal("250.00")
    assert Decimal(ar_entry["credit"]) == Decimal("0")
    assert ar_entry["party_type"] == "customer"
    assert ar_entry["party_id"] == env["customer_id"]

    # Find Revenue (credit) entry
    income_entries = [g for g in gl_list if g["account_id"] == env["income_id"]]
    assert len(income_entries) >= 1
    income_entry = income_entries[0]
    assert Decimal(income_entry["debit"]) == Decimal("0")
    assert Decimal(income_entry["credit"]) == Decimal("250.00")


# ---------------------------------------------------------------------------
# 9. test_submit_sales_invoice_ple
# ---------------------------------------------------------------------------

def test_submit_sales_invoice_ple(fresh_db):
    """Submit an invoice and verify payment_ledger_entry is created for AR."""
    env = setup_selling_environment(fresh_db)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "4",
        "rate": "25.00",
        "warehouse_id": env["warehouse_id"],
    }])

    create_result = _call_action(
        ACTIONS["create-sales-invoice"], fresh_db,
        customer_id=env["customer_id"],
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=items_json,
    )
    assert create_result["status"] == "ok"
    si_id = create_result["sales_invoice_id"]

    submit_result = _call_action(
        ACTIONS["submit-sales-invoice"], fresh_db,
        sales_invoice_id=si_id,
    )
    assert submit_result["status"] == "ok"

    # Check PLE
    ple_rows = fresh_db.execute(
        """SELECT * FROM payment_ledger_entry
           WHERE voucher_type = 'sales_invoice' AND voucher_id = ?""",
        (si_id,),
    ).fetchall()
    assert len(ple_rows) >= 1

    ple = dict(ple_rows[0])
    assert ple["party_type"] == "customer"
    assert ple["party_id"] == env["customer_id"]
    assert ple["against_voucher_type"] == "sales_invoice"
    assert ple["against_voucher_id"] == si_id
    assert Decimal(ple["amount"]) == Decimal("100.00")
    assert ple["account_id"] == env["receivable_id"]


# ---------------------------------------------------------------------------
# 10. test_submit_sales_invoice_with_tax
# ---------------------------------------------------------------------------

def test_submit_sales_invoice_with_tax(fresh_db):
    """Submit an invoice with a tax template and verify tax GL entries."""
    env = setup_selling_environment(fresh_db)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "25.00",
        "warehouse_id": env["warehouse_id"],
    }])

    # Create invoice WITH tax template
    create_result = _call_action(
        ACTIONS["create-sales-invoice"], fresh_db,
        customer_id=env["customer_id"],
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=items_json,
        tax_template_id=env["tax_template_id"],
    )
    assert create_result["status"] == "ok"
    si_id = create_result["sales_invoice_id"]
    # Net = 250.00, Tax 8% = 20.00, Grand = 270.00
    assert create_result["total_amount"] == "250.00"
    assert create_result["tax_amount"] == "20.00"
    assert create_result["grand_total"] == "270.00"

    submit_result = _call_action(
        ACTIONS["submit-sales-invoice"], fresh_db,
        sales_invoice_id=si_id,
    )
    assert submit_result["status"] == "ok"

    # Check GL entries
    gl_rows = fresh_db.execute(
        """SELECT account_id, debit, credit
           FROM gl_entry
           WHERE voucher_type = 'sales_invoice' AND voucher_id = ?
             AND is_cancelled = 0""",
        (si_id,),
    ).fetchall()
    gl_list = [dict(r) for r in gl_rows]

    # AR should be debited for grand total (270.00)
    ar_entries = [g for g in gl_list if g["account_id"] == env["receivable_id"]]
    assert len(ar_entries) >= 1
    assert Decimal(ar_entries[0]["debit"]) == Decimal("270.00")

    # Revenue should be credited for net amount (250.00)
    income_entries = [g for g in gl_list if g["account_id"] == env["income_id"]]
    assert len(income_entries) >= 1
    assert Decimal(income_entries[0]["credit"]) == Decimal("250.00")

    # Tax account should be credited for tax amount (20.00)
    tax_entries = [g for g in gl_list if g["account_id"] == env["tax_account_id"]]
    assert len(tax_entries) >= 1
    assert Decimal(tax_entries[0]["credit"]) == Decimal("20.00")


# ---------------------------------------------------------------------------
# 11. test_submit_already_submitted
# ---------------------------------------------------------------------------

def test_submit_already_submitted(fresh_db):
    """Submitting an already-submitted invoice should return an error."""
    env = setup_selling_environment(fresh_db)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "25.00",
        "warehouse_id": env["warehouse_id"],
    }])

    create_result = _call_action(
        ACTIONS["create-sales-invoice"], fresh_db,
        customer_id=env["customer_id"],
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=items_json,
    )
    assert create_result["status"] == "ok"
    si_id = create_result["sales_invoice_id"]

    # First submit succeeds
    submit1 = _call_action(
        ACTIONS["submit-sales-invoice"], fresh_db,
        sales_invoice_id=si_id,
    )
    assert submit1["status"] == "ok"

    # Second submit should fail
    submit2 = _call_action(
        ACTIONS["submit-sales-invoice"], fresh_db,
        sales_invoice_id=si_id,
    )
    assert submit2["status"] == "error"
    assert "draft" in submit2["message"].lower() or "submitted" in submit2["message"].lower()


# ---------------------------------------------------------------------------
# 12. test_cancel_sales_invoice
# ---------------------------------------------------------------------------

def test_cancel_sales_invoice(fresh_db):
    """Submit then cancel an invoice, verify GL entries are reversed."""
    env = setup_selling_environment(fresh_db)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "25.00",
        "warehouse_id": env["warehouse_id"],
    }])

    create_result = _call_action(
        ACTIONS["create-sales-invoice"], fresh_db,
        customer_id=env["customer_id"],
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=items_json,
    )
    assert create_result["status"] == "ok"
    si_id = create_result["sales_invoice_id"]

    submit_result = _call_action(
        ACTIONS["submit-sales-invoice"], fresh_db,
        sales_invoice_id=si_id,
    )
    assert submit_result["status"] == "ok"
    gl_created = submit_result["gl_entries_created"]

    cancel_result = _call_action(
        ACTIONS["cancel-sales-invoice"], fresh_db,
        sales_invoice_id=si_id,
    )
    assert cancel_result["status"] == "ok"
    assert cancel_result["sales_invoice_id"] == si_id
    assert cancel_result["status_field"] if "status_field" in cancel_result else True
    assert cancel_result["gl_reversals"] >= gl_created

    # Verify status in database
    row = fresh_db.execute(
        "SELECT status, outstanding_amount FROM sales_invoice WHERE id = ?",
        (si_id,),
    ).fetchone()
    assert row["status"] == "cancelled"
    assert row["outstanding_amount"] == "0"

    # Verify GL reversals exist (is_cancelled = 1 on originals)
    cancelled_gl = fresh_db.execute(
        """SELECT COUNT(*) as cnt FROM gl_entry
           WHERE voucher_type = 'sales_invoice' AND voucher_id = ?
             AND is_cancelled = 1""",
        (si_id,),
    ).fetchone()["cnt"]
    assert cancelled_gl >= 2  # At least AR + Revenue reversed

    # Verify PLE is delinked
    ple = fresh_db.execute(
        """SELECT delinked FROM payment_ledger_entry
           WHERE voucher_type = 'sales_invoice' AND voucher_id = ?""",
        (si_id,),
    ).fetchone()
    assert ple["delinked"] == 1


# ---------------------------------------------------------------------------
# 13. test_cancel_draft_invoice
# ---------------------------------------------------------------------------

def test_cancel_draft_invoice(fresh_db):
    """Trying to cancel a draft invoice should return an error."""
    env = setup_selling_environment(fresh_db)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "25.00",
        "warehouse_id": env["warehouse_id"],
    }])

    create_result = _call_action(
        ACTIONS["create-sales-invoice"], fresh_db,
        customer_id=env["customer_id"],
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=items_json,
    )
    assert create_result["status"] == "ok"
    si_id = create_result["sales_invoice_id"]

    # Try to cancel without submitting first
    cancel_result = _call_action(
        ACTIONS["cancel-sales-invoice"], fresh_db,
        sales_invoice_id=si_id,
    )
    assert cancel_result["status"] == "error"
    assert "draft" in cancel_result["message"].lower() or "submitted" in cancel_result["message"].lower()


# ---------------------------------------------------------------------------
# 14. test_create_credit_note
# ---------------------------------------------------------------------------

def test_create_credit_note(fresh_db):
    """Create a credit note against a submitted invoice."""
    env = setup_selling_environment(fresh_db)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "25.00",
        "warehouse_id": env["warehouse_id"],
    }])

    # Create and submit the original invoice
    create_result = _call_action(
        ACTIONS["create-sales-invoice"], fresh_db,
        customer_id=env["customer_id"],
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=items_json,
    )
    assert create_result["status"] == "ok"
    si_id = create_result["sales_invoice_id"]

    submit_result = _call_action(
        ACTIONS["submit-sales-invoice"], fresh_db,
        sales_invoice_id=si_id,
    )
    assert submit_result["status"] == "ok"

    # Create credit note for partial return (5 items)
    cn_items = json.dumps([{
        "item_id": env["item_id"],
        "qty": "5",
        "rate": "25.00",
    }])

    cn_result = _call_action(
        ACTIONS["create-credit-note"], fresh_db,
        against_invoice_id=si_id,
        items=cn_items,
        reason="Customer return",
    )

    assert cn_result["status"] == "ok"
    assert "credit_note_id" in cn_result
    assert cn_result["against_invoice_id"] == si_id
    assert cn_result["is_return"] is True
    # Grand total should be negative: -(5 * 25) = -125.00
    assert Decimal(cn_result["grand_total"]) == Decimal("-125.00")

    # Verify the credit note record in database
    cn_row = fresh_db.execute(
        "SELECT is_return, return_against, status FROM sales_invoice WHERE id = ?",
        (cn_result["credit_note_id"],),
    ).fetchone()
    assert cn_row["is_return"] == 1
    assert cn_row["return_against"] == si_id
    assert cn_row["status"] == "draft"


# ---------------------------------------------------------------------------
# 15. test_update_invoice_outstanding
# ---------------------------------------------------------------------------

def test_update_invoice_outstanding(fresh_db):
    """Call update-invoice-outstanding to reduce outstanding amount."""
    env = setup_selling_environment(fresh_db)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "25.00",
        "warehouse_id": env["warehouse_id"],
    }])

    # Create and submit invoice (grand_total = 250.00)
    create_result = _call_action(
        ACTIONS["create-sales-invoice"], fresh_db,
        customer_id=env["customer_id"],
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=items_json,
    )
    assert create_result["status"] == "ok"
    si_id = create_result["sales_invoice_id"]

    submit_result = _call_action(
        ACTIONS["submit-sales-invoice"], fresh_db,
        sales_invoice_id=si_id,
    )
    assert submit_result["status"] == "ok"

    # Partial payment of 100.00
    pay_result = _call_action(
        ACTIONS["update-invoice-outstanding"], fresh_db,
        sales_invoice_id=si_id,
        amount="100.00",
    )

    assert pay_result["status"] == "ok"
    assert pay_result["outstanding_amount"] == "150.00"
    assert pay_result["status_field"] if "status_field" in pay_result else True

    # Verify in database
    row = fresh_db.execute(
        "SELECT outstanding_amount, status FROM sales_invoice WHERE id = ?",
        (si_id,),
    ).fetchone()
    assert row["outstanding_amount"] == "150.00"
    assert row["status"] == "partially_paid"

    # Full remaining payment of 150.00
    pay_result2 = _call_action(
        ACTIONS["update-invoice-outstanding"], fresh_db,
        sales_invoice_id=si_id,
        amount="150.00",
    )

    assert pay_result2["status"] == "ok"
    assert pay_result2["outstanding_amount"] == "0"

    row2 = fresh_db.execute(
        "SELECT outstanding_amount, status FROM sales_invoice WHERE id = ?",
        (si_id,),
    ).fetchone()
    assert row2["outstanding_amount"] == "0"
    assert row2["status"] == "paid"
