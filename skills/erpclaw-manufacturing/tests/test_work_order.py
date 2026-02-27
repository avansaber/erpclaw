"""Tests for work order lifecycle actions.

12 tests covering: add, BOM-item scaling, start, transfer-materials (SLE/GL),
complete (with and without operating cost), cancel, status validation,
get, and list with filters.
"""
import json
import pytest

from db_query import ACTIONS
from helpers import (
    _call_action,
    setup_manufacturing_environment,
    create_test_operation,
    create_test_workstation,
    create_test_bom,
)


# ---------------------------------------------------------------------------
# Helper: full work order lifecycle (create -> start -> transfer -> complete)
# ---------------------------------------------------------------------------

def _full_lifecycle(fresh_db, env, bom_id):
    """Create WO -> start -> transfer -> complete. Returns wo_id."""
    # Create WO
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

    # Start
    start_result = _call_action(
        ACTIONS["start-work-order"], fresh_db,
        work_order_id=wo_id,
    )
    assert start_result["status"] == "ok"

    # Transfer materials (all required)
    wo_items = fresh_db.execute(
        "SELECT item_id, required_qty FROM work_order_item WHERE work_order_id = ?",
        (wo_id,),
    ).fetchall()
    transfer_items = [
        {"item_id": r["item_id"], "qty": r["required_qty"]}
        for r in wo_items
    ]
    transfer_result = _call_action(
        ACTIONS["transfer-materials"], fresh_db,
        work_order_id=wo_id,
        items=json.dumps(transfer_items),
    )
    assert transfer_result["status"] == "ok"

    # Complete
    complete_result = _call_action(
        ACTIONS["complete-work-order"], fresh_db,
        work_order_id=wo_id,
    )
    assert complete_result["status"] == "ok"

    return wo_id


# ---------------------------------------------------------------------------
# 1. test_add_work_order
# ---------------------------------------------------------------------------

def test_add_work_order(fresh_db):
    """Create a WO from a BOM, verify naming_series, status, and items."""
    env = setup_manufacturing_environment(fresh_db)
    bom_id = create_test_bom(fresh_db, env)

    result = _call_action(
        ACTIONS["add-work-order"], fresh_db,
        bom_id=bom_id,
        quantity="10",
        company_id=env["company_id"],
        source_warehouse_id=env["source_wh_id"],
        target_warehouse_id=env["target_wh_id"],
        wip_warehouse_id=env["wip_wh_id"],
    )

    assert result["status"] == "ok"
    assert "work_order_id" in result
    assert result["naming_series"].startswith("WO-")

    # Verify DB state
    wo_id = result["work_order_id"]
    wo = fresh_db.execute(
        "SELECT * FROM work_order WHERE id = ?", (wo_id,),
    ).fetchone()
    assert wo is not None
    assert wo["status"] == "draft"
    assert wo["bom_id"] == bom_id

    # Verify work_order_item rows exist with correct required_qty
    wo_items = fresh_db.execute(
        "SELECT * FROM work_order_item WHERE work_order_id = ?", (wo_id,),
    ).fetchall()
    assert len(wo_items) >= 1
    for woi in wo_items:
        assert float(woi["required_qty"]) > 0


# ---------------------------------------------------------------------------
# 2. test_add_work_order_copies_bom_items
# ---------------------------------------------------------------------------

def test_add_work_order_copies_bom_items(fresh_db):
    """WO quantity=5 from BOM with qty=1: work_order_item.required_qty = bom_item.qty * 5."""
    env = setup_manufacturing_environment(fresh_db)
    bom_id = create_test_bom(fresh_db, env)

    # Read BOM items to know expected quantities
    bom_items = fresh_db.execute(
        "SELECT item_id, quantity FROM bom_item WHERE bom_id = ?", (bom_id,),
    ).fetchall()

    bom_qty = fresh_db.execute(
        "SELECT quantity FROM bom WHERE id = ?", (bom_id,),
    ).fetchone()["quantity"]
    bom_qty_dec = float(bom_qty)

    wo_quantity = 5
    result = _call_action(
        ACTIONS["add-work-order"], fresh_db,
        bom_id=bom_id,
        quantity=str(wo_quantity),
        company_id=env["company_id"],
        source_warehouse_id=env["source_wh_id"],
        target_warehouse_id=env["target_wh_id"],
        wip_warehouse_id=env["wip_wh_id"],
    )
    assert result["status"] == "ok"
    wo_id = result["work_order_id"]

    wo_items = fresh_db.execute(
        "SELECT item_id, required_qty FROM work_order_item WHERE work_order_id = ?",
        (wo_id,),
    ).fetchall()

    # Build map from BOM items
    bom_item_map = {r["item_id"]: float(r["quantity"]) for r in bom_items}

    for woi in wo_items:
        item_id = woi["item_id"]
        expected_qty = (bom_item_map[item_id] / bom_qty_dec) * wo_quantity
        actual_qty = float(woi["required_qty"])
        assert abs(actual_qty - expected_qty) < 0.01, (
            f"Item {item_id}: expected required_qty={expected_qty}, got {actual_qty}"
        )


