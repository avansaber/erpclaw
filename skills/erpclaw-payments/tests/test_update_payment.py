"""Tests for update-payment action."""
import uuid
import pytest

from db_query import ACTIONS
from helpers import (
    _call_action,
    create_test_company,
    create_test_fiscal_year,
    create_test_account,
    create_test_payment_entry,
    force_payment_status,
)


@pytest.fixture
def setup_with_payment(fresh_db):
    """Create company + accounts + a draft payment entry."""
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
    party_id = str(uuid.uuid4())
    pe_id, naming = create_test_payment_entry(
        conn, company_id,
        payment_type="receive",
        party_type="customer",
        party_id=party_id,
        paid_from_account=receivable_acct,
        paid_to_account=bank_acct,
        paid_amount="1000.00",
    )
    return {
        "conn": conn,
        "company_id": company_id,
        "bank_acct": bank_acct,
        "receivable_acct": receivable_acct,
        "pe_id": pe_id,
        "naming": naming,
        "party_id": party_id,
    }


def test_update_paid_amount(setup_with_payment):
    """Change the paid amount on a draft payment."""
    s = setup_with_payment
    result = _call_action(
        ACTIONS["update-payment"], s["conn"],
        payment_entry_id=s["pe_id"],
        paid_amount="2000.00",
    )
    assert result["status"] == "ok"
    assert "paid_amount" in result["updated_fields"]

    row = s["conn"].execute(
        "SELECT paid_amount FROM payment_entry WHERE id = ?", (s["pe_id"],)
    ).fetchone()
    assert row["paid_amount"] == "2000.00"


def test_update_reference_number(setup_with_payment):
    """Change the reference number on a draft payment."""
    s = setup_with_payment
    result = _call_action(
        ACTIONS["update-payment"], s["conn"],
        payment_entry_id=s["pe_id"],
        reference_number="CHK-12345",
    )
    assert result["status"] == "ok"
    assert "reference_number" in result["updated_fields"]

    row = s["conn"].execute(
        "SELECT reference_number FROM payment_entry WHERE id = ?", (s["pe_id"],)
    ).fetchone()
    assert row["reference_number"] == "CHK-12345"


def test_update_submitted_fails(setup_with_payment):
    """Cannot update a submitted payment."""
    s = setup_with_payment
    # Force status to submitted (bypasses GL workflow for guard-condition test)
    force_payment_status(s["conn"], s["pe_id"], "submitted")

    result = _call_action(
        ACTIONS["update-payment"], s["conn"],
        payment_entry_id=s["pe_id"],
        paid_amount="2000.00",
    )
    assert result["status"] == "error"
    assert "must be 'draft'" in result["message"]
