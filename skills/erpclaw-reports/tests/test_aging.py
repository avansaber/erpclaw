"""Tests for the ar-aging and ap-aging actions."""
from helpers import (
    _call_action,
    create_test_company,
    create_test_account,
    create_test_customer,
    create_test_supplier,
    create_test_ple,
)
from db_query import ACTIONS


def test_ar_aging_empty(fresh_db):
    """AR aging with no PLE data returns empty result."""
    conn = fresh_db
    cid = create_test_company(conn)

    result = _call_action(
        ACTIONS["ar-aging"], conn,
        company_id=cid, as_of_date="2026-06-30",
    )
    assert result["status"] == "ok"
    assert result["customers"] == []
    assert result["total_outstanding"] == "0.00"


def test_ar_aging_with_data(fresh_db):
    """AR aging shows customer outstanding in correct aging buckets."""
    conn = fresh_db
    cid = create_test_company(conn)
    ar_id = create_test_account(conn, cid, "AR", "asset",
                                account_type="receivable", account_number="1100")
    cust_id = create_test_customer(conn, cid, "Acme Corp")

    # Invoice 90 days ago (2026-04-01 from as_of_date 2026-06-30)
    create_test_ple(conn, ar_id, "customer", cust_id, "2026-04-01",
                    "5000.00", voucher_type="sales_invoice")

    # Invoice 15 days ago (2026-06-15 from as_of_date 2026-06-30)
    create_test_ple(conn, ar_id, "customer", cust_id, "2026-06-15",
                    "3000.00", voucher_type="sales_invoice")

    result = _call_action(
        ACTIONS["ar-aging"], conn,
        company_id=cid, as_of_date="2026-06-30",
    )
    assert result["status"] == "ok"
    assert result["total_outstanding"] == "8000.00"
    assert len(result["customers"]) == 1

    cust = result["customers"][0]
    assert cust["customer_name"] == "Acme Corp"
    assert cust["total"] == "8000.00"


def test_ap_aging_with_data(fresh_db):
    """AP aging shows supplier outstanding amounts."""
    conn = fresh_db
    cid = create_test_company(conn)
    ap_id = create_test_account(conn, cid, "AP", "liability",
                                account_type="payable", account_number="2001")
    sup_id = create_test_supplier(conn, cid, "Parts Inc")

    # Supplier invoice 45 days ago
    create_test_ple(conn, ap_id, "supplier", sup_id, "2026-05-16",
                    "7500.00", voucher_type="purchase_invoice")

    result = _call_action(
        ACTIONS["ap-aging"], conn,
        company_id=cid, as_of_date="2026-06-30",
    )
    assert result["status"] == "ok"
    assert result["total_outstanding"] == "7500.00"
    assert len(result["suppliers"]) == 1

    sup = result["suppliers"][0]
    assert sup["supplier_name"] == "Parts Inc"
    assert sup["total"] == "7500.00"
