"""Tests for the budget-vs-actual action."""
from decimal import Decimal
from helpers import (
    _call_action,
    create_test_company,
    create_test_account,
    create_test_fiscal_year,
    create_test_budget,
    create_test_gl_entries,
)
from db_query import ACTIONS


def test_budget_vs_actual_with_budget(fresh_db):
    """Budget vs actual shows budget, actual, and variance."""
    conn = fresh_db
    cid = create_test_company(conn)
    fy_id = create_test_fiscal_year(conn, cid)
    expense_id = create_test_account(conn, cid, "Marketing", "expense",
                                     account_type="expense", account_number="5100")

    create_test_budget(conn, fy_id, cid, expense_id, "10000.00")

    # Actual spending of 7000
    cash_id = create_test_account(conn, cid, "Cash", "asset",
                                  account_type="cash", account_number="1001")
    v1 = "JE-MKT"
    create_test_gl_entries(conn, [
        {"account_id": expense_id, "posting_date": "2026-03-15",
         "debit": "7000.00", "credit": "0.00", "voucher_id": v1},
        {"account_id": cash_id, "posting_date": "2026-03-15",
         "debit": "0.00", "credit": "7000.00", "voucher_id": v1},
    ])

    result = _call_action(
        ACTIONS["budget-vs-actual"], conn,
        company_id=cid, fiscal_year_id=fy_id,
    )
    assert result["status"] == "ok"
    assert len(result["items"]) == 1

    item = result["items"][0]
    assert item["budget"] == "10000.00"
    assert item["actual"] == "7000.00"
    assert item["variance"] == "3000.00"


def test_budget_vs_actual_variance_calculation(fresh_db):
    """Variance = budget - actual; variance_pct computed correctly."""
    conn = fresh_db
    cid = create_test_company(conn)
    fy_id = create_test_fiscal_year(conn, cid)
    expense_id = create_test_account(conn, cid, "Travel", "expense",
                                     account_type="expense", account_number="5200")

    create_test_budget(conn, fy_id, cid, expense_id, "5000.00",
                       action_if_exceeded="stop")

    # Actual spending of 6000 (over budget)
    cash_id = create_test_account(conn, cid, "Cash", "asset",
                                  account_type="cash", account_number="1001")
    v1 = "JE-TRAVEL"
    create_test_gl_entries(conn, [
        {"account_id": expense_id, "posting_date": "2026-06-01",
         "debit": "6000.00", "credit": "0.00", "voucher_id": v1},
        {"account_id": cash_id, "posting_date": "2026-06-01",
         "debit": "0.00", "credit": "6000.00", "voucher_id": v1},
    ])

    result = _call_action(
        ACTIONS["budget-vs-actual"], conn,
        company_id=cid, fiscal_year_id=fy_id,
    )
    assert result["status"] == "ok"
    item = result["items"][0]

    assert item["budget"] == "5000.00"
    assert item["actual"] == "6000.00"
    assert item["variance"] == "-1000.00"
    assert item["action_if_exceeded"] == "stop"

    # Variance pct: (5000 - 6000) / 5000 * 100 = -20
    assert Decimal(item["variance_pct"]) == Decimal("-20.00")
