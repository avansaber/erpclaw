"""Tests for stock reconciliation actions: add-stock-reconciliation, submit-stock-reconciliation."""
import json
import uuid
import pytest
from helpers import (
    _call_action,
    create_test_stock_entry,
    setup_inventory_environment,
    submit_test_stock_entry,
)
from db_query import ACTIONS


class TestAddStockReconciliation:
    """Tests for the add-stock-reconciliation action."""

    def test_add_stock_reconciliation(self, fresh_db):
        """Create a reconciliation with one item, verify draft status and difference."""
        env = setup_inventory_environment(fresh_db)

        # No prior stock: current qty = 0, counted qty = 15
        # Difference should be +15, value difference = 15 * 25.00 = 375.00
        items_json = json.dumps([{
            "item_id": env["item_id"],
            "warehouse_id": env["warehouse_id"],
            "qty": "15",
            "valuation_rate": "25.00",
        }])

        result = _call_action(
            ACTIONS["add-stock-reconciliation"], fresh_db,
            posting_date="2026-02-16",
            items=items_json,
            company_id=env["company_id"],
        )
        assert result["status"] == "ok"
        assert "stock_reconciliation_id" in result
        assert result["item_count"] == 1
        assert result["difference_amount"] == "375.00"

        # Verify the record is in draft status in the database
        sr_row = fresh_db.execute(
            "SELECT * FROM stock_reconciliation WHERE id = ?",
            (result["stock_reconciliation_id"],),
        ).fetchone()
        assert sr_row is not None
        assert sr_row["status"] == "draft"
        assert sr_row["difference_amount"] == "375.00"
        assert sr_row["company_id"] == env["company_id"]

        # Verify the reconciliation item was saved
        sri_row = fresh_db.execute(
            """SELECT * FROM stock_reconciliation_item
               WHERE stock_reconciliation_id = ?""",
            (result["stock_reconciliation_id"],),
        ).fetchone()
        assert sri_row is not None
        assert sri_row["item_id"] == env["item_id"]
        assert sri_row["warehouse_id"] == env["warehouse_id"]
        assert sri_row["current_qty"] == "0.00"
        assert sri_row["qty"] == "15.00"
        assert sri_row["quantity_difference"] == "15.00"

    def test_add_stock_reconciliation_with_existing_stock(self, fresh_db):
        """Submit receipt (10 units), then reconcile to 15 units, verify +5 difference."""
        env = setup_inventory_environment(fresh_db)

        # First, receive 10 units at $25
        receipt_items = json.dumps([{
            "item_id": env["item_id"],
            "to_warehouse_id": env["warehouse_id"],
            "qty": 10,
            "rate": "25.00",
        }])
        se_id = create_test_stock_entry(
            fresh_db, env["company_id"], "receive", receipt_items,
        )
        submit_result = submit_test_stock_entry(fresh_db, se_id)
        assert submit_result["status"] == "ok"

        # Now reconcile: physical count is 15 units
        recon_items = json.dumps([{
            "item_id": env["item_id"],
            "warehouse_id": env["warehouse_id"],
            "qty": "15",
            "valuation_rate": "25.00",
        }])

        result = _call_action(
            ACTIONS["add-stock-reconciliation"], fresh_db,
            posting_date="2026-02-16",
            items=recon_items,
            company_id=env["company_id"],
        )
        assert result["status"] == "ok"
        assert result["item_count"] == 1
        # Difference: (15 * 25) - (10 * 25) = 375 - 250 = 125.00
        assert result["difference_amount"] == "125.00"

        # Verify the item-level difference
        sri_row = fresh_db.execute(
            """SELECT * FROM stock_reconciliation_item
               WHERE stock_reconciliation_id = ?""",
            (result["stock_reconciliation_id"],),
        ).fetchone()
        assert sri_row["current_qty"] == "10.00"
        assert sri_row["qty"] == "15.00"
        assert sri_row["quantity_difference"] == "5.00"
        assert sri_row["amount_difference"] == "125.00"


