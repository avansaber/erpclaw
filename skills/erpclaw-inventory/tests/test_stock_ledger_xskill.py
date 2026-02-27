"""Tests for cross-skill SLE actions: create-stock-ledger-entries, reverse-stock-ledger-entries."""
import json
import uuid
import pytest
from helpers import _call_action, setup_inventory_environment
from db_query import ACTIONS


class TestCreateStockLedgerEntries:
    """Tests for the create-stock-ledger-entries action (cross-skill)."""

    def test_create_sle_entries(self, fresh_db):
        """Create SLE entries for a purchase_receipt voucher, verify rows exist."""
        env = setup_inventory_environment(fresh_db)
        voucher_id = str(uuid.uuid4())

        entries_json = json.dumps([{
            "item_id": env["item_id"],
            "warehouse_id": env["warehouse_id"],
            "actual_qty": "10",
            "incoming_rate": "25.00",
        }])

        result = _call_action(
            ACTIONS["create-stock-ledger-entries"], fresh_db,
            voucher_type="purchase_receipt",
            voucher_id=voucher_id,
            posting_date="2026-02-16",
            entries=entries_json,
            company_id=env["company_id"],
        )
        assert result["status"] == "ok"
        assert result["count"] == 1
        assert len(result["sle_ids"]) == 1

        # Verify the SLE row was persisted in the database
        sle_row = fresh_db.execute(
            "SELECT * FROM stock_ledger_entry WHERE id = ?",
            (result["sle_ids"][0],),
        ).fetchone()
        assert sle_row is not None
        assert sle_row["item_id"] == env["item_id"]
        assert sle_row["warehouse_id"] == env["warehouse_id"]
        assert sle_row["voucher_type"] == "purchase_receipt"
        assert sle_row["voucher_id"] == voucher_id
        assert sle_row["actual_qty"] == "10.00"
        assert sle_row["incoming_rate"] == "25.00"
        assert sle_row["is_cancelled"] == 0

    def test_create_sle_duplicate(self, fresh_db):
        """Same voucher_type + voucher_id called twice should not create duplicates.

        The shared lib's insert_sle_entries is expected to be idempotent or
        at minimum not raise an error on a second call. We verify the total
        SLE count for this voucher does not grow beyond the first call.
        """
        env = setup_inventory_environment(fresh_db)
        voucher_id = str(uuid.uuid4())

        entries_json = json.dumps([{
            "item_id": env["item_id"],
            "warehouse_id": env["warehouse_id"],
            "actual_qty": "5",
            "incoming_rate": "20.00",
        }])

        result1 = _call_action(
            ACTIONS["create-stock-ledger-entries"], fresh_db,
            voucher_type="purchase_receipt",
            voucher_id=voucher_id,
            posting_date="2026-02-16",
            entries=entries_json,
            company_id=env["company_id"],
        )
        assert result1["status"] == "ok"
        count_after_first = result1["count"]

        # Second call with same voucher
        result2 = _call_action(
            ACTIONS["create-stock-ledger-entries"], fresh_db,
            voucher_type="purchase_receipt",
            voucher_id=voucher_id,
            posting_date="2026-02-16",
            entries=entries_json,
            company_id=env["company_id"],
        )

        # Count total SLE rows for this voucher (non-cancelled)
        total = fresh_db.execute(
            """SELECT COUNT(*) as cnt FROM stock_ledger_entry
               WHERE voucher_type = 'purchase_receipt' AND voucher_id = ?
               AND is_cancelled = 0""",
            (voucher_id,),
        ).fetchone()["cnt"]

        # If second call succeeded, verify no extra rows were created;
        # if it returned an error (due to existing entries), that is also acceptable
        if result2["status"] == "ok":
            assert total == count_after_first
        else:
            # Error on duplicate is acceptable behavior
            assert total == count_after_first


class TestReverseStockLedgerEntries:
    """Tests for the reverse-stock-ledger-entries action (cross-skill)."""

    def test_reverse_sle_entries(self, fresh_db):
        """Create SLE entries, then reverse them. Verify is_cancelled flags."""
        env = setup_inventory_environment(fresh_db)
        voucher_id = str(uuid.uuid4())

        entries_json = json.dumps([{
            "item_id": env["item_id"],
            "warehouse_id": env["warehouse_id"],
            "actual_qty": "15",
            "incoming_rate": "30.00",
        }])

        # Create
        create_result = _call_action(
            ACTIONS["create-stock-ledger-entries"], fresh_db,
            voucher_type="purchase_receipt",
            voucher_id=voucher_id,
            posting_date="2026-02-16",
            entries=entries_json,
            company_id=env["company_id"],
        )
        assert create_result["status"] == "ok"
        original_sle_id = create_result["sle_ids"][0]

        # Reverse
        reverse_result = _call_action(
            ACTIONS["reverse-stock-ledger-entries"], fresh_db,
            voucher_type="purchase_receipt",
            voucher_id=voucher_id,
            posting_date="2026-02-16",
        )
        assert reverse_result["status"] == "ok"
        assert reverse_result["count"] >= 1

        # Verify original entry is flagged as cancelled
        original = fresh_db.execute(
            "SELECT is_cancelled FROM stock_ledger_entry WHERE id = ?",
            (original_sle_id,),
        ).fetchone()
        assert original["is_cancelled"] == 1

        # Verify reversal entries exist (negative qty to offset originals)
        reversal_rows = fresh_db.execute(
            """SELECT * FROM stock_ledger_entry
               WHERE voucher_type = 'purchase_receipt' AND voucher_id = ?
               AND id != ? AND is_cancelled = 0""",
            (voucher_id, original_sle_id),
        ).fetchall()
        # Reversal entries should have negative actual_qty
        for row in reversal_rows:
            assert float(row["actual_qty"]) < 0

    def test_reverse_nonexistent(self, fresh_db):
        """Reversing entries for a non-existent voucher should return zero reversals."""
        env = setup_inventory_environment(fresh_db)
        fake_voucher_id = str(uuid.uuid4())

        result = _call_action(
            ACTIONS["reverse-stock-ledger-entries"], fresh_db,
            voucher_type="purchase_receipt",
            voucher_id=fake_voucher_id,
            posting_date="2026-02-16",
        )

        # Should succeed with zero reversals (nothing to reverse)
        if result["status"] == "ok":
            assert result["count"] == 0
            assert result["reversal_ids"] == []
        else:
            # An error indicating nothing to reverse is also acceptable
            msg = result["message"].lower()
            assert "no" in msg or "not found" in msg or "reversal failed" in msg
