"""Part A tests for WS2 D3 — payment deductions (dossier §3).

A payment can carry --deductions (JSON array of {account_id, amount, type,
description?}) recording the non-cash slice of paid_amount (early-pay discount
given, TDS withheld, commission kept). Invariant under test:

    paid_amount = allocations + deductions + unallocated

GL posting shape (receive): DR bank (paid − deductions) + DR deduction accounts
/ CR AR (full paid). Pay type mirrors: DR AP (full paid) / CR bank + CR
deduction accounts. The deducted total rides the invoice clearing pro-rata, so
the dossier's $980 wire + $20 discount fully clears a $1,000 invoice with
gl_balanced and the AR subledger (INV-08/INV-22) green. Cancel reverses every
leg (deduction legs ride the same voucher reversal).

All money assertions are exact Decimal, never float.
"""
import importlib.util
import json
import os
import uuid
from decimal import Decimal

import pytest

from payments_helpers import (
    call_action, ns, is_error, is_ok, load_db_query, _uuid,
    build_ar_env, build_ap_env, seed_sales_invoice, seed_purchase_invoice,
    seed_account,
)

mod = load_db_query()

# ── invariant engine: import straight from testing/invariant_engine.py ───────
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))


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


# The invariant engine lives in the monorepo test harness (testing/), which is NOT
# part of the published skill tree. In the published-repo CI it is absent, so load it
# defensively and skip the invariant-backed assertions there; the full monorepo CI runs
# them. (Root-find can also fail when the tree lacks CLAUDE.md/.git, e.g. a shallow CI
# checkout — treat that as "harness absent" too.)
try:
    _INV_PATH = os.path.join(_find_repo_root(_TESTS_DIR), "testing", "invariant_engine.py")
except RuntimeError:
    _INV_PATH = ""
if _INV_PATH and os.path.exists(_INV_PATH):
    _spec = importlib.util.spec_from_file_location("invariant_engine_pd", _INV_PATH)
    inv_engine = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(inv_engine)
else:
    inv_engine = None


def _invariants_green(conn):
    """run_invariants raises InvariantViolation on any failure; green = no raise."""
    if inv_engine is None:
        pytest.skip("invariant_engine harness not present (published skill tree)")
    inv_engine.run_invariants(conn)


def _inv24(conn):
    """INV-24 directly; None = GREEN (stock subledger untouched by payments)."""
    if inv_engine is None:
        pytest.skip("invariant_engine harness not present (published skill tree)")
    return inv_engine._check_inv24_stock_account_gl_matches_ledger(conn)


# ── flow helpers ─────────────────────────────────────────────────────────────

def _add_payment(conn, env, *, payment_type, party_type, party_id,
                 paid_from, paid_to, paid_amount,
                 allocations=None, deductions=None):
    return call_action(mod.add_payment, conn, ns(
        company_id=env["company_id"], payment_type=payment_type,
        posting_date="2026-06-01", party_type=party_type, party_id=party_id,
        paid_from_account=paid_from, paid_to_account=paid_to,
        paid_amount=paid_amount, exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None,
        allocations=json.dumps(allocations) if allocations is not None else None,
        deductions=json.dumps(deductions) if deductions is not None else None))


def _add_receive(conn, env, paid_amount, allocations=None, deductions=None):
    return _add_payment(conn, env, payment_type="receive",
                        party_type="customer", party_id=env["customer"],
                        paid_from=env["ar"], paid_to=env["bank"],
                        paid_amount=paid_amount,
                        allocations=allocations, deductions=deductions)


def _add_pay(conn, env, paid_amount, allocations=None, deductions=None):
    return _add_payment(conn, env, payment_type="pay",
                        party_type="supplier", party_id=env["supplier"],
                        paid_from=env["bank"], paid_to=env["ap"],
                        paid_amount=paid_amount,
                        allocations=allocations, deductions=deductions)


def _submit(conn, pe_id):
    return call_action(mod.submit_payment, conn, ns(payment_entry_id=pe_id))


def _cancel(conn, pe_id):
    return call_action(mod.cancel_payment, conn, ns(payment_entry_id=pe_id))


