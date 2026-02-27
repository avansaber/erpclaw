"""Tests for the gl-summary action."""
from helpers import (
    _call_action,
    create_test_company,
    create_test_account,
    create_test_gl_entries,
)
from db_query import ACTIONS


def test_gl_summary_empty(fresh_db):
    """GL summary with no entries returns empty by_voucher_type."""
    conn = fresh_db
    cid = create_test_company(conn)

    result = _call_action(
        ACTIONS["gl-summary"], conn,
        company_id=cid, from_date="2026-01-01", to_date="2026-12-31",
    )
    assert result["status"] == "ok"
    assert result["by_voucher_type"] == []


def test_gl_summary_grouped_by_voucher_type(fresh_db):
    """GL summary groups entries by voucher_type with totals."""
    conn = fresh_db
    cid = create_test_company(conn)
    cash_id = create_test_account(conn, cid, "Cash", "asset",
                                  account_type="cash", account_number="1001")
    revenue_id = create_test_account(conn, cid, "Revenue", "income",
                                     account_type="revenue", account_number="4001")
    ar_id = create_test_account(conn, cid, "AR", "asset",
                                account_type="receivable", account_number="1100")

    # Journal entries
    v1 = "JE-001"
    create_test_gl_entries(conn, [
        {"account_id": cash_id, "posting_date": "2026-02-01",
         "debit": "5000.00", "credit": "0.00",
         "voucher_type": "journal_entry", "voucher_id": v1},
        {"account_id": revenue_id, "posting_date": "2026-02-01",
         "debit": "0.00", "credit": "5000.00",
         "voucher_type": "journal_entry", "voucher_id": v1},
    ])

    # Sales invoice entries
    v2 = "SI-001"
    create_test_gl_entries(conn, [
        {"account_id": ar_id, "posting_date": "2026-03-01",
         "debit": "3000.00", "credit": "0.00",
         "voucher_type": "sales_invoice", "voucher_id": v2},
        {"account_id": revenue_id, "posting_date": "2026-03-01",
         "debit": "0.00", "credit": "3000.00",
         "voucher_type": "sales_invoice", "voucher_id": v2},
    ])

    v3 = "SI-002"
    create_test_gl_entries(conn, [
        {"account_id": ar_id, "posting_date": "2026-04-01",
         "debit": "2000.00", "credit": "0.00",
         "voucher_type": "sales_invoice", "voucher_id": v3},
        {"account_id": revenue_id, "posting_date": "2026-04-01",
         "debit": "0.00", "credit": "2000.00",
         "voucher_type": "sales_invoice", "voucher_id": v3},
    ])

    result = _call_action(
        ACTIONS["gl-summary"], conn,
        company_id=cid, from_date="2026-01-01", to_date="2026-12-31",
    )
    assert result["status"] == "ok"
    assert len(result["by_voucher_type"]) == 2

    by_type = {r["voucher_type"]: r for r in result["by_voucher_type"]}
    assert by_type["journal_entry"]["count"] == 2
    assert by_type["journal_entry"]["total_debit"] == "5000.00"
    assert by_type["journal_entry"]["total_credit"] == "5000.00"

    assert by_type["sales_invoice"]["count"] == 4
    assert by_type["sales_invoice"]["total_debit"] == "5000.00"
    assert by_type["sales_invoice"]["total_credit"] == "5000.00"
