"""Tests for early payment discount on submit-payment.

When a payment is allocated against an invoice whose customer/supplier has
payment terms with discount_percentage and discount_days, and the payment
is made within the discount window, the GL entries should reflect a reduced
bank amount and a debit to the Sales Discounts (or Purchase Discounts) account.
"""
import json
import uuid
from decimal import Decimal

from helpers import (
    _call_action,
    create_test_company,
    create_test_fiscal_year,
    create_test_account,
    create_test_payment_entry,
)

import db_query


def _setup_discount_scenario(conn, discount_pct="2", discount_days=10,
                              invoice_date="2026-06-01", payment_date="2026-06-05",
                              amount="10000.00"):
    """Set up a complete scenario for early payment discount testing.

    Returns dict with all IDs needed for testing.
    """
    company_id = create_test_company(conn)
    fy_id = create_test_fiscal_year(conn, company_id)
    bank_acct = create_test_account(conn, company_id, "Bank", "asset", "bank",
                                     balance_direction="debit_normal")
    ar_acct = create_test_account(conn, company_id, "Accounts Receivable", "asset",
                                   "receivable", balance_direction="debit_normal")
    disc_acct = create_test_account(conn, company_id, "Sales Discounts", "income",
                                     "revenue", balance_direction="credit_normal")
    revenue_acct = create_test_account(conn, company_id, "Revenue", "income",
                                        "revenue", balance_direction="credit_normal")

    # Create cost center (required for income/expense GL entries)
    cc_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO cost_center (id, name, company_id, is_group)
           VALUES (?, 'Main', ?, 0)""",
        (cc_id, company_id),
    )

    # Create payment terms with discount
    pt_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO payment_terms (id, name, due_days, discount_percentage, discount_days)
           VALUES (?, ?, 30, ?, ?)""",
        (pt_id, "2% 10 Net 30", discount_pct, discount_days),
    )

    # Create customer with payment terms
    cust_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO customer (id, name, customer_type, customer_group,
           payment_terms_id, company_id)
           VALUES (?, 'Acme Corp', 'company', 'Commercial', ?, ?)""",
        (cust_id, pt_id, company_id),
    )

    # Create a submitted sales invoice
    inv_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO sales_invoice
           (id, naming_series, customer_id, posting_date, due_date,
            total_amount, grand_total, outstanding_amount, status,
            payment_terms_id, company_id)
           VALUES (?, 'SINV-2026-00001', ?, ?, ?, ?, ?, ?, 'submitted', ?, ?)""",
        (inv_id, cust_id, invoice_date, "2026-07-01",
         amount, amount, amount, pt_id, company_id),
    )

    # Create draft payment with allocation against the invoice
    pe_id, naming = create_test_payment_entry(
        conn, company_id, payment_type="receive",
        posting_date=payment_date,
        party_type="customer", party_id=cust_id,
        paid_from_account=ar_acct, paid_to_account=bank_acct,
        paid_amount=amount,
    )
    # Add allocation
    alloc_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO payment_allocation
           (id, payment_entry_id, voucher_type, voucher_id, allocated_amount)
           VALUES (?, ?, 'sales_invoice', ?, ?)""",
        (alloc_id, pe_id, inv_id, amount),
    )
    conn.commit()

    return {
        "company_id": company_id,
        "pe_id": pe_id,
        "naming": naming,
        "inv_id": inv_id,
        "cust_id": cust_id,
        "bank_acct": bank_acct,
        "ar_acct": ar_acct,
        "disc_acct": disc_acct,
        "revenue_acct": revenue_acct,
        "pt_id": pt_id,
        "amount": amount,
    }


def test_discount_applied_within_window(fresh_db):
    """Payment within discount window should create 3 GL entries."""
    s = _setup_discount_scenario(fresh_db, discount_pct="2", discount_days=10,
                                  invoice_date="2026-06-01", payment_date="2026-06-05",
                                  amount="10000.00")
    result = _call_action(db_query.submit_payment, fresh_db,
                          payment_entry_id=s["pe_id"])
    assert result["status"] == "ok"
    assert "early_payment_discount" in result
    disc = result["early_payment_discount"]
    assert Decimal(disc["discount_amount"]) == Decimal("200.00")
    assert Decimal(disc["bank_amount"]) == Decimal("9800.00")

    # Verify GL: 3 entries (bank, discount, AR)
    gl_rows = fresh_db.execute(
        "SELECT * FROM gl_entry WHERE voucher_id = ? AND is_cancelled = 0",
        (s["pe_id"],),
    ).fetchall()
    assert len(gl_rows) == 3

    gl = {r["account_id"]: {"debit": r["debit"], "credit": r["credit"]}
          for r in gl_rows}

    # Bank: DR 9800
    assert Decimal(gl[s["bank_acct"]]["debit"]) == Decimal("9800.00")
    # Discount: DR 200
    assert Decimal(gl[s["disc_acct"]]["debit"]) == Decimal("200.00")
    # AR: CR 10000
    assert Decimal(gl[s["ar_acct"]]["credit"]) == Decimal("10000.00")


def test_no_discount_past_window(fresh_db):
    """Payment past discount window should NOT apply discount."""
    s = _setup_discount_scenario(fresh_db, discount_pct="2", discount_days=10,
                                  invoice_date="2026-06-01", payment_date="2026-06-20",
                                  amount="5000.00")
    result = _call_action(db_query.submit_payment, fresh_db,
                          payment_entry_id=s["pe_id"])
    assert result["status"] == "ok"
    assert "early_payment_discount" not in result

    # Verify GL: 2 entries (bank, AR) — no discount entry
    gl_rows = fresh_db.execute(
        "SELECT * FROM gl_entry WHERE voucher_id = ? AND is_cancelled = 0",
        (s["pe_id"],),
    ).fetchall()
    assert len(gl_rows) == 2


def test_no_discount_no_terms(fresh_db):
    """Invoice without payment terms should get no discount."""
    company_id = create_test_company(fresh_db)
    create_test_fiscal_year(fresh_db, company_id)
    bank_acct = create_test_account(fresh_db, company_id, "Bank", "asset", "bank",
                                     balance_direction="debit_normal")
    ar_acct = create_test_account(fresh_db, company_id, "Accounts Receivable", "asset",
                                   "receivable", balance_direction="debit_normal")

    cust_id = str(uuid.uuid4())
    fresh_db.execute(
        """INSERT INTO customer (id, name, customer_type, customer_group, company_id)
           VALUES (?, 'No Terms Corp', 'company', 'Commercial', ?)""",
        (cust_id, company_id),
    )

    inv_id = str(uuid.uuid4())
    fresh_db.execute(
        """INSERT INTO sales_invoice
           (id, naming_series, customer_id, posting_date, due_date,
            total_amount, grand_total, outstanding_amount, status, company_id)
           VALUES (?, 'SINV-2026-00002', ?, '2026-06-01', '2026-07-01',
                   '2000.00', '2000.00', '2000.00', 'submitted', ?)""",
        (inv_id, cust_id, company_id),
    )

    pe_id, _ = create_test_payment_entry(
        fresh_db, company_id, payment_type="receive",
        posting_date="2026-06-03",
        party_type="customer", party_id=cust_id,
        paid_from_account=ar_acct, paid_to_account=bank_acct,
        paid_amount="2000.00",
    )
    alloc_id = str(uuid.uuid4())
    fresh_db.execute(
        """INSERT INTO payment_allocation
           (id, payment_entry_id, voucher_type, voucher_id, allocated_amount)
           VALUES (?, ?, 'sales_invoice', ?, '2000.00')""",
        (alloc_id, pe_id, inv_id),
    )
    fresh_db.commit()

    result = _call_action(db_query.submit_payment, fresh_db,
                          payment_entry_id=pe_id)
    assert result["status"] == "ok"
    assert "early_payment_discount" not in result
    gl_rows = fresh_db.execute(
        "SELECT * FROM gl_entry WHERE voucher_id = ? AND is_cancelled = 0",
        (pe_id,),
    ).fetchall()
    assert len(gl_rows) == 2


def test_discount_exact_boundary(fresh_db):
    """Payment on exact last day of discount window should still get discount."""
    s = _setup_discount_scenario(fresh_db, discount_pct="3", discount_days=10,
                                  invoice_date="2026-06-01", payment_date="2026-06-11",
                                  amount="8000.00")
    result = _call_action(db_query.submit_payment, fresh_db,
                          payment_entry_id=s["pe_id"])
    assert result["status"] == "ok"
    assert "early_payment_discount" in result
    disc = result["early_payment_discount"]
    # 3% of 8000 = 240
    assert Decimal(disc["discount_amount"]) == Decimal("240.00")


def test_discount_gl_balanced(fresh_db):
    """GL entries with discount must still balance (total DR = total CR)."""
    s = _setup_discount_scenario(fresh_db, discount_pct="5", discount_days=15,
                                  invoice_date="2026-06-01", payment_date="2026-06-10",
                                  amount="20000.00")
    result = _call_action(db_query.submit_payment, fresh_db,
                          payment_entry_id=s["pe_id"])
    assert result["status"] == "ok"

    gl_rows = fresh_db.execute(
        "SELECT debit, credit FROM gl_entry WHERE voucher_id = ? AND is_cancelled = 0",
        (s["pe_id"],),
    ).fetchall()

    total_dr = sum(Decimal(r["debit"]) for r in gl_rows)
    total_cr = sum(Decimal(r["credit"]) for r in gl_rows)
    assert total_dr == total_cr
    # DR = 19000 bank + 1000 discount = 20000, CR = 20000 AR
    assert total_dr == Decimal("20000.00")


def test_no_discount_without_allocation(fresh_db):
    """Payment without allocations should have no discount."""
    company_id = create_test_company(fresh_db)
    create_test_fiscal_year(fresh_db, company_id)
    bank_acct = create_test_account(fresh_db, company_id, "Bank", "asset", "bank",
                                     balance_direction="debit_normal")
    ar_acct = create_test_account(fresh_db, company_id, "Accounts Receivable", "asset",
                                   "receivable", balance_direction="debit_normal")

    cust_id = str(uuid.uuid4())
    fresh_db.execute(
        """INSERT INTO customer (id, name, customer_type, customer_group, company_id)
           VALUES (?, 'Unlinked Corp', 'company', 'Commercial', ?)""",
        (cust_id, company_id),
    )

    pe_id, _ = create_test_payment_entry(
        fresh_db, company_id, payment_type="receive",
        posting_date="2026-06-05",
        party_type="customer", party_id=cust_id,
        paid_from_account=ar_acct, paid_to_account=bank_acct,
        paid_amount="3000.00",
    )
    fresh_db.commit()

    result = _call_action(db_query.submit_payment, fresh_db,
                          payment_entry_id=pe_id)
    assert result["status"] == "ok"
    assert "early_payment_discount" not in result
