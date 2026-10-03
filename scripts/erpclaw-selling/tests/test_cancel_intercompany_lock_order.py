"""Cancel-intercompany-invoice takes both companies' chain heads first (m825).

``cancel-intercompany-invoice`` cancels a submitted source sales invoice and
its mirror purchase bill in a second company with the same writes as
``cancel-sales-invoice``. It used to take no head at all: on PostgreSQL it
deadlocked against ``cancel-payment`` on either leg, and two cancels of the
same intercompany invoice could both pass the status read. It now takes both
companies' heads before its first write, re-reads both documents under the
heads and re-runs every refusal, and both final status updates are
compare-and-sets.

The book below is built with ``test_cancel_intercompany_invoice_behaviour``'s
``_ensure_intercompany_linkage``, ``_mirror_env``, ``_submit_si`` and
``_submit_mirror`` (a submitted mirror bill) plus ``_receive_payment`` from
``test_cancel_invoice_allocation_release`` (a submitted 600.00 receipt
allocated to the invoice): the invoice reads ("400.00", "partially_paid") and
the mirror bill is submitted.
"""
import importlib.util
import json
import os
import subprocess
import sys
import time
from decimal import Decimal

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from selling_helpers import (call_action, ns, is_ok, is_error,  # noqa: E402
                              load_db_query, get_conn, seed_account)

try:
    from erpclaw_lib.db import get_connection, get_dialect
except ImportError:
    def get_dialect():
        return os.environ.get("ERPCLAW_DB_DIALECT", "sqlite")
    get_connection = None

from erpclaw_lib.query import P, Q, Table  # noqa: E402

mod = load_db_query()

_MODULE_DIR = os.path.dirname(_TESTS_DIR)
_SCRIPTS_DIR = os.path.dirname(_MODULE_DIR)
_SELLING_SCRIPT = os.path.join(_MODULE_DIR, "db_query.py")
_PAYMENTS_SCRIPT = os.path.join(_SCRIPTS_DIR, "erpclaw-payments", "db_query.py")
_SETUP_LIB = os.path.join(_SCRIPTS_DIR, "erpclaw-setup", "lib")
_PAY_TESTS = os.path.join(_SCRIPTS_DIR, "erpclaw-payments", "tests")


def _load(name, rel_path):
    path = os.path.join(_SCRIPTS_DIR, rel_path)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_beh = _load("ic_behaviour_helpers_m825",
             "erpclaw-selling/tests/test_cancel_intercompany_invoice_behaviour.py")
_alloc = _load("ic_alloc_helpers_m825",
               "erpclaw-selling/tests/test_cancel_invoice_allocation_release.py")
pay = _load("db_query_payments_m825", "erpclaw-payments/db_query.py")

_ensure_intercompany_linkage = _beh._ensure_intercompany_linkage
_mirror_env = _beh._mirror_env
_submit_si = _beh._submit_si
_submit_mirror = _beh._submit_mirror
_receive_payment = _alloc._receive_payment

