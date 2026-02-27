"""Tests for purchase order lifecycle: add, update, get, list, submit, cancel.

10 tests covering PO creation with items, validation errors, retrieval,
listing, update of items, submit/confirm flow, double-submit error,
cancel flow, cancel of draft PO, and tax calculation.
"""
import json
import uuid

from db_query import ACTIONS
from helpers import (
    _call_action,
    setup_buying_environment,
    create_test_supplier,
    create_test_item,
    create_test_tax_template,
)


# ---------------------------------------------------------------------------
# Helper: create a draft PO and return the purchase_order_id
# ---------------------------------------------------------------------------

def _make_po(conn, env, qty="10", rate="25.00", tax_template_id=None):
    """Create a draft purchase order and return the purchase_order_id."""
    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": qty,
        "rate": rate,
        "warehouse_id": env["warehouse_id"],
    }])

    kwargs = {
        "supplier_id": env["supplier_id"],
        "company_id": env["company_id"],
        "posting_date": "2026-02-16",
        "items": items_json,
    }
    if tax_template_id:
        kwargs["tax_template_id"] = tax_template_id

    result = _call_action(ACTIONS["add-purchase-order"], conn, **kwargs)
    assert result["status"] == "ok", f"_make_po failed: {result}"
    return result["purchase_order_id"]


# ---------------------------------------------------------------------------
# 1. test_add_purchase_order
# ---------------------------------------------------------------------------

def test_add_purchase_order(fresh_db):
    """Create a PO with items, assert ok and purchase_order_id returned."""
    env = setup_buying_environment(fresh_db)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "25.00",
        "warehouse_id": env["warehouse_id"],
    }])

    result = _call_action(
        ACTIONS["add-purchase-order"], fresh_db,
        supplier_id=env["supplier_id"],
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=items_json,
    )

    assert result["status"] == "ok"
    assert "purchase_order_id" in result
    assert result["total_amount"] == "250.00"
    assert result["grand_total"] == "250.00"

    # Verify PO was actually written to the database
    po_row = fresh_db.execute(
        "SELECT * FROM purchase_order WHERE id = ?",
        (result["purchase_order_id"],),
    ).fetchone()
    assert po_row is not None
    assert po_row["status"] == "draft"
    assert po_row["supplier_id"] == env["supplier_id"]


# ---------------------------------------------------------------------------
# 2. test_add_purchase_order_missing_supplier
# ---------------------------------------------------------------------------

def test_add_purchase_order_missing_supplier(fresh_db):
    """Creating a PO without supplier_id should return error."""
    env = setup_buying_environment(fresh_db)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "25.00",
        "warehouse_id": env["warehouse_id"],
    }])

    result = _call_action(
        ACTIONS["add-purchase-order"], fresh_db,
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=items_json,
    )

    assert result["status"] == "error"
    assert "supplier" in result["message"].lower()


# ---------------------------------------------------------------------------
# 3. test_get_purchase_order
# ---------------------------------------------------------------------------

def test_get_purchase_order(fresh_db):
    """Create a PO then get it; verify items and totals."""
    env = setup_buying_environment(fresh_db)
    po_id = _make_po(fresh_db, env, qty="10", rate="25.00")

    result = _call_action(
        ACTIONS["get-purchase-order"], fresh_db,
        purchase_order_id=po_id,
    )

    assert result["status"] == "ok"
    assert result["id"] == po_id
    assert result["supplier_id"] == env["supplier_id"]
    assert result["total_amount"] == "250.00"
    assert result["grand_total"] == "250.00"

    # Items should be returned
    assert "items" in result
    assert len(result["items"]) == 1
    assert result["items"][0]["item_id"] == env["item_id"]
    assert result["items"][0]["quantity"] == "10.00"
    assert result["items"][0]["rate"] == "25.00"


# ---------------------------------------------------------------------------
# 4. test_list_purchase_orders
# ---------------------------------------------------------------------------

def test_list_purchase_orders(fresh_db):
    """Create 2 POs, list them, verify count."""
    env = setup_buying_environment(fresh_db)

    po_id_1 = _make_po(fresh_db, env, qty="5", rate="10.00")
    po_id_2 = _make_po(fresh_db, env, qty="20", rate="30.00")

    result = _call_action(
        ACTIONS["list-purchase-orders"], fresh_db,
        company_id=env["company_id"],
    )

    assert result["status"] == "ok"
    assert result["total_count"] == 2
    assert len(result["purchase_orders"]) == 2

    returned_ids = {po["id"] for po in result["purchase_orders"]}
    assert po_id_1 in returned_ids
    assert po_id_2 in returned_ids


# ---------------------------------------------------------------------------
# 5. test_update_purchase_order
# ---------------------------------------------------------------------------

def test_update_purchase_order(fresh_db):
    """Update a draft PO's items (new qty/rate) and verify totals change."""
    env = setup_buying_environment(fresh_db)
    po_id = _make_po(fresh_db, env, qty="10", rate="25.00")

    # Update: change qty to 20 and rate to 30.00
    new_items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "20",
        "rate": "30.00",
        "warehouse_id": env["warehouse_id"],
    }])

    result = _call_action(
        ACTIONS["update-purchase-order"], fresh_db,
        purchase_order_id=po_id,
        items=new_items_json,
    )

    assert result["status"] == "ok"
    assert result["purchase_order_id"] == po_id
    # 20 * 30.00 = 600.00
    assert result["total_amount"] == "600.00"
    assert result["grand_total"] == "600.00"

    # Verify via get
    get_result = _call_action(
        ACTIONS["get-purchase-order"], fresh_db,
        purchase_order_id=po_id,
    )
    assert get_result["status"] == "ok"
    assert get_result["total_amount"] == "600.00"
    assert len(get_result["items"]) == 1
    assert get_result["items"][0]["quantity"] == "20.00"
    assert get_result["items"][0]["rate"] == "30.00"


