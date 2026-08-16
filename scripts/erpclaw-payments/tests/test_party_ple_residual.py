"""Part A — M38/F2: the party-level residual compensation (Fork A, ADR-0032).

``submit-payment`` writes ONE full-amount party-level ledger row per submit AND
a per-allocation row per allocation, so the same cash was subtracted from the
party twice. A customer with a 1,000.00 invoice who paid 300.00 read 400.00
where the truth is 700.00. Fork A (ruling N1) corrects the LEDGER rather than
masking it in each reader: a compensating, append-only row per payment, computed
from allocation and deduction DETAIL.

Every pin here drives the REAL actions against a fresh core DB and asserts exact
Decimals. The party-ledger reading is the canonical one
(``erpclaw_lib.party_ledger``) — payment rows reversal-inclusive, document rows
``delinked = 0``.

The pre-fix figure is MEASURED, not quoted: ``_party_net(..., with_compensation=
False)`` re-reads the same seeded ledger with the compensation rows excluded,
which is exactly the shipped pre-F2 ledger. Pin 2 prints both.

Aging/report-side values live in
source/erpclaw/scripts/erpclaw-reports/tests/test_aging_values.py; the invariant
and its negative controls live in
testing/unit/constitution/test_inv27_party_residual.py.
"""
import importlib.util
import json
import os
import sys
import uuid
from decimal import Decimal

import pytest

from payments_helpers import (build_ap_env, build_ar_env, call_action, is_error,
                              is_ok, load_db_query, ns, seed_purchase_invoice,
                              seed_sales_invoice)

mod = load_db_query()

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(os.path.dirname(_TESTS_DIR))  # scripts/

D = Decimal


def _find_repo_root(start):
    cur = os.path.abspath(start)
    while True:
        if os.path.exists(os.path.join(cur, "CLAUDE.md")) or \
                os.path.isdir(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            raise RuntimeError(f"repo root not found from {start}")
        cur = parent


try:
    _INV_PATH = os.path.join(_find_repo_root(_TESTS_DIR), "testing",
                             "invariant_engine.py")
except RuntimeError:
    _INV_PATH = ""
if _INV_PATH and os.path.exists(_INV_PATH):
    _spec = importlib.util.spec_from_file_location("invariant_engine_f2", _INV_PATH)
    inv_engine = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(inv_engine)
else:
    inv_engine = None


# ── readers ──────────────────────────────────────────────────────────────────

def _party_net(conn, party_type, party_id, with_compensation=True):
    """Party net under the canonical liveness rule.

    ``with_compensation=False`` re-reads the SAME ledger with the Fork-A
    compensation rows excluded — i.e. exactly what the shipped pre-F2 code
    produced. That is how the pre-fix figure gets measured instead of quoted.
    """
    net = D("0")
    for vt, vid, avt, avid, amount, delinked in conn.execute(
            "SELECT voucher_type, voucher_id, against_voucher_type, "
            "against_voucher_id, amount, delinked FROM payment_ledger_entry "
            "WHERE party_type = ? AND party_id = ?", (party_type, party_id)):
        if not (vt == "payment_entry" or delinked == 0):
            continue
        is_comp = (vt == "payment_entry" and avt == "payment_entry" and avid == vid)
        if is_comp and not with_compensation:
            continue
        net += D(amount)
    return net


def _party_gl_net(conn, party_type, party_id, account_id):
    """Σ(debit − credit) over ALL gl_entry rows for a party on one account."""
    net = D("0")
    for debit, credit in conn.execute(
            "SELECT debit, credit FROM gl_entry "
            "WHERE party_type = ? AND party_id = ? AND account_id = ?",
            (party_type, party_id, account_id)):
        net += D(debit) - D(credit)
    return net


def _comp_rows(conn, pe_id):
    """Every compensation row for a payment.

    Ordered by AMOUNT, not created_at: created_at has second resolution and two
    compensations written in the same operation land in the same second, so
    insertion order is not recoverable (the same reason the F1 suite orders its
    release pair this way).
    """
    return conn.execute(
        "SELECT amount, delinked FROM payment_ledger_entry "
        "WHERE voucher_type = 'payment_entry' AND voucher_id = ? "
        "  AND against_voucher_type = 'payment_entry' AND against_voucher_id = ? "
        "ORDER BY CAST(amount AS NUMERIC), id", (pe_id, pe_id)).fetchall()


def _ple_count(conn):
    return conn.execute("SELECT COUNT(*) FROM payment_ledger_entry").fetchone()[0]


def _unallocated(conn, pe_id):
    return D(conn.execute(
        "SELECT unallocated_amount FROM payment_entry WHERE id = ?",
        (pe_id,)).fetchone()[0])


def _inv(conn, name):
    if inv_engine is None:
        pytest.skip("invariant_engine harness not present (published skill tree)")
    inv_engine._ensure_decimal_sum(conn)
    return getattr(inv_engine, name)(conn)


def _inv27(conn):
    return _inv(conn, "_check_inv27_party_level_residual")


# ── fixtures driven through the real actions ─────────────────────────────────

def _receive(conn, env, amount, allocations=None, deductions=None, submit=True,
             currency=None):
    created = call_action(mod.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="receive",
        posting_date="2026-06-01", party_type="customer",
        party_id=env["customer"], paid_from_account=env["ar"],
        paid_to_account=env["bank"], paid_amount=str(amount),
        exchange_rate=None, payment_currency=currency,
        reference_number=None, reference_date=None,
        allocations=json.dumps(allocations) if allocations else None,
        deductions=json.dumps(deductions) if deductions else None))
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    if submit:
        s = call_action(mod.submit_payment, conn, ns(payment_entry_id=pe_id))
        assert is_ok(s), s
    return pe_id