# ---------------------------------------------------------------------------
# 3. test_start_work_order
# ---------------------------------------------------------------------------

def test_start_work_order(fresh_db):
    """Create WO, start it: status becomes not_started, actual_start_date is set."""
    env = setup_manufacturing_environment(fresh_db)
    bom_id = create_test_bom(fresh_db, env)

    wo_result = _call_action(
        ACTIONS["add-work-order"], fresh_db,
        bom_id=bom_id,
        quantity="10",
        company_id=env["company_id"],
        source_warehouse_id=env["source_wh_id"],
        target_warehouse_id=env["target_wh_id"],
        wip_warehouse_id=env["wip_wh_id"],
    )
    wo_id = wo_result["work_order_id"]

    start_result = _call_action(
        ACTIONS["start-work-order"], fresh_db,
        work_order_id=wo_id,
    )

    assert start_result["status"] == "ok"
    assert start_result["status"] != "error"

    # Verify DB
    wo = fresh_db.execute(
        "SELECT status, actual_start_date FROM work_order WHERE id = ?",
        (wo_id,),
    ).fetchone()
    assert wo["status"] == "not_started"
    assert wo["actual_start_date"] is not None


# ---------------------------------------------------------------------------
# 4. test_transfer_materials
# ---------------------------------------------------------------------------

def test_transfer_materials(fresh_db):
    """Create WO, start, transfer: SLE created, transferred_qty updated, status in_process."""
    env = setup_manufacturing_environment(fresh_db)
    bom_id = create_test_bom(fresh_db, env)

    # Create and start WO
    wo_result = _call_action(
        ACTIONS["add-work-order"], fresh_db,
        bom_id=bom_id,
        quantity="10",
        company_id=env["company_id"],
        source_warehouse_id=env["source_wh_id"],
        target_warehouse_id=env["target_wh_id"],
        wip_warehouse_id=env["wip_wh_id"],
    )
    wo_id = wo_result["work_order_id"]

    _call_action(
        ACTIONS["start-work-order"], fresh_db,
        work_order_id=wo_id,
    )

    # Build transfer items from work_order_item
    wo_items = fresh_db.execute(
        "SELECT item_id, required_qty FROM work_order_item WHERE work_order_id = ?",
        (wo_id,),
    ).fetchall()
    transfer_items = [
        {"item_id": r["item_id"], "qty": r["required_qty"]}
        for r in wo_items
    ]

    transfer_result = _call_action(
        ACTIONS["transfer-materials"], fresh_db,
        work_order_id=wo_id,
        items=json.dumps(transfer_items),
    )

    assert transfer_result["status"] == "ok"
    assert transfer_result["items_transferred"] == len(transfer_items)
    assert transfer_result["sle_count"] > 0

    # Verify SLE entries created
    sles = fresh_db.execute(
        """SELECT * FROM stock_ledger_entry
           WHERE voucher_type = 'work_order' AND voucher_id = ?
           AND is_cancelled = 0""",
        (wo_id,),
    ).fetchall()
    assert len(sles) > 0

    # Verify work_order_item.transferred_qty updated
    updated_items = fresh_db.execute(
        "SELECT transferred_qty, required_qty FROM work_order_item WHERE work_order_id = ?",
        (wo_id,),
    ).fetchall()
    for woi in updated_items:
        assert float(woi["transferred_qty"]) == float(woi["required_qty"])

    # Verify WO status = "in_process"
    wo = fresh_db.execute(
        "SELECT status FROM work_order WHERE id = ?", (wo_id,),
    ).fetchone()
    assert wo["status"] == "in_process"


