"""Tests for purchase receipt lifecycle: create, get, list, submit, cancel.

10 tests covering GRN creation from PO, direct creation (error without PO),
retrieval, listing, submit with SLE/GL verification, double-submit error,
cancel with SLE reversal, cancel draft error, and PO status update on receipt.
"""
import json
import uuid

from db_query import ACTIONS
from helpers import (
    _call_action,
    setup_buying_environment,
    create_test_item,
    create_test_supplier,
)


# ---------------------------------------------------------------------------
# Helper: create a confirmed PO and return (po_id, env)
# ---------------------------------------------------------------------------

def _make_confirmed_po(conn, env, qty="10", rate="25.00"):
    """Create and submit a PO so receipts can be created against it."""
    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": qty,
        "rate": rate,
        "warehouse_id": env["warehouse_id"],
    }])

    add_result = _call_action(
        ACTIONS["add-purchase-order"], conn,
        supplier_id=env["supplier_id"],
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=items_json,
    )
    assert add_result["status"] == "ok", f"PO creation failed: {add_result}"
    po_id = add_result["purchase_order_id"]

    submit_result = _call_action(
        ACTIONS["submit-purchase-order"], conn,
        purchase_order_id=po_id,
    )
    assert submit_result["status"] == "ok", f"PO submit failed: {submit_result}"

    return po_id


def _make_receipt(conn, po_id):
    """Create a draft purchase receipt from a confirmed PO."""
    result = _call_action(
        ACTIONS["create-purchase-receipt"], conn,
        purchase_order_id=po_id,
        posting_date="2026-02-16",
    )
    assert result["status"] == "ok", f"Receipt creation failed: {result}"
    return result["purchase_receipt_id"]


# ---------------------------------------------------------------------------
# 1. test_create_purchase_receipt
# ---------------------------------------------------------------------------

def test_create_purchase_receipt(fresh_db):
    """Create a GRN from a submitted PO, assert ok and receipt_id returned."""
    env = setup_buying_environment(fresh_db)
    po_id = _make_confirmed_po(fresh_db, env, qty="10", rate="25.00")

    result = _call_action(
        ACTIONS["create-purchase-receipt"], fresh_db,
        purchase_order_id=po_id,
        posting_date="2026-02-16",
    )

    assert result["status"] == "ok"
    assert "purchase_receipt_id" in result
    assert result["total_qty"] == "10.00"
    assert result["item_count"] == 1

    # Verify receipt in DB
    pr_row = fresh_db.execute(
        "SELECT * FROM purchase_receipt WHERE id = ?",
        (result["purchase_receipt_id"],),
    ).fetchone()
    assert pr_row is not None
    assert pr_row["status"] == "draft"
    assert pr_row["purchase_order_id"] == po_id
    assert pr_row["supplier_id"] == env["supplier_id"]


# ---------------------------------------------------------------------------
# 2. test_create_purchase_receipt_direct
# ---------------------------------------------------------------------------

def test_create_purchase_receipt_direct(fresh_db):
    """Creating a receipt without a PO reference should return error.

    The create-purchase-receipt action requires --purchase-order-id.
    """
    env = setup_buying_environment(fresh_db)

    result = _call_action(
        ACTIONS["create-purchase-receipt"], fresh_db,
        posting_date="2026-02-16",
    )

    assert result["status"] == "error"
    assert "purchase-order-id" in result["message"].lower() or \
           "purchase_order_id" in result["message"].lower() or \
           "required" in result["message"].lower()


# ---------------------------------------------------------------------------
# 3. test_get_purchase_receipt
# ---------------------------------------------------------------------------

def test_get_purchase_receipt(fresh_db):
    """Create a receipt then get it; verify items are returned."""
    env = setup_buying_environment(fresh_db)
    po_id = _make_confirmed_po(fresh_db, env, qty="10", rate="25.00")
    pr_id = _make_receipt(fresh_db, po_id)

    result = _call_action(
        ACTIONS["get-purchase-receipt"], fresh_db,
        purchase_receipt_id=pr_id,
    )

    assert result["status"] == "ok"
    assert result["id"] == pr_id
    assert result["purchase_order_id"] == po_id
    assert result["supplier_id"] == env["supplier_id"]

    # Items should be returned
    assert "items" in result
    assert len(result["items"]) == 1
    assert result["items"][0]["item_id"] == env["item_id"]
    assert result["items"][0]["quantity"] == "10.00"
    assert result["items"][0]["rate"] == "25.00"


# ---------------------------------------------------------------------------
# 4. test_list_purchase_receipts
# ---------------------------------------------------------------------------

