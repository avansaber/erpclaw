"""A payment clears an invoice only when both are in the same currency."""
import json

import pytest

from payments_helpers import (build_ar_env, call_action, is_error, is_ok,
                              load_db_query, ns, seed_sales_invoice)

mod = load_db_query()


@pytest.fixture
def env(conn):
    return build_ar_env(conn)


def _receive(conn, env, amount, allocations=None, submit=True,
             currency=None, posting_date="2026-06-01"):
    created = call_action(mod.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="receive",
        posting_date=posting_date, party_type="customer",
        party_id=env["customer"], paid_from_account=env["ar"],
        paid_to_account=env["bank"], paid_amount=str(amount),
        exchange_rate=None, payment_currency=currency,
        reference_number=None, reference_date=None,
        allocations=json.dumps(allocations) if allocations else None,
        deductions=None))
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    if submit:
        s = call_action(mod.submit_payment, conn, ns(payment_entry_id=pe_id))
        assert is_ok(s), s
    return pe_id


def _count(conn, table):
    return conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]


def test_allocate_refuses_a_payment_in_another_currency(conn, env):
    pe = _receive(conn, env, "500.00", currency="EUR")
    si = seed_sales_invoice(conn, env, "1000.00")
    conn.commit()
    pa_before = _count(conn, "payment_allocation")
    gl_before = _count(conn, "gl_entry")
    ple_before = _count(conn, "payment_ledger_entry")
    r = call_action(mod.allocate_payment, conn, ns(
        payment_entry_id=pe, voucher_type="sales_invoice", voucher_id=si,
        allocated_amount="200.00"))
    assert is_error(r), r
    assert r["message"] == ("currency mismatch: invoice in USD, payment in EUR; "
                            "invoice currency must equal payment currency")
    assert _count(conn, "payment_allocation") == pa_before
    assert _count(conn, "gl_entry") == gl_before
    assert _count(conn, "payment_ledger_entry") == ple_before
    row = conn.execute(
        "SELECT outstanding_amount FROM sales_invoice WHERE id = ?",
        (si,)).fetchone()
    assert row["outstanding_amount"] == "1000.00"
    prow = conn.execute(
        "SELECT unallocated_amount FROM payment_entry WHERE id = ?",
        (pe,)).fetchone()
    assert prow["unallocated_amount"] == "500.00"


def test_allocate_in_the_same_currency_is_unchanged(conn, env):
    pe = _receive(conn, env, "500.00", currency="EUR")
    si = seed_sales_invoice(conn, env, "1000.00")
    conn.execute("UPDATE sales_invoice SET currency = ? WHERE id = ?",
                 ("EUR", si))
    conn.commit()
    r = call_action(mod.allocate_payment, conn, ns(
        payment_entry_id=pe, voucher_type="sales_invoice", voucher_id=si,
        allocated_amount="200.00"))
    assert is_ok(r), r
    assert r["remaining_unallocated"] == "300.00"
    row = conn.execute(
        "SELECT outstanding_amount FROM sales_invoice WHERE id = ?",
        (si,)).fetchone()
    assert row["outstanding_amount"] == "800.00"


def test_reconcile_skips_a_currency_mismatched_pair(conn, env):
    pe = _receive(conn, env, "300.00", currency="EUR")
    si = seed_sales_invoice(conn, env, "1000.00")
    conn.commit()
    r = call_action(mod.reconcile_payments, conn, ns(
        party_type="customer", party_id=env["customer"],
        company_id=env["company_id"]))
    assert is_ok(r), r
    assert r["matched"] == []
    assert r["unmatched_payments"] == 1
    assert r["unmatched_invoices"] == 1
    row = conn.execute(
        "SELECT outstanding_amount FROM sales_invoice WHERE id = ?",
        (si,)).fetchone()
    assert row["outstanding_amount"] == "1000.00"


def test_reconcile_matches_within_each_currency(conn, env):
    pe_eur = _receive(conn, env, "300.00", currency="EUR",
                      posting_date="2026-06-01")
    pe_usd = _receive(conn, env, "200.00", currency="USD",
                      posting_date="2026-06-02")
    si_usd = seed_sales_invoice(conn, env, "1000.00")
    conn.execute(
        "UPDATE sales_invoice SET currency = ?, posting_date = ? WHERE id = ?",
        ("USD", "2026-06-01", si_usd))
    si_eur = seed_sales_invoice(conn, env, "500.00")
    conn.execute(
        "UPDATE sales_invoice SET currency = ?, posting_date = ? WHERE id = ?",
        ("EUR", "2026-06-02", si_eur))
    conn.commit()
    r = call_action(mod.reconcile_payments, conn, ns(
        party_type="customer", party_id=env["customer"],
        company_id=env["company_id"]))
    assert is_ok(r), r
    assert r["matched"] == [
        {"payment_id": pe_eur, "voucher_id": si_eur,
         "allocated_amount": "300.00"},
        {"payment_id": pe_usd, "voucher_id": si_usd,
         "allocated_amount": "200.00"},
    ]
    assert r["unmatched_payments"] == 0
    assert r["unmatched_invoices"] == 2
    eur_row = conn.execute(
        "SELECT outstanding_amount FROM sales_invoice WHERE id = ?",
        (si_eur,)).fetchone()
    usd_row = conn.execute(
        "SELECT outstanding_amount FROM sales_invoice WHERE id = ?",
        (si_usd,)).fetchone()
    assert eur_row["outstanding_amount"] == "200.00"
    assert usd_row["outstanding_amount"] == "800.00"
