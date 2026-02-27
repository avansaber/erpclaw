"""Tests for the tax-summary action."""
from helpers import (
    _call_action,
    create_test_company,
    create_test_account,
    create_test_gl_entries,
)
from db_query import ACTIONS


def test_tax_summary_empty(fresh_db):
    """Tax summary with no tax accounts or data returns empty result."""
    conn = fresh_db
    cid = create_test_company(conn)

    result = _call_action(
        ACTIONS["tax-summary"], conn,
        company_id=cid, from_date="2026-01-01", to_date="2026-12-31",
    )
    assert result["status"] == "ok"
    assert result["collected"] == "0.00"
    assert result["paid"] == "0.00"
    assert result["net_liability"] == "0.00"
    assert result["by_account"] == []


def test_tax_summary_with_tax_accounts(fresh_db):
    """Tax summary shows collected and paid amounts for tax accounts."""
    conn = fresh_db
    cid = create_test_company(conn)

    # Create a tax account with DDL-valid account_type='tax'
    tax_id = create_test_account(conn, cid, "Sales Tax Payable", "liability",
                                 account_type="tax", account_number="2100")

    # Add GL entries for tax collected
    cash_id = create_test_account(conn, cid, "Cash", "asset",
                                  account_type="cash", account_number="1001")
    v1 = "SI-001"
    create_test_gl_entries(conn, [
        {"account_id": cash_id, "posting_date": "2026-03-15",
         "debit": "1080.00", "credit": "0.00", "voucher_id": v1},
        {"account_id": tax_id, "posting_date": "2026-03-15",
         "debit": "0.00", "credit": "80.00", "voucher_id": v1},
    ])

    result = _call_action(
        ACTIONS["tax-summary"], conn,
        company_id=cid, from_date="2026-01-01", to_date="2026-12-31",
    )
    assert result["status"] == "ok"
    # Tax account with type 'tax' is found, credit of 80 = collected
    assert result["collected"] == "80.00"
    assert result["paid"] == "0.00"
    assert result["net_liability"] == "80.00"
    assert len(result["by_account"]) == 1
    assert result["by_account"][0]["account_name"] == "Sales Tax Payable"
    assert result["by_account"][0]["amount"] == "80.00"
