"""Tests for job card actions.

Covers: create-job-card, complete-job-card, status.
"""
import json
from decimal import Decimal

from helpers import (
    _call_action,
    setup_manufacturing_environment,
    create_test_bom,
    create_test_workstation,
    create_test_operation,
)


# ---------------------------------------------------------------------------
# Shared helper: create a BOM + started Work Order for job card tests
# ---------------------------------------------------------------------------

def _create_started_work_order(fresh_db, env, ws_id=None, op_id=None):
    """Build the full prerequisite chain for job card tests.

    Creates a BOM (optionally with operations), a work order, and starts
    the work order so it is in 'not_started' state (valid for job card
    creation).

    Returns (bom_id, wo_id, op_id).
    """
    from db_query import ACTIONS

    items_json = json.dumps([
        {"item_id": env["rm1_id"], "quantity": "2", "rate": "10.00", "uom": "Each"},
        {"item_id": env["rm2_id"], "quantity": "3", "rate": "5.00", "uom": "Each"},
    ])

    operations_json = None
    if op_id and ws_id:
        operations_json = json.dumps([
            {
                "operation_id": op_id,
                "workstation_id": ws_id,
                "time_in_minutes": "30",
                "sequence": 1,
            },
        ])

    bom_id = create_test_bom(
        fresh_db, env["fg_id"], items_json, env["company_id"],
        operations_json=operations_json,
    )

    # Create work order
    wo_result = _call_action(
        ACTIONS["add-work-order"], fresh_db,
        bom_id=bom_id,
        quantity="10",
        company_id=env["company_id"],
        source_warehouse_id=env["source_wh_id"],
        target_warehouse_id=env["target_wh_id"],
        wip_warehouse_id=env["wip_wh_id"],
    )
    assert wo_result["status"] == "ok", f"add-work-order failed: {wo_result}"
    wo_id = wo_result["work_order_id"]

    # Start work order (draft -> not_started)
    start_result = _call_action(
        ACTIONS["start-work-order"], fresh_db,
        work_order_id=wo_id,
    )
    assert start_result["status"] == "ok", f"start-work-order failed: {start_result}"
    assert start_result["status_field"] if "status_field" in start_result else True

    return bom_id, wo_id, op_id


# ---------------------------------------------------------------------------
# 1. test_create_job_card — basic creation, naming series starts with JC-
# ---------------------------------------------------------------------------

def test_create_job_card(fresh_db):
    """Create a job card for a started work order operation."""
    from db_query import ACTIONS
    env = setup_manufacturing_environment(fresh_db)

    ws_id = create_test_workstation(fresh_db, "Station Alpha", hour_rate="80.00")
    op_id = create_test_operation(fresh_db, "Assembly", workstation_id=ws_id)
    _bom_id, wo_id, _ = _create_started_work_order(
        fresh_db, env, ws_id=ws_id, op_id=op_id,
    )

    result = _call_action(
        ACTIONS["create-job-card"], fresh_db,
        work_order_id=wo_id,
        operation_id=op_id,
    )

    assert result["status"] == "ok"
    assert "job_card_id" in result
    assert result["naming_series"].startswith("JC-")
    assert result["work_order_id"] == wo_id
    assert result["operation_id"] == op_id
    # Default for_quantity should match the WO qty (10)
    assert Decimal(result["for_quantity"]) == Decimal("10.00")
    assert result["status_field"] if "status_field" in result else True
    # The returned status key for the job card is "status"
    assert result.get("status") == "ok"  # top-level OK

    # Also verify the JC is stored as 'open' in the DB
    jc = fresh_db.execute(
        "SELECT status FROM job_card WHERE id = ?", (result["job_card_id"],)
    ).fetchone()
    assert jc["status"] == "open"


# ---------------------------------------------------------------------------
# 2. test_complete_job_card — complete with actual time
# ---------------------------------------------------------------------------

def test_complete_job_card(fresh_db):
    """Complete a job card with actual time, verify status=completed."""
    from db_query import ACTIONS
    env = setup_manufacturing_environment(fresh_db)

    ws_id = create_test_workstation(fresh_db, "Station Beta", hour_rate="60.00")
    op_id = create_test_operation(fresh_db, "Finishing", workstation_id=ws_id)
    _bom_id, wo_id, _ = _create_started_work_order(
        fresh_db, env, ws_id=ws_id, op_id=op_id,
    )

    # Create job card
    jc_result = _call_action(
        ACTIONS["create-job-card"], fresh_db,
        work_order_id=wo_id,
        operation_id=op_id,
    )
    assert jc_result["status"] == "ok"
    jc_id = jc_result["job_card_id"]

    # Complete the job card
    result = _call_action(
        ACTIONS["complete-job-card"], fresh_db,
        job_card_id=jc_id,
        actual_time_in_mins="45",
        completed_qty="10",
    )

    assert result["status"] == "ok"
    assert result["job_card_id"] == jc_id
    assert Decimal(result["total_time_in_minutes"]) == Decimal("45.00")
    assert Decimal(result["completed_qty"]) == Decimal("10.00")
    assert result["time_completed"] is not None

    # Verify DB state
    jc = fresh_db.execute(
        "SELECT status, total_time_in_minutes, completed_qty FROM job_card WHERE id = ?",
        (jc_id,),
    ).fetchone()
    assert jc["status"] == "completed"
    assert Decimal(jc["total_time_in_minutes"]) == Decimal("45.00")
    assert Decimal(jc["completed_qty"]) == Decimal("10.00")


# ---------------------------------------------------------------------------
# 3. test_job_card_validates_work_order — non-existent WO returns error
# ---------------------------------------------------------------------------

