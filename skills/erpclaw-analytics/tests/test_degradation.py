"""Tests for graceful degradation — 5 tests.

These tests drop tables to simulate missing skills and verify
the analytics skill returns helpful error messages instead of crashing.
"""
import sys
import os
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))
sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))

from helpers import _call_action, create_test_company, create_test_account, create_test_gl_pair
from db_query import (
    action_revenue_by_customer, action_abc_analysis,
    action_headcount_analytics, action_efficiency_ratios,
    action_executive_dashboard,
)


def _drop_selling_tables(conn):
    """Drop all selling-related tables."""
    for t in ["sales_invoice_tax", "sales_invoice_item", "delivery_note_item",
              "delivery_note", "sales_order_item", "sales_order",
              "quotation_item", "quotation", "sales_invoice", "customer"]:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit()


def _drop_inventory_tables(conn):
    """Drop inventory-related tables."""
    for t in ["stock_reconciliation", "pricing_rule", "item_price", "price_list",
              "serial_number", "batch", "stock_ledger_entry", "stock_entry_item",
              "stock_entry", "warehouse", "item_group", "item"]:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit()


def _drop_hr_tables(conn):
    """Drop HR-related tables."""
    for t in ["expense_claim", "attendance", "leave_application",
              "leave_allocation", "leave_type", "holiday_list",
              "employee", "designation", "department"]:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit()


class TestGracefulDegradation:
    def test_revenue_by_customer_missing_selling(self, fresh_db):
        """DEG-01: revenue-by-customer returns clear error when selling missing."""
        cid = create_test_company(fresh_db)
        _drop_selling_tables(fresh_db)

        result = _call_action(action_revenue_by_customer, fresh_db,
                              company_id=cid, from_date="2026-01-01", to_date="2026-01-31")
        assert result["status"] == "error"
        assert "erpclaw-selling" in result["message"]

    def test_abc_analysis_missing_inventory(self, fresh_db):
        """DEG-02: abc-analysis returns clear error when inventory missing."""
        cid = create_test_company(fresh_db)
        _drop_inventory_tables(fresh_db)

        result = _call_action(action_abc_analysis, fresh_db, company_id=cid)
        assert result["status"] == "error"
        assert "erpclaw-inventory" in result["message"]

    def test_headcount_missing_hr(self, fresh_db):
        """DEG-03: headcount-analytics returns clear error when HR missing."""
        cid = create_test_company(fresh_db)
        _drop_hr_tables(fresh_db)

        result = _call_action(action_headcount_analytics, fresh_db, company_id=cid)
        assert result["status"] == "error"
        assert "erpclaw-hr" in result["message"]

    def test_efficiency_ratios_partial(self, fresh_db):
        """DEG-04: efficiency-ratios skips DSO/DPO when modules missing but doesn't crash."""
        cid = create_test_company(fresh_db)
        cash = create_test_account(fresh_db, cid, "Cash", "asset", "cash")
        revenue = create_test_account(fresh_db, cid, "Sales", "income", "revenue")
        equity = create_test_account(fresh_db, cid, "Equity", "equity", "equity")
        create_test_gl_pair(fresh_db, cash, equity, "2026-01-01", "100000")
        create_test_gl_pair(fresh_db, cash, revenue, "2026-01-15", "50000")

        _drop_selling_tables(fresh_db)
        _drop_inventory_tables(fresh_db)

        result = _call_action(action_efficiency_ratios, fresh_db,
                              company_id=cid, from_date="2026-01-01", to_date="2026-01-31")
        assert result["status"] == "ok"
        # DSO should be null (selling not installed)
        assert result["ratios"]["dso"] is None
        # Asset turnover should still work
        assert result["ratios"]["asset_turnover"] != "N/A"
        assert len(result["notes"]) >= 2

    def test_dashboard_partial_degradation(self, fresh_db):
        """DEG-05: Executive dashboard shows available: false for missing modules."""
        cid = create_test_company(fresh_db)
        cash = create_test_account(fresh_db, cid, "Cash", "asset", "cash")
        equity = create_test_account(fresh_db, cid, "Equity", "equity", "equity")
        create_test_gl_pair(fresh_db, cash, equity, "2026-01-01", "100000")

        _drop_selling_tables(fresh_db)
        _drop_hr_tables(fresh_db)

        result = _call_action(action_executive_dashboard, fresh_db,
                              company_id=cid, from_date="2026-01-01", to_date="2026-01-31")
        assert result["status"] == "ok"
        assert result["sections"]["financial"]["available"] is True
        assert result["sections"]["selling"]["available"] is False
        assert result["sections"]["hr"]["available"] is False
