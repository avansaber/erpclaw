"""Tests for submit-payment action."""
import uuid
from decimal import Decimal
import pytest

from db_query import ACTIONS
from helpers import (
    _call_action,
    create_test_company,
    create_test_fiscal_year,
    create_test_account,
)


@pytest.fixture
def setup_submit(fresh_db):
    """Create company + fiscal year + accounts for submit tests."""
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
    payable_acct = create_test_account(
        conn, company_id, "Accounts Payable", "liability",
        account_type="payable", balance_direction="credit_normal",
    )
    return {
        "conn": conn,
        "company_id": company_id,
        "bank_acct": bank_acct,
        "receivable_acct": receivable_acct,
        "payable_acct": payable_acct,
    }


def _create_draft_payment(s, payment_type="receive", paid_amount="5000.00"):
    """Helper to create a draft payment via the add-payment action."""
    party_type = "customer" if payment_type == "receive" else "supplier"
    if payment_type == "receive":
        paid_from = s["receivable_acct"]
        paid_to = s["bank_acct"]
    else:
        paid_from = s["bank_acct"]
        paid_to = s["payable_acct"]

    party_id = str(uuid.uuid4())
    result = _call_action(
        ACTIONS["add-payment"], s["conn"],
        company_id=s["company_id"],
        payment_type=payment_type,
        posting_date="2026-06-15",
        party_type=party_type,
        party_id=party_id,
        paid_from_account=paid_from,
        paid_to_account=paid_to,
        paid_amount=paid_amount,
    )
    assert result["status"] == "ok"
    return result["payment_entry_id"], party_id


def test_submit_receive(setup_submit):
    """Submit a receive payment and verify GL entries."""
    s = setup_submit
    pe_id, party_id = _create_draft_payment(s, "receive", "5000.00")

    result = _call_action(
        ACTIONS["submit-payment"], s["conn"],
        payment_entry_id=pe_id,
    )
    assert result["status"] == "ok"
    assert result["gl_entries_created"] == 2

    # Verify GL entries: DR bank, CR receivable
    gl_rows = s["conn"].execute(
        "SELECT * FROM gl_entry WHERE voucher_id = ? AND is_cancelled = 0 ORDER BY debit DESC",
        (pe_id,),
    ).fetchall()
    assert len(gl_rows) == 2

    # Identify debit and credit rows using Decimal comparison
    debit_row = [r for r in gl_rows if Decimal(r["debit"]) > 0][0]
    credit_row = [r for r in gl_rows if Decimal(r["credit"]) > 0][0]
    assert debit_row["account_id"] == s["bank_acct"]
    assert debit_row["debit"] == "5000.00"
    assert credit_row["account_id"] == s["receivable_acct"]
    assert credit_row["credit"] == "5000.00"

    # Verify payment entry status
    pe = s["conn"].execute(
        "SELECT status FROM payment_entry WHERE id = ?", (pe_id,)
    ).fetchone()
    assert pe["status"] == "submitted"


def test_submit_pay(setup_submit):
    """Submit a pay payment."""
    s = setup_submit
    pe_id, party_id = _create_draft_payment(s, "pay", "3000.00")

    result = _call_action(
        ACTIONS["submit-payment"], s["conn"],
        payment_entry_id=pe_id,
    )
    assert result["status"] == "ok"
    assert result["gl_entries_created"] == 2

    # Verify GL entries: DR payable, CR bank
    gl_rows = s["conn"].execute(
        "SELECT * FROM gl_entry WHERE voucher_id = ? AND is_cancelled = 0",
        (pe_id,),
    ).fetchall()
    debit_row = [r for r in gl_rows if Decimal(r["debit"]) > 0][0]
    credit_row = [r for r in gl_rows if Decimal(r["credit"]) > 0][0]
    assert debit_row["account_id"] == s["payable_acct"]
    assert credit_row["account_id"] == s["bank_acct"]


def test_submit_creates_ple(setup_submit):
    """Submit creates a PLE with negative amount (reduces outstanding)."""
    s = setup_submit
    pe_id, party_id = _create_draft_payment(s, "receive", "2000.00")

    result = _call_action(
        ACTIONS["submit-payment"], s["conn"],
        payment_entry_id=pe_id,
    )
    assert result["status"] == "ok"

    # Verify PLE created
    ple = s["conn"].execute(
        """SELECT * FROM payment_ledger_entry
           WHERE voucher_type = 'payment_entry' AND voucher_id = ?""",
        (pe_id,),
    ).fetchone()
    assert ple is not None
    assert ple["amount"] == "-2000.00"
    assert ple["party_type"] == "customer"
    assert ple["party_id"] == party_id
    # For receive, PLE account should be the receivable (paid_from_account)
    assert ple["account_id"] == s["receivable_acct"]


def test_submit_non_draft_fails(setup_submit):
    """Cannot submit a payment that is already submitted."""
    s = setup_submit
    pe_id, party_id = _create_draft_payment(s, "receive", "1000.00")

    # Submit once
    result1 = _call_action(
        ACTIONS["submit-payment"], s["conn"],
        payment_entry_id=pe_id,
    )
    assert result1["status"] == "ok"

    # Try submitting again
    result2 = _call_action(
        ACTIONS["submit-payment"], s["conn"],
        payment_entry_id=pe_id,
    )
    assert result2["status"] == "error"
    assert "must be 'draft'" in result2["message"]
