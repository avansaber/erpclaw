"""Tests for the update-journal-entry action.

Test IDs: JE-UPD-01 through JE-UPD-04
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
# JE-UPD-01: update posting_date on draft
# ---------------------------------------------------------------------------
def test_update_posting_date(fresh_db):
    company_id, cash_id, equity_id = _setup_env(fresh_db)
    je_id, _ = create_test_journal_entry(fresh_db, company_id, [
        {"account_id": cash_id, "debit": "1000.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "1000.00"},
    ], posting_date="2026-06-15")

    result = _call_action(
        db_query.update_journal_entry, fresh_db,
        journal_entry_id=je_id,
        posting_date="2026-07-01",
    )
    assert result["status"] == "ok"
    assert "posting_date" in result["updated_fields"]

    je = fresh_db.execute(
        "SELECT posting_date FROM journal_entry WHERE id = ?", (je_id,)
    ).fetchone()
    assert je["posting_date"] == "2026-07-01"


# ---------------------------------------------------------------------------
# JE-UPD-02: update lines (replace all)
# ---------------------------------------------------------------------------
def test_update_lines(fresh_db):
    company_id, cash_id, equity_id = _setup_env(fresh_db)
    je_id, _ = create_test_journal_entry(fresh_db, company_id, [
        {"account_id": cash_id, "debit": "1000.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "1000.00"},
    ])

    new_lines = json.dumps([
        {"account_id": cash_id, "debit": "2000.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "2000.00"},
    ])

    result = _call_action(
        db_query.update_journal_entry, fresh_db,
        journal_entry_id=je_id,
        lines=new_lines,
    )
    assert result["status"] == "ok"
    assert "lines" in result["updated_fields"]

    je = fresh_db.execute(
        "SELECT total_debit, total_credit FROM journal_entry WHERE id = ?", (je_id,)
    ).fetchone()
    assert je["total_debit"] == "2000.00"
    assert je["total_credit"] == "2000.00"


# ---------------------------------------------------------------------------
# JE-UPD-03: cannot update submitted JE
# ---------------------------------------------------------------------------
def test_update_submitted_fails(fresh_db):
    company_id, cash_id, equity_id = _setup_env(fresh_db)
    je_id, _ = create_test_journal_entry(fresh_db, company_id, [
        {"account_id": cash_id, "debit": "1000.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "1000.00"},
    ], status="submitted")

    result = _call_action(
        db_query.update_journal_entry, fresh_db,
        journal_entry_id=je_id,
        posting_date="2026-07-01",
    )
    assert result["status"] == "error"
    assert "submitted" in result["message"].lower() or "draft" in result["message"].lower()


# ---------------------------------------------------------------------------
# JE-UPD-04: no fields to update
# ---------------------------------------------------------------------------
def test_update_no_fields(fresh_db):
    company_id, cash_id, equity_id = _setup_env(fresh_db)
    je_id, _ = create_test_journal_entry(fresh_db, company_id, [
        {"account_id": cash_id, "debit": "1000.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "1000.00"},
    ])

    result = _call_action(
        db_query.update_journal_entry, fresh_db,
        journal_entry_id=je_id,
    )
    assert result["status"] == "error"
    assert "no fields" in result["message"].lower()