def _gl_rows(conn, pe_id, cancelled=0):
    return [dict(r) for r in conn.execute(
        "SELECT account_id, debit, credit, cost_center_id, is_cancelled "
        "FROM gl_entry WHERE voucher_type = 'payment_entry' AND voucher_id = ? "
        "AND is_cancelled = ? ORDER BY created_at, id", (pe_id, cancelled))]


def _leg(rows, account_id):
    matches = [r for r in rows if r["account_id"] == account_id]
    assert len(matches) == 1, f"expected exactly one leg on {account_id}: {matches}"
    return matches[0]


def _doc(conn, table, doc_id):
    assert table in ("sales_invoice", "purchase_invoice")
    return dict(conn.execute(
        f"SELECT outstanding_amount, status FROM {table} WHERE id = ?",  # noqa: S608 — whitelisted table
        (doc_id,)).fetchone())


def _pe(conn, pe_id):
    return dict(conn.execute(
        "SELECT * FROM payment_entry WHERE id = ?", (pe_id,)).fetchone())


def _ple_net(conn, voucher_type, voucher_id):
    """INV-22 math: net all non-delinked PLE referencing this doc."""
    rows = conn.execute(
        "SELECT amount FROM payment_ledger_entry WHERE delinked = 0 AND ("
        " (voucher_type = ? AND voucher_id = ?) OR "
        " (against_voucher_type = ? AND against_voucher_id = ?))",
        (voucher_type, voucher_id, voucher_type, voucher_id)).fetchall()
    return sum((Decimal(r["amount"]) for r in rows), Decimal("0"))


# ── 1. The dossier example: $980 wire + $20 discount vs a $1,000 invoice ─────

def test_dossier_example_receive_with_discount_deduction(conn):
    env = build_ar_env(conn)
    si = seed_sales_invoice(conn, env, "1000")

    r = _add_receive(conn, env, "1000",
                     allocations=[{"voucher_type": "sales_invoice",
                                   "voucher_id": si,
                                   "allocated_amount": "980"}],
                     deductions=[{"account_id": env["discount"],
                                  "amount": "20",
                                  "type": "early_payment_discount",
                                  "description": "2% early-pay discount"}])
    assert is_ok(r) or r.get("status") == "created", r
    pe_id = r["payment_entry_id"]

    # Draft remainder math: paid (1000) = alloc (980) + ded (20) + unalloc (0)
    assert Decimal(_pe(conn, pe_id)["unallocated_amount"]) == Decimal("0")

    s = _submit(conn, pe_id)
    assert not is_error(s), s
    assert s["deductions"] == {"total": "20.00", "count": 1}

    # Invoice fully allocated/paid
    d = _doc(conn, "sales_invoice", si)
    assert d["outstanding_amount"] == "0"
    assert d["status"] == "paid"

    # GL legs: bank 980, discount 20 (with cost center), AR credit 1000
    rows = _gl_rows(conn, pe_id)
    assert len(rows) == 3
    bank = _leg(rows, env["bank"])
    assert Decimal(bank["debit"]) == Decimal("980.00")
    assert Decimal(bank["credit"]) == Decimal("0")
    disc = _leg(rows, env["discount"])
    assert Decimal(disc["debit"]) == Decimal("20.00")
    assert Decimal(disc["credit"]) == Decimal("0")
    assert disc["cost_center_id"] == env["cc"]  # P&L leg carries the default CC
    ar = _leg(rows, env["ar"])
    assert Decimal(ar["credit"]) == Decimal("1000.00")
    assert Decimal(ar["debit"]) == Decimal("0")

    # gl_balanced: exact debit/credit equality on the voucher
    assert sum(Decimal(x["debit"]) for x in rows) == \
        sum(Decimal(x["credit"]) for x in rows) == Decimal("1000.00")

    # ar_subledger_consistent: paid invoice nets to zero in PLE
    assert _ple_net(conn, "sales_invoice", si) == Decimal("0")

    # Full engine green (INV-01/02/17 + INV-08/INV-22 + the rest)
    _invariants_green(conn)
    # INV-24 unaffected — no stock accounts anywhere near this flow
    assert _inv24(conn) is None

    # Deduction row persisted + surfaced by get-payment
    g = call_action(mod.get_payment, conn, ns(payment_entry_id=pe_id))
    assert len(g["deductions"]) == 1
    ded = g["deductions"][0]
    assert ded["account_id"] == env["discount"]
    assert Decimal(ded["amount"]) == Decimal("20.00")
    assert ded["type"] == "early_payment_discount"


