"""Invoice clearing takes the row lock first and writes compare-and-set.

Two payments clearing one invoice at the same time must both count: the
library locks the invoice row before reading its outstanding, and the write
carries the outstanding it read so a stale decision writes nothing.
"""
import json
import os
import sys
import threading
import uuid

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import pytest

from payments_helpers import (build_ap_env, build_ar_env, call_action, is_error,
                              is_ok, load_db_query, ns, seed_purchase_invoice,
                              seed_sales_invoice)

from erpclaw_lib import payment_clearing
from erpclaw_lib.db import get_connection, get_dialect

mod = load_db_query()


@pytest.fixture
def env(conn):
    return build_ar_env(conn)


def _receive_submitted(conn, env, amount):
    created = call_action(mod.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="receive",
        posting_date="2026-06-01", party_type="customer",
        party_id=env["customer"], paid_from_account=env["ar"],
        paid_to_account=env["bank"], paid_amount=str(amount),
        exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None,
        allocations=None, deductions=None))
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    s = call_action(mod.submit_payment, conn, ns(payment_entry_id=pe_id))
    assert is_ok(s), s
    return pe_id


def _invoice_state(conn, voucher_id):
    return conn.execute(
        "SELECT outstanding_amount, status FROM sales_invoice WHERE id = ?",
        (voucher_id,)).fetchone()


def test_apply_against_a_stale_outstanding_writes_nothing(conn, env, monkeypatch):
    si = seed_sales_invoice(conn, env, "1000.00")
    conn.execute(
        "UPDATE sales_invoice SET outstanding_amount = ?, status = ? "
        "WHERE id = ?", ("700.00", "partially_paid", si))
    conn.commit()
    real_read = payment_clearing._read_doc
    calls = {"n": 0}

    def _stale(conn_, voucher_type, voucher_id, columns):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"outstanding_amount": "1000.00", "status": "submitted"}
        return real_read(conn_, voucher_type, voucher_id, columns)

    monkeypatch.setattr(payment_clearing, "_read_doc", _stale)
    with pytest.raises(ValueError) as excinfo:
        payment_clearing.apply_payment_to_document(
            conn, "sales_invoice", si, "200.00")
    assert str(excinfo.value) == (
        "sales_invoice %s changed while the payment was being applied; "
        "nothing was written, retry" % si)
    conn.rollback()
    row = _invoice_state(conn, si)
    assert (row["outstanding_amount"], row["status"]) == (
        "700.00", "partially_paid")


def test_reverse_against_a_stale_outstanding_writes_nothing(conn, env, monkeypatch):
    si = seed_sales_invoice(conn, env, "1000.00")
    conn.execute(
        "UPDATE sales_invoice SET outstanding_amount = ?, status = ? "
        "WHERE id = ?", ("500.00", "partially_paid", si))
    conn.commit()
    real_read = payment_clearing._read_doc
    calls = {"n": 0}

    def _stale(conn_, voucher_type, voucher_id, columns):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"outstanding_amount": "700.00"}
        return real_read(conn_, voucher_type, voucher_id, columns)

    monkeypatch.setattr(payment_clearing, "_read_doc", _stale)
    with pytest.raises(ValueError) as excinfo:
        payment_clearing.reverse_payment_on_document(
            conn, "sales_invoice", si, "200.00", "1000.00")
    assert str(excinfo.value) == (
        "sales_invoice %s changed while the payment was being reversed; "
        "nothing was written, retry" % si)
    conn.rollback()
    assert _invoice_state(conn, si)["outstanding_amount"] == "500.00"


def test_apply_of_a_missing_invoice_is_not_found(conn, env):
    missing = str(uuid.uuid4())
    with pytest.raises(ValueError) as excinfo:
        payment_clearing.apply_payment_to_document(
            conn, "sales_invoice", missing, "200.00")
    assert str(excinfo.value) == "sales_invoice %s not found" % missing
    with pytest.raises(ValueError) as excinfo:
        payment_clearing.reverse_payment_on_document(
            conn, "sales_invoice", missing, "200.00", "1000.00")
    assert str(excinfo.value) == "sales_invoice %s not found" % missing


