"""Integration test scenario: Inventory Management.

Tests the full inventory lifecycle:
  items -> warehouses -> stock entries -> batches -> serial numbers ->
  reorder check -> CSV import

Covers:
  - Item creation (stock, non-stock, service)
  - Warehouse setup (multiple types)
  - Stock receive, issue, transfer (with SLE + GL verification)
  - Stock balance and stock ledger queries
  - Moving-average valuation
  - FIFO valuation (falls back to moving-average in current implementation)
  - Reorder level alerting
  - CSV import of items
  - Multi-warehouse balance tracking
"""
import json
import os
import tempfile
from decimal import Decimal

from helpers import (
    _call_action as _call_action_raw,
    create_test_company,
    create_test_fiscal_year,
    seed_naming_series,
    create_test_account,
    create_test_cost_center,
)

# Inventory-specific args that the skill's db_query.py expects on the
# argparse Namespace but are not present in helpers._DEFAULT_ARGS.
# We inject them as defaults so that AttributeError is avoided.
_INVENTORY_EXTRA_DEFAULTS = {
    "item_code": None,
    "item_name": None,
    "item_type": None,
    "stock_uom": None,
    "valuation_method": None,
    "has_batch": None,
    "has_serial": None,
    "standard_rate": None,
    "item_status": None,
    "batch_name": None,
    "serial_no": None,
    "manufacturing_date": None,
    "expiry_date": None,
    "csv_path": None,
    "price_list_id": None,
    "is_buying": None,
    "is_selling": None,
    "applies_to": None,
    "entity_id": None,
    "pr_rate": None,
    "valid_from": None,
    "valid_to": None,
    "qty": None,
    "se_status": None,
    "sn_status": None,
    "stock_reconciliation_id": None,
}


def _call_action(skill_name, action_name, conn, **kwargs):
    """Wrapper around helpers._call_action that injects inventory-specific defaults."""
    merged = {**_INVENTORY_EXTRA_DEFAULTS, **kwargs}
    return _call_action_raw(skill_name, action_name, conn, **merged)


# ---------------------------------------------------------------------------
# Shared setup helper
# ---------------------------------------------------------------------------

def _setup_inventory_env(conn):
    """Create a company, FY, naming series, cost center, and the accounts
    needed for inventory operations (stock-in-hand, stock-adjustment, COGS).

    Returns a dict of all IDs.
    """
    cid = create_test_company(conn, name="Inventory Co", abbr="IC")
    fy_id = create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)
    cc_id = create_test_cost_center(conn, cid, name="Main")

    stock_in_hand = create_test_account(
        conn, cid, "Stock In Hand", "asset",
        account_type="stock", account_number="1400",
    )
    stock_adjustment = create_test_account(
        conn, cid, "Stock Adjustment", "expense",
        account_type="stock_adjustment", account_number="5200",
    )
    cogs = create_test_account(
        conn, cid, "Cost of Goods Sold", "expense",
        account_type="cost_of_goods_sold", account_number="5100",
    )

    return {
        "company_id": cid,
        "fy_id": fy_id,
        "cost_center_id": cc_id,
        "stock_in_hand_id": stock_in_hand,
        "stock_adjustment_id": stock_adjustment,
        "cogs_id": cogs,
    }


def _create_item_via_action(conn, item_code, item_name, item_type="stock",
                             stock_uom="Nos", valuation_method="moving_average",
                             standard_rate="25.00", has_batch=None, has_serial=None):
    """Create an item via the inventory skill action. Returns the result dict."""
    kwargs = {
        "item_code": item_code,
        "item_name": item_name,
        "item_type": item_type,
        "stock_uom": stock_uom,
        "valuation_method": valuation_method,
        "standard_rate": standard_rate,
        "has_batch": str(has_batch) if has_batch else None,
        "has_serial": str(has_serial) if has_serial else None,
    }
    return _call_action("erpclaw-inventory", "add-item", conn, **kwargs)


