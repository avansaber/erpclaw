"""Tests for get-account-balance action.

Test IDs: GL-AB-01 through GL-AB-03
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
# GL-AB-01: get-account-balance on account with entries
# ---------------------------------------------------------------------------
def test_get_account_balance_with_entries(fresh_db):
    company_id = create_test_company(fresh_db)
    create_test_fiscal_year(fresh_db, company_id)
    cash_id = create_test_account(
        fresh_db, company_id, "Cash", "asset",
        account_number="1000",
    )
    equity_id = create_test_account(
        fresh_db, company_id, "Equity", "equity",
        account_number="3000",
    )

    # Post entries
    post_test_gl_entries(fresh_db, company_id, [
        {"account_id": cash_id, "debit": "5000.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "5000.00"},
    ], posting_date="2026-06-15")

    result = _call_action(
        db_query.get_account_balance_action, fresh_db,
        account_id=cash_id,
        as_of_date="2026-12-31",
    )
    assert result["status"] == "ok"
    assert result["debit_total"] == "5000.00"
    assert result["credit_total"] == "0"
    assert result["balance"] == "5000.00"
    assert result["currency"] == "USD"


# ---------------------------------------------------------------------------
# GL-AB-02: get-account-balance with as-of-date filter
# ---------------------------------------------------------------------------
def test_get_account_balance_as_of_date(fresh_db):
    company_id = create_test_company(fresh_db)
    create_test_fiscal_year(fresh_db, company_id)
    cash_id = create_test_account(
        fresh_db, company_id, "Cash", "asset",
        account_number="1000",
    )
    equity_id = create_test_account(
        fresh_db, company_id, "Equity", "equity",
        account_number="3000",
    )

    # Post entries at different dates
    post_test_gl_entries(fresh_db, company_id, [
        {"account_id": cash_id, "debit": "1000.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "1000.00"},
    ], posting_date="2026-03-01")

    post_test_gl_entries(fresh_db, company_id, [
        {"account_id": cash_id, "debit": "2000.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "2000.00"},
    ], posting_date="2026-09-01")

    # As of June 30: should only include March entries
    result = _call_action(
        db_query.get_account_balance_action, fresh_db,
        account_id=cash_id,
        as_of_date="2026-06-30",
    )
    assert result["status"] == "ok"
    assert result["debit_total"] == "1000.00"
    assert result["balance"] == "1000.00"

    # As of Dec 31: should include both
    result2 = _call_action(
        db_query.get_account_balance_action, fresh_db,
        account_id=cash_id,
        as_of_date="2026-12-31",
    )
    assert result2["balance"] == "3000.00"


# ---------------------------------------------------------------------------
# GL-AB-03: balance direction (debit_normal vs credit_normal)
# ---------------------------------------------------------------------------
def test_balance_direction(fresh_db):
    company_id = create_test_company(fresh_db)
    create_test_fiscal_year(fresh_db, company_id)

    # Credit-normal account (liability)
    payable_id = create_test_account(
        fresh_db, company_id, "Accounts Payable", "liability",
        account_number="2000",
        balance_direction="credit_normal",
    )
    cash_id = create_test_account(
        fresh_db, company_id, "Cash", "asset",
        account_number="1000",
    )

    # Credit the payable account (increase liability)
    post_test_gl_entries(fresh_db, company_id, [
        {"account_id": cash_id, "debit": "0", "credit": "3000.00"},
        {"account_id": payable_id, "debit": "0", "credit": "3000.00"},
    ], posting_date="2026-06-15")

    # For credit_normal account, balance = credit - debit (positive = normal)
    result = _call_action(
        db_query.get_account_balance_action, fresh_db,
        account_id=payable_id,
        as_of_date="2026-12-31",
    )
    assert result["status"] == "ok"
    assert result["credit_total"] == "3000.00"
    assert result["balance"] == "3000.00"  # credit_normal: credit - debit

    # For debit_normal account (cash), debit=0, credit=3000
    # balance = debit - credit = -3000
    result2 = _call_action(
        db_query.get_account_balance_action, fresh_db,
        account_id=cash_id,
        as_of_date="2026-12-31",
    )
    assert result2["balance"] == "-3000.00"  # debit_normal: debit - credit
