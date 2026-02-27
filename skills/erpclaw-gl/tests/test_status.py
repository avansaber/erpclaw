"""Tests for the status action.

Test IDs: GL-ST-01 through GL-ST-02
"""
import db_query
from helpers import (
    _call_action,
    create_test_company,
    create_test_fiscal_year,
    create_test_account,
    post_test_gl_entries,
)


# ---------------------------------------------------------------------------
# GL-ST-01: status with no data
# ---------------------------------------------------------------------------
def test_status_no_data(fresh_db):
    result = _call_action(db_query.status, fresh_db)
    assert result["status"] == "ok"
    assert result["companies"] == 0
    assert result["accounts"] == 0
    assert result["fiscal_years"] == 0
    assert result["gl_entries"] == 0
    assert result["latest_posting_date"] is None


# ---------------------------------------------------------------------------
# GL-ST-02: status with company_id
# ---------------------------------------------------------------------------
def test_status_with_company(fresh_db):
    company_id = create_test_company(fresh_db)
    create_test_fiscal_year(fresh_db, company_id)
    cash_id = create_test_account(
        fresh_db, company_id, "Cash", "asset", account_number="1000",
    )
    equity_id = create_test_account(
        fresh_db, company_id, "Equity", "equity", account_number="3000",
    )
    post_test_gl_entries(fresh_db, company_id, [
        {"account_id": cash_id, "debit": "1000.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "1000.00"},
    ], posting_date="2026-06-15")

    result = _call_action(
        db_query.status, fresh_db, company_id=company_id,
    )
    assert result["status"] == "ok"
    assert result["companies"] == 1
    assert result["accounts"] == 2
    assert result["fiscal_years"] == 1
    assert result["gl_entries"] == 2
    assert result["latest_posting_date"] == "2026-06-15"