SNAPSHOT_TABLES = (
    "gl_entry",
    "payment_ledger_entry",
    "payment_allocation",
    "payment_entry",
    "sales_invoice",
    "purchase_invoice",
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


def _build_book(conn, env, db_path):
    """Submitted intercompany invoice + submitted mirror + 600.00 receipt."""
    _ensure_intercompany_linkage(conn, db_path)
    menv = _mirror_env(conn)
    si_id = _submit_si(conn, env)
    pi_id = _submit_mirror(conn, si_id, menv, env["item1"])
    pe_id = _receive_payment(conn, env, "600.00", [
        {"voucher_type": "sales_invoice", "voucher_id": si_id,
         "allocated_amount": "600.00"}])
    row = conn.execute(
        "SELECT outstanding_amount, status FROM sales_invoice WHERE id = ?",
        (si_id,)).fetchone()
    assert (row["outstanding_amount"], row["status"]) == (
        "400.00", "partially_paid"), dict(row)
    mrow = conn.execute(
        "SELECT status FROM purchase_invoice WHERE id = ?", (pi_id,)).fetchone()
    assert mrow["status"] == "submitted", dict(mrow)
    conn.commit()
    return menv, si_id, pi_id, pe_id


def _submit_supplier_payment(conn, menv, cash_account, pi_id, amount="600.00"):
    """Submitted supplier payment in the mirror's company, allocated to it."""
    created = call_action(pay.add_payment, conn, ns(
        company_id=menv["company_id"], payment_type="pay",
        posting_date="2026-06-25", party_type="supplier",
        party_id=menv["supplier"],
        paid_from_account=cash_account, paid_to_account=menv["payable"],
        paid_amount=amount, exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None,
        allocations=json.dumps([{"voucher_type": "purchase_invoice",
                                 "voucher_id": pi_id,
                                 "allocated_amount": amount}]),
        deductions=None,
    ))
    assert is_ok(created), created
    spe_id = created["payment_entry_id"]
    assert is_ok(call_action(pay.submit_payment, conn,
                             ns(payment_entry_id=spe_id)))
    return spe_id


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


def _assert_only_one_status_changed(before, after, table, doc_id,
                                    old_status, new_status):
    """Snapshots are equal except one row's status in one table."""
    for name in SNAPSHOT_TABLES:
        if name == table:
            continue
        assert after[name] == before[name], name

    def _by_id(rows):
        out = {}
        for tup in rows:
            d = dict(tup)
            out[d["id"]] = d
        return out

    old_rows = _by_id(before[table])
    new_rows = _by_id(after[table])
    assert set(old_rows) == set(new_rows)
    for row_id, old_row in old_rows.items():
        new_row = new_rows[row_id]
        if row_id == doc_id:
            assert old_row["status"] == old_status, old_row
            assert new_row["status"] == new_status, new_row
            for key in old_row:
                if key == "status":
                    continue
                assert new_row[key] == old_row[key], key
        else:
            assert new_row == old_row, row_id


def test_stale_source_status_under_heads_refused_before_any_write(
        db_path, conn, env, monkeypatch):
    from erpclaw_lib.gl_posting import take_chain_heads as real_take
    menv, si_id, pi_id, pe_id = _build_book(conn, env, db_path)
    before = _snapshot(conn)

    def wrapper(c, company_ids):
        real_take(c, company_ids)
        t = Table("sales_invoice")
        uq = Q.update(t).set(t.status, P()).where(t.id == P())
        c.execute(uq.get_sql(), ("paid", si_id))
        c.commit()
        real_take(c, company_ids)

    monkeypatch.setattr(mod, "take_chain_heads", wrapper, raising=False)
    calls = []
    orig_reverse = mod.reverse_gl_entries

    def counting(*a, **k):
        calls.append(1)
        return orig_reverse(*a, **k)

    monkeypatch.setattr(mod, "reverse_gl_entries", counting)
    result = call_action(mod.cancel_intercompany_invoice, conn,
                         ns(sales_invoice_id=si_id))
    assert is_error(result), result
    assert result.get("message") == (
        "Cannot cancel sales invoice in status: paid"), result
    assert len(calls) == 0, calls
    mrow = conn.execute(
        "SELECT status FROM purchase_invoice WHERE id = ?", (pi_id,)).fetchone()
    assert mrow["status"] == "submitted", dict(mrow)
    fresh = _fresh_conn(db_path)
    try:
        _assert_only_one_status_changed(before, _snapshot(fresh),
                                        "sales_invoice", si_id,
                                        "partially_paid", "paid")
    finally:
        fresh.close()


def test_both_heads_first_ascending(db_path, conn, env, monkeypatch):
    if _PAY_TESTS not in sys.path:
        sys.path.insert(0, _PAY_TESTS)
    spec = importlib.util.spec_from_file_location(
        "pay_cas_m825",
        os.path.join(
            _PAY_TESTS,
            "test_payment_edit_and_allocation_compare_and_set.py"))
    recmod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recmod)
    _RecordingProxy = recmod._RecordingProxy
    menv, si_id, pi_id, pe_id = _build_book(conn, env, db_path)
    import erpclaw_lib.gl_posting as gl_posting
    real_take_head = gl_posting._take_chain_head
    seen = []

    def recorder(c, company_id):
        seen.append(company_id)
        return real_take_head(c, company_id)

    monkeypatch.setattr(gl_posting, "_take_chain_head", recorder)
    source_company = conn.execute(
        "SELECT company_id FROM sales_invoice WHERE id = ?",
        (si_id,)).fetchone()["company_id"]
    mirror_company = conn.execute(
        "SELECT company_id FROM purchase_invoice WHERE id = ?",
        (pi_id,)).fetchone()["company_id"]
    proxy = _RecordingProxy(conn)
    result = call_action(mod.cancel_intercompany_invoice, proxy,
                         ns(sales_invoice_id=si_id))
    assert is_ok(result), result
    writes = [s for s in proxy.statements
              if s.lstrip()[:6].upper() in ("INSERT", "UPDATE", "DELETE")]
    assert len(writes) >= 2, proxy.statements[:6]
    for stmt in writes[:2]:
        assert stmt.lstrip().upper().startswith("INSERT"), stmt
        assert "gl_chain_head" in stmt, stmt
    assert seen == sorted([source_company, mirror_company]), seen


