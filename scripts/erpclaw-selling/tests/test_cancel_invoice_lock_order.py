"""Cancel-sales-invoice takes the company chain head first (m819).

Every action that cancels a posted document and can touch a payment's rows
takes the company's ledger chain head before it writes anything, then
re-checks the document's status under that lock.
"""
import importlib.util
import json
import os
import subprocess
import sys
import time
from decimal import Decimal

import pytest

from selling_helpers import call_action, ns, is_ok, is_error, load_db_query, get_conn

try:
    from erpclaw_lib.db import get_connection, get_dialect
except ImportError:
    def get_dialect():
        return os.environ.get("ERPCLAW_DB_DIALECT", "sqlite")
    get_connection = None

from erpclaw_lib.query import P, Q, Table

mod = load_db_query()

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_MODULE_DIR = os.path.dirname(_TESTS_DIR)
_SCRIPTS_DIR = os.path.dirname(_MODULE_DIR)
_SELLING_SCRIPT = os.path.join(_MODULE_DIR, "db_query.py")
_PAYMENTS_SCRIPT = os.path.join(_SCRIPTS_DIR, "erpclaw-payments", "db_query.py")
_SETUP_LIB = os.path.join(_SCRIPTS_DIR, "erpclaw-setup", "lib")
_PAY_TESTS = os.path.join(_SCRIPTS_DIR, "erpclaw-payments", "tests")

EXPECTED_STATUS_MSG = (
    "Cannot cancel: sales invoice is '{status}' "
    "(must be 'submitted', 'overdue', or 'partially_paid')"
)

SNAPSHOT_TABLES = (
    "gl_entry",
    "payment_ledger_entry",
    "payment_allocation",
    "payment_entry",
    "stock_ledger_entry",
)


def _pg_only():
    if get_dialect() != "postgresql":
        pytest.skip("PostgreSQL-only case: needs the ERPCLAW_PG_TEST_URL lane")


def _proc_env(**overrides):
    env = dict(os.environ)
    env["PYTHONPATH"] = _SETUP_LIB + os.pathsep + env.get("PYTHONPATH", "")
    env.update(overrides)
    return env


def _load_pay():
    spec = importlib.util.spec_from_file_location(
        "db_query_payments_lockorder", _PAYMENTS_SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


pay = _load_pay()


def _items(env, *specs):
    return json.dumps([
        {"item_id": env[k], "qty": q, "rate": r, "warehouse_id": env["warehouse"]}
        for k, q, r in specs
    ])


def _sales_invoice(conn, env, qty="10", rate="100.00"):
    create = call_action(mod.create_sales_invoice, conn, ns(
        sales_order_id=None, delivery_note_id=None,
        customer_id=env["customer"], company_id=env["company_id"],
        posting_date="2026-06-20", due_date="2026-07-20",
        items=_items(env, ("item1", qty, rate)), tax_template_id=None,
        payment_terms_id=None,
    ))
    assert is_ok(create), create
    si_id = create["sales_invoice_id"]
    assert is_ok(call_action(mod.submit_sales_invoice, conn, ns(sales_invoice_id=si_id)))
    return si_id


def _receive_payment(conn, env, si_id, amount="600.00"):
    created = call_action(pay.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="receive",
        posting_date="2026-06-25", party_type="customer", party_id=env["customer"],
        paid_from_account=env["ar"], paid_to_account=env["cash"],
        paid_amount=amount, exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None,
        allocations=json.dumps([{"voucher_type": "sales_invoice",
                                 "voucher_id": si_id,
                                 "allocated_amount": amount}]),
        deductions=None,
    ))
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    assert is_ok(call_action(pay.submit_payment, conn, ns(payment_entry_id=pe_id)))
    return pe_id


def _setup_invoice_payment(conn, env):
    si_id = _sales_invoice(conn, env)
    pe_id = _receive_payment(conn, env, si_id, "600.00")
    row = conn.execute(
        "SELECT outstanding_amount, status FROM sales_invoice WHERE id = ?",
        (si_id,)).fetchone()
    assert (row["outstanding_amount"], row["status"]) == ("400.00", "partially_paid"), dict(row)
    return si_id, pe_id


def _snapshot(conn):
    snap = {}
    for table in SNAPSHOT_TABLES:
        rows = conn.execute("SELECT * FROM %s ORDER BY id" % table).fetchall()
        norm = []
        for r in rows:
            d = dict(r)
            norm.append(tuple(sorted((k, str(v)) for k, v in d.items())))
        norm.sort()
        snap[table] = norm
    return snap


def _fresh_conn(db_path):
    if get_dialect() == "postgresql":
        return get_connection()
    return get_conn(db_path)


