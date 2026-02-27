"""Tests for liquidity-ratios and profitability-ratios — 6 tests."""
import sys
import os
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))
sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))

from helpers import (
    _call_action, create_test_company, create_test_account,
    create_test_gl_pair, create_test_fiscal_year,
)
from db_query import action_liquidity_ratios, action_profitability_ratios, action_efficiency_ratios


class TestLiquidityRatios:
    def test_basic_ratios(self, fresh_db):
        """RAT-01: Computes current, quick, and cash ratios correctly."""
        cid = create_test_company(fresh_db)
        cash = create_test_account(fresh_db, cid, "Cash", "asset", "cash")
        bank = create_test_account(fresh_db, cid, "Bank", "asset", "bank")
        stock_acct = create_test_account(fresh_db, cid, "Inventory", "asset", "stock")
        ap = create_test_account(fresh_db, cid, "AP", "liability", "payable")
        equity = create_test_account(fresh_db, cid, "Equity", "equity", "equity")

        # Cash: 10,000; Bank: 20,000; Inventory: 15,000
        # Total current assets = 45,000; AP = 30,000
        # For assets: debit increases (debit_normal). GL pair = debit asset, credit equity.
        create_test_gl_pair(fresh_db, cash, equity, "2026-01-01", "10000")
        create_test_gl_pair(fresh_db, bank, equity, "2026-01-01", "20000")
        create_test_gl_pair(fresh_db, stock_acct, equity, "2026-01-01", "15000")
        # For liabilities: credit increases (credit_normal). GL pair = debit equity, credit AP.
        create_test_gl_pair(fresh_db, equity, ap, "2026-01-01", "30000")

        result = _call_action(action_liquidity_ratios, fresh_db,
                              company_id=cid, as_of_date="2026-02-16")
        assert result["status"] == "ok"
        # Current ratio = 45000 / 30000 = 1.50
        assert result["ratios"]["current_ratio"] == "1.50"
        # Quick = (45000 - 15000) / 30000 = 1.00
        assert result["ratios"]["quick_ratio"] == "1.00"
        # Cash = 30000 / 30000 = 1.00
        assert result["ratios"]["cash_ratio"] == "1.00"

    def test_zero_liabilities(self, fresh_db):
        """RAT-02: Ratios return N/A when no liabilities exist."""
        cid = create_test_company(fresh_db)
        cash = create_test_account(fresh_db, cid, "Cash", "asset", "cash")
        equity = create_test_account(fresh_db, cid, "Equity", "equity", "equity")
        create_test_gl_pair(fresh_db, cash, equity, "2026-01-01", "10000")

        result = _call_action(action_liquidity_ratios, fresh_db,
                              company_id=cid, as_of_date="2026-02-16")
        assert result["status"] == "ok"
        assert result["ratios"]["current_ratio"] == "N/A"

    def test_empty_company(self, fresh_db):
        """RAT-03: Returns zeros for company with no transactions."""
        cid = create_test_company(fresh_db)
        result = _call_action(action_liquidity_ratios, fresh_db,
                              company_id=cid, as_of_date="2026-02-16")
        assert result["status"] == "ok"
        assert result["current_assets"] == "0.00"


class TestProfitabilityRatios:
    def test_basic_profitability(self, fresh_db):
        """RAT-04: Computes margins, ROA, ROE correctly."""
        cid = create_test_company(fresh_db)
        revenue_acct = create_test_account(fresh_db, cid, "Sales", "income", "revenue")
        cogs_acct = create_test_account(fresh_db, cid, "COGS", "expense", "cost_of_goods_sold")
        expense_acct = create_test_account(fresh_db, cid, "OpEx", "expense", "expense")
        cash = create_test_account(fresh_db, cid, "Cash", "asset", "cash")
        equity = create_test_account(fresh_db, cid, "Equity", "equity", "equity")

        # Revenue: 100,000; COGS: 40,000; OpEx: 20,000
        # Gross profit = 60,000; Net income = 40,000
        create_test_gl_pair(fresh_db, cash, revenue_acct, "2026-01-15", "100000")
        create_test_gl_pair(fresh_db, cogs_acct, cash, "2026-01-15", "40000")
        create_test_gl_pair(fresh_db, expense_acct, cash, "2026-01-20", "20000")
        # Equity injection
        create_test_gl_pair(fresh_db, cash, equity, "2026-01-01", "200000")

        result = _call_action(action_profitability_ratios, fresh_db,
                              company_id=cid, from_date="2026-01-01", to_date="2026-01-31")
        assert result["status"] == "ok"
        assert result["revenue"] == "100000.00"
        assert result["cogs"] == "40000.00"
        assert result["gross_profit"] == "60000.00"
        # Gross margin = 60000/100000 = 60.0%
        assert result["ratios"]["gross_margin"] == "60.0%"
        # Net income = 100000 - 60000 = 40000
        assert Decimal(result["net_income"]) == Decimal("40000.00")

    def test_no_revenue(self, fresh_db):
        """RAT-05: Returns N/A margins when no revenue."""
        cid = create_test_company(fresh_db)
        result = _call_action(action_profitability_ratios, fresh_db,
                              company_id=cid, from_date="2026-01-01", to_date="2026-01-31")
        assert result["status"] == "ok"
        assert result["ratios"]["gross_margin"] == "N/A"
        assert result["ratios"]["net_profit_margin"] == "N/A"

    def test_loss_period(self, fresh_db):
        """RAT-06: Handles net loss correctly (negative net income)."""
        cid = create_test_company(fresh_db)
        revenue_acct = create_test_account(fresh_db, cid, "Sales", "income", "revenue")
        expense_acct = create_test_account(fresh_db, cid, "OpEx", "expense", "expense")
        cash = create_test_account(fresh_db, cid, "Cash", "asset", "cash")
        equity = create_test_account(fresh_db, cid, "Equity", "equity", "equity")

        create_test_gl_pair(fresh_db, cash, equity, "2026-01-01", "100000")
        create_test_gl_pair(fresh_db, cash, revenue_acct, "2026-01-15", "10000")
        create_test_gl_pair(fresh_db, expense_acct, cash, "2026-01-20", "50000")

        result = _call_action(action_profitability_ratios, fresh_db,
                              company_id=cid, from_date="2026-01-01", to_date="2026-01-31")
        assert result["status"] == "ok"
        assert Decimal(result["net_income"]) < 0
        assert "loss" in result["interpretation"].lower()
