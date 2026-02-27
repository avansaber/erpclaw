"""Tests for BOM (Bill of Materials) actions.

Covers: add-bom, update-bom, get-bom, list-boms, explode-bom.
"""
import json
from decimal import Decimal

from helpers import (
    _call_action,
    setup_manufacturing_environment,
    create_test_item,
    create_test_bom,
    create_test_workstation,
    create_test_operation,
)


# ---------------------------------------------------------------------------
# 1. test_add_bom — basic BOM creation with two raw materials
# ---------------------------------------------------------------------------

def test_add_bom(fresh_db):
    """Create a BOM with 2 RM items, verify bom_id and naming series."""
    from db_query import ACTIONS
    env = setup_manufacturing_environment(fresh_db)

    items_json = json.dumps([
        {"item_id": env["rm1_id"], "quantity": "2", "rate": "10.00", "uom": "Each"},
        {"item_id": env["rm2_id"], "quantity": "3", "rate": "5.00", "uom": "Each"},
    ])
    result = _call_action(
        ACTIONS["add-bom"], fresh_db,
        item_id=env["fg_id"],
        items=items_json,
        company_id=env["company_id"],
    )

    assert result["status"] == "ok"
    assert "bom_id" in result
    assert result["naming_series"].startswith("BOM-")
    assert result["item_count"] == 2
    # RM cost: (2 * 10) + (3 * 5) = 35
    assert Decimal(result["raw_material_cost"]) == Decimal("35.00")
    assert result["is_default"] == 1  # First BOM for this item => default


# ---------------------------------------------------------------------------
# 2. test_add_bom_with_operations — BOM with inline operations
# ---------------------------------------------------------------------------

def test_add_bom_with_operations(fresh_db):
    """Create a BOM with operations and verify operating_cost is calculated."""
    from db_query import ACTIONS
    env = setup_manufacturing_environment(fresh_db)

    ws_id = create_test_workstation(fresh_db, "Weld Station", hour_rate="120.00")
    op_id = create_test_operation(fresh_db, "Welding", workstation_id=ws_id)

    items_json = json.dumps([
        {"item_id": env["rm1_id"], "quantity": "1", "rate": "10.00", "uom": "Each"},
    ])
    operations_json = json.dumps([
        {
            "operation_id": op_id,
            "workstation_id": ws_id,
            "time_in_minutes": "30",
            "sequence": 1,
        },
    ])

    result = _call_action(
        ACTIONS["add-bom"], fresh_db,
        item_id=env["fg_id"],
        items=items_json,
        operations=operations_json,
        company_id=env["company_id"],
    )

    assert result["status"] == "ok"
    assert result["operation_count"] == 1
    # Operating cost: (30 / 60) * 120 = 60.00
    assert Decimal(result["operating_cost"]) == Decimal("60.00")


# ---------------------------------------------------------------------------
# 3. test_add_bom_cost_calculation — verify full cost breakdown
# ---------------------------------------------------------------------------

def test_add_bom_cost_calculation(fresh_db):
    """Verify raw_material_cost, operating_cost, and total_cost are correct."""
    from db_query import ACTIONS
    env = setup_manufacturing_environment(fresh_db)

    ws_id = create_test_workstation(fresh_db, "Paint Booth", hour_rate="90.00")
    op_id = create_test_operation(fresh_db, "Painting", workstation_id=ws_id)

    items_json = json.dumps([
        {"item_id": env["rm1_id"], "quantity": "4", "rate": "10.00", "uom": "Each"},
        {"item_id": env["rm2_id"], "quantity": "6", "rate": "5.00", "uom": "Each"},
    ])
    operations_json = json.dumps([
        {
            "operation_id": op_id,
            "workstation_id": ws_id,
            "time_in_minutes": "60",
            "sequence": 1,
        },
    ])

    result = _call_action(
        ACTIONS["add-bom"], fresh_db,
        item_id=env["fg_id"],
        items=items_json,
        operations=operations_json,
        company_id=env["company_id"],
    )

    assert result["status"] == "ok"
    # RM: (4 * 10) + (6 * 5) = 70
    expected_rm = Decimal("70.00")
    # OP: (60 / 60) * 90 = 90
    expected_op = Decimal("90.00")
    expected_total = expected_rm + expected_op  # 160.00

    assert Decimal(result["raw_material_cost"]) == expected_rm
    assert Decimal(result["operating_cost"]) == expected_op
    assert Decimal(result["total_cost"]) == expected_total


# ---------------------------------------------------------------------------
# 4. test_update_bom — update BOM items and verify costs recalculated
# ---------------------------------------------------------------------------

