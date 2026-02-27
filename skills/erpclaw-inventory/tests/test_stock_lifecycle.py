"""Tests for stock entry lifecycle: submit-stock-entry, cancel-stock-entry.

8 tests covering submission with SLE/GL creation, cancellation with reversals,
error conditions (already submitted, not found, draft cancel, double cancel),
and naming series verification.
"""
import json
import uuid

from db_query import ACTIONS
from helpers import (
    _call_action,
    setup_inventory_environment,
    create_test_stock_entry,
    submit_test_stock_entry,
)


def _make_receipt(conn, env, qty=100, rate="25.00"):
    """Helper: create a draft material receipt and return the stock_entry_id."""
    items_json = json.dumps([{
        "item_id": env["item_id"],
        "to_warehouse_id": env["warehouse_id"],
        "qty": qty,
        "rate": rate,
    }])
    return create_test_stock_entry(
        conn, env["company_id"], "receive", items_json,
    )


# ---------------------------------------------------------------------------
# 1. test_submit_stock_entry_receipt
# ---------------------------------------------------------------------------

def test_submit_stock_entry_receipt(fresh_db):
    """Submit a receipt and verify SLE + GL entries are created."""
    env = setup_inventory_environment(fresh_db)
    se_id = _make_receipt(fresh_db, env, qty=100, rate="25.00")

    result = submit_test_stock_entry(fresh_db, se_id)

    assert result["status"] == "ok"
    assert result["stock_entry_id"] == se_id
    assert result["sle_entries_created"] >= 1
    assert result["gl_entries_created"] >= 1

    # Verify SLE was created with positive qty
    sle_count = fresh_db.execute(
        "SELECT COUNT(*) as cnt FROM stock_ledger_entry "
        "WHERE voucher_id = ? AND is_cancelled = 0",
        (se_id,),
    ).fetchone()["cnt"]
    assert sle_count >= 1

    # Verify the SLE has positive actual_qty (receipt adds stock)
    sle_row = fresh_db.execute(
        "SELECT actual_qty FROM stock_ledger_entry "
        "WHERE voucher_id = ? AND is_cancelled = 0 LIMIT 1",
        (se_id,),
    ).fetchone()
    assert float(sle_row["actual_qty"]) > 0

    # Verify stock entry status changed to submitted
    se_row = fresh_db.execute(
        "SELECT status FROM stock_entry WHERE id = ?", (se_id,)
    ).fetchone()
    assert se_row["status"] == "submitted"

    # Verify GL entries exist
    gl_count = fresh_db.execute(
        "SELECT COUNT(*) as cnt FROM gl_entry "
        "WHERE voucher_id = ? AND is_cancelled = 0",
        (se_id,),
    ).fetchone()["cnt"]
    assert gl_count >= 1


# ---------------------------------------------------------------------------
# 2. test_submit_stock_entry_issue
# ---------------------------------------------------------------------------

def test_submit_stock_entry_issue(fresh_db):
    """Submit a receipt first to build stock, then submit an issue.
    Verify SLE with negative qty."""
    env = setup_inventory_environment(fresh_db)

    # First, receive stock so we have something to issue
    receipt_id = _make_receipt(fresh_db, env, qty=200, rate="25.00")
    receipt_result = submit_test_stock_entry(fresh_db, receipt_id)
    assert receipt_result["status"] == "ok"

    # Now create and submit an issue
    issue_json = json.dumps([{
        "item_id": env["item_id"],
        "from_warehouse_id": env["warehouse_id"],
        "qty": 50,
        "rate": "25.00",
    }])
    issue_id = create_test_stock_entry(
        fresh_db, env["company_id"], "issue", issue_json,
    )

    result = _call_action(
        ACTIONS["submit-stock-entry"], fresh_db,
        stock_entry_id=issue_id,
    )

    assert result["status"] == "ok"
    assert result["sle_entries_created"] >= 1

    # Verify the issue SLE has negative actual_qty
    sle_row = fresh_db.execute(
        "SELECT actual_qty FROM stock_ledger_entry "
        "WHERE voucher_id = ? AND is_cancelled = 0 LIMIT 1",
        (issue_id,),
    ).fetchone()
    assert float(sle_row["actual_qty"]) < 0


# ---------------------------------------------------------------------------
# 3. test_submit_stock_entry_already_submitted
# ---------------------------------------------------------------------------

def test_submit_stock_entry_already_submitted(fresh_db):
    """Submitting an already-submitted entry should error."""
    env = setup_inventory_environment(fresh_db)
    se_id = _make_receipt(fresh_db, env)

    # Submit the first time
    result1 = submit_test_stock_entry(fresh_db, se_id)
    assert result1["status"] == "ok"

    # Attempt second submit
    result2 = _call_action(
        ACTIONS["submit-stock-entry"], fresh_db,
        stock_entry_id=se_id,
    )

    assert result2["status"] == "error"
    assert "submitted" in result2["message"].lower() or "draft" in result2["message"].lower()


# ---------------------------------------------------------------------------
# 4. test_submit_stock_entry_not_found
# ---------------------------------------------------------------------------

