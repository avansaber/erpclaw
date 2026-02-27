"""Tests for delivery note lifecycle: create, get, list, submit, cancel.

10 tests covering creation from SO, missing SO error, retrieval, listing,
submit with SLE creation, submit with GL entries (COGS), insufficient stock,
cancel with SLE reversal, cancel-draft error, and SO delivery_status update.
"""
import json
from decimal import Decimal

from db_query import ACTIONS
from helpers import (
    _call_action,
    setup_selling_environment,
    create_test_item,
    create_test_warehouse,
    seed_stock_for_item,
)


def _create_submitted_so(fresh_db, env, qty="10", rate="25.00"):
    """Helper: create and submit a sales order. Returns sales_order_id."""
    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": qty,
        "rate": rate,
        "warehouse_id": env["warehouse_id"],
    }])

    add_result = _call_action(
        ACTIONS["add-sales-order"], fresh_db,
        customer_id=env["customer_id"],
        company_id=env["company_id"],
        posting_date="2026-02-16",
        delivery_date="2026-03-01",
        items=items_json,
    )
    assert add_result["status"] == "ok"
    so_id = add_result["sales_order_id"]

    submit_result = _call_action(
        ACTIONS["submit-sales-order"], fresh_db,
        sales_order_id=so_id,
    )
    assert submit_result["status"] == "ok"
    return so_id


# ---------------------------------------------------------------------------
# 1. test_create_delivery_note
# ---------------------------------------------------------------------------

def test_create_delivery_note(fresh_db):
    """Create a delivery note from a submitted sales order; assert ok."""
    env = setup_selling_environment(fresh_db)
    so_id = _create_submitted_so(fresh_db, env)

    result = _call_action(
        ACTIONS["create-delivery-note"], fresh_db,
        sales_order_id=so_id,
        posting_date="2026-02-16",
    )

    assert result["status"] == "ok"
    assert "delivery_note_id" in result
    assert result["sales_order_id"] == so_id
    assert result["item_count"] == 1
    assert result["total_qty"] == "10.00"


# ---------------------------------------------------------------------------
# 2. test_create_delivery_note_no_so
# ---------------------------------------------------------------------------

def test_create_delivery_note_no_so(fresh_db):
    """Creating a delivery note without a sales_order_id should return error.

    The create-delivery-note action requires --sales-order-id.
    """
    env = setup_selling_environment(fresh_db)

    result = _call_action(
        ACTIONS["create-delivery-note"], fresh_db,
        posting_date="2026-02-16",
    )

    assert result["status"] == "error"
    assert "sales-order-id" in result["message"].lower() or \
           "sales_order" in result["message"].lower() or \
           "required" in result["message"].lower()


# ---------------------------------------------------------------------------
# 3. test_get_delivery_note
# ---------------------------------------------------------------------------

def test_get_delivery_note(fresh_db):
    """Create a delivery note then get it; verify items are returned."""
    env = setup_selling_environment(fresh_db)
    so_id = _create_submitted_so(fresh_db, env, qty="8", rate="30.00")

    create_result = _call_action(
        ACTIONS["create-delivery-note"], fresh_db,
        sales_order_id=so_id,
        posting_date="2026-02-16",
    )
    assert create_result["status"] == "ok"
    dn_id = create_result["delivery_note_id"]

    get_result = _call_action(
        ACTIONS["get-delivery-note"], fresh_db,
        delivery_note_id=dn_id,
    )

    assert get_result["status"] == "ok"
    assert get_result["id"] == dn_id
    assert get_result["sales_order_id"] == so_id
    assert get_result["customer_id"] == env["customer_id"]
    assert "items" in get_result
    assert len(get_result["items"]) == 1
    assert get_result["items"][0]["item_id"] == env["item_id"]
    assert get_result["items"][0]["quantity"] == "8.00"
    assert get_result["items"][0]["rate"] == "30.00"


# ---------------------------------------------------------------------------
# 4. test_list_delivery_notes
# ---------------------------------------------------------------------------

def test_list_delivery_notes(fresh_db):
    """Create 2 delivery notes and list them; verify total_count = 2."""
    env = setup_selling_environment(fresh_db)

    # Create two separate SOs and DNs
    for qty in ["5", "10"]:
        so_id = _create_submitted_so(fresh_db, env, qty=qty)
        r = _call_action(
            ACTIONS["create-delivery-note"], fresh_db,
            sales_order_id=so_id,
            posting_date="2026-02-16",
        )
        assert r["status"] == "ok"

    list_result = _call_action(
        ACTIONS["list-delivery-notes"], fresh_db,
        company_id=env["company_id"],
    )

    assert list_result["status"] == "ok"
    assert list_result["total_count"] == 2
    assert len(list_result["delivery_notes"]) == 2


# ---------------------------------------------------------------------------
# 5. test_submit_delivery_note
# ---------------------------------------------------------------------------