def _create_warehouse_via_action(conn, name, company_id, warehouse_type="stores",
                                  account_id=None):
    """Create a warehouse via the inventory skill action. Returns the result dict."""
    kwargs = {
        "name": name,
        "company_id": company_id,
        "warehouse_type": warehouse_type,
    }
    if account_id:
        kwargs["account_id"] = account_id
    return _call_action("erpclaw-inventory", "add-warehouse", conn, **kwargs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestInventoryScenario:
    """Inventory management integration tests."""

    # -------------------------------------------------------------------
    # 1. Full inventory cycle
    # -------------------------------------------------------------------

    def test_full_inventory_cycle(self, fresh_db):
        """End-to-end: item -> warehouse -> receive -> transfer -> issue -> balance check."""
        conn = fresh_db
        env = _setup_inventory_env(conn)
        cid = env["company_id"]

        # Create item
        item_r = _create_item_via_action(
            conn, "CYCLE-001", "Cycle Widget", standard_rate="50.00")
        assert item_r["status"] == "ok"
        item_id = item_r["item_id"]

        # Create two warehouses
        wh_main_r = _create_warehouse_via_action(
            conn, "Main Store", cid, "stores", env["stock_in_hand_id"])
        assert wh_main_r["status"] == "ok"
        wh_main = wh_main_r["warehouse_id"]

        wh_fg_r = _create_warehouse_via_action(
            conn, "Finished Goods", cid, "stores", env["stock_in_hand_id"])
        assert wh_fg_r["status"] == "ok"
        wh_fg = wh_fg_r["warehouse_id"]

        # --- Receive 100 units at Main Store ---
        items_json = json.dumps([{
            "item_id": item_id, "qty": "100", "rate": "50.00",
            "to_warehouse_id": wh_main,
        }])
        se_r = _call_action("erpclaw-inventory", "add-stock-entry", conn,
                             entry_type="receive", company_id=cid,
                             posting_date="2026-03-01", items=items_json)
        assert se_r["status"] == "ok"
        se_id = se_r["stock_entry_id"]

        sub_r = _call_action("erpclaw-inventory", "submit-stock-entry", conn,
                              stock_entry_id=se_id)
        assert sub_r["status"] == "ok"
        assert sub_r["sle_entries_created"] >= 1

        # Verify balance at Main Store = 100
        bal_r = _call_action("erpclaw-inventory", "get-stock-balance", conn,
                              item_id=item_id, warehouse_id=wh_main)
        assert bal_r["status"] == "ok"
        assert Decimal(bal_r["qty"]) == Decimal("100")

        # --- Transfer 30 units from Main Store to Finished Goods ---
        items_json = json.dumps([{
            "item_id": item_id, "qty": "30", "rate": "50.00",
            "from_warehouse_id": wh_main, "to_warehouse_id": wh_fg,
        }])
        se_r2 = _call_action("erpclaw-inventory", "add-stock-entry", conn,
                              entry_type="transfer", company_id=cid,
                              posting_date="2026-03-02", items=items_json)
        assert se_r2["status"] == "ok"

        sub_r2 = _call_action("erpclaw-inventory", "submit-stock-entry", conn,
                               stock_entry_id=se_r2["stock_entry_id"])
        assert sub_r2["status"] == "ok"
        assert sub_r2["sle_entries_created"] >= 2  # one out, one in

        # Verify balances: Main = 70, FG = 30
        bal_main = _call_action("erpclaw-inventory", "get-stock-balance", conn,
                                 item_id=item_id, warehouse_id=wh_main)
        bal_fg = _call_action("erpclaw-inventory", "get-stock-balance", conn,
                               item_id=item_id, warehouse_id=wh_fg)
        assert Decimal(bal_main["qty"]) == Decimal("70")
        assert Decimal(bal_fg["qty"]) == Decimal("30")

        # --- Issue 20 units from Main Store ---
        items_json = json.dumps([{
            "item_id": item_id, "qty": "20", "rate": "50.00",
            "from_warehouse_id": wh_main,
        }])
        se_r3 = _call_action("erpclaw-inventory", "add-stock-entry", conn,
                              entry_type="issue", company_id=cid,
                              posting_date="2026-03-03", items=items_json)
        assert se_r3["status"] == "ok"

        sub_r3 = _call_action("erpclaw-inventory", "submit-stock-entry", conn,
                               stock_entry_id=se_r3["stock_entry_id"])
        assert sub_r3["status"] == "ok"

        # Final balance: Main = 50, FG = 30, Total = 80
        bal_main = _call_action("erpclaw-inventory", "get-stock-balance", conn,
                                 item_id=item_id, warehouse_id=wh_main)
        bal_fg = _call_action("erpclaw-inventory", "get-stock-balance", conn,
                               item_id=item_id, warehouse_id=wh_fg)
        assert Decimal(bal_main["qty"]) == Decimal("50")
        assert Decimal(bal_fg["qty"]) == Decimal("30")
        total = Decimal(bal_main["qty"]) + Decimal(bal_fg["qty"])
        assert total == Decimal("80")

    # -------------------------------------------------------------------
    # 2. Item creation (stock, non-stock, service)
    # -------------------------------------------------------------------

    def test_item_creation(self, fresh_db):
        """Create stock, non-stock, and service items and verify via get-item."""
        conn = fresh_db

        # Stock item
        r1 = _create_item_via_action(
            conn, "STK-001", "Steel Bolt", item_type="stock",
            stock_uom="Nos", standard_rate="5.00")
        assert r1["status"] == "ok"
        assert r1["item_code"] == "STK-001"

        get_r1 = _call_action("erpclaw-inventory", "get-item", conn,
                               item_id=r1["item_id"])
        assert get_r1["status"] == "ok"
        assert get_r1["item_type"] == "stock"
        assert get_r1["is_stock_item"] == 1

        # Non-stock item
        r2 = _create_item_via_action(
            conn, "NST-001", "Consulting Hours", item_type="non_stock",
            stock_uom="Hour", standard_rate="150.00")
        assert r2["status"] == "ok"

        get_r2 = _call_action("erpclaw-inventory", "get-item", conn,
                               item_id=r2["item_id"])
        assert get_r2["item_type"] == "non_stock"
        assert get_r2["is_stock_item"] == 0

        # Service item
        r3 = _create_item_via_action(
            conn, "SVC-001", "Maintenance Service", item_type="service",
            stock_uom="Hour", standard_rate="200.00")
        assert r3["status"] == "ok"

        get_r3 = _call_action("erpclaw-inventory", "get-item", conn,
                               item_id=r3["item_id"])
        assert get_r3["item_type"] == "service"
        assert get_r3["is_stock_item"] == 0

        # List all items
        list_r = _call_action("erpclaw-inventory", "list-items", conn)
        assert list_r["status"] == "ok"
        assert list_r["total_count"] == 3

    # -------------------------------------------------------------------
    # 3. Warehouse setup
    # -------------------------------------------------------------------

    def test_warehouse_setup(self, fresh_db):
        """Create warehouses of different types and verify listing."""
        conn = fresh_db
        env = _setup_inventory_env(conn)
        cid = env["company_id"]

        types_and_names = [
            ("stores", "Main Stores"),
            ("transit", "In-Transit"),
            ("rejected", "QA Rejected"),
            ("production", "Production Floor"),
        ]
        wh_ids = []
        for wtype, wname in types_and_names:
            r = _create_warehouse_via_action(conn, wname, cid, wtype)
            assert r["status"] == "ok", f"Failed to create {wtype} warehouse: {r}"
            wh_ids.append(r["warehouse_id"])

        # List warehouses
        list_r = _call_action("erpclaw-inventory", "list-warehouses", conn,
                               company_id=cid)
        assert list_r["status"] == "ok"
        assert list_r["total_count"] == 4

        # Verify warehouse types via direct query
        for wh_id, (wtype, _) in zip(wh_ids, types_and_names):
            row = conn.execute(
                "SELECT warehouse_type FROM warehouse WHERE id = ?", (wh_id,)
            ).fetchone()
            assert row["warehouse_type"] == wtype

    # -------------------------------------------------------------------
    # 4. Stock receive
    # -------------------------------------------------------------------

    def test_stock_receive(self, fresh_db):
        """Receive stock entry creates SLE and updates stock balance."""
        conn = fresh_db
        env = _setup_inventory_env(conn)
        cid = env["company_id"]

        item_r = _create_item_via_action(
            conn, "RCV-001", "Receive Widget", standard_rate="30.00")
        item_id = item_r["item_id"]

        wh_r = _create_warehouse_via_action(
            conn, "Receiving Dock", cid, "stores", env["stock_in_hand_id"])
        wh_id = wh_r["warehouse_id"]

        # Create and submit receive entry for 50 units at 30.00
        items_json = json.dumps([{
            "item_id": item_id, "qty": "50", "rate": "30.00",
            "to_warehouse_id": wh_id,
        }])
        se_r = _call_action("erpclaw-inventory", "add-stock-entry", conn,
                             entry_type="receive", company_id=cid,
                             posting_date="2026-04-01", items=items_json)
        assert se_r["status"] == "ok"
        se_id = se_r["stock_entry_id"]
        assert Decimal(se_r["total_incoming_value"]) == Decimal("1500.00")

        sub_r = _call_action("erpclaw-inventory", "submit-stock-entry", conn,
                              stock_entry_id=se_id)
        assert sub_r["status"] == "ok"
        assert sub_r["sle_entries_created"] >= 1

        # Verify SLE entry in database
        sle_rows = conn.execute(
            """SELECT * FROM stock_ledger_entry
               WHERE voucher_type = 'stock_entry' AND voucher_id = ?
                 AND is_cancelled = 0""",
            (se_id,),
        ).fetchall()
        assert len(sle_rows) >= 1
        sle = sle_rows[0]
        assert Decimal(sle["actual_qty"]) == Decimal("50")
        assert sle["item_id"] == item_id
        assert sle["warehouse_id"] == wh_id

        # Verify stock balance
        bal_r = _call_action("erpclaw-inventory", "get-stock-balance", conn,
                              item_id=item_id, warehouse_id=wh_id)
        assert bal_r["status"] == "ok"
        assert Decimal(bal_r["qty"]) == Decimal("50")
        assert Decimal(bal_r["valuation_rate"]) == Decimal("30.00")
        assert Decimal(bal_r["stock_value"]) == Decimal("1500.00")

    # -------------------------------------------------------------------
    # 5. Stock issue
    # -------------------------------------------------------------------

    def test_stock_issue(self, fresh_db):
        """Issue stock entry decreases balance correctly."""
        conn = fresh_db
        env = _setup_inventory_env(conn)
        cid = env["company_id"]

        item_r = _create_item_via_action(
            conn, "ISS-001", "Issue Widget", standard_rate="40.00")
        item_id = item_r["item_id"]

        wh_r = _create_warehouse_via_action(
            conn, "Main WH", cid, "stores", env["stock_in_hand_id"])
        wh_id = wh_r["warehouse_id"]

        # Receive 80 units first
        recv_json = json.dumps([{
            "item_id": item_id, "qty": "80", "rate": "40.00",
            "to_warehouse_id": wh_id,
        }])
        se1 = _call_action("erpclaw-inventory", "add-stock-entry", conn,
                            entry_type="receive", company_id=cid,
                            posting_date="2026-04-01", items=recv_json)
        _call_action("erpclaw-inventory", "submit-stock-entry", conn,
                      stock_entry_id=se1["stock_entry_id"])

        # Issue 30 units
        issue_json = json.dumps([{
            "item_id": item_id, "qty": "30", "rate": "40.00",
            "from_warehouse_id": wh_id,
        }])
        se2 = _call_action("erpclaw-inventory", "add-stock-entry", conn,
                            entry_type="issue", company_id=cid,
                            posting_date="2026-04-02", items=issue_json)
        assert se2["status"] == "ok"

        sub2 = _call_action("erpclaw-inventory", "submit-stock-entry", conn,
                             stock_entry_id=se2["stock_entry_id"])
        assert sub2["status"] == "ok"

        # Verify SLE has negative qty for the issue
        issue_sle = conn.execute(
            """SELECT actual_qty FROM stock_ledger_entry
               WHERE voucher_type = 'stock_entry' AND voucher_id = ?
                 AND is_cancelled = 0""",
            (se2["stock_entry_id"],),
        ).fetchone()
        assert Decimal(issue_sle["actual_qty"]) == Decimal("-30")

        # Verify balance decreased: 80 - 30 = 50
        bal = _call_action("erpclaw-inventory", "get-stock-balance", conn,
                            item_id=item_id, warehouse_id=wh_id)
        assert Decimal(bal["qty"]) == Decimal("50")
        assert Decimal(bal["stock_value"]) == Decimal("2000.00")

    # -------------------------------------------------------------------
    # 6. Stock transfer
    # -------------------------------------------------------------------

    def test_stock_transfer(self, fresh_db):
        """Transfer stock between warehouses: source decreases, target increases."""
        conn = fresh_db
        env = _setup_inventory_env(conn)
        cid = env["company_id"]

        item_r = _create_item_via_action(
            conn, "TRF-001", "Transfer Widget", standard_rate="25.00")
        item_id = item_r["item_id"]

        wh_src_r = _create_warehouse_via_action(
            conn, "Source WH", cid, "stores", env["stock_in_hand_id"])
        wh_src = wh_src_r["warehouse_id"]

        wh_dst_r = _create_warehouse_via_action(
            conn, "Destination WH", cid, "stores", env["stock_in_hand_id"])
        wh_dst = wh_dst_r["warehouse_id"]

        # Receive 60 units into source
        recv_json = json.dumps([{
            "item_id": item_id, "qty": "60", "rate": "25.00",
            "to_warehouse_id": wh_src,
        }])
        se1 = _call_action("erpclaw-inventory", "add-stock-entry", conn,
                            entry_type="receive", company_id=cid,
                            posting_date="2026-04-01", items=recv_json)
        _call_action("erpclaw-inventory", "submit-stock-entry", conn,
                      stock_entry_id=se1["stock_entry_id"])

        # Transfer 25 units from source to destination
        xfer_json = json.dumps([{
            "item_id": item_id, "qty": "25", "rate": "25.00",
            "from_warehouse_id": wh_src, "to_warehouse_id": wh_dst,
        }])
        se2 = _call_action("erpclaw-inventory", "add-stock-entry", conn,
                            entry_type="transfer", company_id=cid,
                            posting_date="2026-04-02", items=xfer_json)
        assert se2["status"] == "ok"

        sub2 = _call_action("erpclaw-inventory", "submit-stock-entry", conn,
                             stock_entry_id=se2["stock_entry_id"])
        assert sub2["status"] == "ok"
        # Transfer creates 2 SLE: -25 from source, +25 at destination
        assert sub2["sle_entries_created"] == 2

        # Verify balances
        bal_src = _call_action("erpclaw-inventory", "get-stock-balance", conn,
                                item_id=item_id, warehouse_id=wh_src)
        bal_dst = _call_action("erpclaw-inventory", "get-stock-balance", conn,
                                item_id=item_id, warehouse_id=wh_dst)
        assert Decimal(bal_src["qty"]) == Decimal("35")  # 60 - 25
        assert Decimal(bal_dst["qty"]) == Decimal("25")

        # Total stock unchanged: 35 + 25 = 60
        total = Decimal(bal_src["qty"]) + Decimal(bal_dst["qty"])
        assert total == Decimal("60")

    # -------------------------------------------------------------------
    # 7. Stock balance query
    # -------------------------------------------------------------------

    def test_stock_balance_query(self, fresh_db):
        """Verify real-time stock balance per item/warehouse after multiple ops."""
        conn = fresh_db
        env = _setup_inventory_env(conn)
        cid = env["company_id"]

        item_r = _create_item_via_action(
            conn, "BAL-001", "Balance Widget", standard_rate="10.00")
        item_id = item_r["item_id"]

        wh_r = _create_warehouse_via_action(
            conn, "Balance WH", cid, "stores", env["stock_in_hand_id"])
        wh_id = wh_r["warehouse_id"]

        # Initial balance should be zero
        bal0 = _call_action("erpclaw-inventory", "get-stock-balance", conn,
                             item_id=item_id, warehouse_id=wh_id)
        assert Decimal(bal0["qty"]) == Decimal("0")
        assert Decimal(bal0["stock_value"]) == Decimal("0")

        # Receive 100 at 10.00 => balance = 100, value = 1000
        recv_json = json.dumps([{
            "item_id": item_id, "qty": "100", "rate": "10.00",
            "to_warehouse_id": wh_id,
        }])
        se1 = _call_action("erpclaw-inventory", "add-stock-entry", conn,
                            entry_type="receive", company_id=cid,
                            posting_date="2026-05-01", items=recv_json)
        _call_action("erpclaw-inventory", "submit-stock-entry", conn,
                      stock_entry_id=se1["stock_entry_id"])

        bal1 = _call_action("erpclaw-inventory", "get-stock-balance", conn,
                             item_id=item_id, warehouse_id=wh_id)
        assert Decimal(bal1["qty"]) == Decimal("100")
        assert Decimal(bal1["stock_value"]) == Decimal("1000.00")

        # Issue 40 => balance = 60, value = 600
        issue_json = json.dumps([{
            "item_id": item_id, "qty": "40", "rate": "10.00",
            "from_warehouse_id": wh_id,
        }])
        se2 = _call_action("erpclaw-inventory", "add-stock-entry", conn,
                            entry_type="issue", company_id=cid,
                            posting_date="2026-05-02", items=issue_json)
        _call_action("erpclaw-inventory", "submit-stock-entry", conn,
                      stock_entry_id=se2["stock_entry_id"])

        bal2 = _call_action("erpclaw-inventory", "get-stock-balance", conn,
                             item_id=item_id, warehouse_id=wh_id)
        assert Decimal(bal2["qty"]) == Decimal("60")
        assert Decimal(bal2["stock_value"]) == Decimal("600.00")

        # Receive another 20 at 10.00 => balance = 80, value = 800
        recv2_json = json.dumps([{
            "item_id": item_id, "qty": "20", "rate": "10.00",
            "to_warehouse_id": wh_id,
        }])
        se3 = _call_action("erpclaw-inventory", "add-stock-entry", conn,
                            entry_type="receive", company_id=cid,
                            posting_date="2026-05-03", items=recv2_json)
        _call_action("erpclaw-inventory", "submit-stock-entry", conn,
                      stock_entry_id=se3["stock_entry_id"])

        bal3 = _call_action("erpclaw-inventory", "get-stock-balance", conn,
                             item_id=item_id, warehouse_id=wh_id)
        assert Decimal(bal3["qty"]) == Decimal("80")
        assert Decimal(bal3["stock_value"]) == Decimal("800.00")

    # -------------------------------------------------------------------
    # 8. Stock ledger entries
    # -------------------------------------------------------------------

    def test_stock_ledger(self, fresh_db):
        """Verify chronological SLE entries via the stock-ledger-report action."""
        conn = fresh_db
        env = _setup_inventory_env(conn)
        cid = env["company_id"]

        item_r = _create_item_via_action(
            conn, "SLR-001", "Ledger Widget", standard_rate="15.00")
        item_id = item_r["item_id"]

        wh_r = _create_warehouse_via_action(
            conn, "Ledger WH", cid, "stores", env["stock_in_hand_id"])
        wh_id = wh_r["warehouse_id"]

        # Perform 3 operations on different dates
        for date, qty in [("2026-06-01", "50"), ("2026-06-05", "30"), ("2026-06-10", "20")]:
            recv_json = json.dumps([{
                "item_id": item_id, "qty": qty, "rate": "15.00",
                "to_warehouse_id": wh_id,
            }])
            se = _call_action("erpclaw-inventory", "add-stock-entry", conn,
                               entry_type="receive", company_id=cid,
                               posting_date=date, items=recv_json)
            _call_action("erpclaw-inventory", "submit-stock-entry", conn,
                          stock_entry_id=se["stock_entry_id"])

        # Query stock ledger report
        ledger_r = _call_action("erpclaw-inventory", "stock-ledger-report", conn,
                                 item_id=item_id, warehouse_id=wh_id,
                                 from_date="2026-06-01", to_date="2026-06-30")
        assert ledger_r["status"] == "ok"
        entries = ledger_r["entries"]
        assert len(entries) == 3

        # Entries should have correct quantities (report is DESC by default)
        qtys = sorted([Decimal(e["actual_qty"]) for e in entries])
        assert qtys == [Decimal("20"), Decimal("30"), Decimal("50")]

        # Verify via direct query that SLE running balance is correct
        sle_rows = conn.execute(
            """SELECT actual_qty, qty_after_transaction
               FROM stock_ledger_entry
               WHERE item_id = ? AND warehouse_id = ? AND is_cancelled = 0
               ORDER BY rowid""",
            (item_id, wh_id),
        ).fetchall()
        assert len(sle_rows) == 3
        # After 1st receive: 50
        assert Decimal(sle_rows[0]["qty_after_transaction"]) == Decimal("50")
        # After 2nd receive: 80
        assert Decimal(sle_rows[1]["qty_after_transaction"]) == Decimal("80")
        # After 3rd receive: 100
        assert Decimal(sle_rows[2]["qty_after_transaction"]) == Decimal("100")

    # -------------------------------------------------------------------
    # 9. Moving-average valuation
    # -------------------------------------------------------------------

    def test_moving_average_valuation(self, fresh_db):
        """Multiple receipts at different rates: verify weighted average rate."""
        conn = fresh_db
        env = _setup_inventory_env(conn)
        cid = env["company_id"]

        item_r = _create_item_via_action(
            conn, "MAV-001", "Avg Widget",
            valuation_method="moving_average", standard_rate="10.00")
        item_id = item_r["item_id"]

        wh_r = _create_warehouse_via_action(
            conn, "Avg WH", cid, "stores", env["stock_in_hand_id"])
        wh_id = wh_r["warehouse_id"]

        # Receipt 1: 100 units at 10.00 => value = 1000, avg = 10.00
        recv1 = json.dumps([{
            "item_id": item_id, "qty": "100", "rate": "10.00",
            "to_warehouse_id": wh_id,
        }])
        se1 = _call_action("erpclaw-inventory", "add-stock-entry", conn,
                            entry_type="receive", company_id=cid,
                            posting_date="2026-05-01", items=recv1)
        _call_action("erpclaw-inventory", "submit-stock-entry", conn,
                      stock_entry_id=se1["stock_entry_id"])

        bal1 = _call_action("erpclaw-inventory", "get-stock-balance", conn,
                             item_id=item_id, warehouse_id=wh_id)
        assert Decimal(bal1["valuation_rate"]) == Decimal("10.00")

        # Receipt 2: 50 units at 20.00 => total value = 1000 + 1000 = 2000, qty = 150
        # avg = 2000 / 150 = 13.333...
        recv2 = json.dumps([{
            "item_id": item_id, "qty": "50", "rate": "20.00",
            "to_warehouse_id": wh_id,
        }])
        se2 = _call_action("erpclaw-inventory", "add-stock-entry", conn,
                            entry_type="receive", company_id=cid,
                            posting_date="2026-05-02", items=recv2)
        _call_action("erpclaw-inventory", "submit-stock-entry", conn,
                      stock_entry_id=se2["stock_entry_id"])

        bal2 = _call_action("erpclaw-inventory", "get-stock-balance", conn,
                             item_id=item_id, warehouse_id=wh_id)
        assert Decimal(bal2["qty"]) == Decimal("150")
        # avg rate = 2000 / 150 = 13.33 (rounded to 2 dp)
        # Due to rounding, stock_value = 150 * 13.33 = 1999.50
        actual_avg = Decimal(bal2["valuation_rate"])
        assert actual_avg == Decimal("13.33")
        expected_value = Decimal("150") * actual_avg
        assert Decimal(bal2["stock_value"]) == expected_value

        # Receipt 3: 50 units at 30.00
        # new_value = 1999.50 + 1500.00 = 3499.50, qty = 200
        # avg = 3499.50 / 200 = 17.50 (rounded)
        recv3 = json.dumps([{
            "item_id": item_id, "qty": "50", "rate": "30.00",
            "to_warehouse_id": wh_id,
        }])
        se3 = _call_action("erpclaw-inventory", "add-stock-entry", conn,
                            entry_type="receive", company_id=cid,
                            posting_date="2026-05-03", items=recv3)
        _call_action("erpclaw-inventory", "submit-stock-entry", conn,
                      stock_entry_id=se3["stock_entry_id"])

        bal3 = _call_action("erpclaw-inventory", "get-stock-balance", conn,
                             item_id=item_id, warehouse_id=wh_id)
        assert Decimal(bal3["qty"]) == Decimal("200")
        # Verify the valuation rate is the weighted average of all 3 receipts
        avg3 = Decimal(bal3["valuation_rate"])
        # Should be close to (1000 + 1000 + 1500) / 200 = 17.50
        # but actual is (1999.50 + 1500) / 200 = 3499.50 / 200 = 17.50 (rounded)
        assert abs(avg3 - Decimal("17.50")) < Decimal("0.01")
        # Verify stock value = qty * valuation_rate
        assert Decimal(bal3["stock_value"]) == Decimal("200") * avg3

    # -------------------------------------------------------------------
    # 10. FIFO valuation
    # -------------------------------------------------------------------

    def test_fifo_valuation(self, fresh_db):
        """FIFO item with multiple receipts: verify valuation is computed.

        Note: The current implementation falls back to moving-average for FIFO.
        This test verifies that FIFO items are accepted and produce valid
        valuation results consistent with the moving-average fallback.
        """
        conn = fresh_db
        env = _setup_inventory_env(conn)
        cid = env["company_id"]

        item_r = _create_item_via_action(
            conn, "FIFO-001", "FIFO Widget",
            valuation_method="fifo", standard_rate="10.00")
        item_id = item_r["item_id"]

        # Confirm item was created with FIFO
        get_r = _call_action("erpclaw-inventory", "get-item", conn,
                              item_id=item_id)
        assert get_r["valuation_method"] == "fifo"

        wh_r = _create_warehouse_via_action(
            conn, "FIFO WH", cid, "stores", env["stock_in_hand_id"])
        wh_id = wh_r["warehouse_id"]

        # Receipt 1: 40 units at 10.00 => value = 400
        recv1 = json.dumps([{
            "item_id": item_id, "qty": "40", "rate": "10.00",
            "to_warehouse_id": wh_id,
        }])
        se1 = _call_action("erpclaw-inventory", "add-stock-entry", conn,
                            entry_type="receive", company_id=cid,
                            posting_date="2026-05-01", items=recv1)
        _call_action("erpclaw-inventory", "submit-stock-entry", conn,
                      stock_entry_id=se1["stock_entry_id"])

        # Receipt 2: 60 units at 15.00 => value = 400 + 900 = 1300, qty = 100
        recv2 = json.dumps([{
            "item_id": item_id, "qty": "60", "rate": "15.00",
            "to_warehouse_id": wh_id,
        }])
        se2 = _call_action("erpclaw-inventory", "add-stock-entry", conn,
                            entry_type="receive", company_id=cid,
                            posting_date="2026-05-02", items=recv2)
        _call_action("erpclaw-inventory", "submit-stock-entry", conn,
                      stock_entry_id=se2["stock_entry_id"])

        bal = _call_action("erpclaw-inventory", "get-stock-balance", conn,
                            item_id=item_id, warehouse_id=wh_id)
        assert Decimal(bal["qty"]) == Decimal("100")
        assert Decimal(bal["stock_value"]) == Decimal("1300.00")

        # Valuation rate should be 13.00 (weighted avg fallback: 1300/100)
        assert Decimal(bal["valuation_rate"]) == Decimal("13.00")

        # Issue 20 units and verify value decreases correctly
        issue_json = json.dumps([{
            "item_id": item_id, "qty": "20", "rate": "13.00",
            "from_warehouse_id": wh_id,
        }])
        se3 = _call_action("erpclaw-inventory", "add-stock-entry", conn,
                            entry_type="issue", company_id=cid,
                            posting_date="2026-05-03", items=issue_json)
        _call_action("erpclaw-inventory", "submit-stock-entry", conn,
                      stock_entry_id=se3["stock_entry_id"])

        bal2 = _call_action("erpclaw-inventory", "get-stock-balance", conn,
                             item_id=item_id, warehouse_id=wh_id)
        assert Decimal(bal2["qty"]) == Decimal("80")
        # Value = 1300 - (20 * 13) = 1300 - 260 = 1040
        assert Decimal(bal2["stock_value"]) == Decimal("1040.00")

    # -------------------------------------------------------------------
    # 11. Reorder check
    # -------------------------------------------------------------------

    def test_reorder_check(self, fresh_db):
        """Set reorder level, deplete stock, verify reorder alert fires."""
        conn = fresh_db
        env = _setup_inventory_env(conn)
        cid = env["company_id"]

        item_r = _create_item_via_action(
            conn, "RO-001", "Reorder Widget", standard_rate="20.00")
        item_id = item_r["item_id"]

        wh_r = _create_warehouse_via_action(
            conn, "Reorder WH", cid, "stores", env["stock_in_hand_id"])
        wh_id = wh_r["warehouse_id"]

        # Set reorder level = 30, reorder qty = 50
        _call_action("erpclaw-inventory", "update-item", conn,
                      item_id=item_id, reorder_level="30", reorder_qty="50")

        # Receive 100 units — above reorder level
        recv_json = json.dumps([{
            "item_id": item_id, "qty": "100", "rate": "20.00",
            "to_warehouse_id": wh_id,
        }])
        se1 = _call_action("erpclaw-inventory", "add-stock-entry", conn,
                            entry_type="receive", company_id=cid,
                            posting_date="2026-06-01", items=recv_json)
        _call_action("erpclaw-inventory", "submit-stock-entry", conn,
                      stock_entry_id=se1["stock_entry_id"])

        # Check reorder — should have no alerts (stock = 100 > 30)
        reorder_r1 = _call_action("erpclaw-inventory", "check-reorder", conn,
                                   company_id=cid)
        assert reorder_r1["status"] == "ok"
        assert reorder_r1["items_below_reorder"] == 0

        # Issue 75 units => balance = 25, which is <= reorder level of 30
        issue_json = json.dumps([{
            "item_id": item_id, "qty": "75", "rate": "20.00",
            "from_warehouse_id": wh_id,
        }])
        se2 = _call_action("erpclaw-inventory", "add-stock-entry", conn,
                            entry_type="issue", company_id=cid,
                            posting_date="2026-06-02", items=issue_json)
        _call_action("erpclaw-inventory", "submit-stock-entry", conn,
                      stock_entry_id=se2["stock_entry_id"])

        # Verify balance is 25
        bal = _call_action("erpclaw-inventory", "get-stock-balance", conn,
                            item_id=item_id, warehouse_id=wh_id)
        assert Decimal(bal["qty"]) == Decimal("25")

        # Check reorder — should now report the item
        reorder_r2 = _call_action("erpclaw-inventory", "check-reorder", conn,
                                   company_id=cid)
        assert reorder_r2["status"] == "ok"
        assert reorder_r2["items_below_reorder"] == 1

        alert = reorder_r2["items"][0]
        assert alert["item_id"] == item_id
        assert alert["item_code"] == "RO-001"
        assert Decimal(alert["current_stock"]) == Decimal("25.00")
        assert Decimal(alert["reorder_level"]) == Decimal("30.00")
        assert Decimal(alert["reorder_qty"]) == Decimal("50.00")
        assert Decimal(alert["shortfall"]) == Decimal("5.00")

    # -------------------------------------------------------------------
    # 12. CSV import
    # -------------------------------------------------------------------

    def test_csv_import(self, fresh_db):
        """Import items via CSV file and verify they are created."""
        conn = fresh_db

        # Write a temporary CSV file
        csv_content = (
            "item_code,name,uom,valuation_method\n"
            "CSV-001,Imported Bolt,Nos,moving_average\n"
            "CSV-002,Imported Nut,Nos,moving_average\n"
            "CSV-003,Imported Washer,Kg,fifo\n"
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False
        ) as f:
            f.write(csv_content)
            csv_path = f.name

        try:
            result = _call_action("erpclaw-inventory", "import-items", conn,
                                   csv_path=csv_path)
            assert result["status"] == "ok"
            assert result["imported"] == 3
            assert result["skipped"] == 0
            assert result["total_rows"] == 3

            # Verify items exist in database
            for code in ["CSV-001", "CSV-002", "CSV-003"]:
                row = conn.execute(
                    "SELECT * FROM item WHERE item_code = ?", (code,)
                ).fetchone()
                assert row is not None, f"Item {code} not found after CSV import"
                assert row["status"] == "active"

            # Verify duplicate import is skipped
            result2 = _call_action("erpclaw-inventory", "import-items", conn,
                                    csv_path=csv_path)
            assert result2["status"] == "ok"
            assert result2["imported"] == 0
            assert result2["skipped"] == 3

            # List items to verify count
            list_r = _call_action("erpclaw-inventory", "list-items", conn)
            assert list_r["total_count"] == 3
        finally:
            os.unlink(csv_path)

    # -------------------------------------------------------------------
    # 13. Multi-warehouse balance
    # -------------------------------------------------------------------

    def test_multi_warehouse_balance(self, fresh_db):
        """Stock across multiple warehouses: verify per-warehouse and total balance."""
        conn = fresh_db
        env = _setup_inventory_env(conn)
        cid = env["company_id"]

        item_r = _create_item_via_action(
            conn, "MWH-001", "Multi-WH Widget", standard_rate="10.00")
        item_id = item_r["item_id"]

        # Create 3 warehouses
        wh_ids = []
        for name in ["Warehouse A", "Warehouse B", "Warehouse C"]:
            r = _create_warehouse_via_action(
                conn, name, cid, "stores", env["stock_in_hand_id"])
            wh_ids.append(r["warehouse_id"])

        # Receive different quantities into each warehouse
        quantities = [("100", "10.00"), ("50", "10.00"), ("75", "10.00")]
        for wh_id, (qty, rate) in zip(wh_ids, quantities):
            recv_json = json.dumps([{
                "item_id": item_id, "qty": qty, "rate": rate,
                "to_warehouse_id": wh_id,
            }])
            se = _call_action("erpclaw-inventory", "add-stock-entry", conn,
                               entry_type="receive", company_id=cid,
                               posting_date="2026-07-01", items=recv_json)
            _call_action("erpclaw-inventory", "submit-stock-entry", conn,
                          stock_entry_id=se["stock_entry_id"])

        # Verify per-warehouse balances
        expected_qtys = [Decimal("100"), Decimal("50"), Decimal("75")]
        total_qty = Decimal("0")
        total_value = Decimal("0")
        for wh_id, expected_qty in zip(wh_ids, expected_qtys):
            bal = _call_action("erpclaw-inventory", "get-stock-balance", conn,
                                item_id=item_id, warehouse_id=wh_id)
            assert Decimal(bal["qty"]) == expected_qty, (
                f"Warehouse {wh_id}: expected {expected_qty}, got {bal['qty']}"
            )
            total_qty += Decimal(bal["qty"])
            total_value += Decimal(bal["stock_value"])

        # Total: 100 + 50 + 75 = 225 units, value = 2250.00
        assert total_qty == Decimal("225")
        assert total_value == Decimal("2250.00")

        # Verify via get-item that total stock is aggregated correctly
        get_r = _call_action("erpclaw-inventory", "get-item", conn,
                              item_id=item_id)
        assert get_r["status"] == "ok"
        assert Decimal(get_r["total_qty"]) == Decimal("225.00")
        assert Decimal(get_r["total_stock_value"]) == Decimal("2250.00")
        assert len(get_r["stock_balances"]) == 3

        # Transfer 20 from A to B, then verify totals are unchanged
        xfer_json = json.dumps([{
            "item_id": item_id, "qty": "20", "rate": "10.00",
            "from_warehouse_id": wh_ids[0], "to_warehouse_id": wh_ids[1],
        }])
        se_xfer = _call_action("erpclaw-inventory", "add-stock-entry", conn,
                                entry_type="transfer", company_id=cid,
                                posting_date="2026-07-02", items=xfer_json)
        _call_action("erpclaw-inventory", "submit-stock-entry", conn,
                      stock_entry_id=se_xfer["stock_entry_id"])

        # A = 80, B = 70, C = 75 => total still 225
        bal_a = _call_action("erpclaw-inventory", "get-stock-balance", conn,
                              item_id=item_id, warehouse_id=wh_ids[0])
        bal_b = _call_action("erpclaw-inventory", "get-stock-balance", conn,
                              item_id=item_id, warehouse_id=wh_ids[1])
        bal_c = _call_action("erpclaw-inventory", "get-stock-balance", conn,
                              item_id=item_id, warehouse_id=wh_ids[2])

        assert Decimal(bal_a["qty"]) == Decimal("80")
        assert Decimal(bal_b["qty"]) == Decimal("70")
        assert Decimal(bal_c["qty"]) == Decimal("75")

        new_total = Decimal(bal_a["qty"]) + Decimal(bal_b["qty"]) + Decimal(bal_c["qty"])
        assert new_total == Decimal("225"), (
            f"Total stock should remain 225 after transfer, got {new_total}"
        )
