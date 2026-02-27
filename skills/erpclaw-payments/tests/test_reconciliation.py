"""Tests for reconcile-payments, bank-reconciliation, and status actions."""
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
def setup_reconcile(fresh_db):
    """Create company + accounts + submitted payments + invoice PLEs."""
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

    # Create and submit two payments
    pe_ids = []
    for amount in ["3000.00", "2000.00"]:
        add_r = _call_action(
            ACTIONS["add-payment"], conn,
            company_id=company_id,
            payment_type="receive",
            posting_date="2026-06-15",
            party_type="customer",
            party_id=party_id,
            paid_from_account=receivable_acct,
            paid_to_account=bank_acct,
            paid_amount=amount,
        )
        assert add_r["status"] == "ok"
        sub_r = _call_action(
            ACTIONS["submit-payment"], conn,
            payment_entry_id=add_r["payment_entry_id"],
        )
        assert sub_r["status"] == "ok"
        pe_ids.append(add_r["payment_entry_id"])

    # Create PLE entries for invoices (positive = outstanding receivable)
    inv1_id = str(uuid.uuid4())
    inv2_id = str(uuid.uuid4())
    _call_action(
        ACTIONS["create-payment-ledger-entry"], conn,
        voucher_type="sales_invoice",
        voucher_id=inv1_id,
        party_type="customer",
        party_id=party_id,
        ple_amount="2500.00",
        posting_date="2026-06-10",
        account_id=receivable_acct,
    )
    _call_action(
        ACTIONS["create-payment-ledger-entry"], conn,
        voucher_type="sales_invoice",
        voucher_id=inv2_id,
        party_type="customer",
        party_id=party_id,
        ple_amount="1500.00",
        posting_date="2026-06-12",
        account_id=receivable_acct,
    )

    return {
        "conn": conn,
        "company_id": company_id,
        "bank_acct": bank_acct,
        "receivable_acct": receivable_acct,
        "party_id": party_id,
        "pe_ids": pe_ids,
        "inv1_id": inv1_id,
        "inv2_id": inv2_id,
    }


def test_reconcile_basic(setup_reconcile):
    """Reconcile payments against invoices FIFO, verify matches."""
    s = setup_reconcile

    result = _call_action(
        ACTIONS["reconcile-payments"], s["conn"],
        party_type="customer",
        party_id=s["party_id"],
        company_id=s["company_id"],
    )
    assert result["status"] == "ok"
    assert len(result["matched"]) > 0

    # Total allocated should be min(total payments unallocated, total invoices outstanding)
    # Payments: 3000 + 2000 = 5000
    # Invoices: 2500 + 1500 = 4000
    # So all 4000 of invoices should be allocated
    from decimal import Decimal
    total_allocated = sum(
        Decimal(m["allocated_amount"]) for m in result["matched"]
    )
    assert total_allocated == Decimal("4000.00")

    # Verify unmatched counts
    # All invoices matched (4000 invoices, 4000 allocated)
    assert result["unmatched_invoices"] == 0
    # 5000 payments - 4000 allocated = 1000 remaining
    assert result["unmatched_payments"] == 1


def test_bank_reconciliation(setup_reconcile):
    """Verify GL balance for bank account in date range."""
    s = setup_reconcile

    result = _call_action(
        ACTIONS["bank-reconciliation"], s["conn"],
        bank_account_id=s["bank_acct"],
        from_date="2026-01-01",
        to_date="2026-12-31",
    )
    assert result["status"] == "ok"
    assert result["bank_account"] == "Bank Account"
    # Two submitted payments DR bank for 3000 + 2000 = 5000
    assert result["gl_entries"] > 0
    assert result["payment_entries"] == 2

    # GL balance should be positive (debit > credit for bank)
    from decimal import Decimal
    gl_bal = Decimal(result["gl_balance"])
    assert gl_bal == Decimal("5000.00")


def test_status(setup_reconcile):
    """Verify status counts and totals."""
    s = setup_reconcile

    result = _call_action(
        ACTIONS["status"], s["conn"],
        company_id=s["company_id"],
    )
    assert result["status"] == "ok"
    assert result["total"] == 2
    assert result["submitted"] == 2
    assert result["draft"] == 0
    assert result["cancelled"] == 0

    # total_received should be 5000 (3000 + 2000)
    from decimal import Decimal
    assert Decimal(result["total_received"]) == Decimal("5000.00")
    assert Decimal(result["total_paid"]) == Decimal("0")
