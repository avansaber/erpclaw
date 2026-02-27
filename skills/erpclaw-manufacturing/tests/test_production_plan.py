"""Tests for production planning and MRP actions.

8 tests covering: create-production-plan, run-mrp (with and without stock),
get-production-plan, generate-work-orders (and back-links),
generate-purchase-requests, and validation.
"""
import json
import pytest

from db_query import ACTIONS
from helpers import (
    _call_action,
    setup_manufacturing_environment,
    create_test_bom,
)


# ---------------------------------------------------------------------------
# 1. test_create_production_plan
# ---------------------------------------------------------------------------

def test_create_production_plan(fresh_db):
    """Create plan with 1 item: verify plan_id, naming_series, and plan item row."""
    env = setup_manufacturing_environment(fresh_db)
    bom_id = create_test_bom(fresh_db, env)

    plan_items = json.dumps([{
        "item_id": env["fg_id"],
        "bom_id": bom_id,
        "planned_qty": "10",
    }])

    result = _call_action(
        ACTIONS["create-production-plan"], fresh_db,
        company_id=env["company_id"],
        items=plan_items,
    )

    assert result["status"] == "ok"
    assert "production_plan_id" in result
    assert result["naming_series"].startswith("PP-")
    assert result["item_count"] == 1
    assert result["status"] != "error"

    plan_id = result["production_plan_id"]

    # Verify production_plan row in DB
    plan = fresh_db.execute(
        "SELECT * FROM production_plan WHERE id = ?", (plan_id,),
    ).fetchone()
    assert plan is not None
    assert plan["status"] == "draft"

    # Verify production_plan_item row exists
    plan_item = fresh_db.execute(
        "SELECT * FROM production_plan_item WHERE production_plan_id = ?",
        (plan_id,),
    ).fetchone()
    assert plan_item is not None
    assert plan_item["item_id"] == env["fg_id"]
    assert plan_item["bom_id"] == bom_id
    assert float(plan_item["planned_qty"]) == 10.0


# ---------------------------------------------------------------------------
# 2. test_run_mrp
# ---------------------------------------------------------------------------

def test_run_mrp(fresh_db):
    """Create plan, run MRP: verify materials created with calculated quantities."""
    env = setup_manufacturing_environment(fresh_db)
    bom_id = create_test_bom(fresh_db, env)

    plan_items = json.dumps([{
        "item_id": env["fg_id"],
        "bom_id": bom_id,
        "planned_qty": "10",
    }])

    plan_result = _call_action(
        ACTIONS["create-production-plan"], fresh_db,
        company_id=env["company_id"],
        items=plan_items,
    )
    assert plan_result["status"] == "ok"
    plan_id = plan_result["production_plan_id"]

    # Run MRP
    mrp_result = _call_action(
        ACTIONS["run-mrp"], fresh_db,
        production_plan_id=plan_id,
    )

    assert mrp_result["status"] == "ok"
    assert mrp_result["material_count"] > 0

    # Verify production_plan_material rows created
    materials = fresh_db.execute(
        "SELECT * FROM production_plan_material WHERE production_plan_id = ?",
        (plan_id,),
    ).fetchall()
    assert len(materials) > 0

    # Each material should have required_qty > 0 (from BOM explosion)
    for mat in materials:
        assert float(mat["required_qty"]) > 0
        # available_qty and shortfall_qty should be set
        assert mat["available_qty"] is not None
        assert mat["shortfall_qty"] is not None
        # shortfall = max(0, required - available - on_order)
        required = float(mat["required_qty"])
        available = float(mat["available_qty"])
        on_order = float(mat["on_order_qty"])
        shortfall = float(mat["shortfall_qty"])
        expected_shortfall = max(0, required - available - on_order)
        assert abs(shortfall - expected_shortfall) < 0.01, (
            f"shortfall mismatch: expected {expected_shortfall}, got {shortfall}"
        )

    # Plan status should now be "submitted" after MRP
    plan = fresh_db.execute(
        "SELECT status FROM production_plan WHERE id = ?", (plan_id,),
    ).fetchone()
    assert plan["status"] == "submitted"


# ---------------------------------------------------------------------------
# 3. test_run_mrp_with_stock
# ---------------------------------------------------------------------------