# ---------------------------------------------------------------------------
# 5. test_transfer_materials_sle_gl
# ---------------------------------------------------------------------------

def test_transfer_materials_sle_gl(fresh_db):
    """After transfer, verify SLE direction and GL balance."""
    env = setup_manufacturing_environment(fresh_db)
    bom_id = create_test_bom(fresh_db, env)

    wo_result = _call_action(
        ACTIONS["add-work-order"], fresh_db,
        bom_id=bom_id,
        quantity="10",
        company_id=env["company_id"],
        source_warehouse_id=env["source_wh_id"],
        target_warehouse_id=env["target_wh_id"],
        wip_warehouse_id=env["wip_wh_id"],
    )
    wo_id = wo_result["work_order_id"]

    _call_action(ACTIONS["start-work-order"], fresh_db, work_order_id=wo_id)

    wo_items = fresh_db.execute(
        "SELECT item_id, required_qty FROM work_order_item WHERE work_order_id = ?",
        (wo_id,),
    ).fetchall()
    transfer_items = [
        {"item_id": r["item_id"], "qty": r["required_qty"]}
        for r in wo_items
    ]
    _call_action(
        ACTIONS["transfer-materials"], fresh_db,
        work_order_id=wo_id,
        items=json.dumps(transfer_items),
    )

    # Check SLE entries: negative qty at source, positive at WIP
    sles = fresh_db.execute(
        """SELECT * FROM stock_ledger_entry
           WHERE voucher_type = 'work_order' AND voucher_id = ?
           AND is_cancelled = 0
           ORDER BY rowid""",
        (wo_id,),
    ).fetchall()
    assert len(sles) >= 2

    # Pair SLE entries: for each item there should be one negative and one positive
    neg_sles = [s for s in sles if float(s["actual_qty"]) < 0]
    pos_sles = [s for s in sles if float(s["actual_qty"]) > 0]
    assert len(neg_sles) > 0, "Expected negative SLE (source warehouse outflow)"
    assert len(pos_sles) > 0, "Expected positive SLE (WIP warehouse inflow)"

    # Negative SLEs should be from source warehouse
    for s in neg_sles:
        assert s["warehouse_id"] == env["source_wh_id"]

    # Positive SLEs should be to WIP warehouse
    for s in pos_sles:
        assert s["warehouse_id"] == env["wip_wh_id"]

    # Check GL entries exist for voucher_type="work_order"
    gl_rows = fresh_db.execute(
        """SELECT * FROM gl_entry
           WHERE voucher_type = 'work_order' AND voucher_id = ?
           AND is_cancelled = 0""",
        (wo_id,),
    ).fetchall()

    # GL may be empty if no valuation rate was set (stock received at rate 0),
    # but if present, debits must equal credits
    if len(gl_rows) > 0:
        total_debit = sum(float(r["debit"]) for r in gl_rows)
        total_credit = sum(float(r["credit"]) for r in gl_rows)
        assert abs(total_debit - total_credit) < 0.01, (
            f"GL imbalance: debits={total_debit}, credits={total_credit}"
        )


# ---------------------------------------------------------------------------
# 6. test_complete_work_order
# ---------------------------------------------------------------------------

def test_complete_work_order(fresh_db):
    """Full lifecycle: create -> start -> transfer -> complete. Verify status + FG SLE."""
    env = setup_manufacturing_environment(fresh_db)
    bom_id = create_test_bom(fresh_db, env)

    wo_id = _full_lifecycle(fresh_db, env, bom_id)

    # Verify WO status = "completed"
    wo = fresh_db.execute(
        "SELECT * FROM work_order WHERE id = ?", (wo_id,),
    ).fetchone()
    assert wo["status"] == "completed"
    assert float(wo["produced_qty"]) > 0

    # Verify FG SLE created in target warehouse
    completion_voucher_id = f"{wo_id}:completion"
    fg_sles = fresh_db.execute(
        """SELECT * FROM stock_ledger_entry
           WHERE voucher_type = 'work_order' AND voucher_id = ?
           AND is_cancelled = 0""",
        (completion_voucher_id,),
    ).fetchall()
    assert len(fg_sles) >= 1

    # FG SLE should have positive qty at target warehouse
    fg_sle = fg_sles[0]
    assert float(fg_sle["actual_qty"]) > 0
    assert fg_sle["warehouse_id"] == env["target_wh_id"]
    assert fg_sle["item_id"] == wo["item_id"]


