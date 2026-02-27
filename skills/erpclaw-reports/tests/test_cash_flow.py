"""Tests for the cash-flow action."""
from helpers import (
    _call_action,
    create_test_company,
    create_test_account,
    create_test_gl_entries,
)
from db_query import ACTIONS


def test_cash_flow_empty(fresh_db):
    """Cash flow with no GL data returns zero balances."""
    conn = fresh_db
    cid = create_test_company(conn)

    result = _call_action(
        ACTIONS["cash-flow"], conn,
        company_id=cid, from_date="2026-01-01", to_date="2026-12-31",
    )
    assert result["status"] == "ok"
    assert result["opening_balance"] == "0.00"
    assert result["closing_balance"] == "0.00"
    assert result["net_change"] == "0.00"
    assert result["details"] == []


def test_cash_flow_with_data(fresh_db):
    """Cash flow categorizes movements and computes net change."""
    conn = fresh_db
    cid = create_test_company(conn)

    # Create accounts with DDL-valid lowercase types
    cash_id = create_test_account(conn, cid, "Cash", "asset",
                                  account_type="cash", account_number="1001")
    revenue_id = create_test_account(conn, cid, "Revenue", "income",
                                     account_type="revenue", account_number="4001")
    expense_id = create_test_account(conn, cid, "Rent Expense", "expense",
                                     account_type="expense", account_number="5001")

    # Revenue earned (cash in): DR Cash 8000, CR Revenue 8000
    v1 = "JE-REV"
    create_test_gl_entries(conn, [
        {"account_id": cash_id, "posting_date": "2026-03-01",
         "debit": "8000.00", "credit": "0.00", "voucher_id": v1},
        {"account_id": revenue_id, "posting_date": "2026-03-01",
         "debit": "0.00", "credit": "8000.00", "voucher_id": v1},
    ])

    # Rent paid (cash out): DR Expense 2000, CR Cash 2000
    v2 = "JE-RENT"
    create_test_gl_entries(conn, [
        {"account_id": expense_id, "posting_date": "2026-04-01",
         "debit": "2000.00", "credit": "0.00", "voucher_id": v2},
        {"account_id": cash_id, "posting_date": "2026-04-01",
         "debit": "0.00", "credit": "2000.00", "voucher_id": v2},
    ])

    result = _call_action(
        ACTIONS["cash-flow"], conn,
        company_id=cid, from_date="2026-01-01", to_date="2026-12-31",
    )
    assert result["status"] == "ok"

    # Cash account: DR 8000, CR 2000 → closing balance = 6000
    assert result["opening_balance"] == "0.00"
    assert result["closing_balance"] == "6000.00"
    assert result["net_change"] == "6000.00"

    # Non-cash movements: revenue (operating) + expense (operating)
    assert len(result["details"]) > 0
    # Operating = revenue income (8000) + expense cost (-2000) = 6000
    assert result["operating"] == "6000.00"