def _pg_setup_new_round(conn, db_path):
    from selling_helpers import build_selling_env
    env = build_selling_env(conn)
    menv, si_id, pi_id, pe_id = _build_book(conn, env, db_path)
    conn.commit()
    return env, menv, si_id, pi_id, pe_id


def test_head_first_blocks_before_any_row_lock(db_path):
    _pg_only()
    conn = get_connection()
    holder = get_connection()
    probe = get_connection()
    proc = None
    try:
        env, menv, si_id, pi_id, pe_id = _pg_setup_new_round(conn, db_path)
        mirror_company = menv["company_id"]
        holder.execute(
            "UPDATE gl_chain_head SET updated_at = updated_at "
            "WHERE company_id = ?", (mirror_company,))
        penv = _proc_env(ERPCLAW_PG_LOCK_TIMEOUT="10s")
        proc = subprocess.Popen(
            [sys.executable, _SELLING_SCRIPT,
             "--action", "cancel-intercompany-invoice",
             "--sales-invoice-id", si_id],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=penv)
        try:
            proc.wait(timeout=1.0)
            pytest.fail("cancel-intercompany-invoice should block on the "
                        "held head (rc=%s)" % (proc.returncode,))
        except subprocess.TimeoutExpired:
            assert proc.poll() is None, "cancel must still be running"
        probe.execute("SET lock_timeout = '1s'")
        probe.execute(
            "UPDATE sales_invoice SET status = status WHERE id = ?", (si_id,))
        probe.execute(
            "UPDATE payment_entry SET status = status WHERE id = ?", (pe_id,))
        probe.execute(
            "UPDATE purchase_invoice SET status = status WHERE id = ?",
            (pi_id,))
        probe.rollback()
        holder.rollback()
        out, err = proc.communicate(timeout=12)
        assert proc.returncode == 0, (out, err)
        check = get_connection()
        try:
            sirow = check.execute(
                "SELECT status FROM sales_invoice WHERE id = ?",
                (si_id,)).fetchone()
            assert sirow["status"] == "cancelled", dict(sirow)
            pirow = check.execute(
                "SELECT status FROM purchase_invoice WHERE id = ?",
                (pi_id,)).fetchone()
            assert pirow["status"] == "cancelled", dict(pirow)
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


# A racer that dies inside foundation connection setup (before any
# business write) is redone on brand-new companies, not counted. Every
# subprocess runs ``erpclaw_lib.db._ensure_pg_decimal_sum`` (CREATE OR REPLACE
# FUNCTION plus a guarded CREATE AGGREGATE) while opening its connection, and
# three starters inside 50 ms collide there now and then with
# ``tuple concurrently updated``. That DDL race is foundation-owned and
# outside this task; the retry only fires on its exact signature (the setup
# frame plus the error), only before any counted assertion, and at most three
# discarded attempts for the five counted rounds.
_SETUP_RACE_MARKERS = ("_ensure_pg_decimal_sum", "tuple concurrently updated")


def _is_setup_race(text):
    return all(marker in text for marker in _SETUP_RACE_MARKERS)


