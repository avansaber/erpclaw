"""Tests for the general-ledger action."""
from helpers import (
    _call_action,
    create_test_company,
    create_test_account,
    create_test_gl_entries,
)
from db_query import ACTIONS


def test_general_ledger_all_entries(fresh_db):
    """General ledger returns all GL entries for the period."""
    conn = fresh_db
    cid = create_test_company(conn)
    cash_id = create_test_account(conn, cid, "Cash", "asset",
                                  account_type="cash", account_number="1001")
    revenue_id = create_test_account(conn, cid, "Revenue", "income",
                                     account_type="revenue", account_number="4001")

    v1 = "JE-001"
    create_test_gl_entries(conn, [
        {"account_id": cash_id, "posting_date": "2026-02-01",
         "debit": "5000.00", "credit": "0.00", "voucher_id": v1},
        {"account_id": revenue_id, "posting_date": "2026-02-01",
         "debit": "0.00", "credit": "5000.00", "voucher_id": v1},
    ])

    v2 = "JE-002"
    create_test_gl_entries(conn, [
        {"account_id": cash_id, "posting_date": "2026-03-01",
         "debit": "3000.00", "credit": "0.00", "voucher_id": v2},
        {"account_id": revenue_id, "posting_date": "2026-03-01",
         "debit": "0.00", "credit": "3000.00", "voucher_id": v2},
    ])

    result = _call_action(
        ACTIONS["general-ledger"], conn,
        company_id=cid, from_date="2026-01-01", to_date="2026-12-31",
    )
    assert result["status"] == "ok"
    assert len(result["entries"]) == 4
    assert result["total_debit"] == "8000.00"
    assert result["total_credit"] == "8000.00"


def test_general_ledger_filtered_by_account(fresh_db):
    """General ledger filtered by account_id returns only that account's entries."""
    conn = fresh_db
    cid = create_test_company(conn)
    cash_id = create_test_account(conn, cid, "Cash", "asset",
                                  account_type="cash", account_number="1001")
    revenue_id = create_test_account(conn, cid, "Revenue", "income",
                                     account_type="revenue", account_number="4001")

    v1 = "JE-001"
    create_test_gl_entries(conn, [
        {"account_id": cash_id, "posting_date": "2026-02-01",
         "debit": "5000.00", "credit": "0.00", "voucher_id": v1},
        {"account_id": revenue_id, "posting_date": "2026-02-01",
         "debit": "0.00", "credit": "5000.00", "voucher_id": v1},
    ])

    result = _call_action(
        ACTIONS["general-ledger"], conn,
        company_id=cid, from_date="2026-01-01", to_date="2026-12-31",
        account_id=cash_id,
    )
    assert result["status"] == "ok"
    assert len(result["entries"]) == 1
    assert result["entries"][0]["account_name"] == "Cash"
    assert result["total_debit"] == "5000.00"
    assert result["total_credit"] == "0.00"


def test_general_ledger_filtered_by_voucher_type(fresh_db):
    """General ledger filtered by voucher_type returns matching entries only."""
    conn = fresh_db
    cid = create_test_company(conn)
    cash_id = create_test_account(conn, cid, "Cash", "asset",
                                  account_type="cash", account_number="1001")
    revenue_id = create_test_account(conn, cid, "Revenue", "income",
                                     account_type="revenue", account_number="4001")
    ar_id = create_test_account(conn, cid, "AR", "asset",
                                account_type="receivable", account_number="1100")

    # Journal entry
    v1 = "JE-001"
    create_test_gl_entries(conn, [
        {"account_id": cash_id, "posting_date": "2026-02-01",
         "debit": "5000.00", "credit": "0.00",
         "voucher_type": "journal_entry", "voucher_id": v1},
        {"account_id": revenue_id, "posting_date": "2026-02-01",
         "debit": "0.00", "credit": "5000.00",
         "voucher_type": "journal_entry", "voucher_id": v1},
    ])

    # Sales invoice
    v2 = "SI-001"
    create_test_gl_entries(conn, [
        {"account_id": ar_id, "posting_date": "2026-03-01",
         "debit": "3000.00", "credit": "0.00",
         "voucher_type": "sales_invoice", "voucher_id": v2},
        {"account_id": revenue_id, "posting_date": "2026-03-01",
         "debit": "0.00", "credit": "3000.00",
         "voucher_type": "sales_invoice", "voucher_id": v2},
    ])

    result = _call_action(
        ACTIONS["general-ledger"], conn,
        company_id=cid, from_date="2026-01-01", to_date="2026-12-31",
        voucher_type="sales_invoice",
    )
    assert result["status"] == "ok"
    assert len(result["entries"]) == 2
    for e in result["entries"]:
        assert e["voucher_type"] == "sales_invoice"
