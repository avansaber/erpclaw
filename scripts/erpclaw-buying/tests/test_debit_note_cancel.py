"""Cancelling a debit note reverses the note's own ledger rows (m693).

Before the fix, ``cancel_purchase_invoice`` hard-coded
``voucher_type="purchase_invoice"`` for the GL reversal, the stock reversal,
the allocation release and the payment-ledger delink. Cancelling a submitted
debit note (whose rows carry ``debit_note``) therefore reversed nothing: the
``ValueError`` was swallowed, ``gl_reversals`` came back 0, and the note's
ledger rows stayed live while the document said cancelled.

Scenario and helpers are reused from
``test_debit_note_invoice_update_je_delete.py`` in this directory: service
items only, fixed dates (bill 2026-06-20, note 2026-06-25).
"""
import os
import sys
from decimal import Decimal

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from buying_helpers import call_action, is_error, is_ok, load_db_query, ns  # noqa: E402
from test_debit_note_invoice_update_je_delete import (  # noqa: E402
    _bill,
    _debit_note,
    _gl,
    _row,
    _setup,
    _submit_bill,
    _submitted_original,
)

from erpclaw_lib.gl_posting import reverse_gl_entries  # noqa: E402

B = load_db_query()


def _submit_note(conn, env, pi_id):
    dn_id = _debit_note(conn, env, pi_id)["debit_note_id"]
    r = call_action(B.submit_purchase_invoice, conn, ns(purchase_invoice_id=dn_id))
    assert is_ok(r), r
    assert r["voucher_type"] == "debit_note"
    return dn_id


def _per_account_net_zero(rows):
    by_account = {}
    for g in rows:
        by_account.setdefault(g["account_id"], []).append(g)
    for legs in by_account.values():
        net = sum((Decimal(g["debit"]) - Decimal(g["credit"]) for g in legs),
                  Decimal("0"))
        assert str(net.quantize(Decimal("0.00"))) == "0.00"


def test_cancel_submitted_debit_note_reverses_its_own_ledger(conn, env):
    env = _setup(conn, env)
    pi_id = _submitted_original(conn, env)
    dn_id = _submit_note(conn, env, pi_id)
    assert len(_gl(conn, "debit_note", dn_id)) == 3

    r = call_action(B.cancel_purchase_invoice, conn, ns(purchase_invoice_id=dn_id))
    assert is_ok(r), r
    assert r["document_status"] == "cancelled"
    assert r["gl_reversals"] == 3

    rows = _gl(conn, "debit_note", dn_id)
    assert len(rows) == 6
    _per_account_net_zero(rows)

    ap_rows = conn.execute(
        "SELECT debit, credit FROM gl_entry WHERE account_id = ? AND party_id = ? "
        "AND is_cancelled = 0", (env["ap"], env["supplier"])).fetchall()
    ap_net = sum((Decimal(g["credit"]) - Decimal(g["debit"]) for g in ap_rows),
                 Decimal("0"))
    assert str(ap_net.quantize(Decimal("0.00"))) == "1250.00"

    note_ple = dict(conn.execute(
        "SELECT delinked, amount FROM payment_ledger_entry "
        "WHERE voucher_type = ? AND voucher_id = ?",
        ("debit_note", dn_id)).fetchone())
    assert note_ple["delinked"] == 1
    bill_ple = dict(conn.execute(
        "SELECT delinked, amount FROM payment_ledger_entry "
        "WHERE voucher_type = ? AND voucher_id = ?",
        ("purchase_invoice", pi_id)).fetchone())
    assert bill_ple["delinked"] == 0
    assert bill_ple["amount"] == "1250.00"

    assert _row(conn, "purchase_invoice", dn_id)["status"] == "cancelled"
    orig = _row(conn, "purchase_invoice", pi_id)
    assert orig["status"] == "submitted"
    assert orig["outstanding_amount"] == "1250.00"

    audit_rows = conn.execute(
        "SELECT action FROM audit_log WHERE entity_id = ? AND action = ?",
        (dn_id, "cancel-debit-note")).fetchall()
    assert len(audit_rows) == 1


def test_cancel_ordinary_bill_unchanged(conn, env):
    env = _setup(conn, env)
    pi_id = _submitted_original(conn, env)

    r = call_action(B.cancel_purchase_invoice, conn, ns(purchase_invoice_id=pi_id))
    assert is_ok(r), r
    assert r["document_status"] == "cancelled"
    assert r["gl_reversals"] == 3

    rows = _gl(conn, "purchase_invoice", pi_id)
    assert len(rows) == 6
    _per_account_net_zero(rows)

    audit_rows = conn.execute(
        "SELECT action FROM audit_log WHERE entity_id = ? AND action = ?",
        (pi_id, "cancel-purchase-invoice")).fetchall()
    assert len(audit_rows) == 1

    ple = dict(conn.execute(
        "SELECT delinked FROM payment_ledger_entry "
        "WHERE voucher_type = ? AND voucher_id = ?",
        ("purchase_invoice", pi_id)).fetchone())
    assert ple["delinked"] == 1


def test_cancel_refuses_when_gl_already_reversed(conn, env):
    env = _setup(conn, env)
    pi_id = _submitted_original(conn, env)
    bill = _row(conn, "purchase_invoice", pi_id)
    reverse_gl_entries(conn, voucher_type="purchase_invoice", voucher_id=pi_id,
                       posting_date=bill["posting_date"])
    conn.commit()
    audit_before = conn.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()["n"]

    r = call_action(B.cancel_purchase_invoice, conn, ns(purchase_invoice_id=pi_id))
    assert is_error(r), r
    assert r["message"].startswith("GL reversal failed: ")

    assert _row(conn, "purchase_invoice", pi_id)["status"] == "submitted"
    ple = dict(conn.execute(
        "SELECT delinked FROM payment_ledger_entry "
        "WHERE voucher_type = ? AND voucher_id = ?",
        ("purchase_invoice", pi_id)).fetchone())
    assert ple["delinked"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()["n"] == audit_before


def test_cancel_zero_total_bill_succeeds(conn, env):
    env = _setup(conn, env)
    pi_id = _bill(conn, env, [("svc1", "0.001", "1.00")])
    _submit_bill(conn, pi_id)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM gl_entry WHERE voucher_id = ?",
        (pi_id,)).fetchone()["n"] == 0

    r = call_action(B.cancel_purchase_invoice, conn, ns(purchase_invoice_id=pi_id))
    assert is_ok(r), r
    assert r["gl_reversals"] == 0
    assert r["document_status"] == "cancelled"