# ── 2. Pay-type mirror: TDS withheld from a supplier payment ─────────────────

def test_pay_type_mirror_tds_deduction(conn):
    env = build_ap_env(conn)
    pi = seed_purchase_invoice(conn, env, "1000")

    r = _add_pay(conn, env, "1000",
                 allocations=[{"voucher_type": "purchase_invoice",
                               "voucher_id": pi,
                               "allocated_amount": "980"}],
                 deductions=[{"account_id": env["tds"], "amount": "20",
                              "type": "tds"}])
    assert not is_error(r), r
    pe_id = r["payment_entry_id"]

    s = _submit(conn, pe_id)
    assert not is_error(s), s

    d = _doc(conn, "purchase_invoice", pi)
    assert d["outstanding_amount"] == "0"
    assert d["status"] == "paid"

    # Mirrored direction: DR AP full, CR bank (paid − ded), CR TDS liability
    rows = _gl_rows(conn, pe_id)
    assert len(rows) == 3
    ap = _leg(rows, env["ap"])
    assert Decimal(ap["debit"]) == Decimal("1000.00")
    bank = _leg(rows, env["bank"])
    assert Decimal(bank["credit"]) == Decimal("980.00")
    assert Decimal(bank["debit"]) == Decimal("0")
    tds = _leg(rows, env["tds"])
    assert Decimal(tds["credit"]) == Decimal("20.00")
    assert Decimal(tds["debit"]) == Decimal("0")

    assert _ple_net(conn, "purchase_invoice", pi) == Decimal("0")
    _invariants_green(conn)
    assert _inv24(conn) is None


# ── 3. Multiple deductions on one payment ────────────────────────────────────

def test_multi_deduction_single_invoice(conn):
    env = build_ar_env(conn)
    si = seed_sales_invoice(conn, env, "1000")

    r = _add_receive(conn, env, "1000",
                     allocations=[{"voucher_type": "sales_invoice",
                                   "voucher_id": si,
                                   "allocated_amount": "970"}],
                     deductions=[
                         {"account_id": env["discount"], "amount": "20",
                          "type": "early_payment_discount"},
                         {"account_id": env["commission"], "amount": "10",
                          "type": "commission"}])
    assert not is_error(r), r
    pe_id = r["payment_entry_id"]
    assert Decimal(_pe(conn, pe_id)["unallocated_amount"]) == Decimal("0")

    s = _submit(conn, pe_id)
    assert not is_error(s), s
    assert s["deductions"] == {"total": "30.00", "count": 2}

    d = _doc(conn, "sales_invoice", si)
    assert d["outstanding_amount"] == "0"
    assert d["status"] == "paid"

    rows = _gl_rows(conn, pe_id)
    assert len(rows) == 4
    assert Decimal(_leg(rows, env["bank"])["debit"]) == Decimal("970.00")
    assert Decimal(_leg(rows, env["discount"])["debit"]) == Decimal("20.00")
    assert Decimal(_leg(rows, env["commission"])["debit"]) == Decimal("10.00")
    assert Decimal(_leg(rows, env["ar"])["credit"]) == Decimal("1000.00")

    assert _ple_net(conn, "sales_invoice", si) == Decimal("0")
    _invariants_green(conn)


# ── 4. Deductions distribute pro-rata across multiple invoice allocations ────

