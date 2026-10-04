"""An invoice cannot be cancelled under a live credit note; partly refunded notes count as credit."""
import importlib.util
import json
import os
from decimal import Decimal

from selling_helpers import (
    call_action,
    ns,
    is_ok,
    is_error,
    load_db_query,
    seed_account,
)

mod = load_db_query()

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(os.path.dirname(_TESTS_DIR))


def _load(name, rel_path):
    path = os.path.join(_SCRIPTS_DIR, rel_path)
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


pay = _load("db_query_payments_occ", "erpclaw-payments/db_query.py")


def _invoice(conn, env, qty="10"):
    create = call_action(
        mod.create_sales_invoice,
        conn,
        ns(
            sales_order_id=None,
            delivery_note_id=None,
            customer_id=env["customer"],
            company_id=env["company_id"],
            posting_date="2026-06-20",
            due_date="2026-07-20",
            items=json.dumps(
                [{"item_id": env["item1"], "qty": qty, "rate": "100.00",
                  "warehouse_id": env["warehouse"]}]
            ),
            tax_template_id=None,
            payment_terms_id=None,
        ),
    )
    assert is_ok(create), create
    si_id = create["sales_invoice_id"]
    r = call_action(mod.submit_sales_invoice, conn, ns(sales_invoice_id=si_id))
    assert is_ok(r), r
    return si_id


def _credit_note(conn, env, inv, qty):
    cn = call_action(
        mod.create_credit_note,
        conn,
        ns(
            against_invoice_id=inv,
            reason="Returned goods",
            posting_date="2026-06-28",
            items=json.dumps([{"item_id": env["item1"], "qty": qty, "rate": "100.00"}]),
        ),
    )
    assert is_ok(cn), cn
    return cn["credit_note_id"]


def _submit(conn, id):
    return call_action(mod.submit_sales_invoice, conn, ns(sales_invoice_id=id))


def _receive(conn, env, inv, amount):
    created = call_action(
        pay.add_payment,
        conn,
        ns(
            company_id=env["company_id"],
            payment_type="receive",
            posting_date="2026-06-25",
            party_type="customer",
            party_id=env["customer"],
            paid_from_account=env["ar"],
            paid_to_account=env["cash"],
            paid_amount=amount,
            exchange_rate=None,
            payment_currency=None,
            reference_number=None,
            reference_date=None,
            allocations=json.dumps(
                [{"voucher_type": "sales_invoice", "voucher_id": inv,
                  "allocated_amount": amount}]
            ),
            deductions=None,
        ),
    )
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    s = call_action(pay.submit_payment, conn, ns(payment_entry_id=pe_id))
    assert is_ok(s), s
    return pe_id


def _refund(conn, env, cn, amount):
    if "bank" not in env:
        env["bank"] = seed_account(
            conn, env["company_id"], "Bank", "asset", "bank", "1010")
    created = call_action(
        pay.add_payment,
        conn,
        ns(
            company_id=env["company_id"],
            payment_type="pay",
            posting_date="2026-06-29",
            party_type="customer",
            party_id=env["customer"],
            paid_from_account=env["bank"],
            paid_to_account=env["ar"],
            paid_amount=amount,
            exchange_rate=None,
            payment_currency=None,
            reference_number=None,
            reference_date=None,
            allocations=json.dumps(
                [{"voucher_type": "credit_note", "voucher_id": cn,
                  "allocated_amount": amount}]
            ),
            deductions=None,
        ),
    )
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    s = call_action(pay.submit_payment, conn, ns(payment_entry_id=pe_id))
    assert is_ok(s), s
    return pe_id


def _doc(conn, id):
    r = conn.execute(
        "SELECT outstanding_amount, status FROM sales_invoice WHERE id = ?", (id,)
    ).fetchone()
    return (r["outstanding_amount"], r["status"])


def _naming(conn, id):
    r = conn.execute(
        "SELECT naming_series FROM sales_invoice WHERE id = ?", (id,)
    ).fetchone()
    return r["naming_series"] or id


def _snapshot(conn):
    si = [
        dict(r)
        for r in conn.execute("SELECT * FROM sales_invoice ORDER BY id").fetchall()
    ]
    counts = {}
    for tbl in (
        "gl_entry",
        "stock_ledger_entry",
        "payment_ledger_entry",
        "payment_allocation",
        "audit_log",
    ):
        counts[tbl] = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
    return (si, counts)


def test_invoice_with_a_live_credit_note_cannot_be_cancelled(conn, env):
    inv = _invoice(conn, env, "10")
    cn = _credit_note(conn, env, inv, "2")
    r = _submit(conn, cn)
    assert is_ok(r), r
    assert _doc(conn, inv) == ("800.00", "partially_paid")
    naming = _naming(conn, cn)
    snap = _snapshot(conn)
    res = call_action(mod.cancel_sales_invoice, conn, ns(sales_invoice_id=inv))
    assert is_error(res), res
    assert res["message"] == (
        f"Cannot cancel: sales invoice {inv} has credit note {naming} "
        f"('submitted'); cancel credit note {naming} first"
    )
    assert _snapshot(conn) == snap
    c = call_action(mod.cancel_sales_invoice, conn, ns(sales_invoice_id=cn))
    assert is_ok(c), c
    res2 = call_action(mod.cancel_sales_invoice, conn, ns(sales_invoice_id=inv))
    assert is_ok(res2), res2
    assert _doc(conn, inv)[1] == "cancelled"