def test_job_card_validates_work_order(fresh_db):
    """create-job-card with a non-existent work_order_id returns an error."""
    from db_query import ACTIONS
    env = setup_manufacturing_environment(fresh_db)

    op_id = create_test_operation(fresh_db, "Drilling")

    fake_wo_id = "00000000-0000-0000-0000-000000000000"
    result = _call_action(
        ACTIONS["create-job-card"], fresh_db,
        work_order_id=fake_wo_id,
        operation_id=op_id,
    )

    assert result["status"] == "error"
    assert "not found" in result["message"].lower()


# ---------------------------------------------------------------------------
# 4. test_multiple_job_cards — multiple JCs for different operations
# ---------------------------------------------------------------------------

def test_multiple_job_cards(fresh_db):
    """Create multiple job cards for different operations on the same WO."""
    from db_query import ACTIONS
    env = setup_manufacturing_environment(fresh_db)

    ws_id = create_test_workstation(fresh_db, "Station Gamma", hour_rate="100.00")
    op1_id = create_test_operation(fresh_db, "Cutting", workstation_id=ws_id)
    op2_id = create_test_operation(fresh_db, "Polishing", workstation_id=ws_id)

    # Build BOM with both operations
    items_json = json.dumps([
        {"item_id": env["rm1_id"], "quantity": "1", "rate": "10.00", "uom": "Each"},
    ])
    operations_json = json.dumps([
        {
            "operation_id": op1_id,
            "workstation_id": ws_id,
            "time_in_minutes": "20",
            "sequence": 1,
        },
        {
            "operation_id": op2_id,
            "workstation_id": ws_id,
            "time_in_minutes": "15",
            "sequence": 2,
        },
    ])
    bom_id = create_test_bom(
        fresh_db, env["fg_id"], items_json, env["company_id"],
        operations_json=operations_json,
    )

    # Create + start work order
    wo_result = _call_action(
        ACTIONS["add-work-order"], fresh_db,
        bom_id=bom_id,
        quantity="5",
        company_id=env["company_id"],
        source_warehouse_id=env["source_wh_id"],
        target_warehouse_id=env["target_wh_id"],
        wip_warehouse_id=env["wip_wh_id"],
    )
    assert wo_result["status"] == "ok"
    wo_id = wo_result["work_order_id"]

    start_result = _call_action(
        ACTIONS["start-work-order"], fresh_db,
        work_order_id=wo_id,
    )
    assert start_result["status"] == "ok"

    # Create JC for operation 1
    jc1_result = _call_action(
        ACTIONS["create-job-card"], fresh_db,
        work_order_id=wo_id,
        operation_id=op1_id,
    )
    assert jc1_result["status"] == "ok"
    assert jc1_result["operation_id"] == op1_id

    # Create JC for operation 2
    jc2_result = _call_action(
        ACTIONS["create-job-card"], fresh_db,
        work_order_id=wo_id,
        operation_id=op2_id,
    )
    assert jc2_result["status"] == "ok"
    assert jc2_result["operation_id"] == op2_id

    # Verify both JCs exist and have different IDs / naming_series
    assert jc1_result["job_card_id"] != jc2_result["job_card_id"]
    assert jc1_result["naming_series"] != jc2_result["naming_series"]

    # Both should reference the same work order
    assert jc1_result["work_order_id"] == wo_id
    assert jc2_result["work_order_id"] == wo_id

    # Verify DB count
    count = fresh_db.execute(
        "SELECT COUNT(*) FROM job_card WHERE work_order_id = ?", (wo_id,),
    ).fetchone()[0]
    assert count == 2


# ---------------------------------------------------------------------------
# 5. test_status_action — verify summary structure
# ---------------------------------------------------------------------------

def test_status_action(fresh_db):
    """Status action returns the expected summary structure."""
    from db_query import ACTIONS
    env = setup_manufacturing_environment(fresh_db)

    ws_id = create_test_workstation(fresh_db, "Station Delta", hour_rate="70.00")
    op_id = create_test_operation(fresh_db, "Inspection", workstation_id=ws_id)

    # Create a BOM, work order, and job card to populate some counters
    items_json = json.dumps([
        {"item_id": env["rm1_id"], "quantity": "1", "rate": "10.00", "uom": "Each"},
    ])
    bom_id = create_test_bom(
        fresh_db, env["fg_id"], items_json, env["company_id"],
    )

    wo_result = _call_action(
        ACTIONS["add-work-order"], fresh_db,
        bom_id=bom_id,
        quantity="10",
        company_id=env["company_id"],
        source_warehouse_id=env["source_wh_id"],
        target_warehouse_id=env["target_wh_id"],
        wip_warehouse_id=env["wip_wh_id"],
    )
    assert wo_result["status"] == "ok"
    wo_id = wo_result["work_order_id"]

    # Start WO and create a job card
    _call_action(ACTIONS["start-work-order"], fresh_db, work_order_id=wo_id)
    _call_action(
        ACTIONS["create-job-card"], fresh_db,
        work_order_id=wo_id,
        operation_id=op_id,
    )

    # Call status action
    result = _call_action(ACTIONS["status"], fresh_db, company_id=env["company_id"])

    assert result["status"] == "ok"

    # Verify top-level keys exist and have sensible values
    assert "total_boms" in result
    assert result["total_boms"] >= 1
    assert "active_boms" in result
    assert result["active_boms"] >= 1

    assert "total_work_orders" in result
    assert result["total_work_orders"] >= 1
    assert "work_orders_by_status" in result
    assert isinstance(result["work_orders_by_status"], dict)

    assert "open_job_cards" in result
    assert result["open_job_cards"] >= 1

    assert "active_production_plans" in result
    assert "active_subcontracting_orders" in result
    assert "active_operations" in result
    assert result["active_operations"] >= 1
    assert "active_workstations" in result
    assert result["active_workstations"] >= 1
