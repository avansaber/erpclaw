"""Tests for delete-payment action."""
import uuid
import pytest

from db_query import ACTIONS
from helpers import (
    _call_action,
    create_test_company,
    create_test_fiscal_year,
    create_test_account,
    create_test_payment_entry,
)


@pytest.fixture
def setup_delete(fresh_db):
    """Create company + accounts + draft and submitted payments."""
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

    # Draft payment
    draft_id, draft_naming = create_test_payment_entry(
        conn, company_id,
        payment_type="receive",
        party_type="customer",
        party_id=party_id,
        paid_from_account=receivable_acct,
        paid_to_account=bank_acct,
        paid_amount="1000.00",
        status="draft",
    )

    # Submitted payment
    submitted_id, submitted_naming = create_test_payment_entry(
        conn, company_id,
        payment_type="receive",
        party_type="customer",
        party_id=party_id,
        paid_from_account=receivable_acct,
        paid_to_account=bank_acct,
        paid_amount="2000.00",
        status="submitted",
    )

    return {
        "conn": conn,
        "company_id": company_id,
        "draft_id": draft_id,
        "submitted_id": submitted_id,
    }


def test_delete_draft(setup_delete):
    """Delete a draft payment entry."""
    s = setup_delete
    result = _call_action(
        ACTIONS["delete-payment"], s["conn"],
        payment_entry_id=s["draft_id"],
    )
    assert result["status"] == "ok"
    assert result["deleted"] is True

    # Verify deleted from DB
    row = s["conn"].execute(
        "SELECT * FROM payment_entry WHERE id = ?", (s["draft_id"],)
    ).fetchone()
    assert row is None


def test_delete_submitted_fails(setup_delete):
    """Cannot delete a submitted payment."""
    s = setup_delete
    result = _call_action(
        ACTIONS["delete-payment"], s["conn"],
        payment_entry_id=s["submitted_id"],
    )
    assert result["status"] == "error"
    assert "only 'draft' can be deleted" in result["message"]
