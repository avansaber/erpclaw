"""Reconcile takes the company chain head first (m821)."""
import json
import os
import subprocess
import sys
import time

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import pytest

import test_chain_lock_proofs as proofs
import test_payment_edit_and_allocation_compare_and_set as cas
from payments_helpers import (
    build_ar_env,
    call_action,
    get_conn,
    is_ok,
    load_db_query,
    ns,
    seed_sales_invoice,
)

from erpclaw_lib.db import get_connection, get_dialect

mod = load_db_query()

_PAYMENTS_SCRIPT = os.path.join(os.path.dirname(_TESTS_DIR), "db_query.py")


def _receive(conn, env, amount):
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


def _book(conn):
    env = build_ar_env(conn)
    conn.commit()
    si = seed_sales_invoice(conn, env, "1000.00")
    conn.commit()
    pe = _receive(conn, env, "600.00")
    conn.commit()
    return env, si, pe


def test_reconcile_blocks_on_held_chain_head(db_path):
    proofs._pg_only()
    conn = get_connection()
    holder = get_connection()
    try:
        env, si, pe = _book(conn)
        holder.execute(
            "UPDATE gl_chain_head SET updated_at = updated_at WHERE company_id = ?",
            (env["company_id"],))
        penv = proofs._proc_env(ERPCLAW_PG_LOCK_TIMEOUT="10s")
        proc = subprocess.Popen(
            [sys.executable, _PAYMENTS_SCRIPT,
             "--action", "reconcile-payments",
             "--party-type", "customer", "--party-id", env["customer"],
             "--company-id", env["company_id"]],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=penv)
        try:
            try:
                proc.wait(timeout=1.0)
                pytest.fail(
                    "reconcile-payments should block on the held head")
            except subprocess.TimeoutExpired:
                assert proc.poll() is None
            probe = get_connection()
            try:
                probe.execute("SET lock_timeout = '1s'")
                probe.execute(
                    "UPDATE payment_entry SET status = status WHERE id = ?",
                    (pe,))
                probe.execute(
                    "UPDATE sales_invoice SET status = status WHERE id = ?",
                    (si,))
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
        payload = json.loads(out)
        assert payload["matched"] == [
            {"payment_id": pe, "voucher_id": si,
             "allocated_amount": "600.00"}], payload
        fresh = get_connection()
        try:
            inv = fresh.execute(
                "SELECT outstanding_amount, status FROM sales_invoice WHERE id = ?",
                (si,)).fetchone()
            assert (inv["outstanding_amount"], inv["status"]) == (
                "400.00", "partially_paid")
            pay = fresh.execute(
                "SELECT unallocated_amount FROM payment_entry WHERE id = ?",
                (pe,)).fetchone()
            assert pay["unallocated_amount"] == "0.00"
        finally:
            fresh.close()
    finally:
        try:
            holder.rollback()
        except Exception:
            pass
        holder.close()
        conn.close()


def test_reconcile_serialises_with_allocate_payment(db_path):
    proofs._pg_only()
    conn = get_connection()
    try:
        for _round in range(5):
            env = build_ar_env(conn)
            conn.commit()
            si = seed_sales_invoice(conn, env, "1000.00")
            conn.commit()
            pe = _receive(conn, env, "600.00")
            conn.commit()
            penv = proofs._proc_env()
            t0 = time.monotonic()
            p_rec = subprocess.Popen(
                [sys.executable, _PAYMENTS_SCRIPT,
                 "--action", "reconcile-payments",
                 "--party-type", "customer", "--party-id", env["customer"],
                 "--company-id", env["company_id"]],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env=penv)
            t1 = time.monotonic()
            p_alloc = subprocess.Popen(
                [sys.executable, _PAYMENTS_SCRIPT,
                 "--action", "allocate-payment",
                 "--payment-entry-id", pe,
                 "--voucher-type", "sales_invoice", "--voucher-id", si,
                 "--allocated-amount", "600.00"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env=penv)
            t2 = time.monotonic()
            assert (t2 - t1) < 0.5
            out_rec, err_rec = p_rec.communicate(timeout=15)
            out_alloc, err_alloc = p_alloc.communicate(timeout=15)
            assert p_rec.returncode in (0, 1), (out_rec, err_rec)
            assert p_alloc.returncode in (0, 1), (out_alloc, err_alloc)
            assert "deadlock" not in (out_rec + err_rec).lower()
            assert "deadlock" not in (out_alloc + err_alloc).lower()
            rec = json.loads(out_rec) if out_rec.strip() else {}
            alc = json.loads(out_alloc) if out_alloc.strip() else {}
            outcome_a = (
                p_rec.returncode == 0
                and rec.get("matched") == [
                    {"payment_id": pe, "voucher_id": si,
                     "allocated_amount": "600.00"}]
                and rec.get("unmatched_payments") == 0
                and rec.get("unmatched_invoices") == 1
                and p_alloc.returncode == 1
                and alc.get("message") == (
                    "Allocated amount (600.00) exceeds unallocated (0.00)"))
            outcome_b = (
                p_alloc.returncode == 0
                and alc.get("document_status") == "created"
                and alc.get("document_cleared") is True
                and alc.get("remaining_unallocated") == "0.00"
                and p_rec.returncode == 0
                and rec.get("matched") == []
                and rec.get("unmatched_payments") == 0
                and rec.get("unmatched_invoices") == 1)
            assert outcome_a or outcome_b, (rec, alc)
            fresh = get_connection()
            try:
                inv = fresh.execute(
                    "SELECT outstanding_amount, status FROM sales_invoice WHERE id = ?",
                    (si,)).fetchone()
                assert (inv["outstanding_amount"], inv["status"]) == (
                    "400.00", "partially_paid")
                rows = fresh.execute(
                    "SELECT allocated_amount FROM payment_allocation "
                    "WHERE payment_entry_id = ? AND delinked = 0",
                    (pe,)).fetchall()
                assert len(rows) == 1
                assert rows[0]["allocated_amount"] == "600.00"
                proofs._assert_chain_intact(fresh, env["company_id"])
                proofs._assert_contiguous(fresh, env["company_id"])
            finally:
                fresh.close()
    finally:
        conn.close()


def test_reconcile_takes_the_chain_head_before_the_payment_row(conn):
    env = build_ar_env(conn)
    conn.commit()
    si = seed_sales_invoice(conn, env, "1000.00")
    pe = _receive(conn, env, "600.00")
    conn.commit()
    proxy = cas._RecordingProxy(conn)
    r = call_action(mod.reconcile_payments, proxy, ns(
        party_type="customer", party_id=env["customer"],
        company_id=env["company_id"]))
    assert is_ok(r), r
    first_two = cas._first_two_writes(proxy.statements)
    assert len(first_two) >= 2, proxy.statements[:5]
    assert "gl_chain_head" in first_two[0]
    assert cas._write_kind(first_two[0]) == "INSERT"
    assert "payment_entry" in first_two[1]
    assert cas._write_kind(first_two[1]) == "UPDATE"