def test_submit_delivery_note(fresh_db):
    """Submit a delivery note; verify SLE created (stock reduced)."""
    env = setup_selling_environment(fresh_db)
    so_id = _create_submitted_so(fresh_db, env, qty="10", rate="25.00")

    create_result = _call_action(
        ACTIONS["create-delivery-note"], fresh_db,
        sales_order_id=so_id,
        posting_date="2026-02-16",
    )
    assert create_result["status"] == "ok"
    dn_id = create_result["delivery_note_id"]

    submit_result = _call_action(
        ACTIONS["submit-delivery-note"], fresh_db,
        delivery_note_id=dn_id,
    )

    assert submit_result["status"] == "ok"
    assert submit_result["delivery_note_id"] == dn_id
    assert "naming_series" in submit_result
    assert submit_result["naming_series"].startswith("DN-")
    assert submit_result["sle_entries_created"] >= 1

    # Verify SLE entry exists and has negative qty (stock going out)
    sle_rows = fresh_db.execute(
        """SELECT * FROM stock_ledger_entry
           WHERE voucher_type = 'delivery_note' AND voucher_id = ?
             AND is_cancelled = 0""",
        (dn_id,),
    ).fetchall()
    assert len(sle_rows) >= 1

    sle = dict(sle_rows[0])
    assert Decimal(sle["actual_qty"]) < 0  # stock reduced
    assert sle["item_id"] == env["item_id"]
    assert sle["warehouse_id"] == env["warehouse_id"]

    # Verify DN status via direct SQL (get action's _ok() overwrites status to "ok")
    dn_row = fresh_db.execute(
        "SELECT status FROM delivery_note WHERE id = ?", (dn_id,)
    ).fetchone()
    assert dn_row["status"] == "submitted"


# ---------------------------------------------------------------------------
# 6. test_submit_delivery_note_gl
# ---------------------------------------------------------------------------

def test_submit_delivery_note_gl(fresh_db):
    """Submit a delivery note; verify GL entries (COGS DR, Stock In Hand CR)."""
    env = setup_selling_environment(fresh_db)
    so_id = _create_submitted_so(fresh_db, env, qty="10", rate="25.00")

    create_result = _call_action(
        ACTIONS["create-delivery-note"], fresh_db,
        sales_order_id=so_id,
        posting_date="2026-02-16",
    )
    assert create_result["status"] == "ok"
    dn_id = create_result["delivery_note_id"]

    submit_result = _call_action(
        ACTIONS["submit-delivery-note"], fresh_db,
        delivery_note_id=dn_id,
    )

    assert submit_result["status"] == "ok"
    assert submit_result["gl_entries_created"] >= 2

    # Fetch GL entries for this delivery note
    gl_rows = fresh_db.execute(
        """SELECT * FROM gl_entry
           WHERE voucher_type = 'delivery_note' AND voucher_id = ?
             AND is_cancelled = 0""",
        (dn_id,),
    ).fetchall()
    gl_entries = [dict(r) for r in gl_rows]
    assert len(gl_entries) >= 2

    # Verify COGS debit entry exists
    cogs_entries = [
        g for g in gl_entries
        if g["account_id"] == env["cogs_id"] and Decimal(g["debit"]) > 0
    ]
    assert len(cogs_entries) >= 1, "Expected COGS debit GL entry"

    # Verify Stock In Hand credit entry exists
    stock_entries = [
        g for g in gl_entries
        if g["account_id"] == env["stock_in_hand_id"] and Decimal(g["credit"]) > 0
    ]
    assert len(stock_entries) >= 1, "Expected Stock In Hand credit GL entry"

    # COGS debit should equal Stock In Hand credit (balanced)
    total_cogs_debit = sum(Decimal(g["debit"]) for g in cogs_entries)
    total_stock_credit = sum(Decimal(g["credit"]) for g in stock_entries)
    assert total_cogs_debit == total_stock_credit


# ---------------------------------------------------------------------------
# 7. test_submit_insufficient_stock
# ---------------------------------------------------------------------------

def test_submit_insufficient_stock(fresh_db):
    """Try to submit a delivery note for more stock than available; should error."""
    env = setup_selling_environment(fresh_db)

    # env seeds 100 qty of stock. Create SO for 200 qty (exceeds stock).
    so_id = _create_submitted_so(fresh_db, env, qty="200", rate="25.00")

    create_result = _call_action(
        ACTIONS["create-delivery-note"], fresh_db,
        sales_order_id=so_id,
        posting_date="2026-02-16",
    )
    assert create_result["status"] == "ok"
    dn_id = create_result["delivery_note_id"]

    # Submit should fail due to insufficient stock
    submit_result = _call_action(
        ACTIONS["submit-delivery-note"], fresh_db,
        delivery_note_id=dn_id,
    )

    assert submit_result["status"] == "error"
    assert "stock" in submit_result["message"].lower() or \
           "insufficient" in submit_result["message"].lower() or \
           "negative" in submit_result["message"].lower() or \
           "sle" in submit_result["message"].lower()


