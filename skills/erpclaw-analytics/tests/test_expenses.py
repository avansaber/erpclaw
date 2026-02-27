"""Tests for expense-breakdown and cost-trend — 5 tests."""
import sys
import os
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))
sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))

from helpers import (
    _call_action, create_test_company, create_test_account,
    create_test_gl_pair, create_test_cost_center,
)
from db_query import action_expense_breakdown, action_cost_trend, action_opex_vs_capex


class TestExpenseBreakdown:
    def test_by_account(self, fresh_db):
        """EXP-01: Breaks down expenses by account correctly."""
        cid = create_test_company(fresh_db)
        cash = create_test_account(fresh_db, cid, "Cash", "asset", "cash")
        rent = create_test_account(fresh_db, cid, "Rent", "expense", "expense")
        salary = create_test_account(fresh_db, cid, "Salary", "expense", "expense")

        create_test_gl_pair(fresh_db, rent, cash, "2026-01-15", "5000")
        create_test_gl_pair(fresh_db, salary, cash, "2026-01-15", "15000")

        result = _call_action(action_expense_breakdown, fresh_db,
                              company_id=cid, from_date="2026-01-01", to_date="2026-01-31")
        assert result["status"] == "ok"
        assert Decimal(result["total_expenses"]) == Decimal("20000.00")
        assert result["count"] == 2
        # Salary should be first (larger)
        assert result["breakdown"][0]["name"] == "Salary"
        assert Decimal(result["breakdown"][0]["amount"]) == Decimal("15000.00")

    def test_by_cost_center(self, fresh_db):
        """EXP-02: Breaks down expenses by cost center."""
        cid = create_test_company(fresh_db)
        cash = create_test_account(fresh_db, cid, "Cash", "asset", "cash")
        expense = create_test_account(fresh_db, cid, "Office", "expense", "expense")
        cc1 = create_test_cost_center(fresh_db, cid, "Engineering")
        cc2 = create_test_cost_center(fresh_db, cid, "Marketing")

        create_test_gl_pair(fresh_db, expense, cash, "2026-01-15", "10000", cost_center_id=cc1)
        create_test_gl_pair(fresh_db, expense, cash, "2026-01-15", "5000", cost_center_id=cc2)

        result = _call_action(action_expense_breakdown, fresh_db,
                              company_id=cid, from_date="2026-01-01",
                              to_date="2026-01-31", group_by="cost_center")
        assert result["status"] == "ok"
        assert result["group_by"] == "cost_center"
        assert result["count"] == 2

    def test_empty_period(self, fresh_db):
        """EXP-03: Returns zero total for period with no expenses."""
        cid = create_test_company(fresh_db)
        result = _call_action(action_expense_breakdown, fresh_db,
                              company_id=cid, from_date="2026-01-01", to_date="2026-01-31")
        assert result["status"] == "ok"
        assert Decimal(result["total_expenses"]) == Decimal("0")
        assert result["count"] == 0


class TestCostTrend:
    def test_monthly_trend(self, fresh_db):
        """EXP-04: Shows monthly expense trend with change percentages."""
        cid = create_test_company(fresh_db)
        cash = create_test_account(fresh_db, cid, "Cash", "asset", "cash")
        expense = create_test_account(fresh_db, cid, "OpEx", "expense", "expense")
        equity = create_test_account(fresh_db, cid, "Equity", "equity", "equity")
        create_test_gl_pair(fresh_db, cash, equity, "2026-01-01", "100000")

        create_test_gl_pair(fresh_db, expense, cash, "2026-01-15", "10000")
        create_test_gl_pair(fresh_db, expense, cash, "2026-02-15", "12000")

        result = _call_action(action_cost_trend, fresh_db,
                              company_id=cid, from_date="2026-01-01",
                              to_date="2026-02-28", periodicity="monthly")
        assert result["status"] == "ok"
        assert len(result["periods"]) == 2
        assert Decimal(result["periods"][0]["amount"]) == Decimal("10000.00")
        assert Decimal(result["periods"][1]["amount"]) == Decimal("12000.00")
        # 20% increase
        assert result["periods"][1]["change_pct"] == "20.0%"


class TestOpexVsCapex:
    def test_basic_split(self, fresh_db):
        """EXP-05: Separates OpEx from CapEx correctly."""
        cid = create_test_company(fresh_db)
        cash = create_test_account(fresh_db, cid, "Cash", "asset", "cash")
        opex = create_test_account(fresh_db, cid, "OpEx", "expense", "expense")
        capex = create_test_account(fresh_db, cid, "Equipment", "asset", "fixed_asset")
        equity = create_test_account(fresh_db, cid, "Equity", "equity", "equity")

        create_test_gl_pair(fresh_db, cash, equity, "2026-01-01", "200000")
        create_test_gl_pair(fresh_db, opex, cash, "2026-01-15", "30000")
        create_test_gl_pair(fresh_db, capex, cash, "2026-01-20", "50000")

        result = _call_action(action_opex_vs_capex, fresh_db,
                              company_id=cid, from_date="2026-01-01", to_date="2026-01-31")
        assert result["status"] == "ok"
        assert Decimal(result["opex"]) == Decimal("30000.00")
        assert Decimal(result["capex"]) == Decimal("50000.00")
