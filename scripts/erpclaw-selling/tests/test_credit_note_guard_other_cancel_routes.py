"""Credit-note guard on the other cancel routes (m783d).

``cancel-sales-invoice`` already refuses an ordinary invoice that a live
credit note names. This file proves the same rule on
``cancel-intercompany-invoice`` (same refusal text, nothing written) and
pins the ordinary route's message after the helper refactor. A live note
is ``is_return = 1`` with ``status NOT IN ('draft', 'cancelled')`` --
a partly refunded (``partially_paid``) note still blocks.

Money is text throughout: ``Decimal`` in Python, exact string comparisons.
"""
import os
import sys

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from selling_helpers import (  # noqa: E402
    call_action,
    is_error,
    is_ok,
    load_db_query,
    ns,
)

import test_cancel_intercompany_invoice_behaviour as icb  # noqa: E402
import test_credit_note_original_cancel as occ  # noqa: E402

mod = load_db_query()


def _snapshot(conn):
    snap = {}
    for tbl in ("sales_invoice", "purchase_invoice", "payment_entry",
                "payment_allocation"):
        snap[tbl] = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM " + tbl + " ORDER BY id").fetchall()
        ]
    snap["counts"] = {}
    for tbl in ("gl_entry", "stock_ledger_entry", "payment_ledger_entry",
                "audit_log"):
        snap["counts"][tbl] = conn.execute(
            "SELECT COUNT(*) FROM " + tbl).fetchone()[0]
    return snap


def _intercompany_pair(conn, db_path, env):
    icb._ensure_intercompany_linkage(conn, db_path)
    menv = icb._mirror_env(conn)
    si_id = icb._submit_si(conn, env)
    pi_id = icb._submit_mirror(conn, si_id, menv, env["item1"])
    return si_id, pi_id


def test_intercompany_cancel_refuses_under_a_live_credit_note(
        conn, db_path, env):
    si_id, pi_id = _intercompany_pair(conn, db_path, env)
    cn = occ._credit_note(conn, env, si_id, "2")
    assert is_ok(occ._submit(conn, cn)), cn
    naming = occ._naming(conn, cn)
    snap = _snapshot(conn)
    res = call_action(mod.cancel_intercompany_invoice, conn,
                      ns(sales_invoice_id=si_id))
    assert is_error(res), res
    assert res["message"] == (
        f"Cannot cancel: sales invoice {si_id} has credit note {naming} "
        f"('submitted'); cancel credit note {naming} first"
    )
    assert _snapshot(conn) == snap
    assert icb._doc(conn, "purchase_invoice", pi_id)["status"] == "submitted"
    c = call_action(mod.cancel_sales_invoice, conn, ns(sales_invoice_id=cn))
    assert is_ok(c), c
    res2 = call_action(mod.cancel_intercompany_invoice, conn,
                       ns(sales_invoice_id=si_id))
    assert is_ok(res2), res2
    assert icb._doc(conn, "sales_invoice", si_id)["status"] == "cancelled"


def test_intercompany_cancel_refuses_under_a_partially_paid_note(
        conn, db_path, env):
    si_id, _pi_id = _intercompany_pair(conn, db_path, env)
    pe = occ._receive(conn, env, si_id, "1000.00")
    cn = occ._credit_note(conn, env, si_id, "2")
    assert is_ok(occ._submit(conn, cn)), cn
    c = call_action(occ.pay.cancel_payment, conn, ns(payment_entry_id=pe))
    assert is_ok(c), c
    occ._refund(conn, env, cn, "60.00")
    row = conn.execute(
        "SELECT status, naming_series FROM sales_invoice WHERE id = ?",
        (cn,)).fetchone()
    assert row["status"] == "partially_paid"
    naming = row["naming_series"] or cn
    snap = _snapshot(conn)
    res = call_action(mod.cancel_intercompany_invoice, conn,
                      ns(sales_invoice_id=si_id))
    assert is_error(res), res
    assert res["message"] == (
        f"Cannot cancel: sales invoice {si_id} has credit note {naming} "
        f"('partially_paid'); cancel credit note {naming} first"
    )
    assert _snapshot(conn) == snap


def test_intercompany_cancel_refuses_note_submitted_mid_cancel(
        conn, db_path, env, monkeypatch):
    """A note submitted between the head take and the re-read still blocks.

    The draft note exists at the pre-head read (no refusal), is submitted
    inside the head acquisition, and the under-head re-read must refuse
    with the ordinary message before any GL reversal is written.
    """
    si_id, pi_id = _intercompany_pair(conn, db_path, env)
    cn = occ._credit_note(conn, env, si_id, "2")
    real_take = mod.take_chain_heads
    take_calls = {"n": 0}

    def _take_then_submit(conn_, company_ids):
        take_calls["n"] += 1
        if take_calls["n"] == 1:
            r = occ._submit(conn_, cn)
            assert is_ok(r), r
        return real_take(conn_, company_ids)

    monkeypatch.setattr(mod, "take_chain_heads", _take_then_submit)
    real_reverse = mod.reverse_gl_entries
    reversals = {"n": 0}

    def _counting_reverse(*a, **k):
        reversals["n"] += 1
        return real_reverse(*a, **k)

    monkeypatch.setattr(mod, "reverse_gl_entries", _counting_reverse)
    res = call_action(mod.cancel_intercompany_invoice, conn,
                      ns(sales_invoice_id=si_id))
    naming = occ._naming(conn, cn)
    assert is_error(res), res
    assert res["message"] == (
        f"Cannot cancel: sales invoice {si_id} has credit note {naming} "
        f"('submitted'); cancel credit note {naming} first"
    )
    assert reversals["n"] == 0
    assert icb._doc(conn, "sales_invoice", si_id)["status"] == "partially_paid"
    assert icb._doc(conn, "purchase_invoice", pi_id)["status"] == "submitted"


def test_intercompany_cancel_ignores_draft_and_cancelled_notes(
        conn, db_path, env):
    si_id, _pi_id = _intercompany_pair(conn, db_path, env)
    occ._credit_note(conn, env, si_id, "1")
    live = occ._credit_note(conn, env, si_id, "2")
    assert is_ok(occ._submit(conn, live)), live
    c = call_action(mod.cancel_sales_invoice, conn, ns(sales_invoice_id=live))
    assert is_ok(c), c
    res = call_action(mod.cancel_intercompany_invoice, conn,
                      ns(sales_invoice_id=si_id))
    assert is_ok(res), res
    assert icb._doc(conn, "sales_invoice", si_id)["status"] == "cancelled"


def test_ordinary_cancel_message_unchanged(conn, env):
    inv = occ._invoice(conn, env, "10")
    cn = occ._credit_note(conn, env, inv, "2")
    assert is_ok(occ._submit(conn, cn)), cn
    naming = occ._naming(conn, cn)
    res = call_action(mod.cancel_sales_invoice, conn, ns(sales_invoice_id=inv))
    assert is_error(res), res
    assert res["message"] == (
        f"Cannot cancel: sales invoice {inv} has credit note {naming} "
        f"('submitted'); cancel credit note {naming} first"
    )
