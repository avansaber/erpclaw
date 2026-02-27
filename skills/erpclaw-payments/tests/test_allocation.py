"""Tests for allocate-payment and get-unallocated-payments actions."""
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
def setup_allocation(fresh_db):
    """Create company + accounts + a submitted payment."""
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
        paid_amount="5000.00",
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


def test_allocate_payment(setup_allocation):
    """Allocate a submitted payment to a voucher."""
    s = setup_allocation
    voucher_id = str(uuid.uuid4())

    result = _call_action(
        ACTIONS["allocate-payment"], s["conn"],
        payment_entry_id=s["pe_id"],
        voucher_type="sales_invoice",
        voucher_id=voucher_id,
        allocated_amount="2000.00",
    )
    assert result["status"] == "ok"
    assert result["allocation_id"]
    assert result["remaining_unallocated"] == "3000.00"

    # Verify allocation in DB
    alloc = s["conn"].execute(
        "SELECT * FROM payment_allocation WHERE id = ?",
        (result["allocation_id"],),
    ).fetchone()
    assert alloc["voucher_type"] == "sales_invoice"
    assert alloc["allocated_amount"] == "2000.00"


def test_allocate_exceeds_unallocated(setup_allocation):
    """Allocation amount > unallocated should error."""
    s = setup_allocation
    voucher_id = str(uuid.uuid4())

    result = _call_action(
        ACTIONS["allocate-payment"], s["conn"],
        payment_entry_id=s["pe_id"],
        voucher_type="sales_invoice",
        voucher_id=voucher_id,
        allocated_amount="6000.00",  # > 5000 unallocated
    )
    assert result["status"] == "error"
    assert "exceeds unallocated" in result["message"]


def test_get_unallocated_payments(setup_allocation):
    """Verify only submitted with unallocated > 0 returned."""
    s = setup_allocation

    # Also create a draft payment (should NOT appear)
    draft_result = _call_action(
        ACTIONS["add-payment"], s["conn"],
        company_id=s["company_id"],
        payment_type="receive",
        posting_date="2026-06-15",
        party_type="customer",
        party_id=s["party_id"],
        paid_from_account=s["receivable_acct"],
        paid_to_account=s["bank_acct"],
        paid_amount="1000.00",
    )
    assert draft_result["status"] == "ok"

    result = _call_action(
        ACTIONS["get-unallocated-payments"], s["conn"],
        party_type="customer",
        party_id=s["party_id"],
        company_id=s["company_id"],
    )
    assert result["status"] == "ok"
    # Only the submitted payment should appear
    assert len(result["payments"]) == 1
    assert result["payments"][0]["id"] == s["pe_id"]