def test_run_mrp_with_stock(fresh_db):
    """When sufficient stock exists, shortfall_qty should be 0."""
    env = setup_manufacturing_environment(fresh_db)
    bom_id = create_test_bom(fresh_db, env)

    # The setup_manufacturing_environment already stocks raw materials
    # (source warehouse has sufficient RM stock).
    # Plan for small quantity (5 units) so RM needs are within available stock.
    plan_items = json.dumps([{
        "item_id": env["fg_id"],
        "bom_id": bom_id,
        "planned_qty": "5",
        "warehouse_id": env["source_wh_id"],
    }])

    plan_result = _call_action(
        ACTIONS["create-production-plan"], fresh_db,
        company_id=env["company_id"],
        items=plan_items,
    )
    assert plan_result["status"] == "ok"
    plan_id = plan_result["production_plan_id"]

    mrp_result = _call_action(
        ACTIONS["run-mrp"], fresh_db,
        production_plan_id=plan_id,
    )
    assert mrp_result["status"] == "ok"

    # All materials should have shortfall = 0 since stock is sufficient
    materials = fresh_db.execute(
        "SELECT * FROM production_plan_material WHERE production_plan_id = ?",
        (plan_id,),
    ).fetchall()
    assert len(materials) > 0

    for mat in materials:
        shortfall = float(mat["shortfall_qty"])
        assert shortfall == 0.0, (
            f"Item {mat['item_id']}: expected shortfall=0, got {shortfall}. "
            f"required={mat['required_qty']}, available={mat['available_qty']}, "
            f"on_order={mat['on_order_qty']}"
        )

    assert mrp_result["total_shortfall_items"] == 0


# ---------------------------------------------------------------------------
# 4. test_get_production_plan
# ---------------------------------------------------------------------------

def test_get_production_plan(fresh_db):
    """After creating + running MRP: verify returns plan + items + materials."""
    env = setup_manufacturing_environment(fresh_db)
    bom_id = create_test_bom(fresh_db, env)

    plan_items = json.dumps([{
        "item_id": env["fg_id"],
        "bom_id": bom_id,
        "planned_qty": "10",
    }])

    plan_result = _call_action(
        ACTIONS["create-production-plan"], fresh_db,
        company_id=env["company_id"],
        items=plan_items,
    )
    plan_id = plan_result["production_plan_id"]

    # Run MRP to populate materials
    _call_action(ACTIONS["run-mrp"], fresh_db, production_plan_id=plan_id)

    # Get production plan
    get_result = _call_action(
        ACTIONS["get-production-plan"], fresh_db,
        production_plan_id=plan_id,
    )

    assert get_result["status"] == "ok"
    assert get_result["id"] == plan_id

    # Verify items list returned (with item names from JOIN)
    assert "items" in get_result
    assert len(get_result["items"]) >= 1
    plan_item = get_result["items"][0]
    assert plan_item["item_id"] == env["fg_id"]
    assert "item_name" in plan_item or "item_code" in plan_item
    assert "bom_naming_series" in plan_item

    # Verify materials list returned (with shortfall info)
    assert "materials" in get_result
    assert len(get_result["materials"]) >= 1
    for mat in get_result["materials"]:
        assert "item_id" in mat
        assert "required_qty" in mat
        assert "shortfall_qty" in mat

    # Summary field
    assert "total_shortfall_items" in get_result


# ---------------------------------------------------------------------------
# 5. test_generate_work_orders
# ---------------------------------------------------------------------------

def test_generate_work_orders(fresh_db):
    """After MRP, generate-work-orders creates a WO and links it to plan item."""
    env = setup_manufacturing_environment(fresh_db)
    bom_id = create_test_bom(fresh_db, env)

    plan_items = json.dumps([{
        "item_id": env["fg_id"],
        "bom_id": bom_id,
        "planned_qty": "10",
    }])

    plan_result = _call_action(
        ACTIONS["create-production-plan"], fresh_db,
        company_id=env["company_id"],
        items=plan_items,
    )
    plan_id = plan_result["production_plan_id"]

    # Run MRP (transitions plan to "submitted")
    _call_action(ACTIONS["run-mrp"], fresh_db, production_plan_id=plan_id)

    # Generate work orders
    gen_result = _call_action(
        ACTIONS["generate-work-orders"], fresh_db,
        production_plan_id=plan_id,
    )

    assert gen_result["status"] == "ok"
    assert gen_result["work_orders_created"] >= 1
    assert "work_order_ids" in gen_result
    assert len(gen_result["work_order_ids"]) >= 1

    # Verify the WO exists in DB
    wo_id = gen_result["work_order_ids"][0]
    wo = fresh_db.execute(
        "SELECT * FROM work_order WHERE id = ?", (wo_id,),
    ).fetchone()
    assert wo is not None
    assert wo["status"] == "draft"
    assert wo["bom_id"] == bom_id

    # Verify production_plan_item.work_order_id is set
    plan_item = fresh_db.execute(
        "SELECT * FROM production_plan_item WHERE production_plan_id = ?",
        (plan_id,),
    ).fetchone()
    assert plan_item["work_order_id"] == wo_id


