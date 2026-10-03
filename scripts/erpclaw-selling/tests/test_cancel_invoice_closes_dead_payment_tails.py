"""M352b: cancel-sales-invoice closes dead payment tails.

``release_allocations_on_document`` must skip a cancelled payment's
allocation (correction C2 — pinned in test_cancel_invoice_allocation_release:
the skip writes nothing at all), but the payment's own cancel left live
per-allocation mirrors pointing at this invoice, and a cancelled document
reads outstanding zero, so INV-22 counts them. ``close_dead_payment_tails``
delinks exactly those mirrors — no mirror appended, the allocation row
untouched, no residual recomputed, submitted payments left alone.

Every pin drives the REAL actions (selling create/submit-sales-invoice,
payments add/submit/cancel-payment, selling cancel-sales-invoice) against a
fresh core DB. ``add-sales-invoice`` is the foundation-router alias for
``create-sales-invoice``; ``user_confirmed=True`` is passed on every gated
step for the record (the gate itself lives in the foundation router, which
these direct calls bypass — module code never reads the flag).
"""
import importlib.util
import json
import os
from decimal import Decimal

from selling_helpers import call_action, ns, is_ok, is_error, load_db_query

mod = load_db_query()

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(os.path.dirname(_TESTS_DIR))


def _load(name, rel_path):
    path = os.path.join(_SCRIPTS_DIR, rel_path)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pay = _load("db_query_payments_tails", "erpclaw-payments/db_query.py")

D = Decimal

PLE_COLS = ("id", "posting_date", "account_id", "party_type", "party_id",
            "voucher_type", "voucher_id", "against_voucher_type",
            "against_voucher_id", "amount", "amount_in_account_currency",
            "currency", "delinked", "remarks", "created_at", "updated_at")


def _items(env, *specs):
    return json.dumps([
        {"item_id": env[key], "qty": qty, "rate": rate,
         "warehouse_id": env["warehouse"]}
        for key, qty, rate in specs
    ])


def _sales_invoice(conn, env, qty="10", rate="100.00"):
    """Real create + submit through the selling actions."""
    create = call_action(mod.create_sales_invoice, conn, ns(
        sales_order_id=None, delivery_note_id=None,
        customer_id=env["customer"], company_id=env["company_id"],
        posting_date="2026-06-20", due_date="2026-07-20",
        items=_items(env, ("item1", qty, rate)), tax_template_id=None,
        payment_terms_id=None, user_confirmed=True,
    ))
    assert is_ok(create), create
    si_id = create["sales_invoice_id"]
    assert is_ok(call_action(mod.submit_sales_invoice, conn, ns(
        sales_invoice_id=si_id, user_confirmed=True)))
    return si_id


def _receive_payment(conn, env, amount, allocations):
    """Real add + submit of a receive payment with allocations."""
    created = call_action(pay.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="receive",
        posting_date="2026-06-25", party_type="customer",
        party_id=env["customer"],
        paid_from_account=env["ar"], paid_to_account=env["cash"],
        paid_amount=amount, exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None,
        allocations=json.dumps(allocations), deductions=None,
        user_confirmed=True,
    ))
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    assert is_ok(call_action(pay.submit_payment, conn, ns(
        payment_entry_id=pe_id, user_confirmed=True)))
    return pe_id


def _live_tails(conn, invoice_id):
    """Live PLE rows pointing at one invoice (any against spelling)."""
    return conn.execute(
        "SELECT id, voucher_type, voucher_id, against_voucher_type, "
        "against_voucher_id, amount, delinked FROM payment_ledger_entry "
        "WHERE against_voucher_id = ? AND delinked = 0 "
        "ORDER BY id", (invoice_id,)).fetchall()


def _ple_snapshot(conn, invoice_id):
    """Every PLE row pointing at one invoice, as plain tuples by id."""
    cols = ", ".join(PLE_COLS)
    return [tuple(row[col] for col in PLE_COLS)
            for row in conn.execute(
                "SELECT " + cols + " FROM payment_ledger_entry "
                "WHERE against_voucher_id = ? ORDER BY id",
                (invoice_id,)).fetchall()]


