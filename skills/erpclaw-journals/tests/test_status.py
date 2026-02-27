"""Tests for the status action.

Test IDs: JE-ST-01 through JE-ST-02
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
# JE-ST-01: status with no journal entries
# ---------------------------------------------------------------------------
def test_status_empty(fresh_db):
    company_id, _, _ = _setup_env(fresh_db)

    result = _call_action(
        db_query.status, fresh_db,
        company_id=company_id,
    )
    assert result["status"] == "ok"
    assert result["total"] == 0
    assert result["draft"] == 0
    assert result["submitted"] == 0
    assert result["cancelled"] == 0
    assert result["amended"] == 0


# ---------------------------------------------------------------------------
# JE-ST-02: status with mixed statuses
# ---------------------------------------------------------------------------
def test_status_counts(fresh_db):
    company_id, cash_id, equity_id = _setup_env(fresh_db)

    lines = [
        {"account_id": cash_id, "debit": "100.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "100.00"},
    ]

    # Create 2 drafts
    create_test_journal_entry(fresh_db, company_id, lines, status="draft")
    create_test_journal_entry(fresh_db, company_id, lines, status="draft")

    # Create 1 submitted
    create_test_journal_entry(fresh_db, company_id, lines, status="submitted")

    # Create 1 cancelled
    create_test_journal_entry(fresh_db, company_id, lines, status="cancelled")

    result = _call_action(
        db_query.status, fresh_db,
        company_id=company_id,
    )
    assert result["status"] == "ok"
    assert result["total"] == 4
    assert result["draft"] == 2
    assert result["submitted"] == 1
    assert result["cancelled"] == 1
    assert result["amended"] == 0