def _pay(conn, env, amount, allocations=None, submit=True):
    created = call_action(mod.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="pay",
        posting_date="2026-06-01", party_type="supplier",
        party_id=env["supplier"], paid_from_account=env["bank"],
        paid_to_account=env["ap"], paid_amount=str(amount),
        exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None,
        allocations=json.dumps(allocations) if allocations else None,
        deductions=None))
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    if submit:
        s = call_action(mod.submit_payment, conn, ns(payment_entry_id=pe_id))
        assert is_ok(s), s
    return pe_id


@pytest.fixture
def env(conn):
    return build_ar_env(conn)


@pytest.fixture
def apenv(conn):
    return build_ap_env(conn)


# ── pin 2 — the M38 case, pre-fix MEASURED and post-fix pinned ───────────────

def test_pin2_party_double_count_is_compensated(conn, env, capsys):
    """1,000.00 invoice + 300.00 fully allocated.

    Pre-fix the party read 400.00 (+1000 −300 −300); the truth is 700.00, which
    is what the invoice's own outstanding column says and what the AR control
    account nets to. Both figures are printed.
    """
    si = seed_sales_invoice(conn, env, "1000.00")
    pe = _receive(conn, env, "300.00", allocations=[
        {"voucher_type": "sales_invoice", "voucher_id": si,
         "allocated_amount": "300.00"}])

    pre = _party_net(conn, "customer", env["customer"], with_compensation=False)
    post = _party_net(conn, "customer", env["customer"])
    print(f"\npin 2 — party ledger pre-fix {pre} vs post-fix {post}")
    assert pre == D("400.00"), "the pre-fix reading is the M38 defect"
    assert post == D("700.00")

    assert D(conn.execute("SELECT outstanding_amount FROM sales_invoice WHERE id=?",
                          (si,)).fetchone()[0]) == D("700.00")
    # party PLE net == party GL net is pinned where the invoice's GL actually
    # exists: source/erpclaw/scripts/erpclaw-selling/tests/
    # test_cancel_invoice_allocation_release.py pins 1/9/10, which drive the real
    # create+submit_sales_invoice and now read 700.00 == 700.00 (they recorded
    # 400.00 vs 700.00 while M38 was open). This suite's seed helper writes the
    # invoice row and its ledger row but posts no invoice GL, so a GL comparison
    # here would measure the fixture, not the fix.

    comps = _comp_rows(conn, pe)
    assert len(comps) == 1
    assert D(comps[0]["amount"]) == D("300.00")
    assert comps[0]["delinked"] == 0
    assert _inv27(conn) is None


