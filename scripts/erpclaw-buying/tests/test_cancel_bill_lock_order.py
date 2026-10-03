"""Bill cancel takes the company chain head first (m821)."""
import importlib.util
import json
import os
import subprocess
import sys
import time

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import pytest

from buying_helpers import (
    build_buying_env,
    call_action,
    get_conn,
    is_error,
    is_ok,
    load_db_query,
    ns,
)

from erpclaw_lib.db import get_connection, get_dialect
from erpclaw_lib.query import P, Q, Table
from erpclaw_lib.vendor.pypika.terms import ValueWrapper

_PAYMENTS_TESTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(_TESTS_DIR)), "erpclaw-payments", "tests")
_PROOFS_PATH = os.path.join(_PAYMENTS_TESTS_DIR, "test_chain_lock_proofs.py")
_spec = importlib.util.spec_from_file_location(
    "payments_chain_lock_proofs", _PROOFS_PATH)
proofs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(proofs)

_CAS_PATH = os.path.join(
    _PAYMENTS_TESTS_DIR, "test_payment_edit_and_allocation_compare_and_set.py")
_cas_spec = importlib.util.spec_from_file_location(
    "payments_cas_proxy", _CAS_PATH)
_cas = importlib.util.module_from_spec(_cas_spec)
_cas_spec.loader.exec_module(_cas)

B = load_db_query()

_BUYING_SCRIPT = os.path.join(os.path.dirname(_TESTS_DIR), "db_query.py")
_PAYMENTS_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(_TESTS_DIR)), "erpclaw-payments",
    "db_query.py")


def _load_payments():
    spec = importlib.util.spec_from_file_location(
        "db_query_payments_for_buying_tests", _PAYMENTS_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PAY = _load_payments()

_SNAPSHOT_TABLES = ("gl_entry", "payment_ledger_entry", "payment_allocation",
                    "payment_entry", "stock_ledger_entry")


def _book(conn):
    env = build_buying_env(conn)
    items = json.dumps([{"item_id": env["item1"], "qty": "10",
                         "rate": "100.00",
                         "warehouse_id": env["warehouse"]}])
    created = call_action(B.create_purchase_invoice, conn, ns(
        purchase_order_id=None, purchase_receipt_id=None,
        supplier_id=env["supplier"], company_id=env["company_id"],
        posting_date="2026-06-20", due_date="2026-07-20",
        items=items, tax_template_id=None))
    assert is_ok(created), created
    bill = created["purchase_invoice_id"]
    r = call_action(B.submit_purchase_invoice, conn, ns(
        purchase_invoice_id=bill))
    assert is_ok(r), r
    created_p = call_action(PAY.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="pay",
        posting_date="2026-06-01", party_type="supplier",
        party_id=env["supplier"], paid_from_account=env["cash"],
        paid_to_account=env["ap"], paid_amount="600.00",
        exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None,
        allocations=None, deductions=None))
    assert is_ok(created_p), created_p
    pay = created_p["payment_entry_id"]
    s = call_action(PAY.submit_payment, conn, ns(payment_entry_id=pay))
    assert is_ok(s), s
    a = call_action(PAY.allocate_payment, conn, ns(
        payment_entry_id=pay, voucher_type="purchase_invoice",
        voucher_id=bill, allocated_amount="600.00"))
    assert is_ok(a), a
    conn.commit()
    row = conn.execute(
        "SELECT outstanding_amount, status FROM purchase_invoice WHERE id = ?",
        (bill,)).fetchone()
    assert (row["outstanding_amount"], row["status"]) == (
        "400.00", "partially_paid")
    return env, bill, pay


def _snapshot(conn):
    snap = {}
    for table in _SNAPSHOT_TABLES:
        rows = conn.execute("SELECT * FROM %s ORDER BY id" % table).fetchall()
        snap[table] = sorted(
            json.dumps(dict(r), sort_keys=True, default=str) for r in rows)
    return snap


def _bill_state(conn, bill):
    row = conn.execute(
        "SELECT outstanding_amount, status FROM purchase_invoice WHERE id = ?",
        (bill,)).fetchone()
    return (row["outstanding_amount"], row["status"])