# ---------------------------------------------------------------------------
# 8. test_cancel_delivery_note
# ---------------------------------------------------------------------------

def test_cancel_delivery_note(fresh_db):
    """Submit then cancel a delivery note; verify SLE reversed, stock restored."""
    env = setup_selling_environment(fresh_db)
    so_id = _create_submitted_so(fresh_db, env, qty="10", rate="25.00")

    create_result = _call_action(
        ACTIONS["create-delivery-note"], fresh_db,
        sales_order_id=so_id,
        posting_date="2026-02-16",
    )
    assert create_result["status"] == "ok"
    dn_id = create_result["delivery_note_id"]

    # Submit
    submit_result = _call_action(
        ACTIONS["submit-delivery-note"], fresh_db,
        delivery_note_id=dn_id,
    )
    assert submit_result["status"] == "ok"

    # Get stock balance after submit (should be reduced)
    sle_after_submit = fresh_db.execute(
        """SELECT SUM(CAST(actual_qty AS REAL)) as net_qty
           FROM stock_ledger_entry
           WHERE item_id = ? AND warehouse_id = ? AND is_cancelled = 0""",
        (env["item_id"], env["warehouse_id"]),
    ).fetchone()
    stock_after_submit = float(sle_after_submit["net_qty"])

    # Cancel
    cancel_result = _call_action(
        ACTIONS["cancel-delivery-note"], fresh_db,
        delivery_note_id=dn_id,
    )

    assert cancel_result["status"] == "ok"
    assert cancel_result["delivery_note_id"] == dn_id
    assert cancel_result["sle_reversals"] >= 1

    # Verify DN status via direct SQL (get action's _ok() overwrites status to "ok")
    dn_row = fresh_db.execute(
        "SELECT status FROM delivery_note WHERE id = ?", (dn_id,)
    ).fetchone()
    assert dn_row["status"] == "cancelled"

    # Verify stock is restored (reversal SLE entries added)
    sle_after_cancel = fresh_db.execute(
        """SELECT SUM(CAST(actual_qty AS REAL)) as net_qty
           FROM stock_ledger_entry
           WHERE item_id = ? AND warehouse_id = ? AND is_cancelled = 0""",
        (env["item_id"], env["warehouse_id"]),
    ).fetchone()
    stock_after_cancel = float(sle_after_cancel["net_qty"])

    # Stock after cancel should be higher than after submit (restored)
    assert stock_after_cancel > stock_after_submit


# ---------------------------------------------------------------------------
# 9. test_cancel_draft_delivery_note
# ---------------------------------------------------------------------------

def test_cancel_draft_delivery_note(fresh_db):
    """Try to cancel a draft delivery note; should return error.

    The cancel-delivery-note action requires status == 'submitted'.
    """
    env = setup_selling_environment(fresh_db)
    so_id = _create_submitted_so(fresh_db, env)

    create_result = _call_action(
        ACTIONS["create-delivery-note"], fresh_db,
        sales_order_id=so_id,
        posting_date="2026-02-16",
    )
    assert create_result["status"] == "ok"
    dn_id = create_result["delivery_note_id"]

    # Try to cancel a draft DN (not submitted)
    cancel_result = _call_action(
        ACTIONS["cancel-delivery-note"], fresh_db,
        delivery_note_id=dn_id,
    )

    assert cancel_result["status"] == "error"
    assert "submitted" in cancel_result["message"].lower() or \
           "cannot cancel" in cancel_result["message"].lower()


# ---------------------------------------------------------------------------
# 10. test_delivery_updates_so_status
# ---------------------------------------------------------------------------

def test_delivery_updates_so_status(fresh_db):
    """Submit a DN for full SO qty; check SO status becomes fully_delivered."""
    env = setup_selling_environment(fresh_db)
    so_id = _create_submitted_so(fresh_db, env, qty="10", rate="25.00")

    # Create DN for full qty
    create_result = _call_action(
        ACTIONS["create-delivery-note"], fresh_db,
        sales_order_id=so_id,
        posting_date="2026-02-16",
    )
    assert create_result["status"] == "ok"
    dn_id = create_result["delivery_note_id"]

    # Submit DN
    submit_result = _call_action(
        ACTIONS["submit-delivery-note"], fresh_db,
        delivery_note_id=dn_id,
    )
    assert submit_result["status"] == "ok"

    # Verify SO status via direct SQL (get action's _ok() overwrites status to "ok")
    so_row = fresh_db.execute(
        "SELECT status, per_delivered FROM sales_order WHERE id = ?", (so_id,)
    ).fetchone()
    assert so_row["status"] == "fully_delivered"
    assert so_row["per_delivered"] == "100.00"

    # Verify SO items show delivered_qty matches ordered qty
    so_result = _call_action(
        ACTIONS["get-sales-order"], fresh_db,
        sales_order_id=so_id,
    )
    for item in so_result["items"]:
        assert Decimal(item["delivered_qty"]) == Decimal(item["quantity"])
