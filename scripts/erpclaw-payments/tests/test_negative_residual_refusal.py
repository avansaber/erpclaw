"""A payment may never consume more than it pays, with or without deductions.

paid_amount = allocations + deductions + unallocated, and unallocated may be
zero but never negative. add-payment and submit-payment used to check that only
when a deduction was present, and update-payment only when --paid-amount moved,
so an over-allocated payment with no deduction was accepted and posted.
"""
import json
from decimal import Decimal

import pytest
from payments_helpers import (build_ar_env, call_action, is_error, is_ok,
                              load_db_query, ns, seed_sales_invoice)

pay = load_db_query()

D = Decimal
IDENTITY = "paid_amount = allocations + deductions + unallocated"


def _msg(result):
    return result.get("message", "") + result.get("error", "")


@pytest.fixture
def env(conn):
    e = build_ar_env(conn)
    conn.commit()
    return e


def _add(conn, env, paid, allocations, reference="WIRE-OVER"):
    return call_action(pay.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="receive",
        posting_date="2026-06-01", party_type="customer",
        party_id=env["customer"], paid_from_account=env["ar"],
        paid_to_account=env["bank"], paid_amount=str(paid),
        exchange_rate=None, payment_currency=None,
        reference_number=reference, reference_date=None,
        allocations=json.dumps(allocations), deductions=None))


def _alloc(si, amount):
    return [{"voucher_type": "sales_invoice", "voucher_id": si,
             "allocated_amount": amount}]


def test_add_payment_refuses_allocations_above_paid_without_deductions(conn, env):
    si = seed_sales_invoice(conn, env, "1000.00")
    conn.commit()
    r = _add(conn, env, "100.00", _alloc(si, "300.00"))
    assert is_error(r), r
    assert IDENTITY in _msg(r)
    assert conn.execute("SELECT COUNT(*) FROM payment_entry WHERE reference_number = ?",
                        ("WIRE-OVER",)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM payment_allocation").fetchone()[0] == 0


def test_add_payment_allows_allocations_equal_to_paid(conn, env):
    si = seed_sales_invoice(conn, env, "1000.00")
    conn.commit()
    r = _add(conn, env, "300.00", _alloc(si, "300.00"))
    assert is_ok(r), r
    row = conn.execute("SELECT unallocated_amount FROM payment_entry WHERE id = ?",
                       (r["payment_entry_id"],)).fetchone()
    assert D(row["unallocated_amount"]) == D("0")


def test_update_payment_refuses_allocations_alone_above_paid(conn, env):
    si = seed_sales_invoice(conn, env, "1000.00")
    conn.commit()
    created = _add(conn, env, "1000.00", _alloc(si, "300.00"))
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    r = call_action(pay.update_payment, conn, ns(
        payment_entry_id=pe_id, paid_amount=None, reference_number=None,
        allocations=json.dumps(_alloc(si, "1200.00"))))
    assert is_error(r), r
    assert IDENTITY in _msg(r)
    rows = conn.execute("SELECT allocated_amount FROM payment_allocation "
                        "WHERE payment_entry_id = ?", (pe_id,)).fetchall()
    assert [D(x["allocated_amount"]) for x in rows] == [D("300.00")]
    pe = conn.execute("SELECT unallocated_amount FROM payment_entry WHERE id = ?",
                      (pe_id,)).fetchone()
    assert D(pe["unallocated_amount"]) == D("700.00")


def test_submit_payment_refuses_an_over_allocated_draft_and_posts_nothing(conn, env):
    si = seed_sales_invoice(conn, env, "1000.00")
    conn.commit()
    created = _add(conn, env, "100.00", _alloc(si, "100.00"))
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    # A draft that reached this state by a path that skipped add-payment's check.
    conn.execute("UPDATE payment_allocation SET allocated_amount = '300.00' "
                 "WHERE payment_entry_id = ?", (pe_id,))
    conn.commit()
    r = call_action(pay.submit_payment, conn, ns(payment_entry_id=pe_id))
    assert is_error(r), r
    assert "exceed paid amount" in _msg(r)
    assert conn.execute("SELECT COUNT(*) FROM gl_entry WHERE voucher_id = ?",
                        (pe_id,)).fetchone()[0] == 0
    assert conn.execute("SELECT status FROM payment_entry WHERE id = ?",
                        (pe_id,)).fetchone()["status"] == "draft"