# ---------------------------------------------------------------------------
# 6. test_generate_work_orders_links_back
# ---------------------------------------------------------------------------

def test_generate_work_orders_links_back(fresh_db):
    """After generating WOs, verify each plan item has work_order_id referencing correct BOM."""
    env = setup_manufacturing_environment(fresh_db)
    bom_id = create_test_bom(fresh_db, env)

    plan_items = json.dumps([{
        "item_id": env["fg_id"],
        "bom_id": bom_id,
        "planned_qty": "20",
    }])

    plan_result = _call_action(
        ACTIONS["create-production-plan"], fresh_db,
        company_id=env["company_id"],
        items=plan_items,
    )
    plan_id = plan_result["production_plan_id"]

    _call_action(ACTIONS["run-mrp"], fresh_db, production_plan_id=plan_id)

    gen_result = _call_action(
        ACTIONS["generate-work-orders"], fresh_db,
        production_plan_id=plan_id,
    )
    assert gen_result["status"] == "ok"

    # Verify each production_plan_item has work_order_id
    plan_items_rows = fresh_db.execute(
        "SELECT * FROM production_plan_item WHERE production_plan_id = ?",
        (plan_id,),
    ).fetchall()

    for pi in plan_items_rows:
        assert pi["work_order_id"] is not None, (
            f"Plan item {pi['id']} should have work_order_id set"
        )

        # Verify the WO references the correct BOM
        wo = fresh_db.execute(
            "SELECT bom_id FROM work_order WHERE id = ?",
            (pi["work_order_id"],),
        ).fetchone()
        assert wo is not None
        assert wo["bom_id"] == pi["bom_id"], (
            f"WO bom_id ({wo['bom_id']}) should match plan item bom_id ({pi['bom_id']})"
        )

    # Calling generate-work-orders again should create 0 (already all linked)
    gen_result2 = _call_action(
        ACTIONS["generate-work-orders"], fresh_db,
        production_plan_id=plan_id,
    )
    assert gen_result2["status"] == "ok"
    assert gen_result2["work_orders_created"] == 0


# ---------------------------------------------------------------------------
# 7. test_generate_purchase_requests
# ---------------------------------------------------------------------------

def test_generate_purchase_requests(fresh_db):
    """When shortfall exists, generate-purchase-requests returns shortfall items."""
    env = setup_manufacturing_environment(fresh_db)
    bom_id = create_test_bom(fresh_db, env)

    # Plan for very large quantity so shortfall is guaranteed
    # (stock from setup_manufacturing_environment won't be enough)
    plan_items = json.dumps([{
        "item_id": env["fg_id"],
        "bom_id": bom_id,
        "planned_qty": "10000",
    }])

    plan_result = _call_action(
        ACTIONS["create-production-plan"], fresh_db,
        company_id=env["company_id"],
        items=plan_items,
    )
    plan_id = plan_result["production_plan_id"]

    # Run MRP
    mrp_result = _call_action(
        ACTIONS["run-mrp"], fresh_db,
        production_plan_id=plan_id,
    )
    assert mrp_result["status"] == "ok"
    assert mrp_result["total_shortfall_items"] > 0

    # Generate purchase requests
    pr_result = _call_action(
        ACTIONS["generate-purchase-requests"], fresh_db,
        production_plan_id=plan_id,
    )

    assert pr_result["status"] == "ok"
    assert pr_result["shortfall_item_count"] > 0
    assert "purchase_requests" in pr_result
    assert len(pr_result["purchase_requests"]) > 0

    # Each purchase request item should have shortfall > 0
    for pr_item in pr_result["purchase_requests"]:
        assert "item_id" in pr_item
        assert "shortfall_qty" in pr_item
        assert float(pr_item["shortfall_qty"]) > 0
        assert "required_qty" in pr_item
        assert "available_qty" in pr_item


# ---------------------------------------------------------------------------
# 8. test_create_production_plan_validates
# ---------------------------------------------------------------------------

def test_create_production_plan_validates(fresh_db):
    """Invalid BOM in items should return error."""
    env = setup_manufacturing_environment(fresh_db)

    # Use a non-existent BOM ID
    fake_bom_id = "00000000-0000-0000-0000-000000000000"
    plan_items = json.dumps([{
        "item_id": env["fg_id"],
        "bom_id": fake_bom_id,
        "planned_qty": "10",
    }])

    result = _call_action(
        ACTIONS["create-production-plan"], fresh_db,
        company_id=env["company_id"],
        items=plan_items,
    )

    assert result["status"] == "error"
    assert "not found" in result["message"].lower() or "bom" in result["message"].lower()
