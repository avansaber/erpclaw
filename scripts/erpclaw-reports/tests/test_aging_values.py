"""Part A — VALUE tests for the AR/AP aging + outstanding readers.

Wave G F2 (M38) and F21. Before this file, ``ar-aging``'s only test asserted
that the action does not 404 (testing/integration/contract/
test_erpclaw_contract.py) — the necessity register's ``"tested": true`` meant
"has some test", not "has a value test", and that is precisely why a party-level
double count shipped and stayed invisible for months. Every pin here asserts an
exact Decimal produced by the REAL action against a fresh core DB.

What changed under Wave G F2, and is pinned below:

  - LIVENESS. Both aging queries used to filter a flat ``delinked = 0``, which
    drops a payment's delinked original while keeping its active cancel mirror.
    After a ``cancel-payment`` the report read 1,600.00 where the customer owed
    1,000.00. Payment rows are now netted reversal-inclusive.
  - THE PARTY DOUBLE COUNT. A 1,000.00 invoice paid 300.00 aged 400.00 where the
    truth is 700.00. Fixed in the ledger itself by the residual compensation.
  - ATTRIBUTION. ``get-outstanding`` grouped on each row's OWN voucher, so a
    payment's allocations collided into the payment's bucket instead of reducing
    the invoice they were applied to.

All three now come from one shared definition, ``erpclaw_lib.party_ledger``
(ADR-0032 Decision 2), which testing/unit/L0/test_party_ledger_predicate_sync.py
guards against drift.
"""
import importlib.util
import json
import os
from decimal import Decimal

import pytest

from payments_helpers import (build_ap_env, build_ar_env, call_action, is_ok,
                              ns, seed_purchase_invoice, seed_sales_invoice)

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_MODULE_DIR = os.path.dirname(_TESTS_DIR)
_SCRIPTS_DIR = os.path.dirname(_MODULE_DIR)

D = Decimal


def _load(name, rel_path):
    path = os.path.join(_SCRIPTS_DIR, rel_path)
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


rep = _load("db_query_reports_f2", "erpclaw-reports/db_query.py")
pay = _load("db_query_payments_f2", "erpclaw-payments/db_query.py")

AS_OF = "2026-12-31"


@pytest.fixture
def env(conn):
    return build_ar_env(conn)


@pytest.fixture
def apenv(conn):
    return build_ap_env(conn)


def _aging(conn, env, action="ar_aging"):
    r = call_action(getattr(rep, action), conn, ns(
        company_id=env["company_id"], company_name=None, as_of_date=AS_OF,
        aging_buckets=None))
    assert is_ok(r), r
    return r


def _party_row(report, key, party_id):
    rows = [p for p in report[f"{key}s"] if p[f"{key}_id"] == party_id]
    assert len(rows) == 1, f"expected exactly one {key} row, got {rows}"
    return rows[0]


def _outstanding(conn, party_type, party_id):
    r = call_action(pay.get_outstanding, conn, ns(
        party_type=party_type, party_id=party_id,
        voucher_type=None, voucher_id=None))
    assert is_ok(r), r
    return r


def _receive(conn, env, amount, allocations=None, submit=True):
    created = call_action(pay.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="receive",
        posting_date="2026-06-01", party_type="customer",
        party_id=env["customer"], paid_from_account=env["ar"],
        paid_to_account=env["bank"], paid_amount=str(amount),
        exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None,
        allocations=json.dumps(allocations) if allocations else None,
        deductions=None))
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    if submit:
        assert is_ok(call_action(pay.submit_payment, conn,
                                 ns(payment_entry_id=pe_id)))
    return pe_id


# ── pin 1 — an open invoice, no payment ──────────────────────────────────────

def test_pin1_open_invoice_ages_its_full_amount(conn, env):
    si = seed_sales_invoice(conn, env, "1000.00")

    report = _aging(conn, env)
    assert D(report["total_outstanding"]) == D("1000.00")
    assert D(_party_row(report, "customer", env["customer"])["total"]) == D("1000.00")

    out = _outstanding(conn, "customer", env["customer"])
    assert D(out["outstanding"]) == D("1000.00")
    assert [(v["voucher_type"], v["voucher_id"], D(v["outstanding_amount"]))
            for v in out["vouchers"]] == [("sales_invoice", si, D("1000.00"))]