def test_pin2_ap_side(conn, apenv):
    """The same on the AP side: a 1,000.00 bill, 300.00 paid."""
    pi = seed_purchase_invoice(conn, apenv, "1000.00")
    _pay(conn, apenv, "300.00", allocations=[
        {"voucher_type": "purchase_invoice", "voucher_id": pi,
         "allocated_amount": "300.00"}])

    assert _party_net(conn, "supplier", apenv["supplier"],
                      with_compensation=False) == D("400.00")
    assert _party_net(conn, "supplier", apenv["supplier"]) == D("700.00")
    assert _inv27(conn) is None


# ── pin 3 — an advance writes NO compensation (delta 0, correction C5) ───────

def test_pin3_unallocated_advance_writes_no_compensation_row(conn, env):
    pe = _receive(conn, env, "1000.00")
    assert _comp_rows(conn, pe) == []
    assert _unallocated(conn, pe) == D("1000.00")
    assert _party_net(conn, "customer", env["customer"]) == D("-1000.00")
    assert _inv27(conn) is None


# ── pin 4 — the advance applied via allocate-payment ─────────────────────────

def test_pin4_allocate_payment_site(conn, env):
    si = seed_sales_invoice(conn, env, "1000.00")
    pe = _receive(conn, env, "1000.00")
    r = call_action(mod.allocate_payment, conn, ns(
        payment_entry_id=pe, voucher_type="sales_invoice", voucher_id=si,
        allocated_amount="1000.00"))
    assert is_ok(r), r

    assert _unallocated(conn, pe) == D("0.00")
    assert D(_comp_rows(conn, pe)[0]["amount"]) == D("1000.00")
    assert _party_net(conn, "customer", env["customer"]) == D("0")
    assert _inv27(conn) is None


# ── pin 5 — the advance applied via reconcile-payments ───────────────────────

def test_pin5_reconcile_payments_site(conn, env):
    si = seed_sales_invoice(conn, env, "1000.00")
    pe = _receive(conn, env, "1000.00")
    r = call_action(mod.reconcile_payments, conn, ns(
        party_type="customer", party_id=env["customer"],
        company_id=env["company_id"]))
    assert is_ok(r), r
    assert len(r["matched"]) == 1

    assert _unallocated(conn, pe) == D("0.00")
    assert D(_comp_rows(conn, pe)[0]["amount"]) == D("1000.00")
    assert _party_net(conn, "customer", env["customer"]) == D("0")
    assert D(conn.execute("SELECT outstanding_amount FROM sales_invoice WHERE id=?",
                          (si,)).fetchone()[0]) == D("0")
    assert _inv27(conn) is None


def test_pin5b_reconcile_writes_nothing_for_an_untouched_payment(conn, env):
    """The reconcile loop runs the helper over EVERY payment in its list; the
    ones it did not match must produce delta 0 and write nothing."""
    pe = _receive(conn, env, "500.00")          # no invoice to match against
    before = _ple_count(conn)
    r = call_action(mod.reconcile_payments, conn, ns(
        party_type="customer", party_id=env["customer"],
        company_id=env["company_id"]))
    assert is_ok(r), r
    assert r["matched"] == []
    assert _ple_count(conn) == before, "a zero-delta compensation row was written"
    assert _comp_rows(conn, pe) == []
    assert _inv27(conn) is None


# ── pin 8 — a deduction-carrying payment with zero allocations ───────────────