def test_no_deadlock_with_cancel_payment_on_either_leg(db_path):
    _pg_only()
    spec = importlib.util.spec_from_file_location(
        "chain_proofs_m825",
        os.path.join(_PAY_TESTS, "test_chain_lock_proofs.py"))
    chain_proofs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(chain_proofs)
    completed = 0
    attempts = 0
    while completed < 5:
        attempts += 1
        if attempts > 8:
            pytest.fail("too many foundation connection-setup races; "
                        "see _SETUP_RACE_MARKERS")
        conn = get_connection()
        try:
            env, menv, si_id, pi_id, pe_id = _pg_setup_new_round(
                conn, db_path)
            cash_b = seed_account(conn, menv["company_id"], "Cash B",
                                  "asset", "cash", "1000")
            spe_id = _submit_supplier_payment(conn, menv, cash_b, pi_id)
            conn.commit()
            source_company = env["company_id"]
            mirror_company = menv["company_id"]
        finally:
            conn.close()
        penv = _proc_env()
        t0 = time.monotonic()
        p_receipt = subprocess.Popen(
            [sys.executable, _PAYMENTS_SCRIPT,
             "--action", "cancel-payment", "--payment-entry-id", pe_id],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=penv)
        p_supplier = subprocess.Popen(
            [sys.executable, _PAYMENTS_SCRIPT,
             "--action", "cancel-payment", "--payment-entry-id", spe_id],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=penv)
        elapsed = time.monotonic() - t0
        if elapsed < 0.05:
            time.sleep(0.05 - elapsed)
        p_ic = subprocess.Popen(
            [sys.executable, _SELLING_SCRIPT,
             "--action", "cancel-intercompany-invoice",
             "--sales-invoice-id", si_id],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=penv)
        try:
            out_r, err_r = p_receipt.communicate(timeout=15)
            out_s, err_s = p_supplier.communicate(timeout=15)
            out_ic, err_ic = p_ic.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            for p in (p_receipt, p_supplier, p_ic):
                try:
                    p.kill()
                except Exception:
                    pass
            pytest.fail("deadlock or hang in round %d" % completed)
        texts = (out_r + err_r, out_s + err_s, out_ic + err_ic)
        rcs = (p_receipt.returncode, p_supplier.returncode, p_ic.returncode)
        setup_races = [rc != 0 and _is_setup_race(text)
                       for rc, text in zip(rcs, texts)]
        if any(setup_races) and all(
                rc == 0 or race for rc, race in zip(rcs, setup_races)):
            continue
        assert p_receipt.returncode == 0, (out_r, err_r)
        assert p_supplier.returncode == 0, (out_s, err_s)
        assert p_ic.returncode == 0, (out_ic, err_ic)
        for text in texts:
            assert "deadlock" not in text.lower(), text
        check = get_connection()
        try:
            sirow = check.execute(
                "SELECT status FROM sales_invoice WHERE id = ?",
                (si_id,)).fetchone()
            assert sirow["status"] == "cancelled", dict(sirow)
            pirow = check.execute(
                "SELECT status FROM purchase_invoice WHERE id = ?",
                (pi_id,)).fetchone()
            assert pirow["status"] == "cancelled", dict(pirow)
            for payment_id in (pe_id, spe_id):
                prow = check.execute(
                    "SELECT status FROM payment_entry WHERE id = ?",
                    (payment_id,)).fetchone()
                assert prow["status"] == "cancelled", dict(prow)
            assert chain_proofs._assert_contiguous(
                check, source_company) >= 1
            chain_proofs._assert_chain_intact(check, source_company)
            assert chain_proofs._assert_contiguous(
                check, mirror_company) >= 1
            chain_proofs._assert_chain_intact(check, mirror_company)
        finally:
            check.close()
        completed += 1


def test_final_compare_and_sets(db_path, conn, env, monkeypatch):
    _pg_only()
    import erpclaw_lib.payment_clearing as clearing
    real_release = clearing.release_allocations_on_document

    menv, si_id, pi_id, pe_id = _build_book(conn, env, db_path)
    before = _snapshot(conn)
    state = {"calls": 0}

    def wrapped_first(c, voucher_type, voucher_id):
        result = real_release(c, voucher_type, voucher_id)
        state["calls"] += 1
        if state["calls"] == 1:
            other = get_connection()
            try:
                t = Table("sales_invoice")
                uq = Q.update(t).set(t.status, P()).where(t.id == P())
                other.execute(uq.get_sql(), ("paid", si_id))
                other.commit()
            finally:
                other.close()
        return result

    monkeypatch.setattr(clearing, "release_allocations_on_document",
                        wrapped_first)
    result = call_action(mod.cancel_intercompany_invoice, conn,
                         ns(sales_invoice_id=si_id))
    assert is_error(result), result
    assert result.get("message") == (
        "Cannot cancel sales invoice in status: paid"), result
    fresh = get_connection()
    try:
        _assert_only_one_status_changed(before, _snapshot(fresh),
                                        "sales_invoice", si_id,
                                        "partially_paid", "paid")
    finally:
        fresh.close()

    from selling_helpers import build_selling_env
    env2 = build_selling_env(conn)
    menv2, si_id2, pi_id2, pe_id2 = _build_book(conn, env2, db_path)
    before2 = _snapshot(conn)
    state2 = {"calls": 0}

    def wrapped_second(c, voucher_type, voucher_id):
        result = real_release(c, voucher_type, voucher_id)
        state2["calls"] += 1
        if state2["calls"] == 2:
            other = get_connection()
            try:
                t = Table("purchase_invoice")
                uq = Q.update(t).set(t.status, P()).where(t.id == P())
                other.execute(uq.get_sql(), ("paid", pi_id2))
                other.commit()
            finally:
                other.close()
        return result

    monkeypatch.setattr(clearing, "release_allocations_on_document",
                        wrapped_second)
    result2 = call_action(mod.cancel_intercompany_invoice, conn,
                          ns(sales_invoice_id=si_id2))
    assert is_error(result2), result2
    assert result2.get("message") == (
        "Mirror purchase invoice %s changed while cancelling "
        "(status 'paid'); nothing was written" % pi_id2), result2
    fresh2 = get_connection()
    try:
        _assert_only_one_status_changed(before2, _snapshot(fresh2),
                                        "purchase_invoice", pi_id2,
                                        "submitted", "paid")
    finally:
        fresh2.close()