def test_stale_status_under_the_head_is_refused_before_any_write(
        conn, db_path, monkeypatch):
    env, bill, pay = _book(conn)
    before = _snapshot(conn)
    real_head = B.take_chain_heads
    state = {"ran": False}

    def _wrapper(conn_arg, company_ids):
        real_head(conn_arg, company_ids)
        if not state["ran"]:
            state["ran"] = True
            pi_t = Table("purchase_invoice")
            q = (Q.update(pi_t)
                 .set(pi_t.status, ValueWrapper("cancelled"))
                 .where(pi_t.id == P()))
            conn_arg.execute(q.get_sql(), (bill,))
            conn_arg.commit()
            real_head(conn_arg, company_ids)
    monkeypatch.setattr(B, "take_chain_heads", _wrapper)
    real_reverse = B.reverse_gl_entries
    calls = {"n": 0}

    def _counting(conn_arg, **kwargs):
        calls["n"] += 1
        return real_reverse(conn_arg, **kwargs)
    monkeypatch.setattr(B, "reverse_gl_entries", _counting)
    r = call_action(B.cancel_purchase_invoice, conn, ns(
        purchase_invoice_id=bill))
    assert is_error(r), r
    assert r["message"] == (
        "Cannot cancel: invoice is 'cancelled' (must be 'submitted', "
        "'overdue', or 'partially_paid')")
    assert calls["n"] == 0
    assert state["ran"] is True
    if get_dialect() == "postgresql":
        check = get_connection()
    else:
        check = get_conn(db_path)
    try:
        assert _bill_state(check, bill) == ("400.00", "cancelled")
        assert _snapshot(check) == before
        n = check.execute(
            "SELECT COUNT(*) AS n FROM audit_log WHERE entity_id = ? AND action = ?",
            (bill, "cancel-purchase-invoice")).fetchone()["n"]
        assert n == 0
    finally:
        check.close()


def test_cancel_blocks_on_held_chain_head(db_path):
    proofs._pg_only()
    conn = get_connection()
    holder = get_connection()
    try:
        env, bill, pay = _book(conn)
        holder.execute(
            "UPDATE gl_chain_head SET updated_at = updated_at WHERE company_id = ?",
            (env["company_id"],))
        penv = proofs._proc_env(ERPCLAW_PG_LOCK_TIMEOUT="10s")
        proc = subprocess.Popen(
            [sys.executable, _BUYING_SCRIPT,
             "--action", "cancel-purchase-invoice",
             "--purchase-invoice-id", bill],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=penv)
        try:
            try:
                proc.wait(timeout=1.0)
                pytest.fail(
                    "cancel-purchase-invoice should block on the held head")
            except subprocess.TimeoutExpired:
                assert proc.poll() is None
            probe = get_connection()
            try:
                probe.execute("SET lock_timeout = '1s'")
                probe.execute(
                    "UPDATE purchase_invoice SET status = status WHERE id = ?",
                    (bill,))
                probe.execute(
                    "UPDATE payment_entry SET status = status WHERE id = ?",
                    (pay,))
                probe.execute(
                    "UPDATE payment_ledger_entry SET delinked = delinked "
                    "WHERE voucher_type = ? AND voucher_id = ?",
                    ("payment_entry", pay))
                probe.rollback()
            finally:
                probe.close()
        finally:
            try:
                holder.rollback()
            except Exception:
                pass
        out, err = proc.communicate(timeout=12)
        assert proc.returncode == 0, (out, err)
        fresh = get_connection()
        try:
            row = fresh.execute(
                "SELECT status FROM purchase_invoice WHERE id = ?",
                (bill,)).fetchone()
            assert row["status"] == "cancelled"
        finally:
            fresh.close()
    finally:
        try:
            holder.rollback()
        except Exception:
            pass
        holder.close()
        conn.close()


