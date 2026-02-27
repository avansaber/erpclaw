"""Tests for stock report actions: get-stock-balance, stock-balance-report, stock-ledger-report."""
import json
import pytest
from helpers import (
    _call_action,
    create_test_item,
    create_test_stock_entry,
    setup_inventory_environment,
    submit_test_stock_entry,
)
from db_query import ACTIONS


class TestGetStockBalance:
    """Tests for the get-stock-balance action."""

    def test_get_stock_balance_empty(self, fresh_db):
        """With no stock entries submitted, balance should be zero."""
        env = setup_inventory_environment(fresh_db)

        result = _call_action(
            ACTIONS["get-stock-balance"], fresh_db,
            item_id=env["item_id"],
            warehouse_id=env["warehouse_id"],
        )
        assert result["status"] == "ok"
        assert result["item_id"] == env["item_id"]
        assert result["warehouse_id"] == env["warehouse_id"]
        assert result["qty"] == "0.00"
        assert result["stock_value"] == "0.00"

    def test_get_stock_balance_after_receipt(self, fresh_db):
        """Submit a material receipt, then verify qty and stock value."""
        env = setup_inventory_environment(fresh_db)

        # Create and submit a receipt for 100 units at $25 each
        items_json = json.dumps([{
            "item_id": env["item_id"],
            "to_warehouse_id": env["warehouse_id"],
            "qty": 100,
            "rate": "25.00",
        }])
        se_id = create_test_stock_entry(
            fresh_db, env["company_id"], "receive", items_json,
        )
        submit_result = submit_test_stock_entry(fresh_db, se_id)
        assert submit_result["status"] == "ok"

        # Check the balance
        result = _call_action(
            ACTIONS["get-stock-balance"], fresh_db,
            item_id=env["item_id"],
            warehouse_id=env["warehouse_id"],
        )
        assert result["status"] == "ok"
        assert result["qty"] == "100.00"
        assert result["valuation_rate"] == "25.00"
        assert result["stock_value"] == "2500.00"


class TestStockBalanceReport:
    """Tests for the stock-balance-report action."""

    def test_stock_balance_report(self, fresh_db):
        """Submit a receipt, then verify the report contains the item."""
        env = setup_inventory_environment(fresh_db)

        items_json = json.dumps([{
            "item_id": env["item_id"],
            "to_warehouse_id": env["warehouse_id"],
            "qty": 50,
            "rate": "25.00",
        }])
        se_id = create_test_stock_entry(
            fresh_db, env["company_id"], "receive", items_json,
        )
        submit_result = submit_test_stock_entry(fresh_db, se_id)
        assert submit_result["status"] == "ok"

        result = _call_action(
            ACTIONS["stock-balance-report"], fresh_db,
            company_id=env["company_id"],
        )
        assert result["status"] == "ok"
        assert result["row_count"] >= 1

        # Find our item in the report
        item_rows = [r for r in result["report"]
                     if r["item_id"] == env["item_id"]]
        assert len(item_rows) == 1
        assert item_rows[0]["qty"] == "50.00"
        assert item_rows[0]["valuation_rate"] == "25.00"
        assert item_rows[0]["stock_value"] == "1250.00"
        assert item_rows[0]["item_code"] == "SKU-001"
        assert item_rows[0]["item_name"] == "Widget A"
        assert item_rows[0]["warehouse_id"] == env["warehouse_id"]

        # Total stock value should match
        assert result["total_stock_value"] == "1250.00"

    def test_stock_balance_report_empty(self, fresh_db):
        """With no submitted entries, report should have 0 rows."""
        env = setup_inventory_environment(fresh_db)

        result = _call_action(
            ACTIONS["stock-balance-report"], fresh_db,
            company_id=env["company_id"],
        )
        assert result["status"] == "ok"
        assert result["row_count"] == 0
        assert result["report"] == []
        assert result["total_stock_value"] == "0.00"


class TestStockLedgerReport:
    """Tests for the stock-ledger-report action."""

    def test_stock_ledger_report(self, fresh_db):
        """Submit a receipt, verify SLE entries appear in the report."""
        env = setup_inventory_environment(fresh_db)

        items_json = json.dumps([{
            "item_id": env["item_id"],
            "to_warehouse_id": env["warehouse_id"],
            "qty": 30,
            "rate": "25.00",
        }])
        se_id = create_test_stock_entry(
            fresh_db, env["company_id"], "receive", items_json,
        )
        submit_result = submit_test_stock_entry(fresh_db, se_id)
        assert submit_result["status"] == "ok"

        result = _call_action(
            ACTIONS["stock-ledger-report"], fresh_db,
            item_id=env["item_id"],
        )
        assert result["status"] == "ok"
        assert result["count"] >= 1

        # Verify the SLE entry details
        entries = result["entries"]
        assert len(entries) >= 1
        sle = entries[0]
        assert sle["item_id"] == env["item_id"]
        assert sle["warehouse_id"] == env["warehouse_id"]
        assert sle["voucher_type"] == "stock_entry"
        assert sle["voucher_id"] == se_id
        assert sle["item_code"] == "SKU-001"
        assert sle["item_name"] == "Widget A"
        assert sle["is_cancelled"] == 0

    def test_stock_ledger_report_filtered(self, fresh_db):
        """Submit entries for 2 items, filter report by item_id."""
        env = setup_inventory_environment(fresh_db)

        # Create a second item
        item_id_2 = create_test_item(
            fresh_db, item_code="SKU-002", item_name="Widget B",
        )

        # Receipt for item 1: 20 units
        items_json_1 = json.dumps([{
            "item_id": env["item_id"],
            "to_warehouse_id": env["warehouse_id"],
            "qty": 20,
            "rate": "25.00",
        }])
        se_id_1 = create_test_stock_entry(
            fresh_db, env["company_id"], "receive", items_json_1,
        )
        submit_result_1 = submit_test_stock_entry(fresh_db, se_id_1)
        assert submit_result_1["status"] == "ok"

        # Receipt for item 2: 10 units
        items_json_2 = json.dumps([{
            "item_id": item_id_2,
            "to_warehouse_id": env["warehouse_id"],
            "qty": 10,
            "rate": "30.00",
        }])
        se_id_2 = create_test_stock_entry(
            fresh_db, env["company_id"], "receive", items_json_2,
        )
        submit_result_2 = submit_test_stock_entry(fresh_db, se_id_2)
        assert submit_result_2["status"] == "ok"

        # Filter by item 1 only
        result = _call_action(
            ACTIONS["stock-ledger-report"], fresh_db,
            item_id=env["item_id"],
        )
        assert result["status"] == "ok"
        # All returned entries should be for item 1 only
        for entry in result["entries"]:
            assert entry["item_id"] == env["item_id"]
        assert result["count"] >= 1

        # Filter by item 2 only
        result2 = _call_action(
            ACTIONS["stock-ledger-report"], fresh_db,
            item_id=item_id_2,
        )
        assert result2["status"] == "ok"
        for entry in result2["entries"]:
            assert entry["item_id"] == item_id_2
        assert result2["count"] >= 1

        # Unfiltered should include both items
        result_all = _call_action(
            ACTIONS["stock-ledger-report"], fresh_db,
        )
        assert result_all["status"] == "ok"
        all_item_ids = {e["item_id"] for e in result_all["entries"]}
        assert env["item_id"] in all_item_ids
        assert item_id_2 in all_item_ids
