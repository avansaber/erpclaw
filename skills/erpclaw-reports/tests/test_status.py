"""Tests for the status action."""
from helpers import (
    _call_action,
    create_test_company,
    create_test_account,
    create_test_fiscal_year,
    create_test_gl_entries,
)
from db_query import ACTIONS


def test_status_empty(fresh_db):
    """Status with no GL data returns zero counts and null dates."""
    conn = fresh_db
    cid = create_test_company(conn)

    result = _call_action(
        ACTIONS["status"], conn,
        company_id=cid,
    )
    assert result["status"] == "ok"
    assert result["gl_entry_count"] == 0
    assert result["earliest_posting_date"] is None
    assert result["latest_posting_date"] is None
    assert result["fiscal_years"] == 0


def test_status_with_data(fresh_db):
    """Status returns correct GL count, date range, and fiscal year count."""
    conn = fresh_db
    cid = create_test_company(conn)
    create_test_fiscal_year(conn, cid, name="FY 2026")
    create_test_fiscal_year(conn, cid, name="FY 2025",
                            start_date="2025-01-01", end_date="2025-12-31")

    cash_id = create_test_account(conn, cid, "Cash", "asset",
                                  account_type="cash", account_number="1001")
    revenue_id = create_test_account(conn, cid, "Revenue", "income",
                                     account_type="revenue", account_number="4001")

    v1 = "JE-001"
    create_test_gl_entries(conn, [
        {"account_id": cash_id, "posting_date": "2026-01-15",
         "debit": "1000.00", "credit": "0.00", "voucher_id": v1},
        {"account_id": revenue_id, "posting_date": "2026-01-15",
         "debit": "0.00", "credit": "1000.00", "voucher_id": v1},
    ])

    v2 = "JE-002"
    create_test_gl_entries(conn, [
        {"account_id": cash_id, "posting_date": "2026-06-30",
         "debit": "2000.00", "credit": "0.00", "voucher_id": v2},
        {"account_id": revenue_id, "posting_date": "2026-06-30",
         "debit": "0.00", "credit": "2000.00", "voucher_id": v2},
    ])

    result = _call_action(
        ACTIONS["status"], conn,
        company_id=cid,
    )
    assert result["status"] == "ok"
    assert result["gl_entry_count"] == 4
    assert result["earliest_posting_date"] == "2026-01-15"
    assert result["latest_posting_date"] == "2026-06-30"
    assert result["fiscal_years"] == 2
