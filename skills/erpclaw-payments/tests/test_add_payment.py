"""Tests for add-payment action."""
import json
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
def setup_company(fresh_db):
    """Create company + fiscal year + accounts for payment tests."""
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


def test_add_payment_receive(setup_company):
    """Create a receive payment, verify draft status and naming."""
    s = setup_company
    party_id = str(uuid.uuid4())
    result = _call_action(
        ACTIONS["add-payment"], s["conn"],
        company_id=s["company_id"],
        payment_type="receive",
        posting_date="2026-06-15",
        party_type="customer",
        party_id=party_id,
        paid_from_account=s["receivable_acct"],
        paid_to_account=s["bank_acct"],
        paid_amount="5000.00",
    )
    assert result["status"] == "ok"
    assert result["payment_entry_id"]
    assert result["naming_series"].startswith("PAY-2026-")

    # Verify in DB
    row = s["conn"].execute(
        "SELECT * FROM payment_entry WHERE id = ?",
        (result["payment_entry_id"],),
    ).fetchone()
    assert row is not None
    assert row["status"] == "draft"
    assert row["paid_amount"] == "5000.00"
    assert row["unallocated_amount"] == "5000.00"


def test_add_payment_pay(setup_company):
    """Create a pay payment for a supplier."""
    s = setup_company
    party_id = str(uuid.uuid4())
    result = _call_action(
        ACTIONS["add-payment"], s["conn"],
        company_id=s["company_id"],
        payment_type="pay",
        posting_date="2026-06-15",
        party_type="supplier",
        party_id=party_id,
        paid_from_account=s["bank_acct"],
        paid_to_account=s["payable_acct"],
        paid_amount="3000.00",
    )
    assert result["status"] == "ok"
    assert result["naming_series"].startswith("PAY-")

    row = s["conn"].execute(
        "SELECT * FROM payment_entry WHERE id = ?",
        (result["payment_entry_id"],),
    ).fetchone()
    assert row["payment_type"] == "pay"
    assert row["party_type"] == "supplier"


def test_add_payment_with_allocations(setup_company):
    """Create payment with allocations, verify unallocated reduced."""
    s = setup_company
    party_id = str(uuid.uuid4())
    voucher_id = str(uuid.uuid4())
    allocs = json.dumps([
        {"voucher_type": "sales_invoice", "voucher_id": voucher_id,
         "allocated_amount": "300.00"},
    ])
    result = _call_action(
        ACTIONS["add-payment"], s["conn"],
        company_id=s["company_id"],
        payment_type="receive",
        posting_date="2026-06-15",
        party_type="customer",
        party_id=party_id,
        paid_from_account=s["receivable_acct"],
        paid_to_account=s["bank_acct"],
        paid_amount="1000.00",
        allocations=allocs,
    )
    assert result["status"] == "ok"

    row = s["conn"].execute(
        "SELECT unallocated_amount FROM payment_entry WHERE id = ?",
        (result["payment_entry_id"],),
    ).fetchone()
    assert row["unallocated_amount"] == "700.00"

    alloc_rows = s["conn"].execute(
        "SELECT * FROM payment_allocation WHERE payment_entry_id = ?",
        (result["payment_entry_id"],),
    ).fetchall()
    assert len(alloc_rows) == 1
    assert alloc_rows[0]["allocated_amount"] == "300.00"


def test_add_payment_invalid_account(setup_company):
    """Bad account ID should return error."""
    s = setup_company
    party_id = str(uuid.uuid4())
    result = _call_action(
        ACTIONS["add-payment"], s["conn"],
        company_id=s["company_id"],
        payment_type="receive",
        posting_date="2026-06-15",
        party_type="customer",
        party_id=party_id,
        paid_from_account="nonexistent-account-id",
        paid_to_account=s["bank_acct"],
        paid_amount="1000.00",
    )
    assert result["status"] == "error"
    assert "not found" in result["message"]


def test_add_payment_zero_amount(setup_company):
    """Zero amount should return error."""
    s = setup_company
    party_id = str(uuid.uuid4())
    result = _call_action(
        ACTIONS["add-payment"], s["conn"],
        company_id=s["company_id"],
        payment_type="receive",
        posting_date="2026-06-15",
        party_type="customer",
        party_id=party_id,
        paid_from_account=s["receivable_acct"],
        paid_to_account=s["bank_acct"],
        paid_amount="0",
    )
    assert result["status"] == "error"
    assert "must be > 0" in result["message"]
