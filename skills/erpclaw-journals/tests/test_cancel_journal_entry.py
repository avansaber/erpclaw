"""Tests for the cancel-journal-entry action.

Test IDs: JE-CAN-01 through JE-CAN-03
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
# JE-CAN-01: cancel a submitted JE
# ---------------------------------------------------------------------------
def test_cancel_basic(fresh_db):
    company_id, cash_id, equity_id = _setup_env(fresh_db)
    je_id = _create_and_submit(fresh_db, company_id, cash_id, equity_id)

    result = _call_action(
        db_query.cancel_journal_entry, fresh_db,
        journal_entry_id=je_id,
    )
    assert result["status"] == "ok"
    assert result["reversed"] is True

    je = fresh_db.execute(
        "SELECT status FROM journal_entry WHERE id = ?", (je_id,)
    ).fetchone()
    assert je["status"] == "cancelled"


# ---------------------------------------------------------------------------
# JE-CAN-02: cancel creates reversal GL entries
# ---------------------------------------------------------------------------
def test_cancel_reverses_gl(fresh_db):
    company_id, cash_id, equity_id = _setup_env(fresh_db)
    je_id = _create_and_submit(fresh_db, company_id, cash_id, equity_id,
                                amount="500.00")

    _call_action(
        db_query.cancel_journal_entry, fresh_db,
        journal_entry_id=je_id,
    )

    # Check originals are marked as cancelled
    cancelled = fresh_db.execute(
        """SELECT COUNT(*) as cnt FROM gl_entry
           WHERE voucher_id = ? AND is_cancelled = 1""",
        (je_id,),
    ).fetchone()["cnt"]
    assert cancelled == 2

    # Check reversal entries exist (is_cancelled = 0, but with reversed amounts)
    all_entries = fresh_db.execute(
        "SELECT * FROM gl_entry WHERE voucher_id = ?", (je_id,),
    ).fetchall()
    # 2 originals (cancelled) + 2 reversals = 4 total
    assert len(all_entries) == 4

    active_entries = fresh_db.execute(
        """SELECT * FROM gl_entry
           WHERE voucher_id = ? AND is_cancelled = 0""",
        (je_id,),
    ).fetchall()
    assert len(active_entries) == 2


# ---------------------------------------------------------------------------
# JE-CAN-03: cannot cancel a draft
# ---------------------------------------------------------------------------
def test_cancel_non_submitted_fails(fresh_db):
    company_id, cash_id, equity_id = _setup_env(fresh_db)
    je_id, _ = create_test_journal_entry(fresh_db, company_id, [
        {"account_id": cash_id, "debit": "1000.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "1000.00"},
    ])

    result = _call_action(
        db_query.cancel_journal_entry, fresh_db,
        journal_entry_id=je_id,
    )
    assert result["status"] == "error"
    assert "draft" in result["message"].lower() or "submitted" in result["message"].lower()