# ---------------------------------------------------------------------------
# 7. test_complete_work_order_gl
# ---------------------------------------------------------------------------

def test_complete_work_order_gl(fresh_db):
    """After completion, verify GL entries balance."""
    env = setup_manufacturing_environment(fresh_db)
    bom_id = create_test_bom(fresh_db, env)

    wo_id = _full_lifecycle(fresh_db, env, bom_id)

    # Check GL entries for the completion voucher
    completion_voucher_id = f"{wo_id}:completion"
    gl_rows = fresh_db.execute(
        """SELECT * FROM gl_entry
           WHERE voucher_type = 'work_order' AND voucher_id = ?
           AND is_cancelled = 0""",
        (completion_voucher_id,),
    ).fetchall()

    # If GL entries were created, they must balance
    if len(gl_rows) > 0:
        total_debit = sum(float(r["debit"]) for r in gl_rows)
        total_credit = sum(float(r["credit"]) for r in gl_rows)
        assert abs(total_debit - total_credit) < 0.01, (
            f"GL imbalance: debits={total_debit}, credits={total_credit}"
        )

    # Also check material transfer GL entries balance
    transfer_gl = fresh_db.execute(
        """SELECT * FROM gl_entry
           WHERE voucher_type = 'work_order' AND voucher_id = ?
           AND is_cancelled = 0""",
        (wo_id,),
    ).fetchall()
    if len(transfer_gl) > 0:
        total_debit = sum(float(r["debit"]) for r in transfer_gl)
        total_credit = sum(float(r["credit"]) for r in transfer_gl)
        assert abs(total_debit - total_credit) < 0.01, (
            f"Transfer GL imbalance: debits={total_debit}, credits={total_credit}"
        )


# ---------------------------------------------------------------------------
# 8. test_complete_work_order_production_cost
# ---------------------------------------------------------------------------

