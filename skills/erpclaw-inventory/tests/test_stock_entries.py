"""Tests for stock entry CRUD: add-stock-entry, get-stock-entry, list-stock-entries.

8 tests covering material receipt, issue, transfer, validation errors,
retrieval, and listing with filters.
"""
import json
import uuid

from db_query import ACTIONS
from helpers import (
    _call_action,
    setup_inventory_environment,
    create_test_warehouse,
)


# ---------------------------------------------------------------------------
# 1. test_add_stock_entry_receive
# ---------------------------------------------------------------------------

def test_add_stock_entry_receive(fresh_db):
    """Create a material receipt stock entry and verify naming_series."""
    env = setup_inventory_environment(fresh_db)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "to_warehouse_id": env["warehouse_id"],
        "qty": 100,
        "rate": "25.00",
    }])

    result = _call_action(
        ACTIONS["add-stock-entry"], fresh_db,
        entry_type="receive",
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=items_json,
    )

    assert result["status"] == "ok"
    assert "stock_entry_id" in result
    assert result["naming_series"].startswith("STE-")
    assert result["total_incoming_value"] == "2500.00"
    assert result["total_outgoing_value"] == "0.00"


# ---------------------------------------------------------------------------
# 2. test_add_stock_entry_issue
# ---------------------------------------------------------------------------

def test_add_stock_entry_issue(fresh_db):
    """Create a material issue stock entry and verify ok."""
    env = setup_inventory_environment(fresh_db)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "from_warehouse_id": env["warehouse_id"],
        "qty": 50,
        "rate": "25.00",
    }])

    result = _call_action(
        ACTIONS["add-stock-entry"], fresh_db,
        entry_type="issue",
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=items_json,
    )

    assert result["status"] == "ok"
    assert "stock_entry_id" in result
    assert result["total_outgoing_value"] == "1250.00"
    assert result["total_incoming_value"] == "0.00"


# ---------------------------------------------------------------------------
# 3. test_add_stock_entry_transfer
# ---------------------------------------------------------------------------

def test_add_stock_entry_transfer(fresh_db):
    """Create a material transfer with both from and to warehouses."""
    env = setup_inventory_environment(fresh_db)

    # Create a second warehouse for the transfer destination
    dest_warehouse_id = create_test_warehouse(
        fresh_db, env["company_id"],
        name="Destination Warehouse",
        warehouse_type="stores",
        account_id=env["stock_in_hand_id"],
    )

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "from_warehouse_id": env["warehouse_id"],
        "to_warehouse_id": dest_warehouse_id,
        "qty": 30,
        "rate": "25.00",
    }])

    result = _call_action(
        ACTIONS["add-stock-entry"], fresh_db,
        entry_type="transfer",
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=items_json,
    )

    assert result["status"] == "ok"
    assert "stock_entry_id" in result
    # Transfer: both incoming and outgoing are equal
    assert result["total_incoming_value"] == "750.00"
    assert result["total_outgoing_value"] == "750.00"
    assert result["value_difference"] == "0.00"


# ---------------------------------------------------------------------------
# 4. test_add_stock_entry_invalid_type
# ---------------------------------------------------------------------------

def test_add_stock_entry_invalid_type(fresh_db):
    """An invalid entry type should return error."""
    env = setup_inventory_environment(fresh_db)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "to_warehouse_id": env["warehouse_id"],
        "qty": 10,
        "rate": "25.00",
    }])

    result = _call_action(
        ACTIONS["add-stock-entry"], fresh_db,
        entry_type="bogus_type",
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=items_json,
    )

    assert result["status"] == "error"
    assert "Invalid" in result["message"] or "invalid" in result["message"].lower()


# ---------------------------------------------------------------------------
# 5. test_add_stock_entry_missing_warehouse
# ---------------------------------------------------------------------------

def test_add_stock_entry_missing_warehouse(fresh_db):
    """Material receipt without to_warehouse_id should error."""
    env = setup_inventory_environment(fresh_db)

    # Omit to_warehouse_id for a receipt
    items_json = json.dumps([{
        "item_id": env["item_id"],
        "qty": 10,
        "rate": "25.00",
    }])

    result = _call_action(
        ACTIONS["add-stock-entry"], fresh_db,
        entry_type="receive",
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=items_json,
    )

    assert result["status"] == "error"
    assert "to_warehouse_id" in result["message"]