def test_list_purchase_receipts(fresh_db):
    """Create 2 receipts from 2 different POs, list them, verify count."""
    env = setup_buying_environment(fresh_db)

    po_id_1 = _make_confirmed_po(fresh_db, env, qty="5", rate="10.00")
    po_id_2 = _make_confirmed_po(fresh_db, env, qty="20", rate="30.00")

    pr_id_1 = _make_receipt(fresh_db, po_id_1)
    pr_id_2 = _make_receipt(fresh_db, po_id_2)

    result = _call_action(
        ACTIONS["list-purchase-receipts"], fresh_db,
        company_id=env["company_id"],
    )

    assert result["status"] == "ok"
    assert result["total_count"] == 2
    assert len(result["purchase_receipts"]) == 2

    returned_ids = {pr["id"] for pr in result["purchase_receipts"]}
    assert pr_id_1 in returned_ids
    assert pr_id_2 in returned_ids


# ---------------------------------------------------------------------------
# 5. test_submit_purchase_receipt
# ---------------------------------------------------------------------------

def test_submit_purchase_receipt(fresh_db):
    """Submit a receipt and verify SLE entries are created (stock increased)."""
    env = setup_buying_environment(fresh_db)
    po_id = _make_confirmed_po(fresh_db, env, qty="10", rate="25.00")
    pr_id = _make_receipt(fresh_db, po_id)

    result = _call_action(
        ACTIONS["submit-purchase-receipt"], fresh_db,
        purchase_receipt_id=pr_id,
    )

    assert result["status"] == "ok"
    assert result["purchase_receipt_id"] == pr_id
    assert result["naming_series"].startswith("PR-")
    assert result["sle_entries_created"] >= 1

    # Verify SLE was created with positive actual_qty (stock received)
    sle_row = fresh_db.execute(
        """SELECT actual_qty, incoming_rate FROM stock_ledger_entry
           WHERE voucher_type = 'purchase_receipt' AND voucher_id = ?
             AND is_cancelled = 0 LIMIT 1""",
        (pr_id,),
    ).fetchone()
    assert sle_row is not None
    assert float(sle_row["actual_qty"]) > 0
    assert float(sle_row["actual_qty"]) == 10.0
    assert float(sle_row["incoming_rate"]) == 25.0

    # Verify receipt status is now submitted
    pr_row = fresh_db.execute(
        "SELECT status FROM purchase_receipt WHERE id = ?", (pr_id,),
    ).fetchone()
    assert pr_row["status"] == "submitted"


# ---------------------------------------------------------------------------
# 6. test_submit_purchase_receipt_gl
# ---------------------------------------------------------------------------

def test_submit_purchase_receipt_gl(fresh_db):
    """Submit a receipt and verify GL entries: Stock DR, Stock Received CR."""
    env = setup_buying_environment(fresh_db)
    po_id = _make_confirmed_po(fresh_db, env, qty="10", rate="25.00")
    pr_id = _make_receipt(fresh_db, po_id)

    result = _call_action(
        ACTIONS["submit-purchase-receipt"], fresh_db,
        purchase_receipt_id=pr_id,
    )

    assert result["status"] == "ok"
    assert result["gl_entries_created"] >= 1

    # Verify GL entries exist for this receipt
    gl_rows = fresh_db.execute(
        """SELECT * FROM gl_entry
           WHERE voucher_type = 'purchase_receipt' AND voucher_id = ?
             AND is_cancelled = 0
           ORDER BY rowid""",
        (pr_id,),
    ).fetchall()
    assert len(gl_rows) >= 2  # At least a debit and a credit

    gl_dicts = [dict(r) for r in gl_rows]

    # There should be a debit to Stock In Hand (asset account)
    # and a credit to Stock Received But Not Billed (liability account)
    total_debit = sum(float(g["debit"]) for g in gl_dicts)
    total_credit = sum(float(g["credit"]) for g in gl_dicts)

    # GL must balance: total debits == total credits
    assert abs(total_debit - total_credit) < 0.01

    # The total value should be qty * rate = 10 * 25 = 250.00
    assert abs(total_debit - 250.0) < 0.01


# ---------------------------------------------------------------------------
# 7. test_submit_already_submitted
# ---------------------------------------------------------------------------

def test_submit_already_submitted(fresh_db):
    """Submitting an already-submitted receipt should return error."""
    env = setup_buying_environment(fresh_db)
    po_id = _make_confirmed_po(fresh_db, env)
    pr_id = _make_receipt(fresh_db, po_id)

    # First submit
    result1 = _call_action(
        ACTIONS["submit-purchase-receipt"], fresh_db,
        purchase_receipt_id=pr_id,
    )
    assert result1["status"] == "ok"

    # Second submit should error
    result2 = _call_action(
        ACTIONS["submit-purchase-receipt"], fresh_db,
        purchase_receipt_id=pr_id,
    )

    assert result2["status"] == "error"
    assert "draft" in result2["message"].lower() or \
           "submitted" in result2["message"].lower()