def _invoice_state(conn, si_id):
    row = conn.execute(
        "SELECT outstanding_amount, status FROM sales_invoice WHERE id = ?",
        (si_id,)).fetchone()
    if row is None:
        return None
    return (row["outstanding_amount"], row["status"])


def _no_audit_row(conn, si_id):
    row = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action = ? AND entity_id = ?",
        ("cancel-sales-invoice", si_id)).fetchone()
    return row[0] == 0


def test_stale_status_under_head_refused_before_any_write(db_path, conn, env, monkeypatch):
    from erpclaw_lib.gl_posting import take_chain_heads as real_take
    si_id, pe_id = _setup_invoice_payment(conn, env)
    before = _snapshot(conn)

    def wrapper(c, company_ids):
        real_take(c, company_ids)
        t = Table("sales_invoice")
        uq = Q.update(t).set(t.status, P()).where(t.id == P())
        c.execute(uq.get_sql(), ("cancelled", si_id))
        c.commit()
        real_take(c, company_ids)

    monkeypatch.setattr(mod, "take_chain_heads", wrapper, raising=False)
    calls = []
    orig_reverse = mod.reverse_gl_entries

    def counting(*a, **k):
        calls.append(1)
        return orig_reverse(*a, **k)

    monkeypatch.setattr(mod, "reverse_gl_entries", counting)
    result = call_action(mod.cancel_sales_invoice, conn, ns(sales_invoice_id=si_id))
    expected = EXPECTED_STATUS_MSG.format(status="cancelled")
    assert is_error(result), result
    assert result.get("message") == expected, result
    assert len(calls) == 0, calls
    fresh = _fresh_conn(db_path)
    try:
        assert _invoice_state(fresh, si_id) == ("400.00", "cancelled")
        assert _snapshot(fresh) == before
        assert _no_audit_row(fresh, si_id)
    finally:
        fresh.close()


def _pg_setup_new_round(conn):
    from selling_helpers import build_selling_env
    env = build_selling_env(conn)
    si_id = _sales_invoice(conn, env)
    pe_id = _receive_payment(conn, env, si_id, "600.00")
    conn.commit()
    return env, si_id, pe_id


def test_head_first_blocks_before_any_row_lock(db_path):
    _pg_only()
    conn = get_connection()
    holder = get_connection()
    probe = get_connection()
    proc = None
    try:
        env, si_id, pe_id = _pg_setup_new_round(conn)
        company_id = env["company_id"]
        holder.execute(
            "UPDATE gl_chain_head SET updated_at = updated_at WHERE company_id = ?",
            (company_id,))
        penv = _proc_env(ERPCLAW_PG_LOCK_TIMEOUT="10s")
        proc = subprocess.Popen(
            [sys.executable, _SELLING_SCRIPT,
             "--action", "cancel-sales-invoice",
             "--sales-invoice-id", si_id],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=penv)
        try:
            proc.wait(timeout=1.0)
            pytest.fail("cancel-sales-invoice should block on the held head "
                        "(rc=%s)" % (proc.returncode,))
        except subprocess.TimeoutExpired:
            assert proc.poll() is None, "cancel must still be running"
        probe.execute("SET lock_timeout = '1s'")
        probe.execute(
            "UPDATE sales_invoice SET status = status WHERE id = ?", (si_id,))
        probe.execute(
            "UPDATE payment_ledger_entry SET delinked = delinked "
            "WHERE voucher_type = ? AND voucher_id = ?",
            ("sales_invoice", si_id))
        probe.execute(
            "UPDATE payment_ledger_entry SET delinked = delinked "
            "WHERE voucher_type = ? AND voucher_id = ? AND against_voucher_id = ?",
            ("payment_entry", pe_id, si_id))
        probe.execute(
            "UPDATE payment_entry SET status = status WHERE id = ?", (pe_id,))
        probe.rollback()
        holder.rollback()
        out, err = proc.communicate(timeout=12)
        assert proc.returncode == 0, (out, err)
        check = get_connection()
        try:
            assert _invoice_state(check, si_id) == ("0", "cancelled"), _invoice_state(check, si_id)
        finally:
            check.close()
    finally:
        try:
            holder.rollback()
        except Exception:
            pass
        holder.close()
        try:
            probe.rollback()
        except Exception:
            pass
        probe.close()
        conn.close()
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.communicate()


