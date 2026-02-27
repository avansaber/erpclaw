"""Tests for the add-journal-entry action.

Test IDs: JE-ADD-01 through JE-ADD-06
"""
import json
import uuid

import db_query
from helpers import (
    _call_action,
    create_test_company,
    create_test_fiscal_year,
    create_test_account,
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
# JE-ADD-01: basic 2-line journal entry
# ---------------------------------------------------------------------------
def test_add_journal_entry_basic(fresh_db):
    company_id, cash_id, equity_id = _setup_env(fresh_db)

    lines = json.dumps([
        {"account_id": cash_id, "debit": "1000.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "1000.00"},
    ])

    result = _call_action(
        db_query.add_journal_entry, fresh_db,
        company_id=company_id,
        posting_date="2026-06-15",
        lines=lines,
    )
    assert result["status"] == "ok"
    assert "journal_entry_id" in result
    assert result["naming_series"].startswith("JE-2026-")

    # Verify in DB
    je = fresh_db.execute(
        "SELECT * FROM journal_entry WHERE id = ?", (result["journal_entry_id"],)
    ).fetchone()
    assert je is not None
    assert je["status"] == "draft"
    assert je["entry_type"] == "journal"

    # Verify lines
    line_count = fresh_db.execute(
        "SELECT COUNT(*) as cnt FROM journal_entry_line WHERE journal_entry_id = ?",
        (result["journal_entry_id"],)
    ).fetchone()["cnt"]
    assert line_count == 2


# ---------------------------------------------------------------------------
# JE-ADD-02: multi-line journal entry (4+ lines)
# ---------------------------------------------------------------------------
def test_add_journal_entry_multi_line(fresh_db):
    company_id, cash_id, equity_id = _setup_env(fresh_db)
    bank_id = create_test_account(
        fresh_db, company_id, "Bank", "asset", account_type="bank",
        account_number="1010",
    )
    loan_id = create_test_account(
        fresh_db, company_id, "Loan Payable", "liability",
        account_number="2000",
    )

    lines = json.dumps([
        {"account_id": cash_id, "debit": "500.00", "credit": "0"},
        {"account_id": bank_id, "debit": "500.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "700.00"},
        {"account_id": loan_id, "debit": "0", "credit": "300.00"},
    ])

    result = _call_action(
        db_query.add_journal_entry, fresh_db,
        company_id=company_id,
        posting_date="2026-06-15",
        lines=lines,
    )
    assert result["status"] == "ok"

    # Verify totals
    je = fresh_db.execute(
        "SELECT * FROM journal_entry WHERE id = ?", (result["journal_entry_id"],)
    ).fetchone()
    assert je["total_debit"] == "1000.00"
    assert je["total_credit"] == "1000.00"

    line_count = fresh_db.execute(
        "SELECT COUNT(*) as cnt FROM journal_entry_line WHERE journal_entry_id = ?",
        (result["journal_entry_id"],)
    ).fetchone()["cnt"]
    assert line_count == 4


# ---------------------------------------------------------------------------
# JE-ADD-03: unbalanced lines fail
# ---------------------------------------------------------------------------
def test_add_journal_entry_unbalanced(fresh_db):
    company_id, cash_id, equity_id = _setup_env(fresh_db)

    lines = json.dumps([
        {"account_id": cash_id, "debit": "1000.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "500.00"},
    ])

    result = _call_action(
        db_query.add_journal_entry, fresh_db,
        company_id=company_id,
        posting_date="2026-06-15",
        lines=lines,
    )
    assert result["status"] == "error"
    assert "must equal" in result["message"].lower() or "debit" in result["message"].lower()


# ---------------------------------------------------------------------------
# JE-ADD-04: single line fails
# ---------------------------------------------------------------------------
def test_add_journal_entry_single_line(fresh_db):
    company_id, cash_id, equity_id = _setup_env(fresh_db)

    lines = json.dumps([
        {"account_id": cash_id, "debit": "1000.00", "credit": "0"},
    ])

    result = _call_action(
        db_query.add_journal_entry, fresh_db,
        company_id=company_id,
        posting_date="2026-06-15",
        lines=lines,
    )
    assert result["status"] == "error"
    assert "2 lines" in result["message"].lower() or "at least" in result["message"].lower()


# ---------------------------------------------------------------------------
# JE-ADD-05: invalid account ID fails
# ---------------------------------------------------------------------------
def test_add_journal_entry_invalid_account(fresh_db):
    company_id, cash_id, equity_id = _setup_env(fresh_db)
    fake_id = str(uuid.uuid4())

    lines = json.dumps([
        {"account_id": cash_id, "debit": "1000.00", "credit": "0"},
        {"account_id": fake_id, "debit": "0", "credit": "1000.00"},
    ])

    result = _call_action(
        db_query.add_journal_entry, fresh_db,
        company_id=company_id,
        posting_date="2026-06-15",
        lines=lines,
    )
    assert result["status"] == "error"
    assert "not found" in result["message"].lower()


# ---------------------------------------------------------------------------
# JE-ADD-06: line with both debit and credit > 0 fails
# ---------------------------------------------------------------------------
def test_add_journal_entry_both_debit_credit(fresh_db):
    company_id, cash_id, equity_id = _setup_env(fresh_db)

    lines = json.dumps([
        {"account_id": cash_id, "debit": "500.00", "credit": "500.00"},
        {"account_id": equity_id, "debit": "0", "credit": "0"},
    ])

    result = _call_action(
        db_query.add_journal_entry, fresh_db,
        company_id=company_id,
        posting_date="2026-06-15",
        lines=lines,
    )
    assert result["status"] == "error"
    assert "both" in result["message"].lower() or "debit and credit" in result["message"].lower()
