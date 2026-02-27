"""Tests for item master data actions: add-item, update-item, get-item, list-items, import-items."""
import os
import pytest
from helpers import _call_action, create_test_item
from db_query import ACTIONS


class TestAddItem:
    """Tests for the add-item action."""

    def test_add_item_basic(self, fresh_db):
        """Create an item with minimal required fields and verify success."""
        result = _call_action(
            ACTIONS["add-item"], fresh_db,
            item_code="WIDGET-001",
            item_name="Basic Widget",
        )
        assert result["status"] == "ok"
        assert result["item_code"] == "WIDGET-001"
        assert result["item_name"] == "Basic Widget"
        assert "item_id" in result

        # Verify the item was actually persisted in the database
        row = fresh_db.execute(
            "SELECT * FROM item WHERE id = ?", (result["item_id"],)
        ).fetchone()
        assert row is not None
        assert row["item_code"] == "WIDGET-001"
        assert row["item_name"] == "Basic Widget"
        assert row["item_type"] == "stock"  # default
        assert row["stock_uom"] == "Nos"  # default
        assert row["valuation_method"] == "moving_average"  # default
        assert row["status"] == "active"

    def test_add_item_with_options(self, fresh_db):
        """Create an item with batch tracking, serial tracking, and a custom rate."""
        result = _call_action(
            ACTIONS["add-item"], fresh_db,
            item_code="BATCH-SERIAL-001",
            item_name="Tracked Widget",
            has_batch="1",
            has_serial="1",
            standard_rate="50.00",
            stock_uom="Each",
            valuation_method="fifo",
            item_type="stock",
        )
        assert result["status"] == "ok"
        assert result["item_code"] == "BATCH-SERIAL-001"

        row = fresh_db.execute(
            "SELECT * FROM item WHERE id = ?", (result["item_id"],)
        ).fetchone()
        assert row["has_batch"] == 1
        assert row["has_serial"] == 1
        assert row["standard_rate"] == "50.00"
        assert row["stock_uom"] == "Each"
        assert row["valuation_method"] == "fifo"

    def test_add_item_duplicate_code(self, fresh_db):
        """Creating two items with the same item_code should return an error."""
        result1 = _call_action(
            ACTIONS["add-item"], fresh_db,
            item_code="DUP-001",
            item_name="First Widget",
        )
        assert result1["status"] == "ok"

        result2 = _call_action(
            ACTIONS["add-item"], fresh_db,
            item_code="DUP-001",
            item_name="Second Widget",
        )
        assert result2["status"] == "error"
        assert "failed" in result2["message"].lower() or "integrity" in result2["message"].lower()


class TestUpdateItem:
    """Tests for the update-item action."""

    def test_update_item(self, fresh_db):
        """Update item_name and reorder_level on an existing item."""
        item_id = create_test_item(fresh_db, item_code="UPD-001", item_name="Old Name")

        result = _call_action(
            ACTIONS["update-item"], fresh_db,
            item_id=item_id,
            item_name="New Name",
            reorder_level="10",
        )
        assert result["status"] == "ok"
        assert result["item_id"] == item_id
        assert "item_name" in result["updated_fields"]
        assert "reorder_level" in result["updated_fields"]

        # Verify in database
        row = fresh_db.execute(
            "SELECT * FROM item WHERE id = ?", (item_id,)
        ).fetchone()
        assert row["item_name"] == "New Name"
        assert row["reorder_level"] == "10"

    def test_update_item_not_found(self, fresh_db):
        """Updating a non-existent item should return an error."""
        result = _call_action(
            ACTIONS["update-item"], fresh_db,
            item_id="00000000-0000-0000-0000-000000000000",
            item_name="Ghost Item",
        )
        assert result["status"] == "error"
        assert "not found" in result["message"].lower()


