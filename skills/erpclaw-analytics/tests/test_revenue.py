"""Tests for revenue analytics actions — 5 tests."""
import sys
import os
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))
sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))

from helpers import (
    _call_action, create_test_company, create_test_account,
    create_test_gl_pair, create_test_customer, create_test_sales_invoice,
    create_test_sales_invoice_item, create_test_item,
)
from db_query import (
    action_revenue_by_customer, action_revenue_by_item,
    action_revenue_trend, action_customer_concentration,
)


class TestRevenueByCustomer:
    def test_basic_breakdown(self, fresh_db):
        """REV-01: Revenue by customer shows correct totals and shares."""
        cid = create_test_company(fresh_db)
        c1 = create_test_customer(fresh_db, cid, "Acme Corp")
        c2 = create_test_customer(fresh_db, cid, "Wayne Enterprises")

        create_test_sales_invoice(fresh_db, cid, c1, "2026-01-15", "50000")
        create_test_sales_invoice(fresh_db, cid, c2, "2026-01-20", "30000")

        result = _call_action(action_revenue_by_customer, fresh_db,
                              company_id=cid, from_date="2026-01-01", to_date="2026-01-31")
        assert result["status"] == "ok"
        assert Decimal(result["grand_total"]) == Decimal("80000.00")
        assert result["count"] == 2
        assert result["customers"][0]["customer_name"] == "Acme Corp"
        assert result["customers"][0]["share"] == "62.5%"

    def test_no_invoices(self, fresh_db):
        """REV-02: Returns zero when no invoices exist."""
        cid = create_test_company(fresh_db)
        result = _call_action(action_revenue_by_customer, fresh_db,
                              company_id=cid, from_date="2026-01-01", to_date="2026-01-31")
        assert result["status"] == "ok"
        assert Decimal(result["grand_total"]) == Decimal("0")
        assert result["count"] == 0


class TestRevenueByItem:
    def test_basic_item_breakdown(self, fresh_db):
        """REV-03: Revenue by item shows correct item-level totals."""
        cid = create_test_company(fresh_db)
        cust = create_test_customer(fresh_db, cid, "Customer A")
        item1 = create_test_item(fresh_db, cid, "Widget A")
        item2 = create_test_item(fresh_db, cid, "Widget B")
        inv = create_test_sales_invoice(fresh_db, cid, cust, "2026-01-15", "25000")
        create_test_sales_invoice_item(fresh_db, inv, item1, "10", "1500", "15000")
        create_test_sales_invoice_item(fresh_db, inv, item2, "5", "2000", "10000")

        result = _call_action(action_revenue_by_item, fresh_db,
                              company_id=cid, from_date="2026-01-01", to_date="2026-01-31")
        assert result["status"] == "ok"
        assert Decimal(result["grand_total"]) == Decimal("25000.00")
        assert result["count"] == 2
        assert result["items"][0]["item_name"] == "Widget A"


class TestRevenueTrend:
    def test_gl_fallback(self, fresh_db):
        """REV-04: Revenue trend falls back to GL income when selling not installed."""
        cid = create_test_company(fresh_db)
        cash = create_test_account(fresh_db, cid, "Cash", "asset", "cash")
        revenue = create_test_account(fresh_db, cid, "Sales", "income", "revenue")

        # Drop customer/sales_invoice tables to simulate missing selling module
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

        create_test_gl_pair(fresh_db, cash, revenue, "2026-01-15", "20000")
        create_test_gl_pair(fresh_db, cash, revenue, "2026-02-15", "25000")

        result = _call_action(action_revenue_trend, fresh_db,
                              company_id=cid, from_date="2026-01-01",
                              to_date="2026-02-28", periodicity="monthly")
        assert result["status"] == "ok"
        assert result["source"] == "gl_income_accounts"
        assert len(result["trend"]) == 2
        assert Decimal(result["trend"][0]["revenue"]) == Decimal("20000.00")


class TestCustomerConcentration:
    def test_concentration_analysis(self, fresh_db):
        """REV-05: Customer concentration shows correct top-N shares."""
        cid = create_test_company(fresh_db)
        c1 = create_test_customer(fresh_db, cid, "Big Corp")
        c2 = create_test_customer(fresh_db, cid, "Small LLC")

        create_test_sales_invoice(fresh_db, cid, c1, "2026-01-10", "80000")
        create_test_sales_invoice(fresh_db, cid, c2, "2026-01-20", "20000")

        result = _call_action(action_customer_concentration, fresh_db,
                              company_id=cid, from_date="2026-01-01", to_date="2026-01-31")
        assert result["status"] == "ok"
        assert result["customer_count"] == 2
        assert result["concentration"]["top_1_share"] == "80.0%"
        assert "high concentration" in result["interpretation"].lower()
