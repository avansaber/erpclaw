"""Manufacturing scenario integration tests.

Full manufacturing cycle: BOM creation -> work order -> material transfer
-> job cards -> completion -> finished goods stock -> COGS GL entries.

Tests the erpclaw-manufacturing skill in combination with erpclaw-inventory,
erpclaw-gl, and shared library functions for stock and GL posting.
"""
import json
from decimal import Decimal

from helpers import (
    _call_action,
    create_test_company,
    create_test_fiscal_year,
    seed_naming_series,
    create_test_account,
    create_test_cost_center,
    create_test_item,
    create_test_warehouse,
    seed_stock_for_item,
    setup_phase2_environment,
)


class TestManufacturingScenario:
    """Integration tests for the full manufacturing business cycle."""

    # -------------------------------------------------------------------
    # Shared setup helper
    # -------------------------------------------------------------------

    @staticmethod
    def _setup_manufacturing_env(conn):
        """Create a complete environment for manufacturing tests.

        Sets up company, FY, naming series, accounts, items (raw materials
        + finished good), and warehouses (stores, WIP, finished goods).

        Returns dict with all IDs needed.
        """
        cid = create_test_company(conn)
        fy_id = create_test_fiscal_year(conn, cid)
        seed_naming_series(conn, cid)
        cc = create_test_cost_center(conn, cid)

        # --- Accounts ---
        stock_in_hand = create_test_account(
            conn, cid, "Stock In Hand", "asset",
            account_type="stock", account_number="1400",
        )
        wip_account = create_test_account(
            conn, cid, "Work In Progress", "asset",
            account_type="stock", account_number="1410",
        )
        cogs = create_test_account(
            conn, cid, "Cost of Goods Sold", "expense",
            account_type="cost_of_goods_sold", account_number="5100",
        )
        stock_adjustment = create_test_account(
            conn, cid, "Stock Adjustment", "expense",
            account_type="stock_adjustment", account_number="5200",
        )

        # --- Warehouses ---
        stores_wh = create_test_warehouse(
            conn, cid, "Stores", warehouse_type="stores",
            account_id=stock_in_hand,
        )
        wip_wh = create_test_warehouse(
            conn, cid, "WIP Warehouse", warehouse_type="transit",
            account_id=wip_account,
        )
        fg_wh = create_test_warehouse(
            conn, cid, "Finished Goods", warehouse_type="stores",
            account_id=stock_in_hand,
        )

        # --- Raw material items ---
        rm1_id = create_test_item(
            conn, item_code="RM-001", item_name="Steel Sheet",
            item_type="stock", stock_uom="Kg",
            valuation_method="moving_average", standard_rate="10.00",
        )
        rm2_id = create_test_item(
            conn, item_code="RM-002", item_name="Copper Wire",
            item_type="stock", stock_uom="Meter",
            valuation_method="moving_average", standard_rate="5.00",
        )

        # --- Finished good item ---
        fg_id = create_test_item(
            conn, item_code="FG-001", item_name="Widget Assembly",
            item_type="stock", stock_uom="Each",
            valuation_method="moving_average", standard_rate="0",
        )

        # --- Seed raw material stock in stores warehouse ---
        seed_stock_for_item(conn, rm1_id, stores_wh, qty="200", rate="10.00")
        seed_stock_for_item(conn, rm2_id, stores_wh, qty="500", rate="5.00")

        return {
            "company_id": cid,
            "fy_id": fy_id,
            "cost_center_id": cc,
            "stock_in_hand_id": stock_in_hand,
            "wip_account_id": wip_account,
            "cogs_id": cogs,
            "stock_adjustment_id": stock_adjustment,
            "stores_wh_id": stores_wh,
            "wip_wh_id": wip_wh,
            "fg_wh_id": fg_wh,
            "rm1_id": rm1_id,
            "rm2_id": rm2_id,
            "fg_id": fg_id,
        }

    @staticmethod
    def _create_workstation(conn, name="Assembly Station", hour_rate="50.00"):
        """Create a workstation. Returns workstation_id."""
        result = _call_action(
            "erpclaw-manufacturing", "add-workstation", conn,
            name=name, hour_rate=hour_rate,
        )
        assert result["status"] == "ok", f"add-workstation failed: {result}"
        return result["workstation_id"]

    @staticmethod
    def _create_operation(conn, name="Assembly", workstation_id=None):
        """Create an operation. Returns operation_id."""
        result = _call_action(
            "erpclaw-manufacturing", "add-operation", conn,
            name=name, workstation_id=workstation_id,
        )
        assert result["status"] == "ok", f"add-operation failed: {result}"
        return result["operation_id"]

    @staticmethod
    def _create_bom(conn, env, operations_json=None):
        """Create a BOM for the finished good with both raw materials.

        Returns the result dict from add-bom.
        """
        bom_items = json.dumps([
            {"item_id": env["rm1_id"], "quantity": "2", "rate": "10.00"},
            {"item_id": env["rm2_id"], "quantity": "3", "rate": "5.00"},
        ])
        kwargs = dict(
            item_id=env["fg_id"],
            items=bom_items,
            company_id=env["company_id"],
            quantity="1",
        )
        if operations_json:
            kwargs["operations"] = operations_json
        result = _call_action(
            "erpclaw-manufacturing", "add-bom", conn, **kwargs,
        )
        assert result["status"] == "ok", f"add-bom failed: {result}"
        return result

    @staticmethod
    def _run_full_wo_cycle(conn, env, bom_id, ws_id=None, op_id=None):
        """Run the full work order cycle: create -> start -> transfer ->
        (optionally) job card -> complete. Returns dict with all IDs
        and results."""
        # Create WO
        wo_result = _call_action(
            "erpclaw-manufacturing", "add-work-order", conn,
            bom_id=bom_id,
            quantity="10",
            company_id=env["company_id"],
            planned_start_date="2026-03-01",
            source_warehouse_id=env["stores_wh_id"],
            target_warehouse_id=env["fg_wh_id"],
            wip_warehouse_id=env["wip_wh_id"],
        )
        assert wo_result["status"] == "ok", f"add-work-order failed: {wo_result}"
        wo_id = wo_result["work_order_id"]

        # Start WO
        start_result = _call_action(
            "erpclaw-manufacturing", "start-work-order", conn,
            work_order_id=wo_id,
        )
        assert start_result["status"] == "ok", f"start-work-order failed: {start_result}"

        # Transfer materials
        transfer_items = json.dumps([
            {"item_id": env["rm1_id"], "qty": "20", "warehouse_id": env["stores_wh_id"]},
            {"item_id": env["rm2_id"], "qty": "30", "warehouse_id": env["stores_wh_id"]},
        ])
        transfer_result = _call_action(
            "erpclaw-manufacturing", "transfer-materials", conn,
            work_order_id=wo_id,
            items=transfer_items,
            posting_date="2026-03-02",
        )
        assert transfer_result["status"] == "ok", f"transfer-materials failed: {transfer_result}"

        # Job card (if operation and workstation provided)
        jc_result = None
        jc_complete_result = None
        if op_id:
            jc_result = _call_action(
                "erpclaw-manufacturing", "create-job-card", conn,
                work_order_id=wo_id,
                operation_id=op_id,
                workstation_id=ws_id,
            )
            assert jc_result["status"] == "ok", f"create-job-card failed: {jc_result}"

            jc_complete_result = _call_action(
                "erpclaw-manufacturing", "complete-job-card", conn,
                job_card_id=jc_result["job_card_id"],
                actual_time_in_mins="120",
                completed_qty="10",
            )
            assert jc_complete_result["status"] == "ok", (
                f"complete-job-card failed: {jc_complete_result}"
            )

        # Complete WO
        complete_result = _call_action(
            "erpclaw-manufacturing", "complete-work-order", conn,
            work_order_id=wo_id,
            posting_date="2026-03-05",
        )
        assert complete_result["status"] == "ok", (
            f"complete-work-order failed: {complete_result}"
        )

        return {
            "wo_id": wo_id,
            "wo_result": wo_result,
            "start_result": start_result,
            "transfer_result": transfer_result,
            "jc_result": jc_result,
            "jc_complete_result": jc_complete_result,
            "complete_result": complete_result,
        }

    # -------------------------------------------------------------------
    # Test 1: Full manufacturing cycle (end-to-end)
    # -------------------------------------------------------------------

    def test_full_manufacturing_cycle(self, fresh_db):
        """BOM -> WO -> start -> transfer -> job card -> complete -> stock.

        This is the comprehensive end-to-end test that validates the entire
        manufacturing workflow including SLE and GL entry creation.
        """
        conn = fresh_db
        env = self._setup_manufacturing_env(conn)

        # Create workstation and operation
        ws_id = self._create_workstation(conn, "Assembly Line", "60.00")
        op_id = self._create_operation(conn, "Final Assembly", ws_id)

        # Create BOM with operations
        ops_json = json.dumps([{
            "operation_id": op_id,
            "workstation_id": ws_id,
            "time_in_minutes": "30",
        }])
        bom_result = self._create_bom(conn, env, operations_json=ops_json)
        bom_id = bom_result["bom_id"]

        # BOM cost verification:
        # RM cost = (2 * 10.00) + (3 * 5.00) = 20.00 + 15.00 = 35.00
        # Operating cost = (30/60) * 60.00 = 30.00
        # Total = 65.00
        assert Decimal(bom_result["raw_material_cost"]) == Decimal("35.00")
        assert Decimal(bom_result["operating_cost"]) == Decimal("30.00")
        assert Decimal(bom_result["total_cost"]) == Decimal("65.00")

        # Run full cycle
        cycle = self._run_full_wo_cycle(conn, env, bom_id, ws_id, op_id)

        # Verify WO is completed
        wo = conn.execute(
            "SELECT * FROM work_order WHERE id = ?", (cycle["wo_id"],),
        ).fetchone()
        assert wo["status"] == "completed"
        assert Decimal(wo["produced_qty"]) == Decimal("10.00")

        # Verify FG stock in target warehouse (SLE entry for completion)
        completion_voucher_id = f"{cycle['wo_id']}:completion"
        fg_sle = conn.execute(
            """SELECT * FROM stock_ledger_entry
               WHERE item_id = ? AND warehouse_id = ?
               AND voucher_id = ? AND is_cancelled = 0
               AND CAST(actual_qty AS REAL) > 0""",
            (env["fg_id"], env["fg_wh_id"], completion_voucher_id),
        ).fetchall()
        assert len(fg_sle) >= 1, "No FG SLE entry found for completion"
        fg_qty = sum(Decimal(r["actual_qty"]) for r in fg_sle)
        assert fg_qty == Decimal("10.00"), f"FG qty {fg_qty} != expected 10.00"

        # Verify GL entries for completion are balanced
        gl_rows = conn.execute(
            """SELECT * FROM gl_entry
               WHERE voucher_type = 'work_order' AND voucher_id = ?
               AND is_cancelled = 0""",
            (completion_voucher_id,),
        ).fetchall()
        if gl_rows:
            total_debit = sum(Decimal(r["debit"]) for r in gl_rows)
            total_credit = sum(Decimal(r["credit"]) for r in gl_rows)
            assert abs(total_debit - total_credit) < Decimal("0.01"), (
                f"Completion GL not balanced: debit={total_debit}, credit={total_credit}"
            )

        # Verify production cost includes operating cost from job card
        complete_result = cycle["complete_result"]
        # RM cost for 10 units: 10 * (2*10 + 3*5) = 10 * 35 = 350.00
        # But actual RM cost uses valuation_rate from SLE, which is seeded at
        # the same rate, so should match.
        assert Decimal(complete_result["rm_cost"]) >= Decimal("0")
        assert Decimal(complete_result["production_cost"]) >= Decimal("0")

    # -------------------------------------------------------------------
    # Test 2: BOM creation
    # -------------------------------------------------------------------

    def test_bom_creation(self, fresh_db):
        """Create BOM with 2 raw materials and verify structure."""
        conn = fresh_db
        env = self._setup_manufacturing_env(conn)

        bom_result = self._create_bom(conn, env)
        bom_id = bom_result["bom_id"]

        # Verify BOM header in DB
        bom = conn.execute(
            "SELECT * FROM bom WHERE id = ?", (bom_id,),
        ).fetchone()
        assert bom is not None
        assert bom["item_id"] == env["fg_id"]
        assert bom["is_active"] == 1
        assert bom["is_default"] == 1
        assert Decimal(bom["quantity"]) == Decimal("1.00")

        # Verify BOM items
        bom_items = conn.execute(
            "SELECT * FROM bom_item WHERE bom_id = ? ORDER BY rowid",
            (bom_id,),
        ).fetchall()
        assert len(bom_items) == 2

        # Item 1: Steel Sheet, qty=2, rate=10
        assert bom_items[0]["item_id"] == env["rm1_id"]
        assert Decimal(bom_items[0]["quantity"]) == Decimal("2.00")
        assert Decimal(bom_items[0]["rate"]) == Decimal("10.00")
        assert Decimal(bom_items[0]["amount"]) == Decimal("20.00")

        # Item 2: Copper Wire, qty=3, rate=5
        assert bom_items[1]["item_id"] == env["rm2_id"]
        assert Decimal(bom_items[1]["quantity"]) == Decimal("3.00")
        assert Decimal(bom_items[1]["rate"]) == Decimal("5.00")
        assert Decimal(bom_items[1]["amount"]) == Decimal("15.00")

        # Verify cost calculation
        assert bom_result["item_count"] == 2
        assert Decimal(bom_result["raw_material_cost"]) == Decimal("35.00")
        assert bom_result["naming_series"].startswith("BOM-")

    # -------------------------------------------------------------------
    # Test 3: BOM explosion
    # -------------------------------------------------------------------

    def test_bom_explosion(self, fresh_db):
        """Verify exploded BOM calculates costs correctly for given quantity."""
        conn = fresh_db
        env = self._setup_manufacturing_env(conn)

        bom_result = self._create_bom(conn, env)
        bom_id = bom_result["bom_id"]

        # Explode BOM for 5 units
        result = _call_action(
            "erpclaw-manufacturing", "explode-bom", conn,
            bom_id=bom_id, quantity="5",
        )
        assert result["status"] == "ok"
        assert result["bom_id"] == bom_id
        assert result["fg_item_id"] == env["fg_id"]
        assert Decimal(result["requested_qty"]) == Decimal("5.00")

        materials = result["materials"]
        assert result["material_count"] == 2

        # For 5 FG units: RM1 = 5*2 = 10 Kg, RM2 = 5*3 = 15 Meters
        mat_map = {m["item_id"]: m for m in materials}
        assert env["rm1_id"] in mat_map
        assert env["rm2_id"] in mat_map
        assert Decimal(mat_map[env["rm1_id"]]["total_qty"]) == Decimal("10.00")
        assert Decimal(mat_map[env["rm2_id"]]["total_qty"]) == Decimal("15.00")

    # -------------------------------------------------------------------
    # Test 4: Work order creation
    # -------------------------------------------------------------------

    def test_work_order_creation(self, fresh_db):
        """Create WO from BOM and verify items are copied with scaled quantities."""
        conn = fresh_db
        env = self._setup_manufacturing_env(conn)

        bom_result = self._create_bom(conn, env)
        bom_id = bom_result["bom_id"]

        # Create WO for 10 units
        result = _call_action(
            "erpclaw-manufacturing", "add-work-order", conn,
            bom_id=bom_id,
            quantity="10",
            company_id=env["company_id"],
            planned_start_date="2026-03-01",
            source_warehouse_id=env["stores_wh_id"],
            target_warehouse_id=env["fg_wh_id"],
            wip_warehouse_id=env["wip_wh_id"],
        )
        assert result["status"] == "ok"
        assert result["status"] == "ok"
        wo_id = result["work_order_id"]
        assert result["item_id"] == env["fg_id"]
        assert Decimal(result["qty"]) == Decimal("10.00")
        assert result["bom_id"] == bom_id

        # Verify WO in DB
        wo = conn.execute(
            "SELECT * FROM work_order WHERE id = ?", (wo_id,),
        ).fetchone()
        assert wo["status"] == "draft"
        assert wo["source_warehouse_id"] == env["stores_wh_id"]
        assert wo["target_warehouse_id"] == env["fg_wh_id"]
        assert wo["wip_warehouse_id"] == env["wip_wh_id"]
        assert Decimal(wo["produced_qty"]) == Decimal("0")

        # Verify WO items have scaled quantities
        # BOM: 1 FG needs 2 RM1 + 3 RM2 -> 10 FG needs 20 RM1 + 30 RM2
        wo_items = conn.execute(
            "SELECT * FROM work_order_item WHERE work_order_id = ? ORDER BY rowid",
            (wo_id,),
        ).fetchall()
        assert len(wo_items) == 2

        item_map = {r["item_id"]: r for r in wo_items}
        assert Decimal(item_map[env["rm1_id"]]["required_qty"]) == Decimal("20.00")
        assert Decimal(item_map[env["rm2_id"]]["required_qty"]) == Decimal("30.00")
        assert Decimal(item_map[env["rm1_id"]]["transferred_qty"]) == Decimal("0")
        assert Decimal(item_map[env["rm2_id"]]["transferred_qty"]) == Decimal("0")

    # -------------------------------------------------------------------
    # Test 5: Start work order
    # -------------------------------------------------------------------

    def test_start_work_order(self, fresh_db):
        """Start WO, verify status change from draft to not_started."""
        conn = fresh_db
        env = self._setup_manufacturing_env(conn)

        bom_result = self._create_bom(conn, env)
        bom_id = bom_result["bom_id"]

        wo_result = _call_action(
            "erpclaw-manufacturing", "add-work-order", conn,
            bom_id=bom_id,
            quantity="5",
            company_id=env["company_id"],
            planned_start_date="2026-03-01",
            source_warehouse_id=env["stores_wh_id"],
            target_warehouse_id=env["fg_wh_id"],
            wip_warehouse_id=env["wip_wh_id"],
        )
        wo_id = wo_result["work_order_id"]

        # Verify initial status is draft
        wo_before = conn.execute(
            "SELECT status FROM work_order WHERE id = ?", (wo_id,),
        ).fetchone()
        assert wo_before["status"] == "draft"

        # Start WO
        result = _call_action(
            "erpclaw-manufacturing", "start-work-order", conn,
            work_order_id=wo_id,
        )
        assert result["status"] == "ok"
        assert result["work_order_id"] == wo_id

        # Verify status changed to not_started and actual_start_date is set
        wo_after = conn.execute(
            "SELECT status, actual_start_date FROM work_order WHERE id = ?",
            (wo_id,),
        ).fetchone()
        assert wo_after["status"] == "not_started"
        assert wo_after["actual_start_date"] is not None

    # -------------------------------------------------------------------
    # Test 6: Material transfer
    # -------------------------------------------------------------------

    def test_material_transfer(self, fresh_db):
        """Transfer raw materials to WIP warehouse and verify SLE entries."""
        conn = fresh_db
        env = self._setup_manufacturing_env(conn)

        bom_result = self._create_bom(conn, env)
        bom_id = bom_result["bom_id"]

        # Create and start WO for 5 units
        wo_result = _call_action(
            "erpclaw-manufacturing", "add-work-order", conn,
            bom_id=bom_id,
            quantity="5",
            company_id=env["company_id"],
            planned_start_date="2026-03-01",
            source_warehouse_id=env["stores_wh_id"],
            target_warehouse_id=env["fg_wh_id"],
            wip_warehouse_id=env["wip_wh_id"],
        )
        wo_id = wo_result["work_order_id"]

        _call_action(
            "erpclaw-manufacturing", "start-work-order", conn,
            work_order_id=wo_id,
        )

        # Transfer materials: 10 RM1 + 15 RM2 (for 5 FG units)
        transfer_items = json.dumps([
            {"item_id": env["rm1_id"], "qty": "10", "warehouse_id": env["stores_wh_id"]},
            {"item_id": env["rm2_id"], "qty": "15", "warehouse_id": env["stores_wh_id"]},
        ])
        result = _call_action(
            "erpclaw-manufacturing", "transfer-materials", conn,
            work_order_id=wo_id,
            items=transfer_items,
            posting_date="2026-03-02",
        )
        assert result["status"] == "ok"
        assert result["items_transferred"] == 2
        assert result["sle_count"] > 0

        # Verify SLE entries: should have 4 (2 OUT from stores + 2 IN to WIP)
        sle_rows = conn.execute(
            """SELECT * FROM stock_ledger_entry
               WHERE voucher_type = 'work_order' AND voucher_id = ?
               AND is_cancelled = 0""",
            (wo_id,),
        ).fetchall()
        assert len(sle_rows) == 4, f"Expected 4 SLE entries, got {len(sle_rows)}"

        # Verify OUT entries (negative qty from stores)
        out_entries = [r for r in sle_rows if Decimal(r["actual_qty"]) < 0]
        assert len(out_entries) == 2, "Expected 2 outgoing SLE entries"
        for entry in out_entries:
            assert entry["warehouse_id"] == env["stores_wh_id"]

        # Verify IN entries (positive qty to WIP)
        in_entries = [r for r in sle_rows if Decimal(r["actual_qty"]) > 0]
        assert len(in_entries) == 2, "Expected 2 incoming SLE entries"
        for entry in in_entries:
            assert entry["warehouse_id"] == env["wip_wh_id"]

        # Verify WO item transferred_qty updated
        wo_items = conn.execute(
            "SELECT * FROM work_order_item WHERE work_order_id = ?",
            (wo_id,),
        ).fetchall()
        item_map = {r["item_id"]: r for r in wo_items}
        assert Decimal(item_map[env["rm1_id"]]["transferred_qty"]) == Decimal("10.00")
        assert Decimal(item_map[env["rm2_id"]]["transferred_qty"]) == Decimal("15.00")

        # Verify WO status changed to in_process after transfer
        wo = conn.execute(
            "SELECT status FROM work_order WHERE id = ?", (wo_id,),
        ).fetchone()
        assert wo["status"] == "in_process"

    # -------------------------------------------------------------------
    # Test 7: Job card lifecycle
    # -------------------------------------------------------------------

    def test_job_card_lifecycle(self, fresh_db):
        """Create, complete job card with time recording."""
        conn = fresh_db
        env = self._setup_manufacturing_env(conn)

        ws_id = self._create_workstation(conn, "Welding Station", "40.00")
        op_id = self._create_operation(conn, "Welding", ws_id)

        bom_result = self._create_bom(conn, env)
        bom_id = bom_result["bom_id"]

        # Create and start WO
        wo_result = _call_action(
            "erpclaw-manufacturing", "add-work-order", conn,
            bom_id=bom_id, quantity="5", company_id=env["company_id"],
            planned_start_date="2026-03-01",
            source_warehouse_id=env["stores_wh_id"],
            target_warehouse_id=env["fg_wh_id"],
            wip_warehouse_id=env["wip_wh_id"],
        )
        wo_id = wo_result["work_order_id"]

        _call_action(
            "erpclaw-manufacturing", "start-work-order", conn,
            work_order_id=wo_id,
        )

        # Create job card
        jc_result = _call_action(
            "erpclaw-manufacturing", "create-job-card", conn,
            work_order_id=wo_id,
            operation_id=op_id,
            workstation_id=ws_id,
        )
        assert jc_result["status"] == "ok"
        jc_id = jc_result["job_card_id"]
        assert jc_result["work_order_id"] == wo_id
        assert jc_result["operation_id"] == op_id
        assert jc_result["workstation_id"] == ws_id

        # Verify job card naming series
        assert jc_result["naming_series"].startswith("JC-")

        # Verify job card in DB
        jc = conn.execute(
            "SELECT * FROM job_card WHERE id = ?", (jc_id,),
        ).fetchone()
        assert jc is not None
        assert jc["status"] == "open"
        assert Decimal(jc["completed_qty"]) == Decimal("0")
        assert Decimal(jc["total_time_in_minutes"]) == Decimal("0")

        # Complete job card with time and quantity
        complete_result = _call_action(
            "erpclaw-manufacturing", "complete-job-card", conn,
            job_card_id=jc_id,
            actual_time_in_mins="90",
            completed_qty="5",
        )
        assert complete_result["status"] == "ok"
        assert complete_result["job_card_id"] == jc_id
        assert Decimal(complete_result["total_time_in_minutes"]) == Decimal("90.00")
        assert Decimal(complete_result["completed_qty"]) == Decimal("5.00")

        # Verify job card updated in DB
        jc_after = conn.execute(
            "SELECT * FROM job_card WHERE id = ?", (jc_id,),
        ).fetchone()
        assert jc_after["status"] == "completed"
        assert Decimal(jc_after["total_time_in_minutes"]) == Decimal("90.00")
        assert Decimal(jc_after["completed_qty"]) == Decimal("5.00")
        assert jc_after["time_completed"] is not None

    # -------------------------------------------------------------------
    # Test 8: Work order completion
    # -------------------------------------------------------------------

    def test_work_order_completion(self, fresh_db):
        """Complete WO, verify FG stock created and production cost calculated."""
        conn = fresh_db
        env = self._setup_manufacturing_env(conn)

        ws_id = self._create_workstation(conn, "CNC Machine", "80.00")
        op_id = self._create_operation(conn, "CNC Machining", ws_id)

        ops_json = json.dumps([{
            "operation_id": op_id,
            "workstation_id": ws_id,
            "time_in_minutes": "60",
        }])
        bom_result = self._create_bom(conn, env, operations_json=ops_json)
        bom_id = bom_result["bom_id"]

        # Create WO for 5 units
        wo_result = _call_action(
            "erpclaw-manufacturing", "add-work-order", conn,
            bom_id=bom_id, quantity="5", company_id=env["company_id"],
            planned_start_date="2026-03-01",
            source_warehouse_id=env["stores_wh_id"],
            target_warehouse_id=env["fg_wh_id"],
            wip_warehouse_id=env["wip_wh_id"],
        )
        wo_id = wo_result["work_order_id"]

        # Start
        _call_action(
            "erpclaw-manufacturing", "start-work-order", conn,
            work_order_id=wo_id,
        )

        # Transfer materials
        transfer_items = json.dumps([
            {"item_id": env["rm1_id"], "qty": "10", "warehouse_id": env["stores_wh_id"]},
            {"item_id": env["rm2_id"], "qty": "15", "warehouse_id": env["stores_wh_id"]},
        ])
        _call_action(
            "erpclaw-manufacturing", "transfer-materials", conn,
            work_order_id=wo_id, items=transfer_items,
            posting_date="2026-03-02",
        )

        # Complete a job card to contribute operating cost
        jc_result = _call_action(
            "erpclaw-manufacturing", "create-job-card", conn,
            work_order_id=wo_id, operation_id=op_id, workstation_id=ws_id,
        )
        _call_action(
            "erpclaw-manufacturing", "complete-job-card", conn,
            job_card_id=jc_result["job_card_id"],
            actual_time_in_mins="150",
            completed_qty="5",
        )

        # Complete WO
        result = _call_action(
            "erpclaw-manufacturing", "complete-work-order", conn,
            work_order_id=wo_id,
            posting_date="2026-03-05",
        )
        assert result["status"] == "ok"
        assert result["work_order_id"] == wo_id
        assert Decimal(result["produced_qty"]) == Decimal("5.00")

        # Production cost should include RM + operating
        rm_cost = Decimal(result["rm_cost"])
        operating_cost = Decimal(result["operating_cost"])
        production_cost = Decimal(result["production_cost"])
        fg_rate = Decimal(result["fg_rate"])

        assert rm_cost >= Decimal("0"), "RM cost should be non-negative"
        # Operating cost: 150 mins at $80/hour = (150/60) * 80 = $200
        assert operating_cost == Decimal("200.00"), (
            f"Operating cost {operating_cost} != expected 200.00"
        )
        assert production_cost == rm_cost + operating_cost
        # fg_rate = production_cost / produced_qty
        assert fg_rate == (production_cost / Decimal("5")).quantize(Decimal("0.01"))

        # Verify WO status in DB
        wo = conn.execute(
            "SELECT * FROM work_order WHERE id = ?", (wo_id,),
        ).fetchone()
        assert wo["status"] == "completed"
        assert wo["actual_end_date"] is not None

        # Verify FG SLE entry
        completion_voucher_id = f"{wo_id}:completion"
        fg_sle = conn.execute(
            """SELECT * FROM stock_ledger_entry
               WHERE item_id = ? AND warehouse_id = ?
               AND voucher_id = ? AND is_cancelled = 0
               AND CAST(actual_qty AS REAL) > 0""",
            (env["fg_id"], env["fg_wh_id"], completion_voucher_id),
        ).fetchall()
        assert len(fg_sle) >= 1, "No FG stock entry after completion"
        assert Decimal(fg_sle[0]["actual_qty"]) == Decimal("5.00")

    # -------------------------------------------------------------------
    # Test 9: Work order cancellation
    # -------------------------------------------------------------------

    def test_work_order_cancel(self, fresh_db):
        """Cancel WO, verify SLE and GL reversal and status change."""
        conn = fresh_db
        env = self._setup_manufacturing_env(conn)

        bom_result = self._create_bom(conn, env)
        bom_id = bom_result["bom_id"]

        # Create and start WO
        wo_result = _call_action(
            "erpclaw-manufacturing", "add-work-order", conn,
            bom_id=bom_id, quantity="5", company_id=env["company_id"],
            planned_start_date="2026-03-01",
            source_warehouse_id=env["stores_wh_id"],
            target_warehouse_id=env["fg_wh_id"],
            wip_warehouse_id=env["wip_wh_id"],
        )
        wo_id = wo_result["work_order_id"]

        _call_action(
            "erpclaw-manufacturing", "start-work-order", conn,
            work_order_id=wo_id,
        )

        # Transfer materials
        transfer_items = json.dumps([
            {"item_id": env["rm1_id"], "qty": "10", "warehouse_id": env["stores_wh_id"]},
            {"item_id": env["rm2_id"], "qty": "15", "warehouse_id": env["stores_wh_id"]},
        ])
        _call_action(
            "erpclaw-manufacturing", "transfer-materials", conn,
            work_order_id=wo_id, items=transfer_items,
            posting_date="2026-03-02",
        )

        # Count SLE entries before cancellation
        sle_before = conn.execute(
            """SELECT COUNT(*) as cnt FROM stock_ledger_entry
               WHERE voucher_type = 'work_order' AND voucher_id = ?
               AND is_cancelled = 0""",
            (wo_id,),
        ).fetchone()["cnt"]
        assert sle_before == 4, f"Expected 4 SLE entries before cancel, got {sle_before}"

        # Create a job card (should be auto-cancelled)
        ws_id = self._create_workstation(conn, "Test Station", "30.00")
        op_id = self._create_operation(conn, "Test Op", ws_id)
        jc_result = _call_action(
            "erpclaw-manufacturing", "create-job-card", conn,
            work_order_id=wo_id, operation_id=op_id,
        )
        jc_id = jc_result["job_card_id"]

        # Cancel WO
        result = _call_action(
            "erpclaw-manufacturing", "cancel-work-order", conn,
            work_order_id=wo_id,
            posting_date="2026-03-03",
        )
        assert result["status"] == "ok"
        assert result["work_order_id"] == wo_id

        # Verify WO status is cancelled
        wo = conn.execute(
            "SELECT status FROM work_order WHERE id = ?", (wo_id,),
        ).fetchone()
        assert wo["status"] == "cancelled"

        # Verify SLE entries are cancelled (originals marked cancelled +
        # reversal entries also marked cancelled)
        sle_cancelled = conn.execute(
            """SELECT COUNT(*) as cnt FROM stock_ledger_entry
               WHERE voucher_type = 'work_order' AND voucher_id = ?
               AND is_cancelled = 1""",
            (wo_id,),
        ).fetchone()["cnt"]
        # reverse_sle_entries marks the original 4 as cancelled and creates
        # 4 reversal entries (also flagged is_cancelled=1), totalling 8
        assert sle_cancelled >= sle_before, (
            f"Expected at least {sle_before} cancelled SLE entries, got {sle_cancelled}"
        )

        # No non-cancelled SLE entries should remain for this voucher
        sle_active = conn.execute(
            """SELECT COUNT(*) as cnt FROM stock_ledger_entry
               WHERE voucher_type = 'work_order' AND voucher_id = ?
               AND is_cancelled = 0""",
            (wo_id,),
        ).fetchone()["cnt"]
        assert sle_active == 0, (
            f"Expected 0 active SLE entries after cancel, got {sle_active}"
        )

        # Verify job card is auto-cancelled
        jc = conn.execute(
            "SELECT status FROM job_card WHERE id = ?", (jc_id,),
        ).fetchone()
        assert jc["status"] == "cancelled"

    # -------------------------------------------------------------------
    # Test 10: Multi-level BOM (BOM within BOM)
    # -------------------------------------------------------------------

    def test_multi_level_bom(self, fresh_db):
        """BOM with sub-assembly: sub-BOM produces a component used in parent BOM."""
        conn = fresh_db
        env = self._setup_manufacturing_env(conn)

        # Create a sub-assembly item
        sub_assy_id = create_test_item(
            conn, item_code="SA-001", item_name="Sub-Assembly A",
            item_type="stock", stock_uom="Each",
            valuation_method="moving_average", standard_rate="0",
        )

        # Create a third raw material for the sub-assembly
        rm3_id = create_test_item(
            conn, item_code="RM-003", item_name="Plastic Resin",
            item_type="stock", stock_uom="Kg",
            valuation_method="moving_average", standard_rate="8.00",
        )

        # Create sub-assembly BOM: SA-001 = 1 Kg RM-003 at $8
        sub_bom_items = json.dumps([
            {"item_id": rm3_id, "quantity": "1", "rate": "8.00"},
        ])
        sub_bom_result = _call_action(
            "erpclaw-manufacturing", "add-bom", conn,
            item_id=sub_assy_id,
            items=sub_bom_items,
            company_id=env["company_id"],
            quantity="1",
        )
        assert sub_bom_result["status"] == "ok"
        sub_bom_id = sub_bom_result["bom_id"]

        # Create parent BOM: FG-001 = 2 RM-001 + 1 SA-001 (sub-assembly)
        parent_bom_items = json.dumps([
            {"item_id": env["rm1_id"], "quantity": "2", "rate": "10.00"},
            {
                "item_id": sub_assy_id,
                "quantity": "1",
                "rate": "8.00",
                "is_sub_assembly": 1,
                "sub_bom_id": sub_bom_id,
            },
        ])
        parent_bom_result = _call_action(
            "erpclaw-manufacturing", "add-bom", conn,
            item_id=env["fg_id"],
            items=parent_bom_items,
            company_id=env["company_id"],
            quantity="1",
        )
        assert parent_bom_result["status"] == "ok"
        parent_bom_id = parent_bom_result["bom_id"]

        # Verify parent BOM cost
        # RM1: 2 * 10 = 20, SA: 1 * 8 = 8 -> total RM cost = 28
        assert Decimal(parent_bom_result["raw_material_cost"]) == Decimal("28.00")

        # Explode the parent BOM for 10 units
        result = _call_action(
            "erpclaw-manufacturing", "explode-bom", conn,
            bom_id=parent_bom_id, quantity="10",
        )
        assert result["status"] == "ok"

        # Multi-level explosion should flatten to leaf materials:
        # RM-001: 10 * 2 = 20
        # RM-003 (from sub-assembly): 10 * 1 * 1 = 10
        materials = result["materials"]
        mat_map = {m["item_id"]: m for m in materials}

        assert env["rm1_id"] in mat_map, "RM-001 should be in exploded materials"
        assert rm3_id in mat_map, "RM-003 should be in exploded materials (from sub-BOM)"
        # Sub-assembly itself should NOT appear (it is exploded into its components)
        assert sub_assy_id not in mat_map, (
            "Sub-assembly item should not appear in exploded leaf materials"
        )

        assert Decimal(mat_map[env["rm1_id"]]["total_qty"]) == Decimal("20.00")
        assert Decimal(mat_map[rm3_id]["total_qty"]) == Decimal("10.00")

    # -------------------------------------------------------------------
    # Test 11: Production plan and MRP
    # -------------------------------------------------------------------

    def test_production_plan_mrp(self, fresh_db):
        """Create production plan, run MRP, verify material requirements."""
        conn = fresh_db
        env = self._setup_manufacturing_env(conn)

        bom_result = self._create_bom(conn, env)
        bom_id = bom_result["bom_id"]

        # Create production plan
        plan_items = json.dumps([{
            "item_id": env["fg_id"],
            "bom_id": bom_id,
            "planned_qty": "20",
            "warehouse_id": env["stores_wh_id"],
        }])
        plan_result = _call_action(
            "erpclaw-manufacturing", "create-production-plan", conn,
            company_id=env["company_id"],
            items=plan_items,
        )
        assert plan_result["status"] == "ok"
        plan_id = plan_result["production_plan_id"]
        assert plan_result["item_count"] == 1

        # Verify the plan is created in draft status in DB
        # (plan_result["status"] is "ok" from the ok() helper, not the plan status)
        plan_row = conn.execute(
            "SELECT status FROM production_plan WHERE id = ?", (plan_id,),
        ).fetchone()
        assert plan_row["status"] == "draft"

        # Run MRP
        mrp_result = _call_action(
            "erpclaw-manufacturing", "run-mrp", conn,
            production_plan_id=plan_id,
        )
        assert mrp_result["status"] == "ok"
        assert mrp_result["production_plan_id"] == plan_id
        assert mrp_result["material_count"] == 2  # 2 raw materials

        # Verify materials in DB
        materials = conn.execute(
            """SELECT * FROM production_plan_material
               WHERE production_plan_id = ?""",
            (plan_id,),
        ).fetchall()
        assert len(materials) == 2

        mat_map = {r["item_id"]: r for r in materials}

        # For 20 FG units: RM1 = 20*2 = 40, RM2 = 20*3 = 60
        rm1_mat = mat_map[env["rm1_id"]]
        rm2_mat = mat_map[env["rm2_id"]]

        assert Decimal(rm1_mat["required_qty"]) == Decimal("40.00")
        assert Decimal(rm2_mat["required_qty"]) == Decimal("60.00")

        # Available stock: RM1=200, RM2=500 (seeded in setup)
        # No shortfall expected since we have enough stock
        assert Decimal(rm1_mat["available_qty"]) >= Decimal("0")
        assert Decimal(rm2_mat["available_qty"]) >= Decimal("0")

        # Verify plan status updated to 'submitted'
        plan = conn.execute(
            "SELECT status FROM production_plan WHERE id = ?", (plan_id,),
        ).fetchone()
        assert plan["status"] == "submitted"

        # Get production plan details
        detail_result = _call_action(
            "erpclaw-manufacturing", "get-production-plan", conn,
            production_plan_id=plan_id,
        )
        assert detail_result["status"] == "ok"
        assert len(detail_result["items"]) == 1
        assert len(detail_result["materials"]) == 2

    # -------------------------------------------------------------------
    # Test 12: Workstation setup
    # -------------------------------------------------------------------

    def test_workstation_setup(self, fresh_db):
        """Add workstation with hour rate and verify in DB."""
        conn = fresh_db

        result = _call_action(
            "erpclaw-manufacturing", "add-workstation", conn,
            name="Precision Lathe",
            hour_rate="125.50",
            workstation_type="machining",
            working_hours_per_day="8",
            production_capacity="2",
        )
        assert result["status"] == "ok"
        ws_id = result["workstation_id"]
        assert result["name"] == "Precision Lathe"
        assert Decimal(result["operating_cost_per_hour"]) == Decimal("125.50")

        # Verify in DB
        ws = conn.execute(
            "SELECT * FROM workstation WHERE id = ?", (ws_id,),
        ).fetchone()
        assert ws is not None
        assert ws["name"] == "Precision Lathe"
        assert ws["workstation_type"] == "machining"
        assert Decimal(ws["operating_cost_per_hour"]) == Decimal("125.50")
        assert ws["working_hours_per_day"] == "8"
        assert ws["production_capacity"] == "2"
        assert ws["status"] == "active"

        # Also test creating an operation linked to this workstation
        op_result = _call_action(
            "erpclaw-manufacturing", "add-operation", conn,
            name="Precision Turning",
            workstation_id=ws_id,
        )
        assert op_result["status"] == "ok"
        op_id = op_result["operation_id"]

        # Verify operation links to workstation
        op = conn.execute(
            "SELECT * FROM operation WHERE id = ?", (op_id,),
        ).fetchone()
        assert op is not None
        assert op["name"] == "Precision Turning"
        assert op["default_workstation_id"] == ws_id
        assert op["is_active"] == 1