# ---------------------------------------------------------------------------
# 6. test_submit_purchase_order
# ---------------------------------------------------------------------------

def test_submit_purchase_order(fresh_db):
    """Submit a PO and verify status changes to confirmed with naming_series."""
    env = setup_buying_environment(fresh_db)
    po_id = _make_po(fresh_db, env)

    result = _call_action(
        ACTIONS["submit-purchase-order"], fresh_db,
        purchase_order_id=po_id,
    )

    assert result["status"] == "ok"
    assert result["purchase_order_id"] == po_id
    assert result["status"] == "ok"
    # submit-purchase-order returns status="confirmed" in the data
    assert "naming_series" in result
    assert result["naming_series"].startswith("PO-")

    # Verify DB status
    po_row = fresh_db.execute(
        "SELECT status, naming_series FROM purchase_order WHERE id = ?",
        (po_id,),
    ).fetchone()
    assert po_row["status"] == "confirmed"
    assert po_row["naming_series"] is not None


# ---------------------------------------------------------------------------
# 7. test_submit_already_submitted
# ---------------------------------------------------------------------------

def test_submit_already_submitted(fresh_db):
    """Submitting an already-confirmed PO should return error."""
    env = setup_buying_environment(fresh_db)
    po_id = _make_po(fresh_db, env)

    # First submit
    result1 = _call_action(
        ACTIONS["submit-purchase-order"], fresh_db,
        purchase_order_id=po_id,
    )
    assert result1["status"] == "ok"

    # Second submit should error
    result2 = _call_action(
        ACTIONS["submit-purchase-order"], fresh_db,
        purchase_order_id=po_id,
    )

    assert result2["status"] == "error"
    assert "draft" in result2["message"].lower() or "confirmed" in result2["message"].lower()


# ---------------------------------------------------------------------------
# 8. test_cancel_purchase_order
# ---------------------------------------------------------------------------

def test_cancel_purchase_order(fresh_db):
    """Submit then cancel a PO; verify status=cancelled."""
    env = setup_buying_environment(fresh_db)
    po_id = _make_po(fresh_db, env)

    # Submit first
    submit_result = _call_action(
        ACTIONS["submit-purchase-order"], fresh_db,
        purchase_order_id=po_id,
    )
    assert submit_result["status"] == "ok"

    # Cancel
    cancel_result = _call_action(
        ACTIONS["cancel-purchase-order"], fresh_db,
        purchase_order_id=po_id,
    )

    assert cancel_result["status"] == "ok"
    assert cancel_result["purchase_order_id"] == po_id
    assert cancel_result["status"] == "ok"

    # Verify DB status
    po_row = fresh_db.execute(
        "SELECT status FROM purchase_order WHERE id = ?", (po_id,),
    ).fetchone()
    assert po_row["status"] == "cancelled"


# ---------------------------------------------------------------------------
# 9. test_cancel_draft_purchase_order
# ---------------------------------------------------------------------------

def test_cancel_draft_purchase_order(fresh_db):
    """Cancelling a draft PO (not yet submitted) should succeed.

    The cancel-purchase-order action only blocks if status is 'cancelled'.
    A draft PO can be cancelled directly (no receipts/invoices linked).
    """
    env = setup_buying_environment(fresh_db)
    po_id = _make_po(fresh_db, env)

    # Verify it's currently draft
    po_row = fresh_db.execute(
        "SELECT status FROM purchase_order WHERE id = ?", (po_id,),
    ).fetchone()
    assert po_row["status"] == "draft"

    # Cancel the draft PO
    result = _call_action(
        ACTIONS["cancel-purchase-order"], fresh_db,
        purchase_order_id=po_id,
    )

    assert result["status"] == "ok"
    assert result["purchase_order_id"] == po_id

    # Verify DB status is now cancelled
    po_row = fresh_db.execute(
        "SELECT status FROM purchase_order WHERE id = ?", (po_id,),
    ).fetchone()
    assert po_row["status"] == "cancelled"

    # Cancelling again should error
    result2 = _call_action(
        ACTIONS["cancel-purchase-order"], fresh_db,
        purchase_order_id=po_id,
    )
    assert result2["status"] == "error"
    assert "already cancelled" in result2["message"].lower()


# ---------------------------------------------------------------------------
# 10. test_purchase_order_with_tax
# ---------------------------------------------------------------------------

def test_purchase_order_with_tax(fresh_db):
    """Create a PO with tax_template_id and verify tax is calculated."""
    env = setup_buying_environment(fresh_db)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": "10",
        "rate": "100.00",
        "warehouse_id": env["warehouse_id"],
    }])

    result = _call_action(
        ACTIONS["add-purchase-order"], fresh_db,
        supplier_id=env["supplier_id"],
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=items_json,
        tax_template_id=env["tax_template_id"],
    )

    assert result["status"] == "ok"
    assert "purchase_order_id" in result
    # 10 * 100.00 = 1000.00 subtotal, 8% tax = 80.00
    assert result["total_amount"] == "1000.00"
    assert result["tax_amount"] == "80.00"
    assert result["grand_total"] == "1080.00"

    # Verify via get
    get_result = _call_action(
        ACTIONS["get-purchase-order"], fresh_db,
        purchase_order_id=result["purchase_order_id"],
    )
    assert get_result["status"] == "ok"
    assert get_result["tax_amount"] == "80.00"
    assert get_result["grand_total"] == "1080.00"