def test_a_note_that_absorbed_nothing_still_blocks(conn, env):
    inv = _invoice(conn, env, "10")
    pe_id = _receive(conn, env, inv, "1000.00")
    assert _doc(conn, inv) == ("0", "paid")
    cn = _credit_note(conn, env, inv, "2")
    r = _submit(conn, cn)
    assert is_ok(r), r
    assert _doc(conn, cn) == ("-200.00", "submitted")
    c = call_action(pay.cancel_payment, conn, ns(payment_entry_id=pe_id))
    assert is_ok(c), c
    assert _doc(conn, inv) == ("1000.00", "submitted")
    naming = _naming(conn, cn)
    snap = _snapshot(conn)
    res = call_action(mod.cancel_sales_invoice, conn, ns(sales_invoice_id=inv))
    assert is_error(res), res
    assert res["message"] == (
        f"Cannot cancel: sales invoice {inv} has credit note {naming} "
        f"('submitted'); cancel credit note {naming} first"
    )
    assert _snapshot(conn) == snap


def test_a_draft_or_cancelled_note_does_not_block(conn, env):
    inv_a = _invoice(conn, env, "10")
    _credit_note(conn, env, inv_a, "2")
    res_a = call_action(mod.cancel_sales_invoice, conn, ns(sales_invoice_id=inv_a))
    assert is_ok(res_a), res_a
    assert _doc(conn, inv_a)[1] == "cancelled"
    inv_b = _invoice(conn, env, "10")
    cn_b = _credit_note(conn, env, inv_b, "2")
    assert is_ok(_submit(conn, cn_b)), cn_b
    c = call_action(mod.cancel_sales_invoice, conn, ns(sales_invoice_id=cn_b))
    assert is_ok(c), c
    res_b = call_action(mod.cancel_sales_invoice, conn, ns(sales_invoice_id=inv_b))
    assert is_ok(res_b), res_b
    assert _doc(conn, inv_b)[1] == "cancelled"


def test_partly_refunded_note_counts_as_credit(conn, env):
    inv = _invoice(conn, env, "10")
    _receive(conn, env, inv, "900.00")
    cn = _credit_note(conn, env, inv, "2")
    r = _submit(conn, cn)
    assert is_ok(r), r
    assert _doc(conn, inv) == ("0", "paid")
    assert _doc(conn, cn) == ("-100.00", "submitted")
    _refund(conn, env, cn, "60.00")
    assert _doc(conn, cn) == ("-40.00", "partially_paid")
    assert mod._customer_outstanding_ar(conn, env["customer"]) == Decimal("-40.00")
    inv2 = _invoice(conn, env, "1")
    assert mod._customer_outstanding_ar(conn, env["customer"]) == Decimal("60.00")


def test_get_sales_invoice_lists_the_applied_credit(conn, env):
    inv = _invoice(conn, env, "10")
    cn = _credit_note(conn, env, inv, "2")
    r = _submit(conn, cn)
    assert is_ok(r), r
    got_inv = call_action(mod.get_sales_invoice, conn, ns(sales_invoice_id=inv))
    assert is_ok(got_inv), got_inv
    inv_rows = sorted(
        [(p["voucher_type"], p["against_voucher_type"], p["against_voucher_id"], p["amount"])
         for p in got_inv["payments"]],
        key=lambda t: (Decimal(t[3]), t[0], t[1], t[2]),
    )
    assert inv_rows == [
        ("credit_note", "sales_invoice", inv, "-200.00"),
        ("sales_invoice", "sales_invoice", inv, "1000.00"),
    ]
    got_cn = call_action(mod.get_sales_invoice, conn, ns(sales_invoice_id=cn))
    assert is_ok(got_cn), got_cn
    cn_rows = sorted(
        [(p["voucher_type"], p["against_voucher_type"], p["against_voucher_id"], p["amount"])
         for p in got_cn["payments"]],
        key=lambda t: (Decimal(t[3]), t[0], t[1], t[2]),
    )
    assert cn_rows == [
        ("credit_note", "credit_note", cn, "-200.00"),
        ("credit_note", "credit_note", cn, "200.00"),
    ]


def test_cancel_refuses_note_submitted_mid_cancel(conn, env, monkeypatch):
    """The live-note refusal is decided after the chain head is taken.

    A note submitted between the head take and the re-read must still block
    the original's cancel, before any GL reversal is written.
    """
    inv = _invoice(conn, env, "10")
    assert _doc(conn, inv) == ("1000.00", "submitted")
    cn = _credit_note(conn, env, inv, "1")

    real_take = mod.take_chain_heads
    take_calls = {"n": 0}

    def _take_then_submit(conn_, company_ids):
        take_calls["n"] += 1
        if take_calls["n"] == 1:
            r = _submit(conn_, cn)
            assert is_ok(r), r
        return real_take(conn_, company_ids)

    monkeypatch.setattr(mod, "take_chain_heads", _take_then_submit)

    real_reverse = mod.reverse_gl_entries
    reversals = {"n": 0}

    def _counting_reverse(*a, **k):
        reversals["n"] += 1
        return real_reverse(*a, **k)

    monkeypatch.setattr(mod, "reverse_gl_entries", _counting_reverse)

    res = call_action(mod.cancel_sales_invoice, conn, ns(sales_invoice_id=inv))
    naming = _naming(conn, cn)
    assert is_error(res), res
    assert res["message"] == (
        f"Cannot cancel: sales invoice {inv} has credit note {naming} "
        f"('submitted'); cancel credit note {naming} first"
    )
    assert reversals["n"] == 0
    assert _doc(conn, inv) == ("900.00", "partially_paid")
    assert _doc(conn, cn) == ("0", "submitted")
