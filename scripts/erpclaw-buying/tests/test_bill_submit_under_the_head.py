"""Bill submit decides under the ledger chain head (task m827).

Every ledger-posting action takes the company's chain head before its
first ledger write, then decides on state re-read under the head, and
changes the document's status with a compare-and-set on the status it
decided on. These tests pin that for `submit-purchase-invoice`:

- a bill deleted while waiting on the head is refused with the
  not-found message, writing nothing;
- a bill moved out of draft while waiting is refused with the status
  message, writing nothing;
- a bill changed after the ledger write loses the final compare-and-set
  and rolls everything back;
- two concurrent submits post exactly once, the loser getting the status
  message (never the GL idempotency message);
- a refusal after the head rolls back on the action's own connection;
- the happy path is unchanged.

Money is exact Decimal text, never float. Reads/writes go through
``erpclaw_lib.query`` builders or constant ``?``-parameterised SQL.
"""
import importlib.util
import json
import os
import subprocess
import sys
import time
from decimal import Decimal

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import pytest

from buying_helpers import (
    build_buying_env, call_action, get_conn, is_error, is_ok, load_db_query,
    ns, _init_pg_schema,
)
from erpclaw_lib.db import get_connection, get_dialect
from erpclaw_lib.query import P, Q, Table
from erpclaw_lib.vendor.pypika.terms import ValueWrapper

mod = load_db_query()

assert issubclass(mod._SubmitRefused, Exception)

_TESTS_DIR_ABS = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(os.path.dirname(_TESTS_DIR_ABS))
_BUYING_SCRIPT = os.path.join(_SCRIPTS_DIR, "erpclaw-buying", "db_query.py")
_SETUP_LIB = os.path.join(_SCRIPTS_DIR, "erpclaw-setup", "lib")

_PG_SKIP = pytest.mark.skipif(
    not os.environ.get("ERPCLAW_PG_TEST_URL"),
    reason="needs ERPCLAW_PG_TEST_URL (private PostgreSQL cluster)",
)

_SNAP_TABLES = ("gl_entry", "payment_ledger_entry", "stock_ledger_entry",
                "purchase_invoice", "purchase_invoice_item")


def _pg_only():
    if get_dialect() != "postgresql":
        pytest.skip("PostgreSQL-only case: needs the ERPCLAW_PG_TEST_URL lane")


def _proc_env(**overrides):
    env = dict(os.environ)
    env["PYTHONPATH"] = (_SETUP_LIB + os.pathsep + env.get("PYTHONPATH", ""))
    env.update(overrides)
    env.pop("ERPCLAW_DB_PATH", None)
    return env


def _load_chain_proofs():
    path = os.path.join(_SCRIPTS_DIR, "erpclaw-payments", "tests",
                        "test_chain_lock_proofs.py")
    spec = importlib.util.spec_from_file_location(
        "payments_chain_lock_proofs", path)
    proofs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proofs)
    return proofs


def _make_draft(conn, env):
    """One-line draft bill, quantity 2 at 50.00, non-stock-moving."""
    res = call_action(mod.create_purchase_invoice, conn, ns(
        purchase_order_id=None, purchase_receipt_id=None,
        supplier_id=env["supplier"], company_id=env["company_id"],
        posting_date="2026-06-20", due_date=None,
        items=json.dumps([{"item_id": env["item1"], "qty": "2",
                           "rate": "50.00"}]),
        tax_template_id=None,
    ))
    assert is_ok(res), f"draft bill failed: {res}"
    bill_id = res["purchase_invoice_id"]
    conn.execute("UPDATE purchase_invoice SET update_stock = 0 WHERE id = ?",
                 (bill_id,))
    conn.commit()
    row = conn.execute(
        "SELECT status, update_stock, grand_total FROM purchase_invoice "
        "WHERE id = ?", (bill_id,)).fetchone()
    assert row["status"] == "draft", row
    assert row["update_stock"] == 0, row
    assert row["grand_total"] == "100.00", row
    return bill_id


def _snapshot(conn):
    snap = {}
    for name in _SNAP_TABLES:
        tbl = Table(name)
        rows = conn.execute(
            Q.from_(tbl).select(tbl.star).get_sql()).fetchall()
        snap[name] = sorted(
            json.dumps(dict(r), sort_keys=True, default=str) for r in rows)
    return snap


