"""Tests for check-overdue action."""
import uuid
from helpers import (
    _call_action,
    create_test_company,
    create_test_customer,
)
import db_query
from db_query import ACTIONS


def test_check_overdue_action_exists():
    """check-overdue is registered in the ACTIONS dict."""
    assert "check-overdue" in ACTIONS


def test_check_overdue_no_invoices(fresh_db):
    """No overdue invoices returns empty result."""
    conn = fresh_db
    cid = create_test_company(conn)

    result = _call_action(ACTIONS["check-overdue"], conn, company_id=cid)
    assert result["status"] == "ok"
    assert result["overdue_count"] == 0
    assert result["total_overdue"] == "0.00"
    assert result["invoices"] == []
    assert result["buckets"]["0_30"]["count"] == 0
    assert result["buckets"]["90_plus"]["count"] == 0


def test_check_overdue_auto_detect_company(fresh_db):
    """Auto-detects company when not provided."""
    conn = fresh_db
    create_test_company(conn)

    result = _call_action(ACTIONS["check-overdue"], conn)
    assert result["status"] == "ok"
    assert result["overdue_count"] == 0


def test_check_overdue_with_overdue_invoices(fresh_db):
    """Detects overdue invoices and groups into aging buckets."""
    conn = fresh_db
    cid = create_test_company(conn)
    cust_id = create_test_customer(conn, cid, "Acme Corp")

    # Insert overdue invoices directly
    # Invoice 1: 10 days overdue (0-30 bucket)
    inv1_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO sales_invoice
           (id, naming_series, customer_id, posting_date, due_date,
            grand_total, outstanding_amount, status, company_id)
           VALUES (?, 'SINV-2026-00001', ?, '2026-01-01', '2026-01-20',
                   '3000.00', '3000.00', 'submitted', ?)""",
        (inv1_id, cust_id, cid),
    )

    # Invoice 2: 45 days overdue (31-60 bucket)
    inv2_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO sales_invoice
           (id, naming_series, customer_id, posting_date, due_date,
            grand_total, outstanding_amount, status, company_id)
           VALUES (?, 'SINV-2026-00002', ?, '2025-12-01', '2025-12-15',
                   '4500.00', '4500.00', 'overdue', ?)""",
        (inv2_id, cust_id, cid),
    )

    # Invoice 3: paid, should not appear
    inv3_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO sales_invoice
           (id, naming_series, customer_id, posting_date, due_date,
            grand_total, outstanding_amount, status, company_id)
           VALUES (?, 'SINV-2026-00003', ?, '2026-01-01', '2026-01-10',
                   '1000.00', '0', 'paid', ?)""",
        (inv3_id, cust_id, cid),
    )

    conn.commit()

    result = _call_action(ACTIONS["check-overdue"], conn, company_id=cid)
    assert result["status"] == "ok"
    assert result["overdue_count"] == 2
    # First invoice (sorted by days_overdue DESC) should be the most overdue
    assert result["invoices"][0]["days_overdue"] >= result["invoices"][1]["days_overdue"]
    assert result["invoices"][0]["customer_name"] == "Acme Corp"


def test_check_overdue_skips_cancelled(fresh_db):
    """Cancelled invoices are excluded from overdue check."""
    conn = fresh_db
    cid = create_test_company(conn)
    cust_id = create_test_customer(conn, cid)

    inv_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO sales_invoice
           (id, naming_series, customer_id, posting_date, due_date,
            grand_total, outstanding_amount, status, company_id)
           VALUES (?, 'SINV-2026-00004', ?, '2026-01-01', '2026-01-10',
                   '5000.00', '5000.00', 'cancelled', ?)""",
        (inv_id, cust_id, cid),
    )
    conn.commit()

    result = _call_action(ACTIONS["check-overdue"], conn, company_id=cid)
    assert result["status"] == "ok"
    assert result["overdue_count"] == 0
