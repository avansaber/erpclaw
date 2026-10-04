"""Part A — INV-25 (AR summary ≡ detail, ADR-0031 Decision 2) over REAL
selling flows.

Drives the actual erpclaw-selling actions (create/submit/cancel sales invoice,
create/submit credit note) and asserts the always-on INV-25 equality
(outstanding_amount ≡ payment-ledger net) holds at every lifecycle point —
including the states INV-22's paid-only scope never checked.

The invariant engine lives in the monorepo harness (testing/); import is
defensive so the published skill tree skips the engine-backed assertions
(same pattern as erpclaw-payments/tests/test_payment_deduction.py).
"""
import importlib.util
import json
import os
from decimal import Decimal

import pytest

from selling_helpers import call_action, ns, is_ok, load_db_query

mod = load_db_query()

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


try:
    _INV_PATH = os.path.join(_find_repo_root(_TESTS_DIR), "testing", "invariant_engine.py")
except RuntimeError:
    _INV_PATH = ""
if _INV_PATH and os.path.exists(_INV_PATH):
    _spec = importlib.util.spec_from_file_location("invariant_engine_inv25_sell", _INV_PATH)
    inv_engine = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(inv_engine)
else:
    inv_engine = None


def _inv25(conn):
    if inv_engine is None:
        pytest.skip("invariant_engine harness not present (published skill tree)")
    inv_engine._ensure_decimal_sum(conn)
    return inv_engine._check_inv25_ar_summary_detail(conn)


def _items(env, *specs):
    return json.dumps([
        {"item_id": env[k], "qty": q, "rate": r, "warehouse_id": env["warehouse"]}
        for k, q, r in specs
    ])


def _create_invoice(conn, env, qty="5", rate="100.00"):
    create = call_action(mod.create_sales_invoice, conn, ns(
        sales_order_id=None, delivery_note_id=None,
        customer_id=env["customer"], company_id=env["company_id"],
        posting_date="2026-06-20", due_date="2026-07-20",
        items=_items(env, ("item1", qty, rate)), tax_template_id=None,
        payment_terms_id=None,
    ))
    assert is_ok(create)
    return create["sales_invoice_id"]


def test_inv25_green_across_invoice_lifecycle(conn, env):
    """draft → submitted → cancelled, all via the real actions: INV-25 green
    at every step, and the submitted state satisfies the exact equality."""
    si_id = _create_invoice(conn, env)
    assert _inv25(conn) is None  # draft excluded (no PLE yet)

    r = call_action(mod.submit_sales_invoice, conn, ns(sales_invoice_id=si_id))
    assert is_ok(r)
    row = conn.execute(
        "SELECT outstanding_amount FROM sales_invoice WHERE id=?", (si_id,)).fetchone()
    ple = conn.execute(
        "SELECT amount FROM payment_ledger_entry "
        "WHERE voucher_type='sales_invoice' AND voucher_id=? AND delinked=0",
        (si_id,)).fetchall()
    assert len(ple) == 1
    assert Decimal(row["outstanding_amount"]) == Decimal(ple[0]["amount"]) == Decimal("500.00")
    assert _inv25(conn) is None  # open submitted invoice — INV-22 never checked this

    r = call_action(mod.cancel_sales_invoice, conn, ns(sales_invoice_id=si_id))
    assert is_ok(r)
    assert _inv25(conn) is None  # cancelled = excluded; delinked row inert


def test_inv25_green_with_credit_note_pair(conn, env):
    """Real CN flow (create-credit-note + submit): submitting the note applies
    it to its invoice (m783a) — the 500.00 invoice falls to 300.00
    partially_paid, the fully-absorbed note reads "0" with its posting row
    plus the allocation pair; INV-25 stays green, always-on."""
    si_id = _create_invoice(conn, env)
    r = call_action(mod.submit_sales_invoice, conn, ns(sales_invoice_id=si_id))
    assert is_ok(r)

    cn = call_action(mod.create_credit_note, conn, ns(
        against_invoice_id=si_id, reason="Returned goods",
        posting_date="2026-06-25",
        items=json.dumps([{"item_id": env["item1"], "qty": "2", "rate": "100.00"}]),
    ))
    assert is_ok(cn)
    cn_id = cn["credit_note_id"]
    assert _inv25(conn) is None  # CN draft excluded

    r = call_action(mod.submit_sales_invoice, conn, ns(sales_invoice_id=cn_id))
    assert is_ok(r)

    cn_row = conn.execute(
        "SELECT outstanding_amount, is_return FROM sales_invoice WHERE id=?",
        (cn_id,)).fetchone()
    assert cn_row["is_return"] == 1
    cn_ple = conn.execute(
        "SELECT amount, against_voucher_type, against_voucher_id "
        "FROM payment_ledger_entry WHERE voucher_type='credit_note' "
        "AND voucher_id=? AND delinked=0", (cn_id,)).fetchall()
    assert len(cn_ple) == 3
    # the note is fully absorbed: posting row -200.00 self, allocation pair
    # +200.00 self / -200.00 against the original; outstanding "0".
    assert Decimal(cn_row["outstanding_amount"]) == Decimal("0")
    amounts = sorted(Decimal(r["amount"]) for r in cn_ple)
    assert amounts == [Decimal("-200.00"), Decimal("-200.00"), Decimal("200.00")]
    self_rows = sorted(
        Decimal(r["amount"]) for r in cn_ple
        if r["against_voucher_type"] == "credit_note"
        and r["against_voucher_id"] == cn_id)
    assert self_rows == [Decimal("-200.00"), Decimal("200.00")]
    inv_rows = [
        r for r in cn_ple if r["against_voucher_type"] == "sales_invoice"
        and r["against_voucher_id"] == si_id]
    assert len(inv_rows) == 1
    assert Decimal(inv_rows[0]["amount"]) == Decimal("-200.00")
    # the original absorbs the note: 500.00 -> 300.00 partially_paid
    orig = conn.execute(
        "SELECT outstanding_amount, status FROM sales_invoice WHERE id=?", (si_id,)).fetchone()
    assert Decimal(orig["outstanding_amount"]) == Decimal("300.00")
    assert orig["status"] == "partially_paid"

    assert _inv25(conn) is None  # both documents green under the derived formula