class TestGetItem:
    """Tests for the get-item action."""

    def test_get_item(self, fresh_db):
        """Create an item, then retrieve it and verify all returned fields."""
        item_id = create_test_item(
            fresh_db,
            item_code="GET-001",
            item_name="Gettable Widget",
            item_type="stock",
            stock_uom="Each",
            valuation_method="moving_average",
            standard_rate="75.00",
            has_batch=1,
            has_serial=0,
        )

        result = _call_action(
            ACTIONS["get-item"], fresh_db,
            item_id=item_id,
        )
        assert result["status"] == "ok"
        assert result["id"] == item_id
        assert result["item_code"] == "GET-001"
        assert result["item_name"] == "Gettable Widget"
        assert result["item_type"] == "stock"
        assert result["stock_uom"] == "Each"
        assert result["valuation_method"] == "moving_average"
        assert result["standard_rate"] == "75.00"
        assert result["has_batch"] == 1
        assert result["has_serial"] == 0
        assert result["status"] == "ok"
        # Stock balances should be empty (no SLE entries yet)
        assert result["stock_balances"] == []
        assert result["total_qty"] == "0.00"
        assert result["total_stock_value"] == "0.00"


class TestListItems:
    """Tests for the list-items action."""

    def test_list_items_empty(self, fresh_db):
        """Listing items when none exist should return an empty list."""
        result = _call_action(
            ACTIONS["list-items"], fresh_db,
        )
        assert result["status"] == "ok"
        assert result["items"] == []
        assert result["total_count"] == 0

    def test_list_items_with_search(self, fresh_db):
        """Create two items and search for one by name fragment."""
        create_test_item(fresh_db, item_code="ALPHA-001", item_name="Alpha Widget")
        create_test_item(fresh_db, item_code="BETA-002", item_name="Beta Gadget")

        # Search for "Alpha" -- should match only the first item
        result = _call_action(
            ACTIONS["list-items"], fresh_db,
            search="Alpha",
        )
        assert result["status"] == "ok"
        assert result["total_count"] == 1
        assert len(result["items"]) == 1
        assert result["items"][0]["item_code"] == "ALPHA-001"
        assert result["items"][0]["item_name"] == "Alpha Widget"

        # Search for "Widget" -- should match only the first item
        # (second item is "Beta Gadget", not a widget)
        result2 = _call_action(
            ACTIONS["list-items"], fresh_db,
            search="Widget",
        )
        assert result2["status"] == "ok"
        assert result2["total_count"] == 1
        assert result2["items"][0]["item_code"] == "ALPHA-001"


class TestImportItems:
    """Tests for the import-items action (S37 bug fix: no company_id on item)."""

    def test_import_items_basic(self, fresh_db, tmp_path):
        """Import items from CSV without company_id."""
        csv_file = tmp_path / "items.csv"
        csv_file.write_text("item_code,name,uom\nCSV-001,Widget Alpha,Nos\nCSV-002,Widget Beta,Kg\n")

        result = _call_action(
            ACTIONS["import-items"], fresh_db,
            csv_path=str(csv_file),
        )
        assert result["status"] == "ok"
        assert result["imported"] == 2
        assert result["skipped"] == 0

        # Verify items in DB
        rows = fresh_db.execute("SELECT item_code FROM item ORDER BY item_code").fetchall()
        codes = [r["item_code"] for r in rows]
        assert "CSV-001" in codes
        assert "CSV-002" in codes

    def test_import_items_skip_duplicates(self, fresh_db, tmp_path):
        """Duplicate item_codes are skipped."""
        # Pre-create an item
        create_test_item(fresh_db, item_code="DUP-001", item_name="Existing")

        csv_file = tmp_path / "items.csv"
        csv_file.write_text("item_code,name,uom\nDUP-001,Duplicate,Nos\nNEW-001,New Item,Nos\n")

        result = _call_action(
            ACTIONS["import-items"], fresh_db,
            csv_path=str(csv_file),
        )
        assert result["status"] == "ok"
        assert result["imported"] == 1
        assert result["skipped"] == 1
