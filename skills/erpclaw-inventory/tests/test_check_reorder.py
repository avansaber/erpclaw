"""Tests for check-reorder action."""
import json
from helpers import (
    _call_action,
    create_test_company,
    create_test_item,
    create_test_warehouse,
    create_test_naming_series,
    create_test_fiscal_year,
    create_test_account,
    create_test_cost_center,
    create_test_stock_entry,
    submit_test_stock_entry,
    setup_inventory_environment,
)
import db_query
from db_query import ACTIONS


def test_check_reorder_action_exists():
    """check-reorder is registered in the ACTIONS dict."""
    assert "check-reorder" in ACTIONS


def test_check_reorder_no_items(fresh_db):
    """No items with reorder levels returns empty result."""
    conn = fresh_db
    cid = create_test_company(conn)

    result = _call_action(ACTIONS["check-reorder"], conn, company_id=cid)
    assert result["status"] == "ok"
    assert result["items_below_reorder"] == 0
    assert result["items"] == []


def test_check_reorder_auto_detect_company(fresh_db):
    """Auto-detects company when not provided."""
    conn = fresh_db
    create_test_company(conn)

    result = _call_action(ACTIONS["check-reorder"], conn)
    assert result["status"] == "ok"
    assert result["items_below_reorder"] == 0


def test_check_reorder_item_below_level(fresh_db):
    """Item with zero stock and a reorder level is flagged."""
    conn = fresh_db
    env = setup_inventory_environment(conn)
    company_id = env["company_id"]
    item_id = env["item_id"]

    # Set reorder level on the item via update-item action
    _call_action(ACTIONS["update-item"], conn,
                 item_id=item_id, reorder_level="20", reorder_qty="50")

    result = _call_action(ACTIONS["check-reorder"], conn, company_id=company_id)
    assert result["status"] == "ok"
    assert result["items_below_reorder"] == 1
    assert result["items"][0]["item_id"] == item_id
    assert result["items"][0]["current_stock"] == "0.00"
    assert result["items"][0]["reorder_level"] == "20.00"
    assert result["items"][0]["reorder_qty"] == "50.00"
    assert result["items"][0]["shortfall"] == "20.00"


def test_check_reorder_item_above_level(fresh_db):
    """Item with stock above reorder level is not flagged."""
    conn = fresh_db
    env = setup_inventory_environment(conn)
    company_id = env["company_id"]
    item_id = env["item_id"]
    warehouse_id = env["warehouse_id"]

    # Set reorder level via update-item action
    _call_action(ACTIONS["update-item"], conn,
                 item_id=item_id, reorder_level="10", reorder_qty="50")

    # Receive 25 units into stock
    items_json = json.dumps([{
        "item_id": item_id,
        "qty": "25",
        "rate": "10.00",
        "to_warehouse_id": warehouse_id,
    }])
    se_id = create_test_stock_entry(conn, company_id, "receive", items_json)
    submit_result = submit_test_stock_entry(conn, se_id)
    assert submit_result["status"] == "ok"

    result = _call_action(ACTIONS["check-reorder"], conn, company_id=company_id)
    assert result["status"] == "ok"
    assert result["items_below_reorder"] == 0


def test_check_reorder_item_at_exact_level(fresh_db):
    """Item at exactly the reorder level IS flagged (stock <= reorder_level)."""
    conn = fresh_db
    env = setup_inventory_environment(conn)
    company_id = env["company_id"]
    item_id = env["item_id"]
    warehouse_id = env["warehouse_id"]

    # Set reorder level to 10 via update-item action
    _call_action(ACTIONS["update-item"], conn,
                 item_id=item_id, reorder_level="10", reorder_qty="30")

    # Receive exactly 10 units
    items_json = json.dumps([{
        "item_id": item_id,
        "qty": "10",
        "rate": "10.00",
        "to_warehouse_id": warehouse_id,
    }])
    se_id = create_test_stock_entry(conn, company_id, "receive", items_json)
    submit_result = submit_test_stock_entry(conn, se_id)
    assert submit_result["status"] == "ok"

    result = _call_action(ACTIONS["check-reorder"], conn, company_id=company_id)
    assert result["status"] == "ok"
    assert result["items_below_reorder"] == 1
    assert result["items"][0]["current_stock"] == "10.00"
    assert result["items"][0]["shortfall"] == "0.00"
