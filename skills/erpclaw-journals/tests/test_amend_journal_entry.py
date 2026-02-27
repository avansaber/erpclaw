"""Tests for the amend-journal-entry action.

Test IDs: JE-AMD-01 through JE-AMD-03
"""
import json

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


def _create_and_submit(fresh_db, company_id, cash_id, equity_id,
                        amount="1000.00", posting_date="2026-06-15"):
    """Helper: create and submit a JE, return je_id."""
    je_id, _ = create_test_journal_entry(fresh_db, company_id, [
        {"account_id": cash_id, "debit": amount, "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": amount},
    ], posting_date=posting_date)

    _call_action(
        db_query.submit_journal_entry, fresh_db,
        journal_entry_id=je_id,
    )
    return je_id


# ---------------------------------------------------------------------------
# JE-AMD-01: amend a submitted JE (copies lines from original)
# ---------------------------------------------------------------------------
def test_amend_basic(fresh_db):
    company_id, cash_id, equity_id = _setup_env(fresh_db)
    je_id = _create_and_submit(fresh_db, company_id, cash_id, equity_id)

    result = _call_action(
        db_query.amend_journal_entry, fresh_db,
        journal_entry_id=je_id,
    )
    assert result["status"] == "ok"
    assert "new_journal_entry_id" in result
    assert result["original_id"] == je_id

    # Old JE should be 'amended'
    old_je = fresh_db.execute(
        "SELECT status FROM journal_entry WHERE id = ?", (je_id,)
    ).fetchone()
    assert old_je["status"] == "amended"

    # New JE should be 'draft' with amended_from pointing to original
    new_je = fresh_db.execute(
        "SELECT status, amended_from FROM journal_entry WHERE id = ?",
        (result["new_journal_entry_id"],)
    ).fetchone()
    assert new_je["status"] == "draft"
    assert new_je["amended_from"] == je_id

    # New JE should have same number of lines
    new_lines = fresh_db.execute(
        "SELECT COUNT(*) as cnt FROM journal_entry_line WHERE journal_entry_id = ?",
        (result["new_journal_entry_id"],)
    ).fetchone()["cnt"]
    assert new_lines == 2


# ---------------------------------------------------------------------------
# JE-AMD-02: amend with new lines
# ---------------------------------------------------------------------------
def test_amend_with_new_lines(fresh_db):
    company_id, cash_id, equity_id = _setup_env(fresh_db)
    je_id = _create_and_submit(fresh_db, company_id, cash_id, equity_id,
                                amount="1000.00")

    new_lines = json.dumps([
        {"account_id": cash_id, "debit": "2000.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "2000.00"},
    ])

    result = _call_action(
        db_query.amend_journal_entry, fresh_db,
        journal_entry_id=je_id,
        lines=new_lines,
    )
    assert result["status"] == "ok"

    # Verify new JE has the updated amounts
    new_je = fresh_db.execute(
        "SELECT total_debit, total_credit FROM journal_entry WHERE id = ?",
        (result["new_journal_entry_id"],)
    ).fetchone()
    assert new_je["total_debit"] == "2000.00"
    assert new_je["total_credit"] == "2000.00"


# ---------------------------------------------------------------------------
# JE-AMD-03: cannot amend a draft
# ---------------------------------------------------------------------------
def test_amend_non_submitted_fails(fresh_db):
    company_id, cash_id, equity_id = _setup_env(fresh_db)
    je_id, _ = create_test_journal_entry(fresh_db, company_id, [
        {"account_id": cash_id, "debit": "1000.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "1000.00"},
    ])

    result = _call_action(
        db_query.amend_journal_entry, fresh_db,
        journal_entry_id=je_id,
    )
    assert result["status"] == "error"
    assert "draft" in result["message"].lower() or "submitted" in result["message"].lower()