def test_complete_work_order_production_cost(fresh_db):
    """After completion with a job card, verify production cost includes operating cost."""
    env = setup_manufacturing_environment(fresh_db)

    # Create workstation with hour_rate = 60
    ws_id = create_test_workstation(fresh_db, name="Assembly Station", hour_rate="60")

    # Create operation with the workstation
    op_id = create_test_operation(fresh_db, name="Assembly", workstation_id=ws_id)

    # Create BOM with operations
    bom_id = create_test_bom(fresh_db, env, operations=[{
        "operation_id": op_id,
        "workstation_id": ws_id,
        "time_in_minutes": "60",
    }])

    # Create WO
    wo_result = _call_action(
        ACTIONS["add-work-order"], fresh_db,
        bom_id=bom_id,
        quantity="10",
        company_id=env["company_id"],
        source_warehouse_id=env["source_wh_id"],
        target_warehouse_id=env["target_wh_id"],
        wip_warehouse_id=env["wip_wh_id"],
    )
    wo_id = wo_result["work_order_id"]

    # Start
    _call_action(ACTIONS["start-work-order"], fresh_db, work_order_id=wo_id)

    # Transfer materials
    wo_items = fresh_db.execute(
        "SELECT item_id, required_qty FROM work_order_item WHERE work_order_id = ?",
        (wo_id,),
    ).fetchall()
    transfer_items = [
        {"item_id": r["item_id"], "qty": r["required_qty"]}
        for r in wo_items
    ]
    _call_action(
        ACTIONS["transfer-materials"], fresh_db,
        work_order_id=wo_id,
        items=json.dumps(transfer_items),
    )

    # Create job card for the operation
    jc_result = _call_action(
        ACTIONS["create-job-card"], fresh_db,
        work_order_id=wo_id,
        operation_id=op_id,
        workstation_id=ws_id,
    )
    assert jc_result["status"] == "ok"
    jc_id = jc_result["job_card_id"]

    # Complete job card with actual_time_in_mins=60
    jc_complete = _call_action(
        ACTIONS["complete-job-card"], fresh_db,
        job_card_id=jc_id,
        actual_time_in_mins="60",
    )
    assert jc_complete["status"] == "ok"

    # Complete work order
    complete_result = _call_action(
        ACTIONS["complete-work-order"], fresh_db,
        work_order_id=wo_id,
    )
    assert complete_result["status"] == "ok"

    # Operating cost should be: 60 mins / 60 * $60/hr = $60.00
    operating_cost = float(complete_result["operating_cost"])
    assert abs(operating_cost - 60.0) < 0.01, (
        f"Expected operating_cost=60.00, got {operating_cost}"
    )

    # Production cost should include both RM cost and operating cost
    production_cost = float(complete_result["production_cost"])
    rm_cost = float(complete_result["rm_cost"])
    assert abs(production_cost - (rm_cost + operating_cost)) < 0.01, (
        f"production_cost ({production_cost}) should equal "
        f"rm_cost ({rm_cost}) + operating_cost ({operating_cost})"
    )

    # FG rate = total_production_cost / produced_qty
    fg_rate = float(complete_result["fg_rate"])
    produced_qty = float(complete_result["produced_qty"])
    expected_fg_rate = production_cost / produced_qty if produced_qty > 0 else 0
    assert abs(fg_rate - expected_fg_rate) < 0.01, (
        f"fg_rate ({fg_rate}) should equal "
        f"production_cost ({production_cost}) / produced_qty ({produced_qty})"
    )

    # Verify FG SLE incoming_rate reflects total production cost / produced_qty
    completion_voucher_id = f"{wo_id}:completion"
    fg_sle = fresh_db.execute(
        """SELECT * FROM stock_ledger_entry
           WHERE voucher_type = 'work_order' AND voucher_id = ?
           AND is_cancelled = 0""",
        (completion_voucher_id,),
    ).fetchone()
    assert fg_sle is not None
    sle_incoming_rate = float(fg_sle["incoming_rate"])
    assert abs(sle_incoming_rate - expected_fg_rate) < 0.01, (
        f"SLE incoming_rate ({sle_incoming_rate}) should equal "
        f"expected fg_rate ({expected_fg_rate})"
    )


# ---------------------------------------------------------------------------
# 9. test_cancel_work_order
# ---------------------------------------------------------------------------

def test_cancel_work_order(fresh_db):
    """After transfer, cancel WO: SLE reversed, GL reversed, status cancelled."""
    env = setup_manufacturing_environment(fresh_db)
    bom_id = create_test_bom(fresh_db, env)

    # Create, start, transfer
    wo_result = _call_action(
        ACTIONS["add-work-order"], fresh_db,
        bom_id=bom_id,
        quantity="10",
        company_id=env["company_id"],
        source_warehouse_id=env["source_wh_id"],
        target_warehouse_id=env["target_wh_id"],
        wip_warehouse_id=env["wip_wh_id"],
    )
    wo_id = wo_result["work_order_id"]

    _call_action(ACTIONS["start-work-order"], fresh_db, work_order_id=wo_id)

    wo_items = fresh_db.execute(
        "SELECT item_id, required_qty FROM work_order_item WHERE work_order_id = ?",
        (wo_id,),
    ).fetchall()
    transfer_items = [
        {"item_id": r["item_id"], "qty": r["required_qty"]}
        for r in wo_items
    ]
    _call_action(
        ACTIONS["transfer-materials"], fresh_db,
        work_order_id=wo_id,
        items=json.dumps(transfer_items),
    )

    # Count SLE/GL before cancellation
    sle_before = fresh_db.execute(
        """SELECT COUNT(*) FROM stock_ledger_entry
           WHERE voucher_type = 'work_order' AND voucher_id = ?
           AND is_cancelled = 0""",
        (wo_id,),
    ).fetchone()[0]

    # Cancel
    cancel_result = _call_action(
        ACTIONS["cancel-work-order"], fresh_db,
        work_order_id=wo_id,
    )
    assert cancel_result["status"] == "ok"

    # Verify WO status = "cancelled"
    wo = fresh_db.execute(
        "SELECT status FROM work_order WHERE id = ?", (wo_id,),
    ).fetchone()
    assert wo["status"] == "cancelled"

    # Verify SLE reversed (is_cancelled=1 on originals)
    sle_active = fresh_db.execute(
        """SELECT COUNT(*) FROM stock_ledger_entry
           WHERE voucher_type = 'work_order' AND voucher_id = ?
           AND is_cancelled = 0""",
        (wo_id,),
    ).fetchone()[0]
    # After reversal, there should be no active SLEs for the original voucher
    # (reverse_sle_entries marks originals as is_cancelled=1)
    assert sle_active == 0, (
        f"Expected 0 active SLEs after cancellation, got {sle_active}"
    )

    # Verify GL reversed: net debit and credit should equal zero
    # (reversals are active entries that offset originals)
    gl_net = fresh_db.execute(
        """SELECT
               COALESCE(SUM(CAST(debit AS REAL)), 0) AS total_debit,
               COALESCE(SUM(CAST(credit AS REAL)), 0) AS total_credit
           FROM gl_entry
           WHERE voucher_type = 'work_order' AND voucher_id = ?
           AND is_cancelled = 0""",
        (wo_id,),
    ).fetchone()
    assert abs(gl_net["total_debit"] - gl_net["total_credit"]) < 0.01, (
        f"GL net should balance after cancellation: "
        f"debit={gl_net['total_debit']}, credit={gl_net['total_credit']}"
    )
    # All original GL entries should be marked cancelled
    gl_originals_cancelled = fresh_db.execute(
        """SELECT COUNT(*) FROM gl_entry
           WHERE voucher_type = 'work_order' AND voucher_id = ?
           AND is_cancelled = 1""",
        (wo_id,),
    ).fetchone()[0]
    assert gl_originals_cancelled > 0, "Original GL entries should be marked cancelled"


