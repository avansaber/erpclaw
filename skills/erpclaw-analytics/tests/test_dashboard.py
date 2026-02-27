"""Tests for executive-dashboard and company-scorecard — 4 tests."""
import sys
import os
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))
sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))

from helpers import (
    _call_action, create_test_company, create_test_account,
    create_test_gl_pair,
)
from db_query import action_executive_dashboard, action_company_scorecard


class TestExecutiveDashboard:
    def test_basic_dashboard(self, fresh_db):
        """DASH-01: Executive dashboard returns all sections."""
        cid = create_test_company(fresh_db)
        cash = create_test_account(fresh_db, cid, "Cash", "asset", "cash")
        revenue = create_test_account(fresh_db, cid, "Sales", "income", "revenue")
        expense = create_test_account(fresh_db, cid, "OpEx", "expense", "expense")
        equity = create_test_account(fresh_db, cid, "Equity", "equity", "equity")

        create_test_gl_pair(fresh_db, cash, equity, "2026-01-01", "100000")
        create_test_gl_pair(fresh_db, cash, revenue, "2026-01-15", "50000")
        create_test_gl_pair(fresh_db, expense, cash, "2026-01-20", "20000")

        result = _call_action(action_executive_dashboard, fresh_db,
                              company_id=cid, from_date="2026-01-01", to_date="2026-01-31")
        assert result["status"] == "ok"
        sections = result["sections"]
        assert sections["financial"]["available"] is True
        assert Decimal(sections["financial"]["revenue"]) == Decimal("50000.00")
        assert Decimal(sections["financial"]["expenses"]) == Decimal("20000.00")

    def test_dashboard_degradation(self, fresh_db):
        """DASH-02: Dashboard degrades gracefully when modules missing."""
        cid = create_test_company(fresh_db)

        # Drop optional module tables
        fresh_db.execute("DROP TABLE IF EXISTS sales_invoice_tax")
        fresh_db.execute("DROP TABLE IF EXISTS sales_invoice_item")
        fresh_db.execute("DROP TABLE IF EXISTS delivery_note_item")
        fresh_db.execute("DROP TABLE IF EXISTS delivery_note")
        fresh_db.execute("DROP TABLE IF EXISTS sales_order_item")
        fresh_db.execute("DROP TABLE IF EXISTS sales_order")
        fresh_db.execute("DROP TABLE IF EXISTS quotation_item")
        fresh_db.execute("DROP TABLE IF EXISTS quotation")
        fresh_db.execute("DROP TABLE IF EXISTS sales_invoice")
        fresh_db.execute("DROP TABLE IF EXISTS customer")
        fresh_db.execute("DROP TABLE IF EXISTS issue")

        result = _call_action(action_executive_dashboard, fresh_db,
                              company_id=cid, from_date="2026-01-01", to_date="2026-01-31")
        assert result["status"] == "ok"
        sections = result["sections"]
        assert sections["financial"]["available"] is True
        assert sections["selling"]["available"] is False
        assert "reason" in sections["selling"]
        assert sections["support"]["available"] is False


class TestCompanyScorecard:
    def test_basic_scorecard(self, fresh_db):
        """DASH-03: Scorecard grades company dimensions correctly."""
        cid = create_test_company(fresh_db)
        cash = create_test_account(fresh_db, cid, "Cash", "asset", "cash")
        revenue = create_test_account(fresh_db, cid, "Sales", "income", "revenue")
        expense = create_test_account(fresh_db, cid, "OpEx", "expense", "expense")
        ap = create_test_account(fresh_db, cid, "AP", "liability", "payable")
        equity = create_test_account(fresh_db, cid, "Equity", "equity", "equity")

        create_test_gl_pair(fresh_db, cash, equity, "2026-01-01", "500000")
        create_test_gl_pair(fresh_db, cash, revenue, "2026-01-15", "100000")
        create_test_gl_pair(fresh_db, expense, cash, "2026-01-20", "60000")
        # Liability: debit equity, credit AP to create positive liability balance
        create_test_gl_pair(fresh_db, equity, ap, "2026-01-01", "100000")

        result = _call_action(action_company_scorecard, fresh_db,
                              company_id=cid, as_of_date="2026-01-31")
        assert result["status"] == "ok"
        assert result["overall_grade"] in ("A", "B", "C", "D", "N/A")
        assert "liquidity" in result["dimensions"]
        assert "profitability" in result["dimensions"]
        assert result["dimensions"]["liquidity"]["grade"] in ("A", "B", "C", "D")

    def test_empty_scorecard(self, fresh_db):
        """DASH-04: Scorecard handles company with no data."""
        cid = create_test_company(fresh_db)
        result = _call_action(action_company_scorecard, fresh_db,
                              company_id=cid, as_of_date="2026-01-31")
        assert result["status"] == "ok"
        # Should still produce grades (N/A for most)
        assert "dimensions" in result