GL_COLS = ("id", "posting_date", "created_at", "account_id",
           "party_type", "party_id", "debit", "credit", "currency",
           "debit_base", "credit_base", "exchange_rate", "voucher_type",
           "voucher_id", "entry_set", "cost_center_id", "project_id",
           "remarks", "fiscal_year", "is_cancelled", "cancelled_by",
           "sequence", "gl_checksum", "dimensions_json")


def _gl_snapshot(conn):
    cols = ", ".join(GL_COLS)
    return [{col: row[col] for col in GL_COLS}
            for row in conn.execute(
                "SELECT " + cols + " FROM gl_entry ORDER BY id").fetchall()]


def _gl_key(row):
    return row["id"]


def test_cancelled_payment_then_invoice_leaves_no_live_row(conn, env):
    """Full-pay, cancel payment, cancel invoice: no live tail survives.

    Without the tail close this fails with exactly one live mirror row —
    the cancel-payment mirror of the per-allocation leg, still pointing at
    the now-cancelled invoice.
    """
    si_id = _sales_invoice(conn, env)
    pe_id = _receive_payment(conn, env, "1000.00", [
        {"voucher_type": "sales_invoice", "voucher_id": si_id,
         "allocated_amount": "1000.00"}])
    assert D(conn.execute(
        "SELECT outstanding_amount FROM sales_invoice WHERE id = ?",
        (si_id,)).fetchone()[0]) == D("0.00")

    assert is_ok(call_action(pay.cancel_payment, conn, ns(
        payment_entry_id=pe_id, user_confirmed=True)))
    assert D(conn.execute(
        "SELECT outstanding_amount FROM sales_invoice WHERE id = ?",
        (si_id,)).fetchone()[0]) == D("1000.00")
    # Two live rows point at the invoice here: the payment's cancel mirror
    # (the dead tail the invoice cancel must close) and the invoice's own
    # document row (closed by the cancel's own PLE delink step).
    assert len(_live_tails(conn, si_id)) == 2

    unallocated_before = conn.execute(
        "SELECT unallocated_amount FROM payment_entry WHERE id = ?",
        (pe_id,)).fetchone()[0]
    gl_before = {_gl_key(row): row for row in _gl_snapshot(conn)}

    result = call_action(mod.cancel_sales_invoice, conn, ns(
        sales_invoice_id=si_id, user_confirmed=True))
    assert is_ok(result), result

    assert _live_tails(conn, si_id) == []

    allocs = conn.execute(
        "SELECT allocated_amount, delinked FROM payment_allocation "
        "WHERE payment_entry_id = ? ORDER BY id", (pe_id,)).fetchall()
    assert len(allocs) == 1
    assert allocs[0]["delinked"] == 0
    assert D(allocs[0]["allocated_amount"]) == D("1000.00")

    unallocated_after = conn.execute(
        "SELECT unallocated_amount FROM payment_entry WHERE id = ?",
        (pe_id,)).fetchone()[0]
    assert isinstance(unallocated_after, str)
    assert unallocated_after == unallocated_before

    gl_after = {_gl_key(row): row for row in _gl_snapshot(conn)}
    added = [row for key, row in gl_after.items() if key not in gl_before]
    assert added, "the invoice cancel must still post its GL reversal"
    for row in added:
        assert row["voucher_id"] == si_id, row
    for key, brow in gl_before.items():
        arow = gl_after[key]
        if brow["voucher_id"] == si_id:
            moved = {col for col in brow if brow[col] != arow[col]}
            assert moved <= {"is_cancelled", "cancelled_by"}, (key, moved)
        else:
            assert arow == brow, key

    assert conn.execute(
        "SELECT status FROM sales_invoice WHERE id = ?",
        (si_id,)).fetchone()[0] == "cancelled"