# ── pin 2 — the M38 case, through the readers ────────────────────────────────

def test_pin2_invoice_with_a_partial_payment_ages_700(conn, env, capsys):
    """1,000.00 invoice, 300.00 received and fully allocated.

    ar-aging read **400.00** before Wave G F2 (the party-level row and the
    per-allocation row both subtracted the same 300.00). The truth is 700.00,
    which is also what the invoice's own outstanding column says — the two
    sources of truth F18 says must agree.
    """
    si = seed_sales_invoice(conn, env, "1000.00")
    _receive(conn, env, "300.00", allocations=[
        {"voucher_type": "sales_invoice", "voucher_id": si,
         "allocated_amount": "300.00"}])

    report = _aging(conn, env)
    aged = D(_party_row(report, "customer", env["customer"])["total"])

    # The pre-fix reading, MEASURED on this same ledger by excluding the
    # compensation rows the fix appends — not quoted from a plan.
    # COALESCE is required: the complement of the compensation discriminator is
    # not its bare negation, because the party-level row's against_voucher_* is
    # NULL and `NOT (NULL = ...)` is NULL, which would silently drop that row and
    # hand back 700.00 — the post-fix answer wearing the pre-fix label.
    pre_fix = D(conn.execute(
        "SELECT decimal_sum(amount) AS n FROM payment_ledger_entry "
        "WHERE party_type = 'customer' AND party_id = ? "
        "  AND (voucher_type = 'payment_entry' OR delinked = 0) "
        "  AND NOT (voucher_type = 'payment_entry' "
        "           AND COALESCE(against_voucher_type, '') = 'payment_entry' "
        "           AND COALESCE(against_voucher_id, '') = voucher_id)",
        (env["customer"],)).fetchone()["n"])
    print(f"\nar-aging: pre-fix {pre_fix} vs post-fix {aged}")

    assert pre_fix == D("400.00")
    assert aged == D("700.00")
    assert D(report["total_outstanding"]) == D("700.00")
    assert D(conn.execute("SELECT outstanding_amount FROM sales_invoice WHERE id=?",
                          (si,)).fetchone()[0]) == D("700.00")


# ── pin 6 — a payment cancelled after a full allocation ──────────────────────

def test_pin6_cancelled_payment_ages_exactly_1000(conn, env):
    """SIM correction B1's required pin.

    1,000.00 invoice, paid in full, payment then cancelled: the customer owes
    1,000.00 again. The shipped reader said 1,600.00 (both cancel mirrors
    counted while both delinked originals dropped out). Under the canonical
    reversal-inclusive rule it says 1,000.00.
    """
    si = seed_sales_invoice(conn, env, "1000.00")
    pe = _receive(conn, env, "1000.00", allocations=[
        {"voucher_type": "sales_invoice", "voucher_id": si,
         "allocated_amount": "1000.00"}])
    # A fully settled customer nets to zero and drops out of the report entirely
    # (the HAVING clause filters ±0.005). Absence IS the "owes nothing" answer.
    settled = _aging(conn, env)
    assert settled["customers"] == []
    assert D(settled["total_outstanding"]) == D("0")

    assert is_ok(call_action(pay.cancel_payment, conn, ns(payment_entry_id=pe)))

    report = _aging(conn, env)
    assert D(_party_row(report, "customer", env["customer"])["total"]) == D("1000.00")
    assert D(conn.execute("SELECT outstanding_amount FROM sales_invoice WHERE id=?",
                          (si,)).fetchone()[0]) == D("1000.00")

    out = _outstanding(conn, "customer", env["customer"])
    assert D(out["outstanding"]) == D("1000.00")
    assert [(v["voucher_type"], v["voucher_id"], D(v["outstanding_amount"]))
            for v in out["vouchers"]] == [("sales_invoice", si, D("1000.00"))]


# ── the AP side (ap-aging had no value test either — F21) ────────────────────