# ---------------------------------------------------------------------------
# 10. test_work_order_status_validation
# ---------------------------------------------------------------------------

def test_work_order_status_validation(fresh_db):
    """Invalid status transitions should return errors."""
    env = setup_manufacturing_environment(fresh_db)
    bom_id = create_test_bom(fresh_db, env)

    # --- Can't complete a draft WO ---
    wo_result = _call_action(
        ACTIONS["add-work-order"], fresh_db,
        bom_id=bom_id,
        quantity="10",
        company_id=env["company_id"],
        source_warehouse_id=env["source_wh_id"],
        target_warehouse_id=env["target_wh_id"],
        wip_warehouse_id=env["wip_wh_id"],
    )
    wo_id_draft = wo_result["work_order_id"]

    complete_result = _call_action(
        ACTIONS["complete-work-order"], fresh_db,
        work_order_id=wo_id_draft,
    )
    assert complete_result["status"] == "error"

    # --- Can't transfer materials on a draft WO ---
    transfer_result = _call_action(
        ACTIONS["transfer-materials"], fresh_db,
        work_order_id=wo_id_draft,
        items=json.dumps([{"item_id": "fake", "qty": "1"}]),
    )
    assert transfer_result["status"] == "error"

    # --- Can't start a cancelled WO ---
    # Create another WO, start it, cancel it, then try to start again
    wo_result2 = _call_action(
        ACTIONS["add-work-order"], fresh_db,
        bom_id=bom_id,
        quantity="5",
        company_id=env["company_id"],
        source_warehouse_id=env["source_wh_id"],
        target_warehouse_id=env["target_wh_id"],
        wip_warehouse_id=env["wip_wh_id"],
    )
    wo_id_cancel = wo_result2["work_order_id"]

    # Start it first (draft -> not_started)
    _call_action(ACTIONS["start-work-order"], fresh_db, work_order_id=wo_id_cancel)

    # Cancel it (not_started -> cancelled)
    _call_action(ACTIONS["cancel-work-order"], fresh_db, work_order_id=wo_id_cancel)

    # Verify it is cancelled
    wo_cancelled = fresh_db.execute(
        "SELECT status FROM work_order WHERE id = ?", (wo_id_cancel,),
    ).fetchone()
    assert wo_cancelled["status"] == "cancelled"

    # Try to start the cancelled WO
    start_cancelled = _call_action(
        ACTIONS["start-work-order"], fresh_db,
        work_order_id=wo_id_cancel,
    )
    assert start_cancelled["status"] == "error"