def test_deduction_prorata_across_two_invoices(conn):
    env = build_ar_env(conn)
    si_a = seed_sales_invoice(conn, env, "600")
    si_b = seed_sales_invoice(conn, env, "400")

    # 20 splits 12 / 8 over allocations 588 / 392 → both invoices fully clear
    r = _add_receive(conn, env, "1000",
                     allocations=[
                         {"voucher_type": "sales_invoice", "voucher_id": si_a,
                          "allocated_amount": "588"},
                         {"voucher_type": "sales_invoice", "voucher_id": si_b,
                          "allocated_amount": "392"}],
                     deductions=[{"account_id": env["discount"], "amount": "20",
                                  "type": "early_payment_discount"}])
    assert not is_error(r), r
    s = _submit(conn, r["payment_entry_id"])
    assert not is_error(s), s

    for si in (si_a, si_b):
        d = _doc(conn, "sales_invoice", si)
        assert d["outstanding_amount"] == "0"
        assert d["status"] == "paid"
        assert _ple_net(conn, "sales_invoice", si) == Decimal("0")

    # Per-allocation PLE carries allocation + its deduction share exactly
    applied = {row["against_voucher_id"]: Decimal(row["amount"])
               for row in conn.execute(
                   "SELECT against_voucher_id, amount FROM payment_ledger_entry "
                   "WHERE voucher_id = ? AND against_voucher_id IS NOT NULL "
                   "AND delinked = 0", (r["payment_entry_id"],))}
    assert applied[si_a] == Decimal("-600.00")
    assert applied[si_b] == Decimal("-400.00")
    _invariants_green(conn)


# ── 5. Validation failures ───────────────────────────────────────────────────

def test_validation_invalid_json(conn):
    env = build_ar_env(conn)
    r = call_action(mod.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="receive",
        posting_date="2026-06-01", party_type="customer",
        party_id=env["customer"], paid_from_account=env["ar"],
        paid_to_account=env["bank"], paid_amount="100",
        exchange_rate=None, payment_currency=None, reference_number=None,
        reference_date=None, allocations=None, deductions="not-json"))
    assert is_error(r)
    assert "Invalid JSON" in r["message"]


def test_validation_not_an_array(conn):
    env = build_ar_env(conn)
    r = _add_receive(conn, env, "100",
                     deductions={"account_id": env["discount"],
                                 "amount": "5", "type": "other"})
    assert is_error(r)
    assert "array" in r["message"]


def test_validation_unknown_account(conn):
    env = build_ar_env(conn)
    r = _add_receive(conn, env, "100",
                     deductions=[{"account_id": str(uuid.uuid4()),
                                  "amount": "5", "type": "other"}])
    assert is_error(r)
    assert "not found" in r["message"]


def test_validation_bad_type(conn):
    env = build_ar_env(conn)
    r = _add_receive(conn, env, "100",
                     deductions=[{"account_id": env["discount"],
                                  "amount": "5", "type": "discount"}])
    assert is_error(r)
    assert "Invalid deduction type" in r["message"]


def test_validation_non_positive_amount(conn):
    env = build_ar_env(conn)
    for bad in ("0", "-5"):
        r = _add_receive(conn, env, "100",
                         deductions=[{"account_id": env["discount"],
                                      "amount": bad, "type": "other"}])
        assert is_error(r), bad
        assert "must be > 0" in r["message"]
        conn.rollback()


def test_validation_float_amount_rejected(conn):
    env = build_ar_env(conn)
    r = _add_receive(conn, env, "100",
                     deductions=[{"account_id": env["discount"],
                                  "amount": 20.5, "type": "other"}])
    assert is_error(r)
    assert "float" in r["message"]


def test_validation_over_paid_amount_rejected(conn):
    env = build_ar_env(conn)
    si = seed_sales_invoice(conn, env, "1000")
    r = _add_receive(conn, env, "1000",
                     allocations=[{"voucher_type": "sales_invoice",
                                   "voucher_id": si,
                                   "allocated_amount": "990"}],
                     deductions=[{"account_id": env["discount"], "amount": "20",
                                  "type": "early_payment_discount"}])
    assert is_error(r)
    assert "exceed" in r["message"]
    # nothing committed: draft + child rows discarded on rollback
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) c FROM payment_entry").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM payment_deduction").fetchone()["c"] == 0


