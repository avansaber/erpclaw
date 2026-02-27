"""Tests for the status action: inventory summary for a company."""
import json
import pytest
from helpers import (
    _call_action,
    create_test_company,
    create_test_item,
    create_test_stock_entry,
    create_test_warehouse,
    setup_inventory_environment,
    submit_test_stock_entry,
)
from db_query import ACTIONS


class TestStatus:
    """Tests for the status action."""

    def test_status_empty(self, fresh_db):
        """Fresh DB with company but no stock entries; counts should be zero."""
        env = setup_inventory_environment(fresh_db)

        result = _call_action(
            ACTIONS["status"], fresh_db,
            company_id=env["company_id"],
        )
        assert result["status"] == "ok"

        # Items: setup_inventory_environment creates 1 item (SKU-001)
        assert result["items"] == 1

        # Warehouses: setup_inventory_environment creates 1 warehouse
        assert result["warehouses"] == 1

        # Stock entries: none created yet
        assert result["stock_entries"]["draft"] == 0
        assert result["stock_entries"]["submitted"] == 0
        assert result["stock_entries"]["cancelled"] == 0
        assert result["stock_entries"]["total"] == 0

        # Total stock value: zero (no SLE entries)
        assert result["total_stock_value"] == "0.00"

    def test_status_with_data(self, fresh_db):
        """Create items, warehouses, submit a stock entry, verify counts."""
        env = setup_inventory_environment(fresh_db)

        # Create additional items
        item_id_2 = create_test_item(
            fresh_db, item_code="SKU-002", item_name="Widget B",
        )
        item_id_3 = create_test_item(
            fresh_db, item_code="SKU-003", item_name="Widget C",
        )

        # Create an additional warehouse
        wh_id_2 = create_test_warehouse(
            fresh_db, env["company_id"], name="Secondary Warehouse",
            account_id=env["stock_in_hand_id"],
        )

        # Create and submit a stock entry (receipt of 50 units at $25)
        receipt_items = json.dumps([{
            "item_id": env["item_id"],
            "to_warehouse_id": env["warehouse_id"],
            "qty": 50,
            "rate": "25.00",
        }])
        se_id = create_test_stock_entry(
            fresh_db, env["company_id"], "receive", receipt_items,
        )
        submit_result = submit_test_stock_entry(fresh_db, se_id)
        assert submit_result["status"] == "ok"

        # Create a draft stock entry (not submitted)
        draft_items = json.dumps([{
            "item_id": item_id_2,
            "to_warehouse_id": wh_id_2,
            "qty": 10,
            "rate": "30.00",
        }])
        draft_se_id = create_test_stock_entry(
            fresh_db, env["company_id"], "receive", draft_items,
        )

        # Get status
        result = _call_action(
            ACTIONS["status"], fresh_db,
            company_id=env["company_id"],
        )
        assert result["status"] == "ok"

        # Items: 3 total (SKU-001, SKU-002, SKU-003)
        assert result["items"] == 3

        # Warehouses: 2 total (Main Warehouse + Secondary Warehouse)
        assert result["warehouses"] == 2

        # Stock entries: 1 submitted + 1 draft = 2 total
        assert result["stock_entries"]["submitted"] == 1
        assert result["stock_entries"]["draft"] == 1
        assert result["stock_entries"]["cancelled"] == 0
        assert result["stock_entries"]["total"] == 2

        # Total stock value: 50 * 25 = 1250 from the submitted entry
        # (draft entries do not affect stock value since no SLE is posted)
        total_value = float(result["total_stock_value"])
        assert total_value > 0