def test_pin8_deduction_only_payment(conn, env):
    """paid 1,000.00 with a 250.00 discount deduction and no allocation.

    The party bucket must be −(paid − deduction) = −750.00: the deduction is
    cash the customer never has to pay, so it does not sit in the residual.
    The compensation is driven by the DEDUCTION table alone here — the exact
    flow a compensation hosted inside _post_allocation_ple would have missed.
    """
    pe = _receive(conn, env, "1000.00", deductions=[
        {"type": "early_payment_discount", "amount": "250.00",
         "account_id": env["discount"]}])

    assert _unallocated(conn, pe) == D("750.00")
    assert D(_comp_rows(conn, pe)[0]["amount"]) == D("250.00")
    assert _party_net(conn, "customer", env["customer"]) == D("-750.00")
    assert _inv27(conn) is None


# ── pin 9 — an allocation against a non-invoice voucher type ─────────────────

def test_pin9_non_invoice_voucher_allocation(conn, env):
    """An 'advance' allocation clears no document and posts no per-allocation
    ledger row, but it DOES consume residual — so the compensation still has to
    fire, from the allocation table."""
    pe = _receive(conn, env, "400.00", allocations=[
        {"voucher_type": "advance", "voucher_id": str(uuid.uuid4()),
         "allocated_amount": "400.00"}])

    assert _unallocated(conn, pe) == D("0.00")
    assert D(_comp_rows(conn, pe)[0]["amount"]) == D("400.00")
    assert _party_net(conn, "customer", env["customer"]) == D("0")
    assert _inv27(conn) is None


# ── pin 10 — advance routing configured changes nothing ──────────────────────

def test_pin10_advance_routing_is_answer_independent(conn, env):
    """With an advance sub-account configured the GL splits, but the party
    ledger answer must be identical — config independence."""
    adv = conn.execute(
        "SELECT id FROM account WHERE company_id = ? AND root_type = 'asset' "
        "LIMIT 1", (env["company_id"],)).fetchone()[0]
    conn.execute("UPDATE company SET advance_from_customer_account_id = ? "
                 "WHERE id = ?", (adv, env["company_id"]))
    conn.commit()

    si = seed_sales_invoice(conn, env, "1000.00")
    _receive(conn, env, "300.00", allocations=[
        {"voucher_type": "sales_invoice", "voucher_id": si,
         "allocated_amount": "300.00"}])
    assert _party_net(conn, "customer", env["customer"]) == D("700.00")
    assert _inv27(conn) is None


# ── pin 11 — a same-currency non-USD payment ─────────────────────────────────

def test_pin11_non_usd_same_currency(conn, env):
    """EUR invoice + EUR payment: exact Decimals, no FX in the books
    (_assert_currency_match unchanged)."""
    si = seed_sales_invoice(conn, env, "1000.00")
    conn.execute("UPDATE sales_invoice SET currency = 'EUR' WHERE id = ?", (si,))
    conn.commit()
    _receive(conn, env, "300.00", currency="EUR", allocations=[
        {"voucher_type": "sales_invoice", "voucher_id": si,
         "allocated_amount": "300.00"}])

    assert _party_net(conn, "customer", env["customer"]) == D("700.00")
    currencies = {r[0] for r in conn.execute(
        "SELECT DISTINCT currency FROM payment_ledger_entry "
        "WHERE voucher_type = 'payment_entry'")}
    assert currencies == {"EUR"}
    assert _inv27(conn) is None


# ── pin 14 — the DRAFT negative (correction C3) ──────────────────────────────

def test_pin14_draft_payment_writes_no_ledger_row_at_all(conn, env):
    """``add-payment --allocations`` must write NOTHING to the ledger.

    A draft has no party-level row and is excluded from INV-27's RHS, so a draft
    compensation reads permanently RED (the SIM measured LHS 1,300 vs RHS
    1,000). The compensation appears only at submit.
    """
    si = seed_sales_invoice(conn, env, "1000.00")
    before = _ple_count(conn)
    pe = _receive(conn, env, "300.00", submit=False, allocations=[
        {"voucher_type": "sales_invoice", "voucher_id": si,
         "allocated_amount": "300.00"}])

    assert _ple_count(conn) == before, "a draft payment wrote a ledger row"
    assert _comp_rows(conn, pe) == []
    assert _inv27(conn) is None

    s = call_action(mod.submit_payment, conn, ns(payment_entry_id=pe))
    assert is_ok(s), s
    assert len(_comp_rows(conn, pe)) == 1
    assert _inv27(conn) is None