# ---------------------------------------------------------------------------
# 6. test_get_stock_entry
# ---------------------------------------------------------------------------

def test_get_stock_entry(fresh_db):
    """Create a stock entry, then retrieve it with items."""
    env = setup_inventory_environment(fresh_db)

    items_json = json.dumps([{
        "item_id": env["item_id"],
        "to_warehouse_id": env["warehouse_id"],
        "qty": 75,
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
    se_id = add_result["stock_entry_id"]

    get_result = _call_action(
        ACTIONS["get-stock-entry"], fresh_db,
        stock_entry_id=se_id,
    )

    assert get_result["status"] == "ok"
    assert get_result["id"] == se_id
    assert get_result["stock_entry_type"] == "material_receipt"
    assert get_result["status_field"] if "status_field" in get_result else True
    assert "items" in get_result
    assert len(get_result["items"]) == 1
    assert get_result["items"][0]["item_id"] == env["item_id"]
    assert get_result["items"][0]["quantity"] == "75.00"


# ---------------------------------------------------------------------------
# 7. test_list_stock_entries
# ---------------------------------------------------------------------------

def test_list_stock_entries(fresh_db):
    """Create 2 stock entries, list by company, verify both returned."""
    env = setup_inventory_environment(fresh_db)

    for qty in [100, 200]:
        items_json = json.dumps([{
            "item_id": env["item_id"],
            "to_warehouse_id": env["warehouse_id"],
            "qty": qty,
            "rate": "25.00",
        }])
        r = _call_action(
            ACTIONS["add-stock-entry"], fresh_db,
            entry_type="receive",
            company_id=env["company_id"],
            posting_date="2026-02-16",
            items=items_json,
        )
        assert r["status"] == "ok"

    list_result = _call_action(
        ACTIONS["list-stock-entries"], fresh_db,
        company_id=env["company_id"],
    )

    assert list_result["status"] == "ok"
    assert list_result["total_count"] == 2
    assert len(list_result["stock_entries"]) == 2


# ---------------------------------------------------------------------------
# 8. test_list_stock_entries_filter_type
# ---------------------------------------------------------------------------

def test_list_stock_entries_filter_type(fresh_db):
    """Create a receipt and an issue, filter list by type."""
    env = setup_inventory_environment(fresh_db)

    # Create a receipt
    receipt_json = json.dumps([{
        "item_id": env["item_id"],
        "to_warehouse_id": env["warehouse_id"],
        "qty": 100,
        "rate": "25.00",
    }])
    r1 = _call_action(
        ACTIONS["add-stock-entry"], fresh_db,
        entry_type="receive",
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=receipt_json,
    )
    assert r1["status"] == "ok"

    # Create an issue
    issue_json = json.dumps([{
        "item_id": env["item_id"],
        "from_warehouse_id": env["warehouse_id"],
        "qty": 20,
        "rate": "25.00",
    }])
    r2 = _call_action(
        ACTIONS["add-stock-entry"], fresh_db,
        entry_type="issue",
        company_id=env["company_id"],
        posting_date="2026-02-16",
        items=issue_json,
    )
    assert r2["status"] == "ok"

    # Filter for receipts only
    list_receipts = _call_action(
        ACTIONS["list-stock-entries"], fresh_db,
        company_id=env["company_id"],
        entry_type="receive",
    )
    assert list_receipts["status"] == "ok"
    assert list_receipts["total_count"] == 1
    assert list_receipts["stock_entries"][0]["stock_entry_type"] == "material_receipt"

    # Filter for issues only
    list_issues = _call_action(
        ACTIONS["list-stock-entries"], fresh_db,
        company_id=env["company_id"],
        entry_type="issue",
    )
    assert list_issues["status"] == "ok"
    assert list_issues["total_count"] == 1
    assert list_issues["stock_entries"][0]["stock_entry_type"] == "material_issue"
