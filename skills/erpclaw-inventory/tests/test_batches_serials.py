"""Tests for batch and serial number actions:
add-batch, list-batches, add-serial-number, list-serial-numbers."""
import pytest
from helpers import _call_action, create_test_item
from db_query import ACTIONS


class TestAddBatch:
    """Tests for the add-batch action."""

    def test_add_batch(self, fresh_db):
        """Create a batch for an item and verify success."""
        item_id = create_test_item(
            fresh_db, item_code="BATCH-ITEM-001", item_name="Batch Item",
            has_batch=1,
        )

        result = _call_action(
            ACTIONS["add-batch"], fresh_db,
            item_id=item_id,
            batch_name="BATCH-2026-001",
        )
        assert result["status"] == "ok"
        assert result["batch_name"] == "BATCH-2026-001"
        assert "batch_id" in result

        # Verify persisted in database
        row = fresh_db.execute(
            "SELECT * FROM batch WHERE id = ?", (result["batch_id"],)
        ).fetchone()
        assert row is not None
        assert row["batch_name"] == "BATCH-2026-001"
        assert row["item_id"] == item_id

    def test_add_batch_duplicate_name(self, fresh_db):
        """Creating two batches with the same batch_name should fail (UNIQUE constraint)."""
        item_id = create_test_item(
            fresh_db, item_code="BATCH-DUP-001", item_name="Dup Batch Item",
            has_batch=1,
        )

        result1 = _call_action(
            ACTIONS["add-batch"], fresh_db,
            item_id=item_id,
            batch_name="DUP-BATCH-001",
        )
        assert result1["status"] == "ok"

        result2 = _call_action(
            ACTIONS["add-batch"], fresh_db,
            item_id=item_id,
            batch_name="DUP-BATCH-001",
        )
        assert result2["status"] == "error"
        assert "failed" in result2["message"].lower() or "integrity" in result2["message"].lower()


class TestListBatches:
    """Tests for the list-batches action."""

    def test_list_batches_by_item(self, fresh_db):
        """Create two batches for the same item and list filtered by item_id."""
        item_id = create_test_item(
            fresh_db, item_code="BATCH-LIST-001", item_name="Listed Batch Item",
            has_batch=1,
        )

        _call_action(
            ACTIONS["add-batch"], fresh_db,
            item_id=item_id,
            batch_name="BATCH-A",
        )
        _call_action(
            ACTIONS["add-batch"], fresh_db,
            item_id=item_id,
            batch_name="BATCH-B",
        )

        result = _call_action(
            ACTIONS["list-batches"], fresh_db,
            item_id=item_id,
        )
        assert result["status"] == "ok"
        batch_names = [b["batch_name"] for b in result["batches"]]
        assert "BATCH-A" in batch_names
        assert "BATCH-B" in batch_names
        assert len(result["batches"]) == 2


class TestAddSerialNumber:
    """Tests for the add-serial-number action."""

    def test_add_serial_number(self, fresh_db):
        """Create a serial number for an item and verify success."""
        item_id = create_test_item(
            fresh_db, item_code="SN-ITEM-001", item_name="Serial Item",
            has_serial=1,
        )

        result = _call_action(
            ACTIONS["add-serial-number"], fresh_db,
            item_id=item_id,
            serial_no="SN-2026-00001",
        )
        assert result["status"] == "ok"
        assert result["serial_no"] == "SN-2026-00001"
        assert "serial_number_id" in result

        # Verify persisted in database
        row = fresh_db.execute(
            "SELECT * FROM serial_number WHERE id = ?",
            (result["serial_number_id"],),
        ).fetchone()
        assert row is not None
        assert row["serial_no"] == "SN-2026-00001"
        assert row["item_id"] == item_id
        assert row["status"] == "active"

    def test_add_serial_duplicate(self, fresh_db):
        """Creating two serial numbers with the same serial_no should fail."""
        item_id = create_test_item(
            fresh_db, item_code="SN-DUP-001", item_name="Dup Serial Item",
            has_serial=1,
        )

        result1 = _call_action(
            ACTIONS["add-serial-number"], fresh_db,
            item_id=item_id,
            serial_no="DUP-SN-001",
        )
        assert result1["status"] == "ok"

        result2 = _call_action(
            ACTIONS["add-serial-number"], fresh_db,
            item_id=item_id,
            serial_no="DUP-SN-001",
        )
        assert result2["status"] == "error"
        assert "failed" in result2["message"].lower() or "integrity" in result2["message"].lower()


class TestListSerialNumbers:
    """Tests for the list-serial-numbers action."""

    def test_list_serial_numbers(self, fresh_db):
        """Create two serial numbers for the same item and list filtered by item_id."""
        item_id = create_test_item(
            fresh_db, item_code="SN-LIST-001", item_name="Listed Serial Item",
            has_serial=1,
        )

        _call_action(
            ACTIONS["add-serial-number"], fresh_db,
            item_id=item_id,
            serial_no="SN-AAA-001",
        )
        _call_action(
            ACTIONS["add-serial-number"], fresh_db,
            item_id=item_id,
            serial_no="SN-BBB-002",
        )

        result = _call_action(
            ACTIONS["list-serial-numbers"], fresh_db,
            item_id=item_id,
        )
        assert result["status"] == "ok"
        serial_nos = [sn["serial_no"] for sn in result["serial_numbers"]]
        assert "SN-AAA-001" in serial_nos
        assert "SN-BBB-002" in serial_nos
        assert len(result["serial_numbers"]) == 2
