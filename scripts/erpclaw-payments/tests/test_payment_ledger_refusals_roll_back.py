"""Late payment refusals leave nothing pending on the connection."""
import json

import pytest

from payments_helpers import (
    build_ar_env, call_action, is_error, is_ok, load_db_query, ns,
    seed_sales_invoice,
)

mod = load_db_query()


@pytest.fixture
def env(conn):
    return build_ar_env(conn)


def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _pin(conn, pe_id, company_id):
    conn.execute(
        "UPDATE payment_entry SET updated_at = ? WHERE id = ?",
        ("2000-01-01 00:00:00", pe_id))
    conn.execute(
        "UPDATE gl_chain_head SET updated_at = ? WHERE company_id = ?",
        ("2000-01-01 00:00:00", company_id))
    conn.commit()


def _snapshot(conn, pe_id, si_id=None):
    snap = {}
    snap["pe"] = _rows(
        conn, "SELECT * FROM payment_entry WHERE id = ?", (pe_id,))
    snap["gl"] = _rows(conn, "SELECT * FROM gl_entry ORDER BY id")
    snap["ple"] = _rows(
        conn, "SELECT * FROM payment_ledger_entry ORDER BY id")
    snap["head"] = _rows(
        conn, "SELECT * FROM gl_chain_head ORDER BY company_id")
    snap["audit"] = _rows(conn, "SELECT * FROM audit_log ORDER BY id")
    if si_id is not None:
        snap["inv"] = _rows(
            conn,
            "SELECT outstanding_amount, status FROM sales_invoice WHERE id = ?",
            (si_id,))
    return snap


def _draft_receive(conn, env, si_id, paid="1000.00", alloc="1000.00"):
    created = call_action(mod.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="receive",
        posting_date="2026-06-01", party_type="customer",
        party_id=env["customer"], paid_from_account=env["ar"],
        paid_to_account=env["bank"], paid_amount=paid,
        exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None,
        allocations=json.dumps([{
            "voucher_type": "sales_invoice", "voucher_id": si_id,
            "allocated_amount": alloc}]),
        deductions=None))
    assert is_ok(created), created
    return created["payment_entry_id"]


def test_submit_gl_failure_leaves_nothing(conn, env, monkeypatch):
    si = seed_sales_invoice(conn, env, "1000.00")
    pe = _draft_receive(conn, env, si)
    _pin(conn, pe, env["company_id"])
    before = _snapshot(conn, pe, si)
    try:
        def _boom(*a, **k):
            raise ValueError("boom")
        monkeypatch.setattr(mod, "insert_gl_entries", _boom)
        r = call_action(mod.submit_payment, conn, ns(payment_entry_id=pe))
        assert is_error(r), r
        assert r["message"] == "GL posting failed: boom"
        assert _snapshot(conn, pe, si) == before
    finally:
        conn.rollback()


def test_cancel_gl_reversal_failure_leaves_nothing(conn, env, monkeypatch):
    si = seed_sales_invoice(conn, env, "1000.00")
    pe = _draft_receive(conn, env, si)
    s = call_action(mod.submit_payment, conn, ns(payment_entry_id=pe))
    assert is_ok(s), s
    _pin(conn, pe, env["company_id"])
    before = _snapshot(conn, pe, si)
    try:
        def _boom(*a, **k):
            raise ValueError("boom")
        monkeypatch.setattr(mod, "reverse_gl_entries", _boom)
        r = call_action(mod.cancel_payment, conn, ns(payment_entry_id=pe))
        assert is_error(r), r
        assert r["message"] == "GL reversal failed: boom"
        assert _snapshot(conn, pe, si) == before
    finally:
        conn.rollback()


def test_cancel_document_reversal_failure_leaves_nothing(
        conn, env, monkeypatch):
    si = seed_sales_invoice(conn, env, "1000.00")
    pe = _draft_receive(conn, env, si)
    s = call_action(mod.submit_payment, conn, ns(payment_entry_id=pe))
    assert is_ok(s), s
    _pin(conn, pe, env["company_id"])
    before = _snapshot(conn, pe, si)
    try:
        def _boom(*a, **k):
            raise ValueError("boom")
        monkeypatch.setattr(mod, "reverse_payment_on_document", _boom)
        r = call_action(mod.cancel_payment, conn, ns(payment_entry_id=pe))
        assert is_error(r), r
        assert r["message"] == (
            "Payment reversal on sales_invoice %s failed: boom" % si)
        assert _snapshot(conn, pe, si) == before
    finally:
        conn.rollback()


def test_submit_group_account_refusal_leaves_nothing(conn, env):
    si = seed_sales_invoice(conn, env, "1000.00")
    pe = _draft_receive(conn, env, si)
    conn.execute(
        "UPDATE account SET is_group = 1 WHERE id = ?", (env["bank"],))
    conn.commit()
    row = conn.execute(
        "SELECT name FROM account WHERE id = ?", (env["bank"],)).fetchone()
    name = row["name"]
    _pin(conn, pe, env["company_id"])
    before = _snapshot(conn, pe, si)
    try:
        r = call_action(mod.submit_payment, conn, ns(payment_entry_id=pe))
        assert is_error(r), r
        assert r["message"] == (
            "Account '%s' (paid-to-account) is a group account with no "
            "leaf children. Please create a child account under it first."
            % name)
        assert _snapshot(conn, pe, si) == before
    finally:
        conn.rollback()


def test_submit_currency_mismatch_refusal_leaves_nothing(conn, env):
    si = seed_sales_invoice(conn, env, "1000.00")
    pe = _draft_receive(conn, env, si)
    conn.execute(
        "UPDATE sales_invoice SET currency = ? WHERE id = ?", ("EUR", si))
    conn.commit()
    _pin(conn, pe, env["company_id"])
    before = _snapshot(conn, pe, si)
    try:
        r = call_action(mod.submit_payment, conn, ns(payment_entry_id=pe))
        assert is_error(r), r
        assert r["message"] == (
            "currency mismatch: invoice in EUR, payment in USD; "
            "invoice currency must equal payment currency")
        assert _snapshot(conn, pe, si) == before
    finally:
        conn.rollback()
