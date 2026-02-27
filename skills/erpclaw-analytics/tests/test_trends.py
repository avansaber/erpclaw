"""Tests for metric-trend and period-comparison — 4 tests."""
import sys
import os
import json
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))
sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))

from helpers import (
    _call_action, create_test_company, create_test_account,
    create_test_gl_pair,
)
from db_query import action_metric_trend, action_period_comparison


class TestMetricTrend:
    def test_revenue_trend(self, fresh_db):
        """TRD-01: Revenue metric trend tracks correctly over months."""
        cid = create_test_company(fresh_db)
        cash = create_test_account(fresh_db, cid, "Cash", "asset", "cash")
        revenue = create_test_account(fresh_db, cid, "Sales", "income", "revenue")
        equity = create_test_account(fresh_db, cid, "Equity", "equity", "equity")

        create_test_gl_pair(fresh_db, cash, equity, "2026-01-01", "200000")
        create_test_gl_pair(fresh_db, cash, revenue, "2026-01-15", "30000")
        create_test_gl_pair(fresh_db, cash, revenue, "2026-02-15", "40000")

        result = _call_action(action_metric_trend, fresh_db,
                              company_id=cid, metric="revenue",
                              from_date="2026-01-01", to_date="2026-02-28",
                              periodicity="monthly")
        assert result["status"] == "ok"
        assert result["metric"] == "revenue"
        assert len(result["trend"]) == 2
        assert Decimal(result["trend"][0]["value"]) == Decimal("30000.00")
        assert Decimal(result["trend"][1]["value"]) == Decimal("40000.00")
        assert result["trend"][1]["change_pct"] == "33.3%"

    def test_unknown_metric(self, fresh_db):
        """TRD-02: Returns error for unknown metric name."""
        cid = create_test_company(fresh_db)
        result = _call_action(action_metric_trend, fresh_db,
                              company_id=cid, metric="unknown_xyz",
                              from_date="2026-01-01", to_date="2026-01-31")
        assert result["status"] == "error"
        assert "Unknown metric" in result["message"]


class TestPeriodComparison:
    def test_two_period_comparison(self, fresh_db):
        """TRD-03: Compares revenue/expenses across two periods."""
        cid = create_test_company(fresh_db)
        cash = create_test_account(fresh_db, cid, "Cash", "asset", "cash")
        revenue = create_test_account(fresh_db, cid, "Sales", "income", "revenue")
        expense = create_test_account(fresh_db, cid, "OpEx", "expense", "expense")
        equity = create_test_account(fresh_db, cid, "Equity", "equity", "equity")

        create_test_gl_pair(fresh_db, cash, equity, "2026-01-01", "500000")
        # Jan: 50k revenue, 20k expenses
        create_test_gl_pair(fresh_db, cash, revenue, "2026-01-15", "50000")
        create_test_gl_pair(fresh_db, expense, cash, "2026-01-20", "20000")
        # Feb: 60k revenue, 25k expenses
        create_test_gl_pair(fresh_db, cash, revenue, "2026-02-15", "60000")
        create_test_gl_pair(fresh_db, expense, cash, "2026-02-20", "25000")

        periods = json.dumps([
            {"from_date": "2026-01-01", "to_date": "2026-01-31", "label": "Jan 2026"},
            {"from_date": "2026-02-01", "to_date": "2026-02-28", "label": "Feb 2026"},
        ])

        result = _call_action(action_period_comparison, fresh_db,
                              company_id=cid, periods=periods)
        assert result["status"] == "ok"
        assert len(result["periods"]) == 2
        assert Decimal(result["periods"][0]["revenue"]) == Decimal("50000.00")
        assert Decimal(result["periods"][1]["revenue"]) == Decimal("60000.00")
        # Should have change columns
        assert "revenue_change" in result["periods"][1]
        assert Decimal(result["periods"][1]["revenue_change"]) == Decimal("10000.00")

    def test_missing_periods_arg(self, fresh_db):
        """TRD-04: Returns error when --periods not provided."""
        cid = create_test_company(fresh_db)
        result = _call_action(action_period_comparison, fresh_db, company_id=cid)
        assert result["status"] == "error"
        assert "periods" in result["message"].lower()
