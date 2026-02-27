"""Tests for inventory analytics — 4 tests."""
import sys
import os
import uuid
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))
sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))

from helpers import (
    _call_action, create_test_company, create_test_account,
    create_test_gl_pair, create_test_item,
)
from db_query import action_abc_analysis, action_inventory_turnover, action_aging_inventory


def _create_sle(conn, company_id, item_id, warehouse_id, posting_date,
                actual_qty, stock_value_difference):
    """Insert a stock_ledger_entry for testing."""
    sle_id = str(uuid.uuid4())
    voucher_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO stock_ledger_entry (id, item_id, warehouse_id, posting_date,
           actual_qty, qty_after_transaction, valuation_rate,
           stock_value_difference, voucher_type, voucher_id)
           VALUES (?, ?, ?, ?, ?, ?, '100', ?, 'stock_entry', ?)""",
        (sle_id, item_id, warehouse_id, posting_date,
         str(actual_qty), str(actual_qty), str(stock_value_difference), voucher_id),
    )
    conn.commit()
    return sle_id


def _create_warehouse(conn, company_id, name="Main Warehouse"):
    """Insert a warehouse for testing."""
    wh_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO warehouse (id, name, company_id, warehouse_type) VALUES (?, ?, ?, 'stores')""",
        (wh_id, name, company_id),
    )
    conn.commit()
    return wh_id


class TestABCAnalysis:
    def test_basic_classification(self, fresh_db):
        """INV-01: ABC analysis classifies items into A, B, C correctly."""
        cid = create_test_company(fresh_db)
        wh = _create_warehouse(fresh_db, cid)
        i1 = create_test_item(fresh_db, cid, "High Value Item")
        i2 = create_test_item(fresh_db, cid, "Medium Value Item")
        i3 = create_test_item(fresh_db, cid, "Low Value Item")

        _create_sle(fresh_db, cid, i1, wh, "2026-01-01", 100, 80000)
        _create_sle(fresh_db, cid, i2, wh, "2026-01-01", 50, 15000)
        _create_sle(fresh_db, cid, i3, wh, "2026-01-01", 200, 5000)

        result = _call_action(action_abc_analysis, fresh_db, company_id=cid)
        assert result["status"] == "ok"
        assert result["item_count"] == 3
        assert Decimal(result["total_value"]) == Decimal("100000.00")
        # High value item should be class A (80% of value)
        assert result["items"][0]["item_name"] == "High Value Item"
        assert result["items"][0]["class"] == "A"

    def test_empty_inventory(self, fresh_db):
        """INV-02: Returns empty list when no stock exists."""
        cid = create_test_company(fresh_db)
        result = _call_action(action_abc_analysis, fresh_db, company_id=cid)
        assert result["status"] == "ok"
        assert result["items"] == []


class TestInventoryTurnover:
    def test_basic_turnover(self, fresh_db):
        """INV-03: Inventory turnover ratio computed correctly."""
        cid = create_test_company(fresh_db)
        cash = create_test_account(fresh_db, cid, "Cash", "asset", "cash")
        stock_acct = create_test_account(fresh_db, cid, "Inventory", "asset", "stock")
        cogs_acct = create_test_account(fresh_db, cid, "COGS", "expense", "cost_of_goods_sold")
        equity = create_test_account(fresh_db, cid, "Equity", "equity", "equity")

        create_test_gl_pair(fresh_db, cash, equity, "2026-01-01", "200000")
        # Stock balance: 50,000 at start, 40,000 at end => avg 45,000
        create_test_gl_pair(fresh_db, stock_acct, cash, "2026-01-01", "50000")
        # COGS: 60,000
        create_test_gl_pair(fresh_db, cogs_acct, stock_acct, "2026-01-15", "10000")
        create_test_gl_pair(fresh_db, cogs_acct, cash, "2026-01-20", "50000")

        result = _call_action(action_inventory_turnover, fresh_db,
                              company_id=cid, from_date="2026-01-01", to_date="2026-01-31")
        assert result["status"] == "ok"
        assert Decimal(result["cogs"]) == Decimal("60000.00")


class TestAgingInventory:
    def test_aging_buckets(self, fresh_db):
        """INV-04: Aging inventory places items in correct buckets."""
        cid = create_test_company(fresh_db)
        wh = _create_warehouse(fresh_db, cid)
        i1 = create_test_item(fresh_db, cid, "Fresh Stock")
        i2 = create_test_item(fresh_db, cid, "Old Stock")

        _create_sle(fresh_db, cid, i1, wh, "2026-02-10", 10, 1000)
        _create_sle(fresh_db, cid, i2, wh, "2025-10-01", 5, 500)

        result = _call_action(action_aging_inventory, fresh_db,
                              company_id=cid, as_of_date="2026-02-16")
        assert result["status"] == "ok"
        assert result["total_items"] == 2
        # Old stock should have higher age_days
        old_item = [i for i in result["items"] if i["item_name"] == "Old Stock"][0]
        assert old_item["age_days"] > 100