# ---------------------------------------------------------------------------
# 8. test_cancel_purchase_receipt
# ---------------------------------------------------------------------------

def test_cancel_purchase_receipt(fresh_db):
    """Submit then cancel a receipt; verify SLE entries are reversed."""
    env = setup_buying_environment(fresh_db)
    po_id = _make_confirmed_po(fresh_db, env, qty="10", rate="25.00")
    pr_id = _make_receipt(fresh_db, po_id)

    # Submit
    submit_result = _call_action(
        ACTIONS["submit-purchase-receipt"], fresh_db,
        purchase_receipt_id=pr_id,
    )
    assert submit_result["status"] == "ok"

    # Count SLE before cancel
    sle_before = fresh_db.execute(
        """SELECT COUNT(*) as cnt FROM stock_ledger_entry
           WHERE voucher_id = ? AND is_cancelled = 0""",
        (pr_id,),
    ).fetchone()["cnt"]
    assert sle_before >= 1

    # Cancel
    cancel_result = _call_action(
        ACTIONS["cancel-purchase-receipt"], fresh_db,
        purchase_receipt_id=pr_id,
    )

    assert cancel_result["status"] == "ok"
    assert cancel_result["purchase_receipt_id"] == pr_id
    assert cancel_result["sle_reversals"] >= 1

    # Verify original SLE entries are marked cancelled
    cancelled_sle = fresh_db.execute(
        """SELECT COUNT(*) as cnt FROM stock_ledger_entry
           WHERE voucher_id = ? AND is_cancelled = 1""",
        (pr_id,),
    ).fetchone()["cnt"]
    assert cancelled_sle >= 1

    # Verify receipt status is now cancelled
    pr_row = fresh_db.execute(
        "SELECT status FROM purchase_receipt WHERE id = ?", (pr_id,),
    ).fetchone()
    assert pr_row["status"] == "cancelled"


# ---------------------------------------------------------------------------
# 9. test_cancel_draft_receipt
# ---------------------------------------------------------------------------

def test_cancel_draft_receipt(fresh_db):
    """Cancelling a draft receipt (not yet submitted) should return error."""
    env = setup_buying_environment(fresh_db)
    po_id = _make_confirmed_po(fresh_db, env)
    pr_id = _make_receipt(fresh_db, po_id)

    # Verify it's currently draft
    pr_row = fresh_db.execute(
        "SELECT status FROM purchase_receipt WHERE id = ?", (pr_id,),
    ).fetchone()
    assert pr_row["status"] == "draft"

    # Attempt cancel on a draft -- should fail
    result = _call_action(
        ACTIONS["cancel-purchase-receipt"], fresh_db,
        purchase_receipt_id=pr_id,
    )

    assert result["status"] == "error"
    assert "draft" in result["message"].lower() or \
           "submitted" in result["message"].lower()


# ---------------------------------------------------------------------------
# 10. test_receipt_updates_po_status
# ---------------------------------------------------------------------------

def test_receipt_updates_po_status(fresh_db):
    """Submit a receipt for full PO qty; verify PO status becomes fully_received."""
    env = setup_buying_environment(fresh_db)
    po_id = _make_confirmed_po(fresh_db, env, qty="10", rate="25.00")
    pr_id = _make_receipt(fresh_db, po_id)

    # Before submit: PO should be confirmed
    po_row = fresh_db.execute(
        "SELECT status FROM purchase_order WHERE id = ?", (po_id,),
    ).fetchone()
    assert po_row["status"] == "confirmed"

    # Submit the receipt (full qty)
    submit_result = _call_action(
        ACTIONS["submit-purchase-receipt"], fresh_db,
        purchase_receipt_id=pr_id,
    )
    assert submit_result["status"] == "ok"

    # After submit: PO should be fully_received
    po_row = fresh_db.execute(
        "SELECT status, per_received FROM purchase_order WHERE id = ?",
        (po_id,),
    ).fetchone()
    assert po_row["status"] == "fully_received"
    assert float(po_row["per_received"]) >= 100.0

    # Verify PO item received_qty matches ordered qty
    poi_row = fresh_db.execute(
        """SELECT quantity, received_qty FROM purchase_order_item
           WHERE purchase_order_id = ? LIMIT 1""",
        (po_id,),
    ).fetchone()
    assert float(poi_row["received_qty"]) == float(poi_row["quantity"])
