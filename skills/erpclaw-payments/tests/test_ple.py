"""Tests for create-payment-ledger-entry and get-outstanding actions."""
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
def setup_ple(fresh_db):
    """Create company + accounts for PLE tests."""
    conn = fresh_db
    company_id = create_test_company(conn)
    create_test_fiscal_year(conn, company_id)
    receivable_acct = create_test_account(
        conn, company_id, "Accounts Receivable", "asset",
        account_type="receivable", balance_direction="debit_normal",
    )
    return {
        "conn": conn,
        "company_id": company_id,
        "receivable_acct": receivable_acct,
    }


def test_create_ple(setup_ple):
    """Create a PLE entry directly via the action."""
    s = setup_ple
    party_id = str(uuid.uuid4())
    voucher_id = str(uuid.uuid4())

    result = _call_action(
        ACTIONS["create-payment-ledger-entry"], s["conn"],
        voucher_type="sales_invoice",
        voucher_id=voucher_id,
        party_type="customer",
        party_id=party_id,
        ple_amount="5000.00",
        posting_date="2026-06-15",
        account_id=s["receivable_acct"],
    )
    assert result["status"] == "ok"
    assert result["ple_id"]

    # Verify in DB
    ple = s["conn"].execute(
        "SELECT * FROM payment_ledger_entry WHERE id = ?", (result["ple_id"],)
    ).fetchone()
    assert ple is not None
    assert ple["amount"] == "5000.00"
    assert ple["voucher_type"] == "sales_invoice"
    assert ple["party_type"] == "customer"
    assert ple["delinked"] == 0


def test_get_outstanding(setup_ple):
    """Create PLE entries and verify outstanding aggregation."""
    s = setup_ple
    party_id = str(uuid.uuid4())

    # Create two invoice PLEs (positive = outstanding receivable)
    inv1_id = str(uuid.uuid4())
    inv2_id = str(uuid.uuid4())
    _call_action(
        ACTIONS["create-payment-ledger-entry"], s["conn"],
        voucher_type="sales_invoice",
        voucher_id=inv1_id,
        party_type="customer",
        party_id=party_id,
        ple_amount="3000.00",
        posting_date="2026-06-10",
        account_id=s["receivable_acct"],
    )
    _call_action(
        ACTIONS["create-payment-ledger-entry"], s["conn"],
        voucher_type="sales_invoice",
        voucher_id=inv2_id,
        party_type="customer",
        party_id=party_id,
        ple_amount="2000.00",
        posting_date="2026-06-12",
        account_id=s["receivable_acct"],
    )

    # Create a payment PLE (negative = reduces outstanding)
    pay_id = str(uuid.uuid4())
    _call_action(
        ACTIONS["create-payment-ledger-entry"], s["conn"],
        voucher_type="payment_entry",
        voucher_id=pay_id,
        party_type="customer",
        party_id=party_id,
        ple_amount="-1000.00",
        posting_date="2026-06-14",
        account_id=s["receivable_acct"],
    )

    # Get outstanding
    result = _call_action(
        ACTIONS["get-outstanding"], s["conn"],
        party_type="customer",
        party_id=party_id,
    )
    assert result["status"] == "ok"
    # Total: 3000 + 2000 - 1000 = 4000
    assert result["outstanding"] == "4000.00"
    # Should have 3 voucher entries (all have non-zero outstanding)
    assert len(result["vouchers"]) == 3
