"""Tests for the profit-and-loss action."""
from helpers import (
    _call_action,
    create_test_company,
    create_test_account,
    create_test_gl_entries,
)
from db_query import ACTIONS


def test_pnl_empty(fresh_db):
    """P&L with no GL data returns zero totals."""
    conn = fresh_db
    cid = create_test_company(conn)
    create_test_account(conn, cid, "Sales Revenue", "income",
                        account_type="revenue", account_number="4001")

    result = _call_action(
        ACTIONS["profit-and-loss"], conn,
        company_id=cid, from_date="2026-01-01", to_date="2026-12-31",
    )
    assert result["status"] == "ok"
    assert result["income"] == []
    assert result["expenses"] == []
    assert result["income_total"] == "0.00"
    assert result["expense_total"] == "0.00"
    assert result["net_income"] == "0.00"


def test_pnl_with_income_and_expenses(fresh_db):
    """P&L correctly calculates income and expense totals."""
    conn = fresh_db
    cid = create_test_company(conn)
    cash_id = create_test_account(conn, cid, "Cash", "asset",
                                  account_type="cash", account_number="1001")
    revenue_id = create_test_account(conn, cid, "Sales Revenue", "income",
                                     account_type="revenue", account_number="4001")
    rent_id = create_test_account(conn, cid, "Rent Expense", "expense",
                                  account_type="expense", account_number="5001")

    v1 = "JE-SALE"
    create_test_gl_entries(conn, [
        {"account_id": cash_id, "posting_date": "2026-03-15",
         "debit": "10000.00", "credit": "0.00", "voucher_id": v1},
        {"account_id": revenue_id, "posting_date": "2026-03-15",
         "debit": "0.00", "credit": "10000.00", "voucher_id": v1},
    ])

    v2 = "JE-RENT"
    create_test_gl_entries(conn, [
        {"account_id": rent_id, "posting_date": "2026-04-01",
         "debit": "3000.00", "credit": "0.00", "voucher_id": v2},
        {"account_id": cash_id, "posting_date": "2026-04-01",
         "debit": "0.00", "credit": "3000.00", "voucher_id": v2},
    ])

    result = _call_action(
        ACTIONS["profit-and-loss"], conn,
        company_id=cid, from_date="2026-01-01", to_date="2026-12-31",
    )
    assert result["status"] == "ok"
    assert result["income_total"] == "10000.00"
    assert result["expense_total"] == "3000.00"
    assert len(result["income"]) == 1
    assert len(result["expenses"]) == 1


def test_pnl_net_income_calculation(fresh_db):
    """Net income = income_total - expense_total."""
    conn = fresh_db
    cid = create_test_company(conn)
    cash_id = create_test_account(conn, cid, "Cash", "asset",
                                  account_type="cash", account_number="1001")
    revenue_id = create_test_account(conn, cid, "Services", "income",
                                     account_type="revenue", account_number="4001")
    wages_id = create_test_account(conn, cid, "Wages", "expense",
                                   account_type="expense", account_number="5001")
    supplies_id = create_test_account(conn, cid, "Supplies", "expense",
                                      account_type="expense", account_number="5002")

    v1 = "JE-REV"
    create_test_gl_entries(conn, [
        {"account_id": cash_id, "posting_date": "2026-02-01",
         "debit": "15000.00", "credit": "0.00", "voucher_id": v1},
        {"account_id": revenue_id, "posting_date": "2026-02-01",
         "debit": "0.00", "credit": "15000.00", "voucher_id": v1},
    ])

    v2 = "JE-WAGES"
    create_test_gl_entries(conn, [
        {"account_id": wages_id, "posting_date": "2026-02-28",
         "debit": "8000.00", "credit": "0.00", "voucher_id": v2},
        {"account_id": cash_id, "posting_date": "2026-02-28",
         "debit": "0.00", "credit": "8000.00", "voucher_id": v2},
    ])

    v3 = "JE-SUPPLIES"
    create_test_gl_entries(conn, [
        {"account_id": supplies_id, "posting_date": "2026-03-10",
         "debit": "2000.00", "credit": "0.00", "voucher_id": v3},
        {"account_id": cash_id, "posting_date": "2026-03-10",
         "debit": "0.00", "credit": "2000.00", "voucher_id": v3},
    ])

    result = _call_action(
        ACTIONS["profit-and-loss"], conn,
        company_id=cid, from_date="2026-01-01", to_date="2026-12-31",
    )
    assert result["status"] == "ok"
    assert result["income_total"] == "15000.00"
    assert result["expense_total"] == "10000.00"
    assert result["net_income"] == "5000.00"