def _delete_bill(c, bill_id):
    pii_t = Table("purchase_invoice_item")
    pi_t = Table("purchase_invoice")
    c.execute(Q.from_(pii_t).delete()
              .where(pii_t.purchase_invoice_id == P()).get_sql(), (bill_id,))
    c.execute(Q.from_(pi_t).delete()
              .where(pi_t.id == P()).get_sql(), (bill_id,))


def _set_bill_status(c, bill_id, status):
    pi_t = Table("purchase_invoice")
    c.execute(Q.update(pi_t).set(pi_t.status, ValueWrapper(status))
              .where(pi_t.id == P()).get_sql(), (bill_id,))


def _gl_rows(conn, bill_id):
    return conn.execute(
        "SELECT account_id, debit, credit FROM gl_entry "
        "WHERE voucher_type=? AND voucher_id=? AND is_cancelled=0 "
        "ORDER BY account_id",
        ("purchase_invoice", bill_id)).fetchall()


def _ple_rows(conn, bill_id):
    return conn.execute(
        "SELECT id, amount FROM payment_ledger_entry "
        "WHERE voucher_type=? AND voucher_id=?",
        ("purchase_invoice", bill_id)).fetchall()


def _assert_single_pair(conn, env, bill_id):
    rows = _gl_rows(conn, bill_id)
    assert len(rows) == 2, rows
    by_acct = {r["account_id"]: (r["debit"], r["credit"]) for r in rows}
    assert set(by_acct) == {env["expense"], env["ap"]}, by_acct
    assert by_acct[env["expense"]][0] == "100.00", by_acct
    assert by_acct[env["ap"]][1] == "100.00", by_acct
    ple = _ple_rows(conn, bill_id)
    assert len(ple) == 1, ple
    assert ple[0]["amount"] == "100.00", ple


def test_deleted_while_waiting(db_path, conn, env, monkeypatch):
    bill_id = _make_draft(conn, env)
    real_take = mod.take_chain_heads
    real_insert = mod.insert_gl_entries
    held = {}
    calls = {"n": 0}

    def _take(c, companies):
        _delete_bill(c, bill_id)
        c.commit()
        held["snap"] = _snapshot(c)
        return real_take(c, companies)

    def _insert(c, *args, **kwargs):
        calls["n"] += 1
        return real_insert(c, *args, **kwargs)

    monkeypatch.setattr(mod, "take_chain_heads", _take)
    monkeypatch.setattr(mod, "insert_gl_entries", _insert)
    res = call_action(mod.submit_purchase_invoice, conn,
                      ns(purchase_invoice_id=bill_id))
    assert is_error(res), res
    assert res["message"] == f"Purchase invoice {bill_id} not found"
    assert calls["n"] == 0, calls
    fresh = get_conn(db_path)
    try:
        assert _snapshot(fresh) == held["snap"]
        assert _gl_rows(fresh, bill_id) == []
        assert _ple_rows(fresh, bill_id) == []
    finally:
        fresh.close()


def test_status_changed_while_waiting(db_path, conn, env, monkeypatch):
    bill_id = _make_draft(conn, env)
    real_take = mod.take_chain_heads
    real_insert = mod.insert_gl_entries
    held = {}
    calls = {"n": 0}

    def _take(c, companies):
        _set_bill_status(c, bill_id, "cancelled")
        c.commit()
        held["snap"] = _snapshot(c)
        return real_take(c, companies)

    def _insert(c, *args, **kwargs):
        calls["n"] += 1
        return real_insert(c, *args, **kwargs)

    monkeypatch.setattr(mod, "take_chain_heads", _take)
    monkeypatch.setattr(mod, "insert_gl_entries", _insert)
    res = call_action(mod.submit_purchase_invoice, conn,
                      ns(purchase_invoice_id=bill_id))
    assert is_error(res), res
    assert res["message"] == \
        "Cannot submit: invoice is 'cancelled' (must be 'draft')"
    assert calls["n"] == 0, calls
    fresh = get_conn(db_path)
    try:
        assert _snapshot(fresh) == held["snap"]
        assert _gl_rows(fresh, bill_id) == []
        assert _ple_rows(fresh, bill_id) == []
    finally:
        fresh.close()