def test_validation_internal_transfer_rejected(conn):
    env = build_ar_env(conn)
    bank2 = seed_account(conn, env["company_id"], "Bank 2", "asset", "bank")
    r = _add_payment(conn, env, payment_type="internal_transfer",
                     party_type=None, party_id=None,
                     paid_from=env["bank"], paid_to=bank2, paid_amount="100",
                     deductions=[{"account_id": env["discount"], "amount": "5",
                                  "type": "other"}])
    assert is_error(r)
    assert "internal_transfer" in r["message"]


def test_over_clearing_rejected_and_rolled_back(conn):
    """Full allocation + deduction (1020 effective vs 1000 outstanding) must
    reject at clearing time and roll back — never over-apply an invoice."""
    env = build_ar_env(conn)
    si = seed_sales_invoice(conn, env, "1000")
    r = _add_receive(conn, env, "1020",
                     allocations=[{"voucher_type": "sales_invoice",
                                   "voucher_id": si,
                                   "allocated_amount": "1000"}],
                     deductions=[{"account_id": env["discount"], "amount": "20",
                                  "type": "early_payment_discount"}])
    assert not is_error(r), r
    s = _submit(conn, r["payment_entry_id"])
    assert is_error(s), s
    d = _doc(conn, "sales_invoice", si)
    assert d["outstanding_amount"] == "1000"
    assert d["status"] == "submitted"


# ── 6. Cancel reverses every leg ─────────────────────────────────────────────

def test_cancel_reverses_all_legs(conn):
    env = build_ar_env(conn)
    si = seed_sales_invoice(conn, env, "1000")
    r = _add_receive(conn, env, "1000",
                     allocations=[{"voucher_type": "sales_invoice",
                                   "voucher_id": si,
                                   "allocated_amount": "980"}],
                     deductions=[{"account_id": env["discount"], "amount": "20",
                                  "type": "early_payment_discount"}])
    pe_id = r["payment_entry_id"]
    s = _submit(conn, pe_id)
    assert not is_error(s), s
    assert _doc(conn, "sales_invoice", si)["status"] == "paid"

    c = _cancel(conn, pe_id)
    assert not is_error(c), c
    assert _pe(conn, pe_id)["status"] == "cancelled"

    # Constitutional reversal: 3 originals flagged cancelled, 3 mirrors active,
    # and the mirror set swaps every leg exactly — deduction leg included.
    originals = _gl_rows(conn, pe_id, cancelled=1)
    mirrors = _gl_rows(conn, pe_id, cancelled=0)
    assert len(originals) == 3 and len(mirrors) == 3
    assert Decimal(_leg(mirrors, env["bank"])["credit"]) == Decimal("980.00")
    assert Decimal(_leg(mirrors, env["discount"])["credit"]) == Decimal("20.00")
    assert Decimal(_leg(mirrors, env["ar"])["debit"]) == Decimal("1000.00")

    # Document fully restored: the reversal used the APPLIED amount
    # (allocation 980 + deduction share 20), not the bare allocation.
    d = _doc(conn, "sales_invoice", si)
    assert Decimal(d["outstanding_amount"]) == Decimal("1000")
    assert d["status"] == "submitted"

    # Deduction rows are history, not drafts — they survive cancellation
    assert conn.execute(
        "SELECT COUNT(*) c FROM payment_deduction WHERE payment_entry_id = ?",
        (pe_id,)).fetchone()["c"] == 1

    _invariants_green(conn)
    assert _inv24(conn) is None


# ── 7. INV-24 stays green across the whole deduction lifecycle ───────────────

def test_inv24_unaffected_by_deduction_flows(conn):
    env = build_ar_env(conn)
    assert _inv24(conn) is None  # clean slate
    si = seed_sales_invoice(conn, env, "500")
    r = _add_receive(conn, env, "500",
                     allocations=[{"voucher_type": "sales_invoice",
                                   "voucher_id": si,
                                   "allocated_amount": "490"}],
                     deductions=[{"account_id": env["discount"], "amount": "10",
                                  "type": "early_payment_discount"}])
    pe_id = r["payment_entry_id"]
    assert not is_error(_submit(conn, pe_id))
    assert _inv24(conn) is None
    assert not is_error(_cancel(conn, pe_id))
    assert _inv24(conn) is None