def test_other_invoice_rows_stay_live(conn, env):
    """One payment on two invoices: cancelling invoice 1 leaves 2's rows."""
    si1 = _sales_invoice(conn, env)
    si2 = _sales_invoice(conn, env, qty="5", rate="100.00")
    pe_id = _receive_payment(conn, env, "1500.00", [
        {"voucher_type": "sales_invoice", "voucher_id": si1,
         "allocated_amount": "1000.00"},
        {"voucher_type": "sales_invoice", "voucher_id": si2,
         "allocated_amount": "500.00"}])

    assert is_ok(call_action(pay.cancel_payment, conn, ns(
        payment_entry_id=pe_id, user_confirmed=True)))
    assert len(_live_tails(conn, si1)) == 2
    assert len(_live_tails(conn, si2)) == 2

    inv2_before = _ple_snapshot(conn, si2)

    result = call_action(mod.cancel_sales_invoice, conn, ns(
        sales_invoice_id=si1, user_confirmed=True))
    assert is_ok(result), result

    assert _live_tails(conn, si1) == []
    assert _ple_snapshot(conn, si2) == inv2_before
    assert len(_live_tails(conn, si2)) == 2

    assert conn.execute(
        "SELECT status FROM sales_invoice WHERE id = ?",
        (si1,)).fetchone()[0] == "cancelled"
    si2_row = conn.execute(
        "SELECT status, outstanding_amount FROM sales_invoice WHERE id = ?",
        (si2,)).fetchone()
    assert si2_row["status"] == "submitted"
    assert D(si2_row["outstanding_amount"]) == D("500.00")


def test_submitted_payment_rows_untouched(conn, env):
    """An invoice backed by a live payment cancels via the release, not tails.

    The tail close only delinks cancelled payments' rows, so a submitted
    payment's legs must be closed by the release (delink + mirror, allocation
    delinked, residual back) with nothing left for the tail close to do.
    """
    si_id = _sales_invoice(conn, env)
    pe_id = _receive_payment(conn, env, "300.00", [
        {"voucher_type": "sales_invoice", "voucher_id": si_id,
         "allocated_amount": "300.00"}])

    ple_before = _ple_snapshot(conn, si_id)
    count_before = conn.execute(
        "SELECT COUNT(*) FROM payment_ledger_entry").fetchone()[0]

    result = call_action(mod.cancel_sales_invoice, conn, ns(
        sales_invoice_id=si_id, user_confirmed=True))
    if is_error(result):
        assert "Cannot cancel" in result.get("message", ""), result
        assert _ple_snapshot(conn, si_id) == ple_before
        assert conn.execute(
            "SELECT COUNT(*) FROM payment_ledger_entry").fetchone()[0] \
            == count_before
        return

    assert is_ok(result), result
    released = result.get("allocations_released", [])
    assert [entry["payment_entry_id"] for entry in released] == [pe_id]
    assert result.get("allocations_release_skipped", []) == []

    assert conn.execute(
        "SELECT status FROM payment_entry WHERE id = ?",
        (pe_id,)).fetchone()[0] == "submitted"

    allocs = conn.execute(
        "SELECT delinked FROM payment_allocation "
        "WHERE payment_entry_id = ? ORDER BY id", (pe_id,)).fetchall()
    assert len(allocs) == 1
    assert allocs[0]["delinked"] == 1

    legs = conn.execute(
        "SELECT amount, delinked FROM payment_ledger_entry "
        "WHERE voucher_type = 'payment_entry' AND voucher_id = ? "
        "AND against_voucher_id = ? ORDER BY id",
        (pe_id, si_id)).fetchall()
    assert len(legs) == 2
    assert [leg["delinked"] for leg in legs] == [1, 1]
    assert sum((D(leg["amount"]) for leg in legs), D("0")) == D("0")

    assert _live_tails(conn, si_id) == []

    from erpclaw_lib.payment_clearing import close_dead_payment_tails
    assert close_dead_payment_tails(conn, "sales_invoice", si_id) == 0