def test_submit_stock_entry_not_found(fresh_db):
    """Submitting a non-existent stock entry ID should error."""
    fake_id = str(uuid.uuid4())

    result = _call_action(
        ACTIONS["submit-stock-entry"], fresh_db,
        stock_entry_id=fake_id,
    )

    assert result["status"] == "error"
    assert "not found" in result["message"].lower()


# ---------------------------------------------------------------------------
# 5. test_cancel_stock_entry
# ---------------------------------------------------------------------------

def test_cancel_stock_entry(fresh_db):
    """Submit then cancel a stock entry; verify reversal SLE and GL entries."""
    env = setup_inventory_environment(fresh_db)
    se_id = _make_receipt(fresh_db, env, qty=100, rate="25.00")

    # Submit
    submit_result = submit_test_stock_entry(fresh_db, se_id)
    assert submit_result["status"] == "ok"

    # Count SLE before cancel
    sle_before = fresh_db.execute(
        "SELECT COUNT(*) as cnt FROM stock_ledger_entry WHERE voucher_id = ?",
        (se_id,),
    ).fetchone()["cnt"]

    # Cancel
    cancel_result = _call_action(
        ACTIONS["cancel-stock-entry"], fresh_db,
        stock_entry_id=se_id,
    )

    assert cancel_result["status"] == "ok"
    assert cancel_result["reversed"] is True
    assert cancel_result["sle_reversals"] >= 1

    # Verify original SLE entries are marked cancelled
    cancelled_sle = fresh_db.execute(
        "SELECT COUNT(*) as cnt FROM stock_ledger_entry "
        "WHERE voucher_id = ? AND is_cancelled = 1",
        (se_id,),
    ).fetchone()["cnt"]
    assert cancelled_sle >= 1

    # Verify stock entry status changed to cancelled
    se_row = fresh_db.execute(
        "SELECT status FROM stock_entry WHERE id = ?", (se_id,)
    ).fetchone()
    assert se_row["status"] == "cancelled"


# ---------------------------------------------------------------------------
# 6. test_cancel_stock_entry_not_submitted
# ---------------------------------------------------------------------------

def test_cancel_stock_entry_not_submitted(fresh_db):
    """Cancelling a draft stock entry should error (must be submitted)."""
    env = setup_inventory_environment(fresh_db)
    se_id = _make_receipt(fresh_db, env)

    # Attempt cancel on a draft
    result = _call_action(
        ACTIONS["cancel-stock-entry"], fresh_db,
        stock_entry_id=se_id,
    )

    assert result["status"] == "error"
    assert "draft" in result["message"].lower() or "submitted" in result["message"].lower()


# ---------------------------------------------------------------------------
# 7. test_cancel_stock_entry_already_cancelled
# ---------------------------------------------------------------------------

def test_cancel_stock_entry_already_cancelled(fresh_db):
    """Double-cancelling a stock entry should error."""
    env = setup_inventory_environment(fresh_db)
    se_id = _make_receipt(fresh_db, env)

    # Submit then cancel
    submit_result = submit_test_stock_entry(fresh_db, se_id)
    assert submit_result["status"] == "ok"

    cancel1 = _call_action(
        ACTIONS["cancel-stock-entry"], fresh_db,
        stock_entry_id=se_id,
    )
    assert cancel1["status"] == "ok"

    # Attempt second cancel
    cancel2 = _call_action(
        ACTIONS["cancel-stock-entry"], fresh_db,
        stock_entry_id=se_id,
    )

    assert cancel2["status"] == "error"
    assert "cancelled" in cancel2["message"].lower() or "submitted" in cancel2["message"].lower()


# ---------------------------------------------------------------------------
# 8. test_submit_creates_naming_series
# ---------------------------------------------------------------------------

def test_submit_creates_naming_series(fresh_db):
    """Verify naming series follows the pattern STE-{YEAR}-{SEQUENCE}."""
    env = setup_inventory_environment(fresh_db)

    # Create and check the naming_series assigned at draft creation
    items_json = json.dumps([{
        "item_id": env["item_id"],
        "to_warehouse_id": env["warehouse_id"],
        "qty": 10,
        "rate": "25.00",
    }])

    add_result = _call_action(
        ACTIONS["add-stock-entry"], fresh_db,
        entry_type="receive",
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=items_json,
    )
    assert add_result["status"] == "ok"
    naming = add_result["naming_series"]

    # Verify pattern: STE-YYYY-NNNNN
    assert naming.startswith("STE-")
    parts = naming.split("-")
    assert len(parts) == 3
    assert parts[1] == "2026"
    assert parts[2].isdigit()
    seq_num = int(parts[2])
    assert seq_num >= 1

    # Create a second entry and verify sequence increments
    add_result2 = _call_action(
        ACTIONS["add-stock-entry"], fresh_db,
        entry_type="receive",
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=items_json,
    )
    assert add_result2["status"] == "ok"
    naming2 = add_result2["naming_series"]
    parts2 = naming2.split("-")
    seq_num2 = int(parts2[2])
    assert seq_num2 == seq_num + 1