def test_pin14b_delete_payment_leaves_no_orphaned_ledger_row(conn, env):
    """``delete-payment`` removes allocations, deductions and the payment but no
    ledger rows. A draft-written compensation would be orphaned forever."""
    si = seed_sales_invoice(conn, env, "1000.00")
    before = _ple_count(conn)
    pe = _receive(conn, env, "300.00", submit=False, allocations=[
        {"voucher_type": "sales_invoice", "voucher_id": si,
         "allocated_amount": "300.00"}])
    d = call_action(mod.delete_payment, conn, ns(payment_entry_id=pe))
    assert is_ok(d), d

    assert _ple_count(conn) == before
    assert conn.execute(
        "SELECT COUNT(*) FROM payment_ledger_entry WHERE voucher_id = ?",
        (pe,)).fetchone()[0] == 0
    assert _inv27(conn) is None


# ── pin 15 — the PARTY-LESS negative (correction C9) ─────────────────────────

def test_pin15_internal_transfer_writes_no_ledger_row(conn, env):
    """An ``internal_transfer`` carries no party. The party-level writer already
    guards on that; the compensation must too, or it inserts a NULL-party ledger
    row and breaks INV-07."""
    other = conn.execute(
        "SELECT id FROM account WHERE company_id = ? AND root_type = 'asset' "
        "AND id != ? LIMIT 1", (env["company_id"], env["bank"])).fetchone()[0]
    before = _ple_count(conn)
    created = call_action(mod.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="internal_transfer",
        posting_date="2026-06-01", party_type=None, party_id=None,
        paid_from_account=env["bank"], paid_to_account=other,
        paid_amount="500.00", exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None, allocations=None,
        deductions=None))
    assert is_ok(created), created
    s = call_action(mod.submit_payment, conn, ns(
        payment_entry_id=created["payment_entry_id"]))
    assert is_ok(s), s

    assert _ple_count(conn) == before, "a party-less payment wrote a ledger row"
    assert _inv(conn, "_check_inv07_no_orphaned_ple_parties") is None
    assert _inv27(conn) is None


# ── pin 16 — per-bucket output (correction C6) ───────────────────────────────

def test_pin16_get_outstanding_has_no_none_bucket(conn, env):
    """``get-outstanding``'s vouchers array must carry no phantom (None, None)
    bucket, and the payment's own rows must net inside the PAYMENT's bucket
    while its allocation reduces the INVOICE's."""
    si = seed_sales_invoice(conn, env, "1000.00")
    pe = _receive(conn, env, "300.00", allocations=[
        {"voucher_type": "sales_invoice", "voucher_id": si,
         "allocated_amount": "300.00"}])

    r = call_action(mod.get_outstanding, conn, ns(
        party_type="customer", party_id=env["customer"],
        voucher_type=None, voucher_id=None))
    assert is_ok(r), r
    buckets = {(v["voucher_type"], v["voucher_id"]): D(v["outstanding_amount"])
               for v in r["vouchers"]}
    assert (None, None) not in buckets, f"phantom bucket present: {buckets}"
    assert all(vt is not None and vid is not None for vt, vid in buckets)
    # The invoice bucket holds the real outstanding; the payment's party-level
    # row and its compensation cancel inside the payment's own bucket, so the
    # payment does not appear at all (a zero bucket is filtered by HAVING).
    assert buckets == {("sales_invoice", si): D("700.00")}
    assert D(r["outstanding"]) == D("700.00")
    assert _party_net(conn, "customer", env["customer"]) == D("700.00")
    assert pe  # the payment exists; it simply nets to zero on its own voucher


