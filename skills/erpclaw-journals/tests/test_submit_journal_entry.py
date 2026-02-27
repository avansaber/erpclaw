"""Tests for the submit-journal-entry action.

Test IDs: JE-SUB-01 through JE-SUB-04
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


# ---------------------------------------------------------------------------
# JE-SUB-01: submit a draft JE
# ---------------------------------------------------------------------------
def test_submit_basic(fresh_db):
    company_id, cash_id, equity_id = _setup_env(fresh_db)
    je_id, _ = create_test_journal_entry(fresh_db, company_id, [
        {"account_id": cash_id, "debit": "1000.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "1000.00"},
    ], posting_date="2026-06-15")

    result = _call_action(
        db_query.submit_journal_entry, fresh_db,
        journal_entry_id=je_id,
    )
    assert result["status"] == "ok"
    assert result["gl_entries_created"] == 2

    # Verify status changed to submitted
    je = fresh_db.execute(
        "SELECT status FROM journal_entry WHERE id = ?", (je_id,)
    ).fetchone()
    assert je["status"] == "submitted"


# ---------------------------------------------------------------------------
# JE-SUB-02: submit creates GL entries
# ---------------------------------------------------------------------------
def test_submit_creates_gl_entries(fresh_db):
    company_id, cash_id, equity_id = _setup_env(fresh_db)
    je_id, _ = create_test_journal_entry(fresh_db, company_id, [
        {"account_id": cash_id, "debit": "500.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "500.00"},
    ], posting_date="2026-06-15")

    _call_action(
        db_query.submit_journal_entry, fresh_db,
        journal_entry_id=je_id,
    )

    # Check gl_entry table has rows for this JE
    gl_entries = fresh_db.execute(
        "SELECT * FROM gl_entry WHERE voucher_type = 'journal_entry' AND voucher_id = ?",
        (je_id,),
    ).fetchall()
    assert len(gl_entries) == 2

    # Verify one debit and one credit
    # GL posting normalizes zero values to "0.00", so compare as Decimal
    from decimal import Decimal
    gl_list = [dict(g) for g in gl_entries]
    debits = [g for g in gl_list if Decimal(g["debit"]) > 0]
    credits = [g for g in gl_list if Decimal(g["credit"]) > 0]
    assert len(debits) == 1
    assert len(credits) == 1
    assert debits[0]["account_id"] == cash_id
    assert credits[0]["account_id"] == equity_id


# ---------------------------------------------------------------------------
# JE-SUB-03: cannot submit already submitted JE
# ---------------------------------------------------------------------------
def test_submit_non_draft_fails(fresh_db):
    company_id, cash_id, equity_id = _setup_env(fresh_db)
    je_id, _ = create_test_journal_entry(fresh_db, company_id, [
        {"account_id": cash_id, "debit": "1000.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "1000.00"},
    ], posting_date="2026-06-15")

    # Submit first time
    _call_action(
        db_query.submit_journal_entry, fresh_db,
        journal_entry_id=je_id,
    )

    # Try to submit again
    result = _call_action(
        db_query.submit_journal_entry, fresh_db,
        journal_entry_id=je_id,
    )
    assert result["status"] == "error"
    assert "submitted" in result["message"].lower() or "draft" in result["message"].lower()


# ---------------------------------------------------------------------------
# JE-SUB-04: submit opening entry type
# ---------------------------------------------------------------------------
def test_submit_opening_entry(fresh_db):
    company_id, cash_id, equity_id = _setup_env(fresh_db)
    je_id, _ = create_test_journal_entry(fresh_db, company_id, [
        {"account_id": cash_id, "debit": "5000.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "5000.00"},
    ], posting_date="2026-01-01", entry_type="opening")

    result = _call_action(
        db_query.submit_journal_entry, fresh_db,
        journal_entry_id=je_id,
    )
    assert result["status"] == "ok"
    assert result["gl_entries_created"] == 2

    je = fresh_db.execute(
        "SELECT status, entry_type FROM journal_entry WHERE id = ?", (je_id,)
    ).fetchone()
    assert je["status"] == "submitted"
    assert je["entry_type"] == "opening"