# ---------------------------------------------------------------------------
# 11. test_get_work_order
# ---------------------------------------------------------------------------

def test_get_work_order(fresh_db):
    """Create WO with items and optional job card, call get-work-order."""
    env = setup_manufacturing_environment(fresh_db)

    # Create workstation and operation for job card test
    ws_id = create_test_workstation(fresh_db, name="Test WS", hour_rate="50")
    op_id = create_test_operation(fresh_db, name="Test Op", workstation_id=ws_id)

    bom_id = create_test_bom(fresh_db, env)

    wo_result = _call_action(
        ACTIONS["add-work-order"], fresh_db,
        bom_id=bom_id,
        quantity="10",
        company_id=env["company_id"],
        source_warehouse_id=env["source_wh_id"],
        target_warehouse_id=env["target_wh_id"],
        wip_warehouse_id=env["wip_wh_id"],
    )
    wo_id = wo_result["work_order_id"]

    # Start WO so we can create a job card
    _call_action(ACTIONS["start-work-order"], fresh_db, work_order_id=wo_id)

    # Create a job card
    jc_result = _call_action(
        ACTIONS["create-job-card"], fresh_db,
        work_order_id=wo_id,
        operation_id=op_id,
        workstation_id=ws_id,
    )
    assert jc_result["status"] == "ok"

    # Get work order
    get_result = _call_action(
        ACTIONS["get-work-order"], fresh_db,
        work_order_id=wo_id,
    )

    assert get_result["status"] == "ok"
    assert get_result["id"] == wo_id

    # Verify items list returned with item names
    assert "items" in get_result
    assert len(get_result["items"]) >= 1
    for item in get_result["items"]:
        assert "item_id" in item
        assert "required_qty" in item
        # item_name comes from the JOIN
        assert "item_name" in item or "item_code" in item

    # Verify job_cards list included
    assert "job_cards" in get_result
    assert len(get_result["job_cards"]) >= 1
    assert get_result["job_cards"][0]["work_order_id"] == wo_id


# ---------------------------------------------------------------------------
# 12. test_list_work_orders
# ---------------------------------------------------------------------------

def test_list_work_orders(fresh_db):
    """Create 2 WOs (one draft, one started), filter by status."""
    env = setup_manufacturing_environment(fresh_db)
    bom_id = create_test_bom(fresh_db, env)

    # Create first WO (stays draft)
    wo1_result = _call_action(
        ACTIONS["add-work-order"], fresh_db,
        bom_id=bom_id,
        quantity="10",
        company_id=env["company_id"],
        source_warehouse_id=env["source_wh_id"],
        target_warehouse_id=env["target_wh_id"],
        wip_warehouse_id=env["wip_wh_id"],
    )
    assert wo1_result["status"] == "ok"

    # Create second WO and start it (becomes not_started)
    wo2_result = _call_action(
        ACTIONS["add-work-order"], fresh_db,
        bom_id=bom_id,
        quantity="5",
        company_id=env["company_id"],
        source_warehouse_id=env["source_wh_id"],
        target_warehouse_id=env["target_wh_id"],
        wip_warehouse_id=env["wip_wh_id"],
    )
    assert wo2_result["status"] == "ok"
    wo2_id = wo2_result["work_order_id"]

    _call_action(ACTIONS["start-work-order"], fresh_db, work_order_id=wo2_id)

    # List all WOs
    list_all = _call_action(
        ACTIONS["list-work-orders"], fresh_db,
        company_id=env["company_id"],
    )
    assert list_all["status"] == "ok"
    assert list_all["total_count"] == 2
    assert len(list_all["work_orders"]) == 2

    # List by status="draft": returns 1
    list_draft = _call_action(
        ACTIONS["list-work-orders"], fresh_db,
        company_id=env["company_id"],
        status="draft",
    )
    assert list_draft["status"] == "ok"
    assert list_draft["total_count"] == 1
    assert list_draft["work_orders"][0]["status"] == "draft"

    # List by status="not_started": returns 1
    list_started = _call_action(
        ACTIONS["list-work-orders"], fresh_db,
        company_id=env["company_id"],
        status="not_started",
    )
    assert list_started["status"] == "ok"
    assert list_started["total_count"] == 1
    assert list_started["work_orders"][0]["status"] == "not_started"