def test_the_invoice_row_is_locked_before_it_is_read(conn, env, monkeypatch):
    si = seed_sales_invoice(conn, env, "1000.00")
    real_lock = payment_clearing._lock_doc
    real_read = payment_clearing._read_doc
    order = []

    def _spy_lock(conn_, voucher_type, voucher_id):
        order.append("lock")
        return real_lock(conn_, voucher_type, voucher_id)

    def _spy_read(conn_, voucher_type, voucher_id, columns):
        order.append("read")
        return real_read(conn_, voucher_type, voucher_id, columns)

    monkeypatch.setattr(payment_clearing, "_lock_doc", _spy_lock)
    monkeypatch.setattr(payment_clearing, "_read_doc", _spy_read)
    payment_clearing.apply_payment_to_document(
        conn, "sales_invoice", si, "100.00")
    assert order == ["lock", "read"]
    del order[:]
    payment_clearing.reverse_payment_on_document(
        conn, "sales_invoice", si, "100.00", "1000.00")
    assert order == ["lock", "read"]


def test_uncontended_apply_and_reverse_unchanged(conn, env):
    si = seed_sales_invoice(conn, env, "1000.00")
    first = payment_clearing.apply_payment_to_document(
        conn, "sales_invoice", si, "300.00")
    assert first == {"voucher_type": "sales_invoice", "voucher_id": si,
                     "outstanding_amount": "700.00",
                     "status": "partially_paid", "applied": True}
    second = payment_clearing.apply_payment_to_document(
        conn, "sales_invoice", si, "700.00")
    assert second == {"voucher_type": "sales_invoice", "voucher_id": si,
                      "outstanding_amount": "0", "status": "paid",
                      "applied": True}
    restored = payment_clearing.reverse_payment_on_document(
        conn, "sales_invoice", si, "700.00", "1000.00")
    assert restored == {"voucher_type": "sales_invoice", "voucher_id": si,
                        "outstanding_amount": "700.00",
                        "status": "partially_paid", "applied": True}
    apenv = build_ap_env(conn)
    pi = seed_purchase_invoice(conn, apenv, "1000.00")
    applied = payment_clearing.apply_payment_to_document(
        conn, "purchase_invoice", pi, "300.00")
    assert applied == {"voucher_type": "purchase_invoice", "voucher_id": pi,
                       "outstanding_amount": "700.00",
                       "status": "partially_paid", "applied": True}


def test_two_allocations_to_one_invoice_both_count(conn, env):
    si = seed_sales_invoice(conn, env, "1000.00")
    first_payment = _receive_submitted(conn, env, "300.00")
    second_payment = _receive_submitted(conn, env, "200.00")
    first = call_action(mod.allocate_payment, conn, ns(
        payment_entry_id=first_payment, voucher_type="sales_invoice",
        voucher_id=si, allocated_amount="300.00"))
    assert is_ok(first), first
    second = call_action(mod.allocate_payment, conn, ns(
        payment_entry_id=second_payment, voucher_type="sales_invoice",
        voucher_id=si, allocated_amount="200.00"))
    assert is_ok(second), second
    row = _invoice_state(conn, si)
    assert row["outstanding_amount"] == "500.00"
    assert row["status"] == "partially_paid"


def test_concurrent_applications_to_one_invoice_both_count_on_postgresql(db_path):
    pg_url = os.environ.get("ERPCLAW_PG_TEST_URL")
    if not pg_url:
        pytest.skip("PostgreSQL NOT RUN: ERPCLAW_PG_TEST_URL is not set")
    if get_dialect() != "postgresql":
        pytest.skip("PostgreSQL NOT RUN: dialect is %s" % get_dialect())
    first = get_connection()
    second = get_connection()
    worker = None
    try:
        env = build_ar_env(first)
        si = seed_sales_invoice(first, env, "1000.00")
        first.commit()
        payment_clearing.apply_payment_to_document(
            first, "sales_invoice", si, "300.00")
        errors = []

        def _run_second():
            try:
                payment_clearing.apply_payment_to_document(
                    second, "sales_invoice", si, "200.00")
                second.commit()
            except Exception as exc:  # noqa: BLE001 - reported to the test
                errors.append(exc)

        worker = threading.Thread(target=_run_second, daemon=True)
        worker.start()
        worker.join(1.0)
        assert worker.is_alive(), "second clearer waits on the invoice row lock"
        first.commit()
        worker.join(10)
        assert not worker.is_alive(), "second clearer finishes after the commit"
        assert not errors, errors
        reader = get_connection()
        try:
            outstanding = reader.execute(
                "SELECT outstanding_amount FROM sales_invoice WHERE id = ?",
                (si,)).fetchone()[0]
        finally:
            reader.close()
        assert outstanding == "500.00"
    finally:
        try:
            first.rollback()
        except Exception:  # noqa: BLE001, S110 - best effort release
            pass
        try:
            first.close()
        except Exception:  # noqa: BLE001, S110 - best effort release
            pass
        if worker is not None:
            worker.join(10)
        try:
            second.close()
        except Exception:  # noqa: BLE001, S110 - best effort release
            pass
