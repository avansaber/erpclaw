"""Tests for the balance-sheet action."""
from decimal import Decimal
from helpers import (
    _call_action,
    create_test_company,
    create_test_account,
    create_test_fiscal_year,
    create_test_gl_entries,
)
from db_query import ACTIONS


def test_balance_sheet_empty(fresh_db):
    """Balance sheet with no GL data returns zero totals."""
    conn = fresh_db
    cid = create_test_company(conn)
    create_test_fiscal_year(conn, cid)
    create_test_account(conn, cid, "Cash", "asset", account_type="cash",
                        account_number="1001")

    result = _call_action(
        ACTIONS["balance-sheet"], conn,
        company_id=cid, as_of_date="2026-12-31",
    )
    assert result["status"] == "ok"
    assert result["assets"] == []
    assert result["liabilities"] == []
    assert result["total_assets"] == "0.00"
    assert result["total_liabilities"] == "0.00"


def test_balance_sheet_with_data(fresh_db):
    """Balance sheet shows assets, liabilities, and equity correctly."""
    conn = fresh_db
    cid = create_test_company(conn)
    create_test_fiscal_year(conn, cid)

    cash_id = create_test_account(conn, cid, "Cash", "asset",
                                  account_type="cash", account_number="1001")
    ar_id = create_test_account(conn, cid, "Accounts Receivable", "asset",
                                account_type="receivable", account_number="1100")
    ap_id = create_test_account(conn, cid, "Accounts Payable", "liability",
                                account_type="payable", account_number="2001")
    equity_id = create_test_account(conn, cid, "Owners Equity", "equity",
                                    account_type="equity", account_number="3001")

    # Owner invests 20000 cash
    v1 = "JE-INVEST"
    create_test_gl_entries(conn, [
        {"account_id": cash_id, "posting_date": "2026-01-15",
         "debit": "20000.00", "credit": "0.00", "voucher_id": v1},
        {"account_id": equity_id, "posting_date": "2026-01-15",
         "debit": "0.00", "credit": "20000.00", "voucher_id": v1},
    ])

    # Record a receivable and payable
    v2 = "JE-ACCRUAL"
    create_test_gl_entries(conn, [
        {"account_id": ar_id, "posting_date": "2026-02-01",
         "debit": "5000.00", "credit": "0.00", "voucher_id": v2},
        {"account_id": ap_id, "posting_date": "2026-02-01",
         "debit": "0.00", "credit": "5000.00", "voucher_id": v2},
    ])

    result = _call_action(
        ACTIONS["balance-sheet"], conn,
        company_id=cid, as_of_date="2026-12-31",
    )
    assert result["status"] == "ok"
    assert result["total_assets"] == "25000.00"  # 20000 cash + 5000 AR
    assert result["total_liabilities"] == "5000.00"  # 5000 AP
    # Equity = base equity + net income YTD
    # Base equity = 20000, net income YTD = 0 (no income/expense)
    assert result["total_equity"] == "20000.00"


def test_balance_sheet_equation(fresh_db):
    """Assets = Liabilities + Equity (including net income YTD)."""
    conn = fresh_db
    cid = create_test_company(conn)
    create_test_fiscal_year(conn, cid)

    cash_id = create_test_account(conn, cid, "Cash", "asset",
                                  account_type="cash", account_number="1001")
    equity_id = create_test_account(conn, cid, "Owners Equity", "equity",
                                    account_type="equity", account_number="3001")
    revenue_id = create_test_account(conn, cid, "Revenue", "income",
                                     account_type="revenue", account_number="4001")
    expense_id = create_test_account(conn, cid, "Expenses", "expense",
                                     account_type="expense", account_number="5001")

    # Owner invests 10000
    v1 = "JE-INVEST"
    create_test_gl_entries(conn, [
        {"account_id": cash_id, "posting_date": "2026-01-01",
         "debit": "10000.00", "credit": "0.00", "voucher_id": v1},
        {"account_id": equity_id, "posting_date": "2026-01-01",
         "debit": "0.00", "credit": "10000.00", "voucher_id": v1},
    ])

    # Earn 5000 revenue
    v2 = "JE-REV"
    create_test_gl_entries(conn, [
        {"account_id": cash_id, "posting_date": "2026-03-01",
         "debit": "5000.00", "credit": "0.00", "voucher_id": v2},
        {"account_id": revenue_id, "posting_date": "2026-03-01",
         "debit": "0.00", "credit": "5000.00", "voucher_id": v2},
    ])

    # Incur 2000 expenses
    v3 = "JE-EXP"
    create_test_gl_entries(conn, [
        {"account_id": expense_id, "posting_date": "2026-04-01",
         "debit": "2000.00", "credit": "0.00", "voucher_id": v3},
        {"account_id": cash_id, "posting_date": "2026-04-01",
         "debit": "0.00", "credit": "2000.00", "voucher_id": v3},
    ])

    result = _call_action(
        ACTIONS["balance-sheet"], conn,
        company_id=cid, as_of_date="2026-12-31",
    )
    assert result["status"] == "ok"

    total_assets = Decimal(result["total_assets"])
    total_liabilities = Decimal(result["total_liabilities"])
    total_equity = Decimal(result["total_equity"])
    net_income_ytd = Decimal(result["net_income_ytd"])

    # Assets = 10000 + 5000 - 2000 = 13000
    assert total_assets == Decimal("13000.00")
    # Net income = 5000 - 2000 = 3000
    assert net_income_ytd == Decimal("3000.00")
    # Equity (base) = 10000, total equity = 10000 + 3000 = 13000
    assert total_equity == Decimal("13000.00")
    # A = L + E
    assert total_assets == total_liabilities + total_equity