def test_pin16b_get_outstanding_filter_follows_the_attributed_bucket(conn, env):
    """Filtering by the invoice returns the invoice's NET, allocations included —
    the old grouping put the allocation in the payment's bucket instead."""
    si = seed_sales_invoice(conn, env, "1000.00")
    _receive(conn, env, "300.00", allocations=[
        {"voucher_type": "sales_invoice", "voucher_id": si,
         "allocated_amount": "300.00"}])
    r = call_action(mod.get_outstanding, conn, ns(
        party_type="customer", party_id=env["customer"],
        voucher_type="sales_invoice", voucher_id=si))
    assert is_ok(r), r
    assert D(r["outstanding"]) == D("700.00")


# ── pin 7 — invoice cancelled with a live allocation (F1's pin, through F2) ──

def test_pin7_cancel_invoice_with_live_allocation(conn, env):
    """F1's release re-runs the compensation, so the party stays true across the
    cancel. INV-27 is evaluated after EVERY step, not only at the end."""
    from erpclaw_lib.payment_clearing import release_allocations_on_document

    si = seed_sales_invoice(conn, env, "1000.00")
    pe = _receive(conn, env, "300.00", allocations=[
        {"voucher_type": "sales_invoice", "voucher_id": si,
         "allocated_amount": "300.00"}])
    assert _inv27(conn) is None
    assert _party_net(conn, "customer", env["customer"]) == D("700.00")

    # Cancel the invoice the way selling does: release, then delink its own rows
    # and zero its outstanding. (Driving cancel-sales-invoice itself needs the
    # full selling environment; that pin lives in the selling suite. Here the
    # shared lib is called directly so the compensation half is isolated.)
    result = release_allocations_on_document(conn, "sales_invoice", si)
    conn.execute("UPDATE payment_ledger_entry SET delinked = 1 "
                 "WHERE voucher_type = 'sales_invoice' AND voucher_id = ?", (si,))
    conn.execute("UPDATE sales_invoice SET status = 'cancelled', "
                 "outstanding_amount = '0' WHERE id = ?", (si,))
    conn.commit()

    comp = result["released"][0]["residual_compensation"]
    assert comp["written"] is True
    assert comp["delta"] == "-300.00", comp
    assert [D(r["amount"]) for r in _comp_rows(conn, pe)] == [D("-300.00"),
                                                              D("300.00")]
    assert _unallocated(conn, pe) == D("300.00")
    assert _party_net(conn, "customer", env["customer"]) == D("-300.00")
    assert _inv27(conn) is None
    # The party-PLE == party-GL half of this pin runs in the selling suite,
    # where cancel-sales-invoice posts the real GL reversal (F1 pin 1).


# ── pin 13 — INV-22 / INV-24 / INV-25 are structurally immune ────────────────

def test_pin13_other_invariants_are_blind_to_the_compensation(conn, env):
    """R11/R12: the compensation must be INVISIBLE to INV-22, INV-24 and INV-25.

    Asserted by diffing, not by eyeballing: each check is run on the seeded DB
    with the compensation rows present, then again with them removed, and the
    two outputs must be byte-identical. (INV-22 and INV-25 look only at rows
    referencing a DOCUMENT; a compensation row references its own payment. INV-24
    reads gl_entry and stock_ledger_entry, which this change never touches.)
    """
    si = seed_sales_invoice(conn, env, "1000.00")
    _receive(conn, env, "300.00", allocations=[
        {"voucher_type": "sales_invoice", "voucher_id": si,
         "allocated_amount": "300.00"}])

    names = ("_check_inv22_payment_invoice_reconciliation",
             "_check_inv24_stock_account_gl_matches_ledger",
             "_check_inv25_ar_summary_detail")
    with_comp = {n: _inv(conn, n) for n in names}

    rows = conn.execute(
        "SELECT id, posting_date, account_id, party_type, party_id, "
        " voucher_type, voucher_id, against_voucher_type, against_voucher_id, "
        " amount, amount_in_account_currency, currency, delinked, remarks "
        "FROM payment_ledger_entry WHERE voucher_type = 'payment_entry' "
        "  AND against_voucher_type = 'payment_entry' "
        "  AND against_voucher_id = voucher_id").fetchall()
    assert rows, "fixture must contain at least one compensation row"
    conn.execute("DELETE FROM payment_ledger_entry WHERE voucher_type = "
                 "'payment_entry' AND against_voucher_type = 'payment_entry' "
                 "AND against_voucher_id = voucher_id")
    conn.commit()
    without_comp = {n: _inv(conn, n) for n in names}

    assert with_comp == without_comp, (
        "the compensation row is visible to an invariant that must be blind to "
        f"it: {with_comp} vs {without_comp}")
    # ...and INV-27 is NOT blind to it — that is the whole point.
    assert _inv27(conn) is not None


