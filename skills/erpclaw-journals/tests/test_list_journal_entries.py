"""Tests for the list-journal-entries action.

Test IDs: JE-LIST-01 through JE-LIST-03
"""
import db_query
from helpers import (
    _call_action,
    create_test_company,
    create_test_fiscal_year,
    create_test_account,
    create_test_journal_entry,
)


def _setup_env(fresh_db):
    """Create company, fiscal year, and two balance-sheet accounts.

    Returns (company_id, cash_id, equity_id).
    """
    company_id = create_test_company(fresh_db)
    create_test_fiscal_year(fresh_db, company_id)
    cash_id = create_test_account(
        fresh_db, company_id, "Cash", "asset", account_type="cash",
        account_number="1000",
    )
    equity_id = create_test_account(
        fresh_db, company_id, "Owner Equity", "equity",
        account_number="3000",
    )
    return company_id, cash_id, equity_id


# ---------------------------------------------------------------------------
# JE-LIST-01: list multiple journal entries
# ---------------------------------------------------------------------------
def test_list_basic(fresh_db):
    company_id, cash_id, equity_id = _setup_env(fresh_db)

    # Create 3 journal entries
    for i in range(3):
        create_test_journal_entry(fresh_db, company_id, [
            {"account_id": cash_id, "debit": "100.00", "credit": "0"},
            {"account_id": equity_id, "debit": "0", "credit": "100.00"},
        ], posting_date=f"2026-06-{15 + i:02d}")

    result = _call_action(
        db_query.list_journal_entries, fresh_db,
        company_id=company_id,
    )
    assert result["status"] == "ok"
    assert result["total_count"] == 3
    assert len(result["entries"]) == 3


# ---------------------------------------------------------------------------
# JE-LIST-02: filter by status
# ---------------------------------------------------------------------------
def test_list_by_status(fresh_db):
    company_id, cash_id, equity_id = _setup_env(fresh_db)

    # Create 2 drafts and 1 submitted
    create_test_journal_entry(fresh_db, company_id, [
        {"account_id": cash_id, "debit": "100.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "100.00"},
    ], status="draft")
    create_test_journal_entry(fresh_db, company_id, [
        {"account_id": cash_id, "debit": "200.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "200.00"},
    ], status="draft")
    create_test_journal_entry(fresh_db, company_id, [
        {"account_id": cash_id, "debit": "300.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "300.00"},
    ], status="submitted")

    # Filter drafts
    result = _call_action(
        db_query.list_journal_entries, fresh_db,
        company_id=company_id,
        je_status="draft",
    )
    assert result["status"] == "ok"
    assert result["total_count"] == 2

    # Filter submitted
    result2 = _call_action(
        db_query.list_journal_entries, fresh_db,
        company_id=company_id,
        je_status="submitted",
    )
    assert result2["status"] == "ok"
    assert result2["total_count"] == 1


# ---------------------------------------------------------------------------
# JE-LIST-03: filter by account_id in lines
# ---------------------------------------------------------------------------
def test_list_by_account(fresh_db):
    company_id, cash_id, equity_id = _setup_env(fresh_db)
    bank_id = create_test_account(
        fresh_db, company_id, "Bank", "asset", account_type="bank",
        account_number="1010",
    )

    # JE1 uses cash + equity
    create_test_journal_entry(fresh_db, company_id, [
        {"account_id": cash_id, "debit": "100.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "100.00"},
    ])
    # JE2 uses bank + equity
    create_test_journal_entry(fresh_db, company_id, [
        {"account_id": bank_id, "debit": "200.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "200.00"},
    ])

    # Filter by bank account - should get only JE2
    result = _call_action(
        db_query.list_journal_entries, fresh_db,
        company_id=company_id,
        account_id=bank_id,
    )
    assert result["status"] == "ok"
    assert result["total_count"] == 1

    # Filter by equity account - should get both
    result2 = _call_action(
        db_query.list_journal_entries, fresh_db,
        company_id=company_id,
        account_id=equity_id,
    )
    assert result2["status"] == "ok"
    assert result2["total_count"] == 2
