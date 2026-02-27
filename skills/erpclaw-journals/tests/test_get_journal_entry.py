"""Tests for the get-journal-entry action.

Test IDs: JE-GET-01 through JE-GET-02
"""
import uuid

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
# JE-GET-01: get a journal entry with all lines
# ---------------------------------------------------------------------------
def test_get_journal_entry_with_lines(fresh_db):
    company_id, cash_id, equity_id = _setup_env(fresh_db)
    je_id, naming = create_test_journal_entry(fresh_db, company_id, [
        {"account_id": cash_id, "debit": "1000.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "1000.00"},
    ], posting_date="2026-06-15", remark="Test remark")

    result = _call_action(
        db_query.get_journal_entry, fresh_db,
        journal_entry_id=je_id,
    )
    assert result["status"] == "ok"
    assert result["id"] == je_id
    assert result["naming_series"] == naming
    assert result["posting_date"] == "2026-06-15"
    assert result["entry_type"] == "journal"
    assert result["remark"] == "Test remark"
    assert result["company_id"] == company_id
    assert len(result["lines"]) == 2

    # Verify line details
    debits = [l for l in result["lines"] if l["debit"] != "0"]
    credits = [l for l in result["lines"] if l["credit"] != "0"]
    assert len(debits) == 1
    assert len(credits) == 1
    assert debits[0]["account_id"] == cash_id
    assert debits[0]["account_name"] == "Cash"


# ---------------------------------------------------------------------------
# JE-GET-02: get not found
# ---------------------------------------------------------------------------
def test_get_not_found(fresh_db):
    result = _call_action(
        db_query.get_journal_entry, fresh_db,
        journal_entry_id=str(uuid.uuid4()),
    )
    assert result["status"] == "error"
    assert "not found" in result["message"].lower()