# ── A2 (inherited advisory) — the cancel+release+compensation path is atomic ──

def test_a2_release_and_compensation_roll_back_together(conn, env, monkeypatch):
    """All-or-nothing under an injected failure.

    The release delinks the allocation, closes out its ledger rows, recomputes
    the residual AND re-runs the compensation. If the compensation raises, none
    of the earlier writes may survive: a released allocation without its
    compensation is a party ledger that is wrong in a NEW way, which is worse
    than the state we started from.
    """
    from erpclaw_lib import payment_clearing

    si = seed_sales_invoice(conn, env, "1000.00")
    pe = _receive(conn, env, "300.00", allocations=[
        {"voucher_type": "sales_invoice", "voucher_id": si,
         "allocated_amount": "300.00"}])
    conn.commit()

    before = {
        "ple": _ple_count(conn),
        "alloc_delinked": conn.execute(
            "SELECT delinked FROM payment_allocation WHERE payment_entry_id = ?",
            (pe,)).fetchone()[0],
        "unallocated": _unallocated(conn, pe),
        "party": _party_net(conn, "customer", env["customer"]),
    }

    def _boom(*a, **k):
        raise RuntimeError("injected compensation failure")

    monkeypatch.setattr(payment_clearing, "post_party_residual_compensation",
                        _boom)
    with pytest.raises(RuntimeError):
        payment_clearing.release_allocations_on_document(conn, "sales_invoice", si)
    conn.rollback()          # the caller owns the transaction, exactly as in
                             # cancel_sales_invoice's error path

    assert _ple_count(conn) == before["ple"]
    assert conn.execute(
        "SELECT delinked FROM payment_allocation WHERE payment_entry_id = ?",
        (pe,)).fetchone()[0] == before["alloc_delinked"]
    assert _unallocated(conn, pe) == before["unallocated"]
    assert _party_net(conn, "customer", env["customer"]) == before["party"]
    assert _inv27(conn) is None

    # And the real path still works afterwards — the rollback left no debris.
    monkeypatch.undo()
    result = payment_clearing.release_allocations_on_document(
        conn, "sales_invoice", si)
    conn.commit()
    assert result["released"][0]["residual_compensation"]["written"] is True


def test_a2b_submit_rolls_back_the_compensation_with_everything_else(conn, env,
                                                                     monkeypatch):
    """The submit-time site is inside submit-payment's single transaction.

    A failure AFTER the compensation (the allocation-clearing loop) must leave
    no compensation row behind — submit is all-or-nothing (coding rule: submit =
    one transaction).
    """
    before = _ple_count(conn)
    # The allocation names an invoice that does not exist, so the clearing step
    # raises AFTER the party-level row, the status flip and the compensation
    # have all been written. Nothing else in the ledger is disturbed by the
    # fixture itself, so what survives the failure is the code's answer.
    pe = _receive(conn, env, "300.00", submit=False, allocations=[
        {"voucher_type": "sales_invoice", "voucher_id": str(uuid.uuid4()),
         "allocated_amount": "300.00"}])

    r = call_action(mod.submit_payment, conn, ns(payment_entry_id=pe))
    assert is_error(r), r
    assert _ple_count(conn) == before, "a failed submit left ledger rows behind"
    assert _comp_rows(conn, pe) == []
    assert conn.execute("SELECT status FROM payment_entry WHERE id = ?",
                        (pe,)).fetchone()[0] == "draft"
    assert _inv27(conn) is None