def test_update_bom(fresh_db):
    """Update a BOM's items and verify costs are recalculated."""
    from db_query import ACTIONS
    env = setup_manufacturing_environment(fresh_db)

    # Create initial BOM: 2x RM1 @ $10 = $20
    items_json = json.dumps([
        {"item_id": env["rm1_id"], "quantity": "2", "rate": "10.00", "uom": "Each"},
    ])
    bom_id = create_test_bom(fresh_db, env["fg_id"], items_json, env["company_id"])

    # Update: replace with 5x RM2 @ $5 = $25
    new_items_json = json.dumps([
        {"item_id": env["rm2_id"], "quantity": "5", "rate": "5.00", "uom": "Each"},
    ])
    result = _call_action(
        ACTIONS["update-bom"], fresh_db,
        bom_id=bom_id,
        items=new_items_json,
    )

    assert result["status"] == "ok"
    assert "items" in result["updated_fields"]
    assert Decimal(result["raw_material_cost"]) == Decimal("25.00")
    assert Decimal(result["total_cost"]) == Decimal("25.00")


# ---------------------------------------------------------------------------
# 5. test_get_bom — get BOM returns header, items, and operations
# ---------------------------------------------------------------------------

def test_get_bom(fresh_db):
    """Get BOM returns full header plus items and operations lists."""
    from db_query import ACTIONS
    env = setup_manufacturing_environment(fresh_db)

    ws_id = create_test_workstation(fresh_db, "CNC Mill", hour_rate="200.00")
    op_id = create_test_operation(fresh_db, "Milling", workstation_id=ws_id)

    items_json = json.dumps([
        {"item_id": env["rm1_id"], "quantity": "1", "rate": "10.00", "uom": "Each"},
        {"item_id": env["rm2_id"], "quantity": "2", "rate": "5.00", "uom": "Each"},
    ])
    operations_json = json.dumps([
        {
            "operation_id": op_id,
            "workstation_id": ws_id,
            "time_in_minutes": "45",
            "sequence": 1,
        },
    ])

    bom_id = create_test_bom(
        fresh_db, env["fg_id"], items_json, env["company_id"],
        operations_json=operations_json,
    )

    result = _call_action(ACTIONS["get-bom"], fresh_db, bom_id=bom_id)

    assert result["status"] == "ok"
    assert result["id"] == bom_id
    assert result["item_id"] == env["fg_id"]
    assert result["item_code"] == "FG-001"
    assert result["item_name"] == "Finished Good A"

    # Items list
    assert len(result["items"]) == 2
    item_ids = {it["item_id"] for it in result["items"]}
    assert env["rm1_id"] in item_ids
    assert env["rm2_id"] in item_ids

    # Operations list
    assert len(result["operations"]) == 1
    assert result["operations"][0]["operation_id"] == op_id
    assert result["operations"][0]["operation_name"] == "Milling"


# ---------------------------------------------------------------------------
# 6. test_list_boms — list with item_id filter
# ---------------------------------------------------------------------------

def test_list_boms(fresh_db):
    """List BOMs with item_id filter returns only matching BOMs."""
    from db_query import ACTIONS
    env = setup_manufacturing_environment(fresh_db)

    # Create a second finished-good item
    fg2_id = create_test_item(
        fresh_db, item_code="FG-002", item_name="Finished Good B",
        standard_rate="200.00",
    )

    items_json_1 = json.dumps([
        {"item_id": env["rm1_id"], "quantity": "1", "rate": "10.00", "uom": "Each"},
    ])
    items_json_2 = json.dumps([
        {"item_id": env["rm2_id"], "quantity": "1", "rate": "5.00", "uom": "Each"},
    ])

    create_test_bom(fresh_db, env["fg_id"], items_json_1, env["company_id"])
    create_test_bom(fresh_db, fg2_id, items_json_2, env["company_id"])

    # List all BOMs: should have 2
    result_all = _call_action(ACTIONS["list-boms"], fresh_db,
                              company_id=env["company_id"])
    assert result_all["status"] == "ok"
    assert result_all["total_count"] == 2

    # List filtered by item_id=fg_id: should have 1
    result_filtered = _call_action(
        ACTIONS["list-boms"], fresh_db,
        item_id=env["fg_id"],
    )
    assert result_filtered["status"] == "ok"
    assert result_filtered["total_count"] == 1
    assert result_filtered["boms"][0]["item_id"] == env["fg_id"]


# ---------------------------------------------------------------------------
# 7. test_explode_bom_single_level — single-level explosion
# ---------------------------------------------------------------------------

def test_explode_bom_single_level(fresh_db):
    """Single-level BOM explosion returns leaf materials with scaled qty."""
    from db_query import ACTIONS
    env = setup_manufacturing_environment(fresh_db)

    # BOM for 1x FG: needs 2x RM1 + 3x RM2
    items_json = json.dumps([
        {"item_id": env["rm1_id"], "quantity": "2", "rate": "10.00", "uom": "Each"},
        {"item_id": env["rm2_id"], "quantity": "3", "rate": "5.00", "uom": "Each"},
    ])
    bom_id = create_test_bom(fresh_db, env["fg_id"], items_json, env["company_id"])

    # Explode for quantity=1
    result = _call_action(
        ACTIONS["explode-bom"], fresh_db,
        bom_id=bom_id,
        quantity="1",
    )

    assert result["status"] == "ok"
    assert result["material_count"] == 2

    materials_by_code = {m["item_code"]: m for m in result["materials"]}
    assert Decimal(materials_by_code["RM-001"]["total_qty"]) == Decimal("2")
    assert Decimal(materials_by_code["RM-002"]["total_qty"]) == Decimal("3")