class TestSubmitStockReconciliation:
    """Tests for the submit-stock-reconciliation action."""

    def test_submit_stock_reconciliation(self, fresh_db):
        """Submit a reconciliation and verify SLE entries are created for the difference."""
        env = setup_inventory_environment(fresh_db)

        # Create reconciliation: 0 -> 20 units
        items_json = json.dumps([{
            "item_id": env["item_id"],
            "warehouse_id": env["warehouse_id"],
            "qty": "20",
            "valuation_rate": "25.00",
        }])

        add_result = _call_action(
            ACTIONS["add-stock-reconciliation"], fresh_db,
            posting_date="2026-02-16",
            items=items_json,
            company_id=env["company_id"],
        )
        assert add_result["status"] == "ok"
        sr_id = add_result["stock_reconciliation_id"]

        # Submit the reconciliation
        result = _call_action(
            ACTIONS["submit-stock-reconciliation"], fresh_db,
            stock_reconciliation_id=sr_id,
        )
        assert result["status"] == "ok"
        assert result["stock_reconciliation_id"] == sr_id
        assert result["sle_entries_created"] >= 1

        # Verify the reconciliation is now submitted
        sr_row = fresh_db.execute(
            "SELECT status FROM stock_reconciliation WHERE id = ?",
            (sr_id,),
        ).fetchone()
        assert sr_row["status"] == "submitted"

        # Verify SLE entries exist for this reconciliation
        sle_rows = fresh_db.execute(
            """SELECT * FROM stock_ledger_entry
               WHERE voucher_type = 'stock_reconciliation' AND voucher_id = ?
               AND is_cancelled = 0""",
            (sr_id,),
        ).fetchall()
        assert len(sle_rows) >= 1

        # The SLE should reflect the +20 qty difference
        sle = dict(sle_rows[0])
        assert sle["item_id"] == env["item_id"]
        assert sle["warehouse_id"] == env["warehouse_id"]
        assert float(sle["actual_qty"]) == 20.0

        # Verify the stock balance is now correct
        balance_result = _call_action(
            ACTIONS["get-stock-balance"], fresh_db,
            item_id=env["item_id"],
            warehouse_id=env["warehouse_id"],
        )
        assert balance_result["status"] == "ok"
        assert balance_result["qty"] == "20.00"

    def test_submit_reconciliation_already_submitted(self, fresh_db):
        """Submitting an already-submitted reconciliation should return an error."""
        env = setup_inventory_environment(fresh_db)

        items_json = json.dumps([{
            "item_id": env["item_id"],
            "warehouse_id": env["warehouse_id"],
            "qty": "10",
            "valuation_rate": "25.00",
        }])

        add_result = _call_action(
            ACTIONS["add-stock-reconciliation"], fresh_db,
            posting_date="2026-02-16",
            items=items_json,
            company_id=env["company_id"],
        )
        assert add_result["status"] == "ok"
        sr_id = add_result["stock_reconciliation_id"]

        # First submit should succeed
        result1 = _call_action(
            ACTIONS["submit-stock-reconciliation"], fresh_db,
            stock_reconciliation_id=sr_id,
        )
        assert result1["status"] == "ok"

        # Second submit should fail
        result2 = _call_action(
            ACTIONS["submit-stock-reconciliation"], fresh_db,
            stock_reconciliation_id=sr_id,
        )
        assert result2["status"] == "error"
        assert "draft" in result2["message"].lower() or "submitted" in result2["message"].lower()

    def test_submit_reconciliation_not_found(self, fresh_db):
        """Submitting a non-existent reconciliation ID should return an error."""
        fake_id = str(uuid.uuid4())

        result = _call_action(
            ACTIONS["submit-stock-reconciliation"], fresh_db,
            stock_reconciliation_id=fake_id,
        )
        assert result["status"] == "error"
        assert "not found" in result["message"].lower()
