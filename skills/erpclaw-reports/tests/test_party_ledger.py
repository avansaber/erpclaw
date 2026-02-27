"""Tests for the party-ledger action."""
from helpers import (
    _call_action,
    create_test_company,
    create_test_account,
    create_test_customer,
    create_test_gl_entries,
)
from db_query import ACTIONS


def test_party_ledger_customer(fresh_db):
    """Party ledger shows GL entries for a specific customer."""
    conn = fresh_db
    cid = create_test_company(conn)
    ar_id = create_test_account(conn, cid, "AR", "asset",
                                account_type="receivable", account_number="1100")
    revenue_id = create_test_account(conn, cid, "Revenue", "income",
                                     account_type="revenue", account_number="4001")
    cust_id = create_test_customer(conn, cid, "Acme Corp")

    # Sales invoice
    v1 = "SI-001"
    create_test_gl_entries(conn, [
        {"account_id": ar_id, "posting_date": "2026-03-01",
         "debit": "5000.00", "credit": "0.00",
         "voucher_type": "sales_invoice", "voucher_id": v1,
         "party_type": "customer", "party_id": cust_id},
        {"account_id": revenue_id, "posting_date": "2026-03-01",
         "debit": "0.00", "credit": "5000.00",
         "voucher_type": "sales_invoice", "voucher_id": v1},
    ])

    # Payment received
    v2 = "PE-001"
    create_test_gl_entries(conn, [
        {"account_id": ar_id, "posting_date": "2026-04-01",
         "debit": "0.00", "credit": "3000.00",
         "voucher_type": "payment_entry", "voucher_id": v2,
         "party_type": "customer", "party_id": cust_id},
    ])

    result = _call_action(
        ACTIONS["party-ledger"], conn,
        party_type="customer", party_id=cust_id,
        from_date="2026-01-01", to_date="2026-12-31",
    )
    assert result["status"] == "ok"
    assert result["party_name"] == "Acme Corp"
    assert result["opening_balance"] == "0.00"
    assert len(result["entries"]) == 2
    # First entry: debit 5000 -> balance 5000
    assert result["entries"][0]["debit"] == "5000.00"
    assert result["entries"][0]["balance"] == "5000.00"
    # Second entry: credit 3000 -> balance 2000
    assert result["entries"][1]["credit"] == "3000.00"
    assert result["entries"][1]["balance"] == "2000.00"
    assert result["closing_balance"] == "2000.00"


def test_party_ledger_with_opening_balance(fresh_db):
    """Party ledger correctly calculates opening balance from prior periods."""
    conn = fresh_db
    cid = create_test_company(conn)
    ar_id = create_test_account(conn, cid, "AR", "asset",
                                account_type="receivable", account_number="1100")
    cust_id = create_test_customer(conn, cid, "BigCo")

    # Prior period entry
    v1 = "SI-OLD"
    create_test_gl_entries(conn, [
        {"account_id": ar_id, "posting_date": "2025-12-15",
         "debit": "10000.00", "credit": "0.00",
         "voucher_type": "sales_invoice", "voucher_id": v1,
         "party_type": "customer", "party_id": cust_id},
    ])

    # Current period payment
    v2 = "PE-001"
    create_test_gl_entries(conn, [
        {"account_id": ar_id, "posting_date": "2026-01-15",
         "debit": "0.00", "credit": "4000.00",
         "voucher_type": "payment_entry", "voucher_id": v2,
         "party_type": "customer", "party_id": cust_id},
    ])

    result = _call_action(
        ACTIONS["party-ledger"], conn,
        party_type="customer", party_id=cust_id,
        from_date="2026-01-01", to_date="2026-12-31",
    )
    assert result["status"] == "ok"
    assert result["opening_balance"] == "10000.00"
    assert len(result["entries"]) == 1
    # Opening 10000 - credit 4000 = closing 6000
    assert result["closing_balance"] == "6000.00"
