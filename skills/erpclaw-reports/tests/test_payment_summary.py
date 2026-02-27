"""Tests for the payment-summary action."""
from helpers import (
    _call_action,
    create_test_company,
    create_test_account,
    create_test_customer,
    create_test_payment_entry,
)
from db_query import ACTIONS


def test_payment_summary_empty(fresh_db):
    """Payment summary with no payments returns zero totals."""
    conn = fresh_db
    cid = create_test_company(conn)

    result = _call_action(
        ACTIONS["payment-summary"], conn,
        company_id=cid, from_date="2026-01-01", to_date="2026-12-31",
    )
    assert result["status"] == "ok"
    assert result["total_received"] == "0.00"
    assert result["total_paid"] == "0.00"
    assert result["by_party_type"] == []


def test_payment_summary_with_payments(fresh_db):
    """Payment summary shows received and paid totals and party breakdown."""
    conn = fresh_db
    cid = create_test_company(conn)
    bank_id = create_test_account(conn, cid, "Bank", "asset",
                                  account_type="bank", account_number="1010")
    ar_id = create_test_account(conn, cid, "AR", "asset",
                                account_type="receivable", account_number="1100")
    ap_id = create_test_account(conn, cid, "AP", "liability",
                                account_type="payable", account_number="2001")
    cust_id = create_test_customer(conn, cid, "Acme Corp")

    # Received payment
    create_test_payment_entry(
        conn, cid, "receive", "2026-03-15",
        paid_from_account=ar_id, paid_to_account=bank_id,
        paid_amount="5000.00", party_type="customer", party_id=cust_id,
    )

    # Another received payment
    create_test_payment_entry(
        conn, cid, "receive", "2026-04-01",
        paid_from_account=ar_id, paid_to_account=bank_id,
        paid_amount="3000.00", party_type="customer", party_id=cust_id,
    )

    # Paid to supplier (using bank -> AP)
    from helpers import create_test_supplier
    sup_id = create_test_supplier(conn, cid, "Parts Inc")
    create_test_payment_entry(
        conn, cid, "pay", "2026-05-01",
        paid_from_account=bank_id, paid_to_account=ap_id,
        paid_amount="2000.00", party_type="supplier", party_id=sup_id,
    )

    result = _call_action(
        ACTIONS["payment-summary"], conn,
        company_id=cid, from_date="2026-01-01", to_date="2026-12-31",
    )
    assert result["status"] == "ok"
    assert result["total_received"] == "8000.00"
    assert result["total_paid"] == "2000.00"

    # Check by_party_type breakdown
    assert len(result["by_party_type"]) == 2
    party_types = {p["party_type"]: p for p in result["by_party_type"]}
    assert party_types["customer"]["count"] == 2
    assert party_types["customer"]["amount"] == "8000.00"
    assert party_types["supplier"]["count"] == 1
    assert party_types["supplier"]["amount"] == "2000.00"