# ---------------------------------------------------------------------------
# 8. test_explode_bom_multi_level — nested sub-assembly explosion
# ---------------------------------------------------------------------------

def test_explode_bom_multi_level(fresh_db):
    """Multi-level BOM explosion traverses sub-assemblies to leaf materials.

    Structure:
        FG-001 (BOM-A)
            -> SUB-001 (sub-assembly, BOM-B) x 1
                -> RM-001 x 3
                -> RM-002 x 2
            -> RM-002 x 1 (direct leaf)

    Explode for qty=1 should yield:
        RM-001: 3
        RM-002: 2 + 1 = 3
    """
    from db_query import ACTIONS
    env = setup_manufacturing_environment(fresh_db)

    # Create a sub-assembly item
    sub_id = create_test_item(
        fresh_db, item_code="SUB-001", item_name="Sub Assembly",
        standard_rate="50.00",
    )

    # BOM-B for SUB-001: needs 3x RM1 + 2x RM2
    sub_items_json = json.dumps([
        {"item_id": env["rm1_id"], "quantity": "3", "rate": "10.00", "uom": "Each"},
        {"item_id": env["rm2_id"], "quantity": "2", "rate": "5.00", "uom": "Each"},
    ])
    sub_bom_id = create_test_bom(
        fresh_db, sub_id, sub_items_json, env["company_id"],
    )

    # BOM-A for FG-001: 1x SUB-001 (sub-assembly) + 1x RM-002 (leaf)
    fg_items_json = json.dumps([
        {
            "item_id": sub_id,
            "quantity": "1",
            "rate": "50.00",
            "uom": "Each",
            "is_sub_assembly": 1,
            "sub_bom_id": sub_bom_id,
        },
        {"item_id": env["rm2_id"], "quantity": "1", "rate": "5.00", "uom": "Each"},
    ])
    fg_bom_id = create_test_bom(
        fresh_db, env["fg_id"], fg_items_json, env["company_id"],
    )

    result = _call_action(
        ACTIONS["explode-bom"], fresh_db,
        bom_id=fg_bom_id,
        quantity="1",
    )

    assert result["status"] == "ok"
    assert result["material_count"] == 2  # Only leaf materials

    materials_by_code = {m["item_code"]: m for m in result["materials"]}
    # RM-001 comes only from sub-assembly: 3
    assert Decimal(materials_by_code["RM-001"]["total_qty"]) == Decimal("3")
    # RM-002: 2 from sub-assembly + 1 direct = 3
    assert Decimal(materials_by_code["RM-002"]["total_qty"]) == Decimal("3")


# ---------------------------------------------------------------------------
# 9. test_explode_bom_quantity_scaling — qty=5 scales all materials
# ---------------------------------------------------------------------------

def test_explode_bom_quantity_scaling(fresh_db):
    """Explode with quantity=5 scales all leaf material quantities correctly."""
    from db_query import ACTIONS
    env = setup_manufacturing_environment(fresh_db)

    # BOM for 1x FG: 2x RM1 + 4x RM2
    items_json = json.dumps([
        {"item_id": env["rm1_id"], "quantity": "2", "rate": "10.00", "uom": "Each"},
        {"item_id": env["rm2_id"], "quantity": "4", "rate": "5.00", "uom": "Each"},
    ])
    bom_id = create_test_bom(fresh_db, env["fg_id"], items_json, env["company_id"])

    # Explode for quantity=5
    result = _call_action(
        ACTIONS["explode-bom"], fresh_db,
        bom_id=bom_id,
        quantity="5",
    )

    assert result["status"] == "ok"
    assert result["requested_qty"] == "5.00"

    materials_by_code = {m["item_code"]: m for m in result["materials"]}
    # RM-001: 2 * 5 = 10
    assert Decimal(materials_by_code["RM-001"]["total_qty"]) == Decimal("10.00")
    # RM-002: 4 * 5 = 20
    assert Decimal(materials_by_code["RM-002"]["total_qty"]) == Decimal("20.00")


# ---------------------------------------------------------------------------
# 10. test_add_bom_validates_item — non-existent item_id returns error
# ---------------------------------------------------------------------------

def test_add_bom_validates_item(fresh_db):
    """add-bom with a non-existent item_id returns an error response."""
    from db_query import ACTIONS
    env = setup_manufacturing_environment(fresh_db)

    fake_item_id = "00000000-0000-0000-0000-000000000000"
    items_json = json.dumps([
        {"item_id": env["rm1_id"], "quantity": "1", "rate": "10.00", "uom": "Each"},
    ])

    result = _call_action(
        ACTIONS["add-bom"], fresh_db,
        item_id=fake_item_id,
        items=items_json,
        company_id=env["company_id"],
    )

    assert result["status"] == "error"
    assert "not found" in result["message"].lower()