def test_no_deadlock_with_cancel_payment(db_path):
    _pg_only()
    if _PAY_TESTS not in sys.path:
        sys.path.insert(0, _PAY_TESTS)
    import test_chain_lock_proofs as chain_proofs
    for _round in range(5):
        conn = get_connection()
        try:
            env, si_id, pe_id = _pg_setup_new_round(conn)
        finally:
            conn.close()
        penv = _proc_env()
        t0 = time.monotonic()
        p_pay = subprocess.Popen(
            [sys.executable, _PAYMENTS_SCRIPT,
             "--action", "cancel-payment", "--payment-entry-id", pe_id],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=penv)
        # within 50 ms of each other
        elapsed = time.monotonic() - t0
        if elapsed < 0.05:
            time.sleep(0.05 - elapsed)
        p_inv = subprocess.Popen(
            [sys.executable, _SELLING_SCRIPT,
             "--action", "cancel-sales-invoice",
             "--sales-invoice-id", si_id],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=penv)
        try:
            out_pay, err_pay = p_pay.communicate(timeout=15)
            out_inv, err_inv = p_inv.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            for p in (p_pay, p_inv):
                try:
                    p.kill()
                except Exception:
                    pass
            pytest.fail("deadlock or hang in round %d" % _round)
        assert p_pay.returncode == 0, (out_pay, err_pay)
        assert p_inv.returncode == 0, (out_inv, err_inv)
        assert "deadlock" not in (out_pay + err_pay).lower(), (out_pay, err_pay)
        assert "deadlock" not in (out_inv + err_inv).lower(), (out_inv, err_inv)
        check = get_connection()
        try:
            assert _invoice_state(check, si_id) == ("0", "cancelled"), _invoice_state(check, si_id)
            prow = check.execute(
                "SELECT status FROM payment_entry WHERE id = ?", (pe_id,)).fetchone()
            assert prow["status"] == "cancelled", dict(prow)
            assert chain_proofs._assert_contiguous(check, env["company_id"]) >= 1
            chain_proofs._assert_chain_intact(check, env["company_id"])
        finally:
            check.close()


def test_invoice_row_free_while_waiting_on_payment_row(db_path):
    _pg_only()
    conn = get_connection()
    holder = get_connection()
    probe = get_connection()
    proc = None
    try:
        env, si_id, pe_id = _pg_setup_new_round(conn)
        holder.execute(
            "UPDATE payment_entry SET updated_at = updated_at WHERE id = ?",
            (pe_id,))
        penv = _proc_env(ERPCLAW_PG_LOCK_TIMEOUT="10s")
        proc = subprocess.Popen(
            [sys.executable, _SELLING_SCRIPT,
             "--action", "cancel-sales-invoice",
             "--sales-invoice-id", si_id],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=penv)
        try:
            proc.wait(timeout=1.0)
            pytest.fail("cancel-sales-invoice should block on the held payment row "
                        "(rc=%s)" % (proc.returncode,))
        except subprocess.TimeoutExpired:
            assert proc.poll() is None, "cancel must still be running"
        probe.execute("SET lock_timeout = '1s'")
        probe.execute(
            "UPDATE sales_invoice SET status = status WHERE id = ?", (si_id,))
        probe.rollback()
        holder.rollback()
        out, err = proc.communicate(timeout=12)
        assert proc.returncode == 0, (out, err)
        check = get_connection()
        try:
            assert _invoice_state(check, si_id) == ("0", "cancelled"), _invoice_state(check, si_id)
        finally:
            check.close()
    finally:
        try:
            holder.rollback()
        except Exception:
            pass
        holder.close()
        try:
            probe.rollback()
        except Exception:
            pass
        probe.close()
        conn.close()
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.communicate()


def test_final_compare_and_set(db_path, conn, env, monkeypatch):
    _pg_only()
    import erpclaw_lib.payment_clearing as clearing
    si_id, pe_id = _setup_invoice_payment(conn, env)
    before = _snapshot(conn)
    real_close = clearing.close_dead_payment_tails

    def wrapped(c, voucher_type, voucher_id):
        result = real_close(c, voucher_type, voucher_id)
        other = get_connection()
        try:
            t = Table("sales_invoice")
            uq = Q.update(t).set(t.status, P()).where(t.id == P())
            other.execute(uq.get_sql(), ("paid", si_id))
            other.commit()
        finally:
            other.close()
        return result

    monkeypatch.setattr(clearing, "close_dead_payment_tails", wrapped)
    result = call_action(mod.cancel_sales_invoice, conn, ns(sales_invoice_id=si_id))
    expected = EXPECTED_STATUS_MSG.format(status="paid")
    assert is_error(result), result
    assert result.get("message") == expected, result
    fresh = get_connection()
    try:
        assert _invoice_state(fresh, si_id) == ("400.00", "paid"), _invoice_state(fresh, si_id)
        assert _snapshot(fresh) == before
    finally:
        fresh.close()