def test_cancel_bill_and_cancel_payment_do_not_deadlock(db_path):
    proofs._pg_only()
    conn = get_connection()
    try:
        for _round in range(5):
            env, bill, pay = _book(conn)
            penv = proofs._proc_env()
            t0 = time.monotonic()
            p_pay = subprocess.Popen(
                [sys.executable, _PAYMENTS_SCRIPT,
                 "--action", "cancel-payment",
                 "--payment-entry-id", pay],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env=penv)
            t1 = time.monotonic()
            p_bill = subprocess.Popen(
                [sys.executable, _BUYING_SCRIPT,
                 "--action", "cancel-purchase-invoice",
                 "--purchase-invoice-id", bill],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env=penv)
            t2 = time.monotonic()
            assert (t2 - t1) < 0.5
            out_pay, err_pay = p_pay.communicate(timeout=15)
            out_bill, err_bill = p_bill.communicate(timeout=15)
            assert p_pay.returncode == 0, (out_pay, err_pay)
            assert p_bill.returncode == 0, (out_bill, err_bill)
            assert "deadlock" not in (out_pay + err_pay).lower()
            assert "deadlock" not in (out_bill + err_bill).lower()
            fresh = get_connection()
            try:
                b = fresh.execute(
                    "SELECT status FROM purchase_invoice WHERE id = ?",
                    (bill,)).fetchone()
                assert b["status"] == "cancelled"
                p = fresh.execute(
                    "SELECT status FROM payment_entry WHERE id = ?",
                    (pay,)).fetchone()
                assert p["status"] == "cancelled"
                proofs._assert_chain_intact(fresh, env["company_id"])
                proofs._assert_contiguous(fresh, env["company_id"])
            finally:
                fresh.close()
    finally:
        conn.close()


def test_final_compare_and_set_refuses_a_bill_paid_mid_cancel(
        conn, monkeypatch):
    proofs._pg_only()
    env, bill, pay = _book(conn)
    before = _snapshot(conn)
    import erpclaw_lib.payment_clearing as clearing
    real_release = clearing.release_allocations_on_document

    def _wrapper(conn_arg, voucher_type, voucher_id):
        result = real_release(conn_arg, voucher_type, voucher_id)
        other = get_connection()
        try:
            pi_t = Table("purchase_invoice")
            q = (Q.update(pi_t)
                 .set(pi_t.status, ValueWrapper("paid"))
                 .where(pi_t.id == P()))
            other.execute(q.get_sql(), (bill,))
            other.commit()
        finally:
            other.close()
        return result
    monkeypatch.setattr(clearing, "release_allocations_on_document", _wrapper)
    r = call_action(B.cancel_purchase_invoice, conn, ns(
        purchase_invoice_id=bill))
    assert is_error(r), r
    assert r["message"] == (
        "Cannot cancel: invoice is 'paid' (must be 'submitted', "
        "'overdue', or 'partially_paid')")
    check = get_connection()
    try:
        assert _bill_state(check, bill) == ("400.00", "paid")
        assert _snapshot(check) == before
    finally:
        check.close()


def test_bill_cancel_takes_the_head_first_and_locks_the_bill_last(conn):
    env, bill, pay = _book(conn)
    proxy = _cas._RecordingProxy(conn)
    r = call_action(B.cancel_purchase_invoice, proxy, ns(
        purchase_invoice_id=bill))
    assert is_ok(r), r
    writes = [s for s in proxy.statements
              if s.lstrip()[:6].upper() in ("INSERT", "UPDATE", "DELETE")]
    assert writes, proxy.statements[:5]
    assert "gl_chain_head" in writes[0]
    assert writes[0].lstrip().split(None, 1)[0].upper() == "INSERT"
    first_pay = None
    first_pi = None
    for idx, stmt in enumerate(writes):
        if first_pay is None and "payment_entry" in stmt and stmt.lstrip().upper().startswith("UPDATE"):
            first_pay = idx
        if first_pi is None and "purchase_invoice" in stmt and stmt.lstrip().upper().startswith("UPDATE"):
            first_pi = idx
    assert first_pay is not None, writes
    assert first_pi is not None, writes
    assert first_pi > first_pay, writes