@_PG_SKIP
def test_final_compare_and_set(db_path, conn, env, monkeypatch):
    """A status flip after the ledger write loses the final CAS."""
    _pg_only()
    bill_id = _make_draft(conn, env)
    real_insert = mod.insert_gl_entries

    def _insert(c, *args, **kwargs):
        out = real_insert(c, *args, **kwargs)
        other = get_conn(db_path)
        try:
            _set_bill_status(other, bill_id, "cancelled")
            other.commit()
        finally:
            other.close()
        return out

    monkeypatch.setattr(mod, "insert_gl_entries", _insert)
    res = call_action(mod.submit_purchase_invoice, conn,
                      ns(purchase_invoice_id=bill_id))
    assert is_error(res), res
    assert res["message"] == \
        "Cannot submit: invoice is 'cancelled' (must be 'draft')"
    fresh = get_conn(db_path)
    try:
        assert _gl_rows(fresh, bill_id) == []
        row = fresh.execute(
            "SELECT status FROM purchase_invoice WHERE id = ?",
            (bill_id,)).fetchone()
        assert row is not None and row["status"] == "cancelled", row
    finally:
        fresh.close()


@_PG_SKIP
def test_deleted_after_ledger_write(db_path, conn, env, monkeypatch):
    """A delete after the ledger write loses the final CAS; GL rolls back."""
    _pg_only()
    bill_id = _make_draft(conn, env)
    real_insert = mod.insert_gl_entries

    def _insert(c, *args, **kwargs):
        out = real_insert(c, *args, **kwargs)
        other = get_conn(db_path)
        try:
            _delete_bill(other, bill_id)
            other.commit()
        finally:
            other.close()
        return out

    monkeypatch.setattr(mod, "insert_gl_entries", _insert)
    res = call_action(mod.submit_purchase_invoice, conn,
                      ns(purchase_invoice_id=bill_id))
    assert is_error(res), res
    assert res["message"] == f"Purchase invoice {bill_id} not found"
    fresh = get_conn(db_path)
    try:
        assert _gl_rows(fresh, bill_id) == []
        assert _ple_rows(fresh, bill_id) == []
    finally:
        fresh.close()


def test_final_compare_and_set_same_connection(db_path, conn, env, monkeypatch):
    """A status flip on the action's own connection loses the final CAS."""
    bill_id = _make_draft(conn, env)
    real_insert = mod.insert_gl_entries

    def _insert(c, *args, **kwargs):
        out = real_insert(c, *args, **kwargs)
        _set_bill_status(c, bill_id, "cancelled")
        return out

    monkeypatch.setattr(mod, "insert_gl_entries", _insert)
    res = call_action(mod.submit_purchase_invoice, conn,
                      ns(purchase_invoice_id=bill_id))
    assert is_error(res), res
    assert res["message"] == \
        "Cannot submit: invoice is 'cancelled' (must be 'draft')"
    fresh = get_conn(db_path)
    try:
        assert _gl_rows(fresh, bill_id) == []
        assert _ple_rows(fresh, bill_id) == []
        row = fresh.execute(
            "SELECT status FROM purchase_invoice WHERE id = ?",
            (bill_id,)).fetchone()
        assert row is not None and row["status"] == "draft", row
    finally:
        fresh.close()


def test_final_compare_and_set_same_connection_deleted(
        db_path, conn, env, monkeypatch):
    """A delete on the action's own connection loses the final CAS."""
    bill_id = _make_draft(conn, env)
    real_insert = mod.insert_gl_entries

    def _insert(c, *args, **kwargs):
        out = real_insert(c, *args, **kwargs)
        _delete_bill(c, bill_id)
        return out

    monkeypatch.setattr(mod, "insert_gl_entries", _insert)
    res = call_action(mod.submit_purchase_invoice, conn,
                      ns(purchase_invoice_id=bill_id))
    assert is_error(res), res
    assert res["message"] == f"Purchase invoice {bill_id} not found"
    fresh = get_conn(db_path)
    try:
        assert _gl_rows(fresh, bill_id) == []
        assert _ple_rows(fresh, bill_id) == []
        row = fresh.execute(
            "SELECT status FROM purchase_invoice WHERE id = ?",
            (bill_id,)).fetchone()
        assert row is not None and row["status"] == "draft", row
    finally:
        fresh.close()


