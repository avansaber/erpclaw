"""Tests for the trial-balance action."""
from helpers import (
    _call_action,
    create_test_company,
    create_test_account,
    create_test_gl_entries,
)
from db_query import ACTIONS


def test_trial_balance_empty(fresh_db):
    """Trial balance with no GL entries returns empty accounts list."""
    conn = fresh_db
    cid = create_test_company(conn)
    create_test_account(conn, cid, "Cash", "asset", account_type="cash",
                        account_number="1001")

    result = _call_action(
        ACTIONS["trial-balance"], conn,
        company_id=cid, to_date="2026-12-31",
    )
    assert result["status"] == "ok"
    assert result["accounts"] == []
    assert result["total_debit"] == "0.00"
    assert result["total_credit"] == "0.00"


def test_trial_balance_with_gl_data(fresh_db):
    """Trial balance reflects GL debits and credits correctly."""
    conn = fresh_db
    cid = create_test_company(conn)
    cash_id = create_test_account(conn, cid, "Cash", "asset",
                                  account_type="cash", account_number="1001")
    revenue_id = create_test_account(conn, cid, "Sales Revenue", "income",
                                     account_type="revenue", account_number="4001")

    voucher_id = "JE-001"
    create_test_gl_entries(conn, [
        {"account_id": cash_id, "posting_date": "2026-03-15",
         "debit": "5000.00", "credit": "0.00", "voucher_id": voucher_id},
        {"account_id": revenue_id, "posting_date": "2026-03-15",
         "debit": "0.00", "credit": "5000.00", "voucher_id": voucher_id},
    ])

    result = _call_action(
        ACTIONS["trial-balance"], conn,
        company_id=cid, to_date="2026-12-31",
    )
    assert result["status"] == "ok"
    assert len(result["accounts"]) == 2
    assert result["total_debit"] == "5000.00"
    assert result["total_credit"] == "5000.00"

    # Find cash account
    cash_entry = [a for a in result["accounts"] if a["account_id"] == cash_id][0]
    assert cash_entry["closing_debit"] == "5000.00"
    assert cash_entry["closing_credit"] == "0.00"


def test_trial_balance_with_date_range(fresh_db):
    """Trial balance with from_date splits opening and period balances."""
    conn = fresh_db
    cid = create_test_company(conn)
    cash_id = create_test_account(conn, cid, "Cash", "asset",
                                  account_type="cash", account_number="1001")
    revenue_id = create_test_account(conn, cid, "Sales Revenue", "income",
                                     account_type="revenue", account_number="4001")

    # Pre-period entry (opening)
    v1 = "JE-PRE"
    create_test_gl_entries(conn, [
        {"account_id": cash_id, "posting_date": "2025-12-15",
         "debit": "1000.00", "credit": "0.00", "voucher_id": v1},
        {"account_id": revenue_id, "posting_date": "2025-12-15",
         "debit": "0.00", "credit": "1000.00", "voucher_id": v1},
    ])

    # In-period entry
    v2 = "JE-PER"
    create_test_gl_entries(conn, [
        {"account_id": cash_id, "posting_date": "2026-03-15",
         "debit": "2000.00", "credit": "0.00", "voucher_id": v2},
        {"account_id": revenue_id, "posting_date": "2026-03-15",
         "debit": "0.00", "credit": "2000.00", "voucher_id": v2},
    ])

    result = _call_action(
        ACTIONS["trial-balance"], conn,
        company_id=cid, from_date="2026-01-01", to_date="2026-12-31",
    )
    assert result["status"] == "ok"

    cash_entry = [a for a in result["accounts"] if a["account_id"] == cash_id][0]
    assert cash_entry["opening_debit"] == "1000.00"
    assert cash_entry["debit"] == "2000.00"
    assert cash_entry["closing_debit"] == "3000.00"
