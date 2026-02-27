"""Tests for GL entry posting, reversal, and listing.

Test IDs: GL-GLE-01 through GL-GLE-06
"""
import json
import uuid

import db_query
from helpers import (
    _call_action,
    create_test_company,
    create_test_fiscal_year,
    create_test_account,
    create_test_cost_center,
    post_test_gl_entries,
)


def _setup_gl_env(fresh_db):
    """Create company, fiscal year, cost center, and two balance-sheet accounts.

    Returns (company_id, cash_id, equity_id, cost_center_id).
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
    # Cost center not required for balance-sheet accounts, but we create one
    # for actions that may reference it
    cc_id = create_test_cost_center(fresh_db, company_id)
    return company_id, cash_id, equity_id, cc_id


# ---------------------------------------------------------------------------
# GL-GLE-01: post-gl-entries balanced entries succeed
# ---------------------------------------------------------------------------
def test_post_gl_entries_balanced(fresh_db):
    company_id, cash_id, equity_id, _ = _setup_gl_env(fresh_db)
    voucher_id = str(uuid.uuid4())

    entries = json.dumps([
        {"account_id": cash_id, "debit": "1000.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "1000.00"},
    ])

    result = _call_action(
        db_query.post_gl_entries, fresh_db,
        company_id=company_id,
        voucher_type="journal_entry",
        voucher_id=voucher_id,
        posting_date="2026-06-15",
        entries=entries,
    )
    assert result["status"] == "ok"
    assert result["entries_created"] == 2
    assert len(result["gl_entry_ids"]) == 2


# ---------------------------------------------------------------------------
# GL-GLE-02: post-gl-entries unbalanced entries fail
# ---------------------------------------------------------------------------
def test_post_gl_entries_unbalanced(fresh_db):
    company_id, cash_id, equity_id, _ = _setup_gl_env(fresh_db)
    voucher_id = str(uuid.uuid4())

    entries = json.dumps([
        {"account_id": cash_id, "debit": "1000.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "500.00"},
    ])

    result = _call_action(
        db_query.post_gl_entries, fresh_db,
        company_id=company_id,
        voucher_type="journal_entry",
        voucher_id=voucher_id,
        posting_date="2026-06-15",
        entries=entries,
    )
    assert result["status"] == "error"
    assert "balance" in result["message"].lower() or "Step 1" in result["message"]


# ---------------------------------------------------------------------------
# GL-GLE-03: reverse-gl-entries creates mirror entries
# ---------------------------------------------------------------------------
def test_reverse_gl_entries(fresh_db):
    company_id, cash_id, equity_id, _ = _setup_gl_env(fresh_db)
    voucher_id = str(uuid.uuid4())

    # Post entries directly so we bypass the full validation
    post_test_gl_entries(fresh_db, company_id, [
        {"account_id": cash_id, "debit": "500.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "500.00"},
    ], voucher_id=voucher_id)

    result = _call_action(
        db_query.reverse_gl_entries_action, fresh_db,
        voucher_type="journal_entry",
        voucher_id=voucher_id,
        posting_date="2026-06-16",
    )
    assert result["status"] == "ok"
    assert result["reversed_count"] == 2
    assert len(result["reversal_entry_ids"]) == 2

    # Verify originals are marked cancelled
    cancelled = fresh_db.execute(
        "SELECT COUNT(*) as cnt FROM gl_entry WHERE voucher_id = ? AND is_cancelled = 1",
        (voucher_id,),
    ).fetchone()["cnt"]
    assert cancelled == 2


# ---------------------------------------------------------------------------
# GL-GLE-04: list-gl-entries returns posted entries
# ---------------------------------------------------------------------------
def test_list_gl_entries(fresh_db):
    company_id, cash_id, equity_id, _ = _setup_gl_env(fresh_db)
    voucher_id = str(uuid.uuid4())

    post_test_gl_entries(fresh_db, company_id, [
        {"account_id": cash_id, "debit": "200.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "200.00"},
    ], voucher_id=voucher_id)

    result = _call_action(
        db_query.list_gl_entries, fresh_db,
        company_id=company_id,
    )
    assert result["status"] == "ok"
    assert result["total_count"] == 2
    assert len(result["entries"]) == 2


# ---------------------------------------------------------------------------
# GL-GLE-05: list-gl-entries with filters (date range, account)
# ---------------------------------------------------------------------------
def test_list_gl_entries_with_filters(fresh_db):
    company_id, cash_id, equity_id, _ = _setup_gl_env(fresh_db)
    v1 = str(uuid.uuid4())
    v2 = str(uuid.uuid4())

    post_test_gl_entries(fresh_db, company_id, [
        {"account_id": cash_id, "debit": "100.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "100.00"},
    ], voucher_id=v1, posting_date="2026-03-01")

    post_test_gl_entries(fresh_db, company_id, [
        {"account_id": cash_id, "debit": "200.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "200.00"},
    ], voucher_id=v2, posting_date="2026-09-01")

    # Filter by date range
    result = _call_action(
        db_query.list_gl_entries, fresh_db,
        company_id=company_id,
        from_date="2026-01-01", to_date="2026-06-30",
    )
    assert result["status"] == "ok"
    assert result["total_count"] == 2  # Only March entries

    # Filter by account
    result2 = _call_action(
        db_query.list_gl_entries, fresh_db,
        account_id=cash_id,
    )
    assert result2["status"] == "ok"
    assert result2["total_count"] == 2  # Both vouchers have cash entries


# ---------------------------------------------------------------------------
# GL-GLE-06: check-gl-integrity on balanced GL
# ---------------------------------------------------------------------------
def test_check_gl_integrity(fresh_db):
    company_id, cash_id, equity_id, _ = _setup_gl_env(fresh_db)

    post_test_gl_entries(fresh_db, company_id, [
        {"account_id": cash_id, "debit": "1000.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "1000.00"},
    ])

    result = _call_action(
        db_query.check_gl_integrity, fresh_db,
        company_id=company_id,
    )
    assert result["status"] == "ok"
    assert result["balanced"] is True
    assert result["total_debit"] == "1000.00"
    assert result["total_credit"] == "1000.00"
    assert result["difference"] == "0.00"