@_PG_SKIP
def test_double_submit_posts_once():
    """Two concurrent submits of one draft: one wins, the loser is refused."""
    _pg_only()
    proofs = _load_chain_proofs()
    base_url = os.environ["ERPCLAW_PG_TEST_URL"]
    old_db_url = os.environ.get("ERPCLAW_DB_URL")
    os.environ["ERPCLAW_DB_URL"] = base_url
    conn = None
    try:
        _init_pg_schema()
        conn = get_connection()
        env = build_buying_env(conn)
        for _ in range(5):
            bill_id = _make_draft(conn, env)
            penv = _proc_env(ERPCLAW_DB_URL=base_url,
                             ERPCLAW_DB_DIALECT="postgresql",
                             ERPCLAW_PG_LOCK_TIMEOUT="10s")
            args = [sys.executable, _BUYING_SCRIPT,
                    "--action", "submit-purchase-invoice",
                    "--purchase-invoice-id", bill_id]
            sub_a = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=penv)
            sub_b = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=penv)
            out_a, err_a = sub_a.communicate(timeout=15)
            out_b, err_b = sub_b.communicate(timeout=15)
            assert "deadlock" not in (out_a + err_a).lower(), (out_a, err_a)
            assert "deadlock" not in (out_b + err_b).lower(), (out_b, err_b)
            results = sorted([(sub_a.returncode, out_a, err_a),
                              (sub_b.returncode, out_b, err_b)])
            assert {r[0] for r in results} == {0, 1}, results
            winner = next(r for r in results if r[0] == 0)
            loser = next(r for r in results if r[0] == 1)
            assert winner[0] == 0, results
            assert loser[0] == 1, results
            assert is_ok(json.loads(winner[1])), results
            loser_body = json.loads(loser[1])
            assert loser_body["message"] == \
                "Cannot submit: invoice is 'submitted' (must be 'draft')", \
                results
            conn.rollback()
            _assert_single_pair(conn, env, bill_id)
            proofs._assert_chain_intact(conn, env["company_id"])
            proofs._assert_contiguous(conn, env["company_id"])
    finally:
        for handle in (conn,):
            if handle is not None:
                try:
                    handle.rollback()
                except Exception:
                    pass
                try:
                    handle.close()
                except Exception:
                    pass
        if old_db_url is None:
            os.environ.pop("ERPCLAW_DB_URL", None)
        else:
            os.environ["ERPCLAW_DB_URL"] = old_db_url


def test_rollback_on_refusal(conn, env, monkeypatch):
    bill_id = _make_draft(conn, env)
    before = _snapshot(conn)
    real_audit = mod.audit
    state = {"n": 0}

    def _audit(c, *args, **kwargs):
        if state["n"] == 0:
            state["n"] += 1
            raise mod._SubmitRefused("planted")
        return real_audit(c, *args, **kwargs)

    monkeypatch.setattr(mod, "audit", _audit)
    res = call_action(mod.submit_purchase_invoice, conn,
                      ns(purchase_invoice_id=bill_id))
    assert is_error(res), res
    assert res["message"] == "planted"
    row = conn.execute(
        "SELECT status FROM purchase_invoice WHERE id = ?",
        (bill_id,)).fetchone()
    assert row["status"] == "draft", row
    assert _snapshot(conn) == before


def test_happy_path_unchanged(conn, env):
    bill_id = _make_draft(conn, env)
    res = call_action(mod.submit_purchase_invoice, conn,
                      ns(purchase_invoice_id=bill_id))
    assert is_ok(res), res
    assert res["document_status"] == "submitted", res
    assert res["purchase_invoice_id"] == bill_id, res
    assert res["naming_series"].startswith("PINV-"), res
    assert res["is_return"] is False, res
    assert res["voucher_type"] == "purchase_invoice", res
    assert res["gl_entries_created"] == 2, res
    assert res["sle_entries_created"] == 0, res
    assert res["update_stock"] is False, res
    assert set(res) == {"status", "document_status", "purchase_invoice_id",
                        "naming_series", "is_return", "voucher_type",
                        "gl_entries_created", "sle_entries_created",
                        "update_stock"}, set(res)
    row = conn.execute(
        "SELECT status FROM purchase_invoice WHERE id = ?",
        (bill_id,)).fetchone()
    assert row["status"] == "submitted", row
    _assert_single_pair(conn, env, bill_id)
