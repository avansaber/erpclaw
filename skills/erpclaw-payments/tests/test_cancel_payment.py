"""Tests for cancel-payment action."""
import uuid
import pytest

from db_query import ACTIONS
from helpers import (
    _call_action,
    create_test_company,
    create_test_fiscal_year,
    create_test_account,
)


@pytest.fixture
def setup_cancel(fresh_db):
    """Create company + fiscal year + accounts + a submitted payment."""
    conn = fresh_db
    company_id = create_test_company(conn)
    create_test_fiscal_year(conn, company_id)
    bank_acct = create_test_account(
        conn, company_id, "Bank Account", "asset",
        account_type="bank", balance_direction="debit_normal",
    )
    receivable_acct = create_test_account(
        conn, company_id, "Accounts Receivable", "asset",
        account_type="receivable", balance_direction="debit_normal",
    )

    # Create and submit a payment
    party_id = str(uuid.uuid4())
    add_result = _call_action(
        ACTIONS["add-payment"], conn,
        company_id=company_id,
        payment_type="receive",
        posting_date="2026-06-15",
        party_type="customer",
        party_id=party_id,
        paid_from_account=receivable_acct,
        paid_to_account=bank_acct,
        paid_amount="4000.00",
    )
    pe_id = add_result["payment_entry_id"]

    submit_result = _call_action(
        ACTIONS["submit-payment"], conn,
        payment_entry_id=pe_id,
    )
    assert submit_result["status"] == "ok"

    return {
        "conn": conn,
        "company_id": company_id,
        "bank_acct": bank_acct,
        "receivable_acct": receivable_acct,
        "pe_id": pe_id,
        "party_id": party_id,
    }


def test_cancel_basic(setup_cancel):
    """Cancel a submitted payment, verify status becomes cancelled."""
    s = setup_cancel
    result = _call_action(
        ACTIONS["cancel-payment"], s["conn"],
        payment_entry_id=s["pe_id"],
    )
    assert result["status"] == "ok"
    assert result["reversed"] is True

    pe = s["conn"].execute(
        "SELECT status FROM payment_entry WHERE id = ?", (s["pe_id"],)
    ).fetchone()
    assert pe["status"] == "cancelled"


def test_cancel_reverses_gl_and_ple(setup_cancel):
    """Cancel reverses GL entries and creates reversal PLE."""
    s = setup_cancel

    # Count GL and PLE before cancel
    gl_before = s["conn"].execute(
        "SELECT COUNT(*) as cnt FROM gl_entry WHERE voucher_id = ?",
        (s["pe_id"],),
    ).fetchone()["cnt"]
    ple_before = s["conn"].execute(
        "SELECT COUNT(*) as cnt FROM payment_ledger_entry WHERE voucher_id = ?",
        (s["pe_id"],),
    ).fetchone()["cnt"]
    assert gl_before == 2   # 2 GL entries from submit
    assert ple_before == 1  # 1 PLE from submit

    # Cancel
    result = _call_action(
        ACTIONS["cancel-payment"], s["conn"],
        payment_entry_id=s["pe_id"],
    )
    assert result["status"] == "ok"

    # GL: originals cancelled + 2 reversal entries
    gl_cancelled = s["conn"].execute(
        "SELECT COUNT(*) as cnt FROM gl_entry WHERE voucher_id = ? AND is_cancelled = 1",
        (s["pe_id"],),
    ).fetchone()["cnt"]
    gl_reversals = s["conn"].execute(
        "SELECT COUNT(*) as cnt FROM gl_entry WHERE voucher_id = ? AND is_cancelled = 0",
        (s["pe_id"],),
    ).fetchone()["cnt"]
    assert gl_cancelled == 2  # original 2 marked cancelled
    assert gl_reversals == 2  # 2 new reversal entries

    # PLE: original delinked + reversal entry
    ple_delinked = s["conn"].execute(
        "SELECT COUNT(*) as cnt FROM payment_ledger_entry WHERE voucher_id = ? AND delinked = 1",
        (s["pe_id"],),
    ).fetchone()["cnt"]
    ple_active = s["conn"].execute(
        "SELECT COUNT(*) as cnt FROM payment_ledger_entry WHERE voucher_id = ? AND delinked = 0",
        (s["pe_id"],),
    ).fetchone()["cnt"]
    assert ple_delinked == 1  # original PLE delinked
    assert ple_active == 1    # reversal PLE (active)

    # Reversal PLE should have positive amount (reverses the negative)
    reversal_ple = s["conn"].execute(
        """SELECT * FROM payment_ledger_entry
           WHERE voucher_id = ? AND delinked = 0""",
        (s["pe_id"],),
    ).fetchone()
    assert reversal_ple["amount"] == "4000.00"
    assert "Reversal" in reversal_ple["remarks"]


def test_cancel_non_submitted_fails(setup_cancel):
    """Cannot cancel a draft payment."""
    s = setup_cancel

    # Create a new draft payment (not submitted)
    party_id = str(uuid.uuid4())
    add_result = _call_action(
        ACTIONS["add-payment"], s["conn"],
        company_id=s["company_id"],
        payment_type="receive",
        posting_date="2026-06-15",
        party_type="customer",
        party_id=party_id,
        paid_from_account=s["receivable_acct"],
        paid_to_account=s["bank_acct"],
        paid_amount="1000.00",
    )
    draft_pe_id = add_result["payment_entry_id"]

    result = _call_action(
        ACTIONS["cancel-payment"], s["conn"],
        payment_entry_id=draft_pe_id,
    )
    assert result["status"] == "error"
    assert "must be 'submitted'" in result["message"]