def test_ap_aging_values(conn, apenv):
    pi = seed_purchase_invoice(conn, apenv, "800.00")
    report = call_action(rep.ap_aging, conn, ns(
        company_id=apenv["company_id"], company_name=None, as_of_date=AS_OF,
        aging_buckets=None))
    assert is_ok(report), report
    assert D(_party_row(report, "supplier", apenv["supplier"])["total"]) == D("800.00")

    created = call_action(pay.add_payment, conn, ns(
        company_id=apenv["company_id"], payment_type="pay",
        posting_date="2026-06-01", party_type="supplier",
        party_id=apenv["supplier"], paid_from_account=apenv["bank"],
        paid_to_account=apenv["ap"], paid_amount="300.00", exchange_rate=None,
        payment_currency=None, reference_number=None, reference_date=None,
        allocations=json.dumps([{"voucher_type": "purchase_invoice",
                                 "voucher_id": pi,
                                 "allocated_amount": "300.00"}]),
        deductions=None))
    assert is_ok(created), created
    assert is_ok(call_action(pay.submit_payment, conn,
                             ns(payment_entry_id=created["payment_entry_id"])))

    report = call_action(rep.ap_aging, conn, ns(
        company_id=apenv["company_id"], company_name=None, as_of_date=AS_OF,
        aging_buckets=None))
    assert is_ok(report), report
    assert D(_party_row(report, "supplier", apenv["supplier"])["total"]) == D("500.00")


# ── aging bucket placement still works on the corrected row set ──────────────

def test_aging_buckets_place_rows_by_posting_date(conn, env):
    """The liveness change must not disturb bucket placement: same shape, right
    values. The invoice posts 2026-06-01, so at 2026-06-15 it is current."""
    seed_sales_invoice(conn, env, "1000.00")
    r = call_action(rep.ar_aging, conn, ns(
        company_id=env["company_id"], company_name=None,
        as_of_date="2026-06-15", aging_buckets="30,60,90,120"))
    assert is_ok(r), r
    row = _party_row(r, "customer", env["customer"])
    assert D(row["current"]) == D("1000.00")
    assert D(row["days_60"]) == D("0")
    assert D(row["days_120_plus"]) == D("0")
    assert D(row["total"]) == D("1000.00")


# ── F18 — the two sources of truth must agree ────────────────────────────────

def test_f18_check_overdue_and_get_outstanding_agree_per_invoice(conn, env):
    """F18 (ADR-0032 Decision 2). ERPClaw has exactly TWO sources of truth for
    what is owed: the invoice's own ``outstanding_amount`` (bound by INV-25,
    which ``check-overdue`` reads) and the party payment-ledger net (bound by
    INV-27, which ``get-outstanding`` reads). For the same invoice set they must
    give the same answer, invoice by invoice — that equality is the ruling.
    """
    si_a = seed_sales_invoice(conn, env, "1000.00")
    si_b = seed_sales_invoice(conn, env, "400.00")
    conn.execute("UPDATE sales_invoice SET due_date = '2026-01-31' "
                 "WHERE id IN (?, ?)", (si_a, si_b))
    conn.commit()
    _receive(conn, env, "300.00", allocations=[
        {"voucher_type": "sales_invoice", "voucher_id": si_a,
         "allocated_amount": "300.00"}])

    overdue = call_action(rep.check_overdue, conn, ns(
        company_id=env["company_id"], company_name=None))
    assert is_ok(overdue), overdue
    per_invoice_doc = {i["id"]: D(i["outstanding"])
                       for i in overdue["invoices"]}

    out = _outstanding(conn, "customer", env["customer"])
    per_invoice_ledger = {v["voucher_id"]: D(v["outstanding_amount"])
                          for v in out["vouchers"]
                          if v["voucher_type"] == "sales_invoice"}

    assert per_invoice_doc == {si_a: D("700.00"), si_b: D("400.00")}
    assert per_invoice_ledger == per_invoice_doc, (
        "the document column and the party ledger disagree per invoice — F18's "
        "two truths have diverged")
    assert D(overdue["total_overdue"]) == D(out["outstanding"]) == D("1100.00")
