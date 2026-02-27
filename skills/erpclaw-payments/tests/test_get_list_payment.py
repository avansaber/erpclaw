"""Tests for get-payment and list-payments actions."""
import json
import uuid
import pytest

from db_query import ACTIONS
from helpers import (
    _call_action,
    create_test_company,
    create_test_fiscal_year,
    create_test_account,
    create_test_payment_entry,
    create_test_payment_allocation,
)


@pytest.fixture
def setup_with_payments(fresh_db):
    """Create company + accounts + multiple payment entries."""
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

    party_id = str(uuid.uuid4())

    # Create a draft receive payment
    pe1_id, pe1_naming = create_test_payment_entry(
        conn, company_id,
        payment_type="receive",
        party_type="customer",
        party_id=party_id,
        paid_from_account=receivable_acct,
        paid_to_account=bank_acct,
        paid_amount="1000.00",
        status="draft",
    )

    # Create a submitted receive payment
    pe2_id, pe2_naming = create_test_payment_entry(
        conn, company_id,
        payment_type="receive",
        party_type="customer",
        party_id=party_id,
        paid_from_account=receivable_acct,
        paid_to_account=bank_acct,
        paid_amount="2000.00",
        status="submitted",
    )

    # Add an allocation to pe1
    alloc_voucher_id = str(uuid.uuid4())
    create_test_payment_allocation(conn, pe1_id, alloc_voucher_id, "400.00")

    return {
        "conn": conn,
        "company_id": company_id,
        "pe1_id": pe1_id,
        "pe2_id": pe2_id,
        "party_id": party_id,
        "alloc_voucher_id": alloc_voucher_id,
    }


def test_get_payment_with_allocations(setup_with_payments):
    """Get a payment entry and verify all fields + allocations."""
    s = setup_with_payments
    result = _call_action(
        ACTIONS["get-payment"], s["conn"],
        payment_entry_id=s["pe1_id"],
    )
    assert result["status"] == "ok"
    assert result["id"] == s["pe1_id"]
    assert result["payment_type"] == "receive"
    assert result["paid_amount"] == "1000.00"
    assert result["unallocated_amount"] == "600.00"
    assert len(result["allocations"]) == 1
    assert result["allocations"][0]["voucher_id"] == s["alloc_voucher_id"]
    assert result["allocations"][0]["allocated_amount"] == "400.00"


def test_list_payments_basic(setup_with_payments):
    """List all payments by company."""
    s = setup_with_payments
    result = _call_action(
        ACTIONS["list-payments"], s["conn"],
        company_id=s["company_id"],
    )
    assert result["status"] == "ok"
    assert result["total_count"] == 2
    assert len(result["payments"]) == 2


def test_list_payments_by_status(setup_with_payments):
    """Filter payments by status."""
    s = setup_with_payments
    result = _call_action(
        ACTIONS["list-payments"], s["conn"],
        company_id=s["company_id"],
        pe_status="submitted",
    )
    assert result["status"] == "ok"
    assert result["total_count"] == 1
    assert result["payments"][0]["id"] == s["pe2_id"]
    assert result["payments"][0]["status"] == "submitted"
