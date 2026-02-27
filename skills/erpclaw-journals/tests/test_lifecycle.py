"""Tests for delete and duplicate lifecycle actions.

Test IDs: JE-LC-01 through JE-LC-03
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
# JE-LC-01: duplicate a journal entry
# ---------------------------------------------------------------------------
def test_duplicate(fresh_db):
    company_id, cash_id, equity_id = _setup_env(fresh_db)
    je_id, _ = create_test_journal_entry(fresh_db, company_id, [
        {"account_id": cash_id, "debit": "1000.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "1000.00"},
    ], posting_date="2026-06-15", remark="Original remark")

    result = _call_action(
        db_query.duplicate_journal_entry, fresh_db,
        journal_entry_id=je_id,
    )
    assert result["status"] == "ok"
    assert "new_journal_entry_id" in result
    assert result["new_journal_entry_id"] != je_id

    # New JE should be a draft
    new_je = fresh_db.execute(
        "SELECT status, remark FROM journal_entry WHERE id = ?",
        (result["new_journal_entry_id"],)
    ).fetchone()
    assert new_je["status"] == "draft"
    assert new_je["remark"] == "Original remark"

    # New JE should have same lines
    new_lines = fresh_db.execute(
        "SELECT COUNT(*) as cnt FROM journal_entry_line WHERE journal_entry_id = ?",
        (result["new_journal_entry_id"],)
    ).fetchone()["cnt"]
    assert new_lines == 2


# ---------------------------------------------------------------------------
# JE-LC-02: delete a draft JE
# ---------------------------------------------------------------------------
def test_delete_draft(fresh_db):
    company_id, cash_id, equity_id = _setup_env(fresh_db)
    je_id, _ = create_test_journal_entry(fresh_db, company_id, [
        {"account_id": cash_id, "debit": "1000.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "1000.00"},
    ])

    result = _call_action(
        db_query.delete_journal_entry, fresh_db,
        journal_entry_id=je_id,
    )
    assert result["status"] == "ok"
    assert result["deleted"] is True

    # Verify JE is gone
    je = fresh_db.execute(
        "SELECT * FROM journal_entry WHERE id = ?", (je_id,)
    ).fetchone()
    assert je is None

    # Verify lines are gone
    line_count = fresh_db.execute(
        "SELECT COUNT(*) as cnt FROM journal_entry_line WHERE journal_entry_id = ?",
        (je_id,)
    ).fetchone()["cnt"]
    assert line_count == 0


# ---------------------------------------------------------------------------
# JE-LC-03: cannot delete a submitted JE
# ---------------------------------------------------------------------------
def test_delete_submitted_fails(fresh_db):
    company_id, cash_id, equity_id = _setup_env(fresh_db)
    je_id, _ = create_test_journal_entry(fresh_db, company_id, [
        {"account_id": cash_id, "debit": "1000.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "1000.00"},
    ], status="submitted")

    result = _call_action(
        db_query.delete_journal_entry, fresh_db,
        journal_entry_id=je_id,
    )
    assert result["status"] == "error"
    assert "submitted" in result["message"].lower() or "draft" in result["message"].lower()
