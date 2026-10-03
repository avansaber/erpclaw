"""Stock entries and reconciliations take the chain head first (task m830).

Every action that posts to the ledger and changes a document's state takes
the company's ledger chain head before its first write, then decides on
state re-read under the head. submit-stock-entry, cancel-stock-entry and
submit-stock-reconciliation are pinned here:

1. stale status under the head is refused before any write (SQLite + PG),
2. the head write comes before every stock/document write (SQLite),
3. happy paths are unchanged (SQLite),
4. the head is taken first on PostgreSQL (a FIFO issue blocks on the head
   while holding no FIFO row),
5. concurrent double submits / double cancels post exactly once (PG),
6. the final compare-and-set refuses a mid-flight status change (PG),
7. a refusal after the stock writes rolls everything back (SQLite).
"""
import importlib.util
import json
import os
import subprocess
import sys
import time
import uuid
from decimal import Decimal

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from inventory_helpers import (  # noqa: E402
    call_action, ns, is_error, is_ok, load_db_query,
    init_all_tables, get_conn, seed_company, seed_account,
    seed_fiscal_year, seed_cost_center, seed_item, seed_warehouse,
    seed_stock_entry_sle, seed_naming_series,
)

mod = load_db_query()  # noqa: E402

from erpclaw_lib.db import get_connection  # noqa: E402
from erpclaw_lib.query import P, Q, Table  # noqa: E402
from erpclaw_lib.gl_posting import take_chain_heads as _real_take_heads  # noqa: E402

_SCRIPTS_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PAYMENTS_TESTS = os.path.join(_SCRIPTS_DIR, "erpclaw-payments", "tests")
_PAYMENTS_HELPERS_PATH = os.path.join(_PAYMENTS_TESTS, "payments_helpers.py")
_PROOFS_PATH = os.path.join(_PAYMENTS_TESTS, "test_chain_lock_proofs.py")
_CAS_PATH = os.path.join(
    _PAYMENTS_TESTS, "test_payment_edit_and_allocation_compare_and_set.py")
_INVENTORY_SCRIPT = os.path.join(
    _SCRIPTS_DIR, "erpclaw-inventory", "db_query.py")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


proofs = _load("payments_chain_lock_proofs", _PROOFS_PATH)
_cas = _load("payments_cas_proxy_module", _CAS_PATH)
_RecordingProxy = _cas._RecordingProxy

SUBMIT_MSG = "Cannot submit: stock entry is 'submitted' (must be 'draft')"
CANCEL_MSG = "Cannot cancel: stock entry is 'cancelled' (must be 'submitted')"
RECON_MSG = "Cannot submit: reconciliation is 'submitted' (must be 'draft')"


# ── fixtures ──

@pytest.fixture
def lite_db(tmp_path):
    old_dialect = os.environ.get("ERPCLAW_DB_DIALECT")
    os.environ["ERPCLAW_DB_DIALECT"] = "sqlite"
    try:
        path = str(tmp_path / "lock_order.sqlite")
        init_all_tables(path)
        yield path
    finally:
        if old_dialect is None:
            os.environ.pop("ERPCLAW_DB_DIALECT", None)
        else:
            os.environ["ERPCLAW_DB_DIALECT"] = old_dialect


# ── shared builders ──

def _build_lock_env(conn, with_naming=True):
    """Company + stock item with 10 units in a stores warehouse at 20.00."""
    cid = seed_company(conn)
    seed_fiscal_year(conn, cid)
    ccid = seed_cost_center(conn, cid, "Main CC")
    stock_acct = seed_account(conn, cid, "Stock In Hand", "asset",
                              "stock", "1200")
    cogs_acct = seed_account(conn, cid, "COGS", "expense",
                             "cost_of_goods_sold", "5100")
    stock_adj = seed_account(conn, cid, "Stock Adjustment", "expense",
                             "stock_adjustment", "5200")
    seed_account(conn, cid, "SRNB", "liability",
                 "stock_received_not_billed", "2150")
    wh = seed_warehouse(conn, cid, "Stores", stock_acct)
    conn.execute(
        "UPDATE company SET default_cost_center_id = ?, "
        "default_warehouse_id = ? WHERE id = ?",
        (ccid, wh, cid))
    conn.commit()
    item = seed_item(conn, "Lock Widget", "Each", "stock", "20.00")
    seed_stock_entry_sle(conn, item, wh, "10", "20.00")
    if with_naming:
        seed_naming_series(conn, cid)
    return {
        "company_id": cid, "cc": ccid,
        "stock_acct": stock_acct, "cogs_acct": cogs_acct,
        "stock_adj": stock_adj, "warehouse": wh, "item": item,
    }


def _draft_issue(conn, env, qty="4", rate="20.00"):
    items = json.dumps([{
        "item_id": env["item"], "qty": qty, "rate": rate,
        "from_warehouse_id": env["warehouse"],
    }])
    result = call_action(mod.add_stock_entry, conn, ns(
        entry_type="issue", company_id=env["company_id"],
        posting_date="2026-06-15", items=items))
    assert is_ok(result), result
    return result["stock_entry_id"]


def _draft_recon(conn, env, qty="12", rate="20.00"):
    items = json.dumps([{
        "item_id": env["item"], "warehouse_id": env["warehouse"],
        "qty": qty, "valuation_rate": rate,
    }])
    result = call_action(mod.add_stock_reconciliation, conn, ns(
        posting_date="2026-06-15", items=items,
        company_id=env["company_id"]))
    assert is_ok(result), result
    return result["stock_reconciliation_id"]


def _rows(conn, sql, params=()):
    return [{k: (None if v is None else str(v)) for k, v in dict(r).items()}
            for r in conn.execute(sql, params).fetchall()]


def _snapshot(conn):
    out = {
        "stock_ledger_entry": _rows(
            conn, "SELECT * FROM stock_ledger_entry ORDER BY id"),
        "gl_entry": _rows(conn, "SELECT * FROM gl_entry ORDER BY id"),
        "stock_entry": _rows(conn, "SELECT * FROM stock_entry ORDER BY id"),
        "stock_reconciliation": _rows(
            conn, "SELECT * FROM stock_reconciliation ORDER BY id"),
        "stock_fifo_layer": _rows(
            conn, "SELECT * FROM stock_fifo_layer ORDER BY id"),
    }
    return out


def _assert_only_status_changed(before, after, table, doc_id,
                                old_status, new_status):
    assert set(before) == set(after) == {
        "stock_ledger_entry", "gl_entry", "stock_entry",
        "stock_reconciliation", "stock_fifo_layer"}
    for name in before:
        assert len(before[name]) == len(after[name]), name
        if name != table:
            assert before[name] == after[name], name
            continue
        seen = False
        for brow, arow in zip(before[name], after[name]):
            assert brow["id"] == arow["id"], name
            if brow["id"] != doc_id:
                assert brow == arow, name
                continue
            seen = True
            assert brow["status"] == old_status, (name, brow)
            assert arow["status"] == new_status, (name, arow)
            brest = dict(brow)
            arest = dict(arow)
            del brest["status"]
            del arest["status"]
            assert brest == arest, name
        assert seen, (table, doc_id)


def _install_stale_wrapper(monkeypatch, table, doc_id, new_status, counters):
    """Flip the document to new_status inside the head take, then re-take."""
    real_insert = mod.insert_sle_entries
    real_reverse = mod.reverse_sle_entries

    def fake_take(conn, company_ids):
        _real_take_heads(conn, company_ids)
        flip_t = Table(table)
        flip_q = (Q.update(flip_t).set(flip_t.status, P())
                  .where(flip_t.id == P()))
        conn.execute(flip_q.get_sql(), (new_status, doc_id))
        conn.commit()
        _real_take_heads(conn, company_ids)

    def counting_insert(conn, entries, **kwargs):
        counters["insert"] += 1
        return real_insert(conn, entries, **kwargs)

    def counting_reverse(conn, **kwargs):
        counters["reverse"] += 1
        return real_reverse(conn, **kwargs)

    monkeypatch.setattr(mod, "take_chain_heads", fake_take, raising=False)
    monkeypatch.setattr(mod, "insert_sle_entries", counting_insert,
                        raising=False)
    monkeypatch.setattr(mod, "reverse_sle_entries", counting_reverse,
                        raising=False)


# ── test 1: stale status under the head is refused before any write ──

def test_1a_submit_stale_status_refused_sqlite(lite_conn, lite_db,
                                               monkeypatch):
    env = _build_lock_env(lite_conn)
    se_id = _draft_issue(lite_conn, env)
    counters = {"insert": 0, "reverse": 0}
    _install_stale_wrapper(monkeypatch, "stock_entry", se_id, "submitted",
                           counters)
    fresh = get_conn(lite_db)
    try:
        before = _snapshot(fresh)
    finally:
        fresh.close()
    result = call_action(mod.submit_stock_entry, lite_conn, ns(
        stock_entry_id=se_id))
    assert is_error(result), result
    assert result["message"] == SUBMIT_MSG, result
    assert counters == {"insert": 0, "reverse": 0}, counters
    fresh = get_conn(lite_db)
    try:
        after = _snapshot(fresh)
    finally:
        fresh.close()
    _assert_only_status_changed(before, after, "stock_entry", se_id,
                                "draft", "submitted")


def test_1b_cancel_stale_status_refused_sqlite(lite_conn, lite_db,
                                               monkeypatch):
    env = _build_lock_env(lite_conn)
    se_id = _draft_issue(lite_conn, env)
    submitted = call_action(mod.submit_stock_entry, lite_conn, ns(
        stock_entry_id=se_id))
    assert is_ok(submitted), submitted
    counters = {"insert": 0, "reverse": 0}
    _install_stale_wrapper(monkeypatch, "stock_entry", se_id, "cancelled",
                           counters)
    fresh = get_conn(lite_db)
    try:
        before = _snapshot(fresh)
    finally:
        fresh.close()
    result = call_action(mod.cancel_stock_entry, lite_conn, ns(
        stock_entry_id=se_id))
    assert is_error(result), result
    assert result["message"] == CANCEL_MSG, result
    assert result.get("suggestion") == \
        "Only submitted stock entries can be cancelled.", result
    assert counters == {"insert": 0, "reverse": 0}, counters
    fresh = get_conn(lite_db)
    try:
        after = _snapshot(fresh)
    finally:
        fresh.close()
    _assert_only_status_changed(before, after, "stock_entry", se_id,
                                "submitted", "cancelled")


def test_1c_recon_stale_status_refused_sqlite(lite_conn, lite_db,
                                              monkeypatch):
    env = _build_lock_env(lite_conn)
    sr_id = _draft_recon(lite_conn, env)
    counters = {"insert": 0, "reverse": 0}
    _install_stale_wrapper(monkeypatch, "stock_reconciliation", sr_id,
                           "submitted", counters)
    fresh = get_conn(lite_db)
    try:
        before = _snapshot(fresh)
    finally:
        fresh.close()
    result = call_action(mod.submit_stock_reconciliation, lite_conn, ns(
        stock_reconciliation_id=sr_id))
    assert is_error(result), result
    assert result["message"] == RECON_MSG, result
    assert counters == {"insert": 0, "reverse": 0}, counters
    fresh = get_conn(lite_db)
    try:
        after = _snapshot(fresh)
    finally:
        fresh.close()
    _assert_only_status_changed(before, after, "stock_reconciliation",
                                sr_id, "draft", "submitted")


def test_1d_stale_status_refused_pg(pg_conn, monkeypatch):
    conn = pg_conn
    for table, draft_fn, run_fn, id_key, extra, old, new, message in (
        ("stock_entry", _draft_issue,
         lambda c, i: call_action(mod.submit_stock_entry, c, ns(
             stock_entry_id=i)),
         None, {}, "draft", "submitted", SUBMIT_MSG),
        ("stock_entry", None, None, None, {"cancel": True},
         "submitted", "cancelled", CANCEL_MSG),
        ("stock_reconciliation", _draft_recon,
         lambda c, i: call_action(mod.submit_stock_reconciliation, c, ns(
             stock_reconciliation_id=i)),
         None, {}, "draft", "submitted", RECON_MSG),
    ):
        env = _build_lock_env(conn, with_naming=False)
        if extra.get("cancel"):
            doc_id = _draft_issue(conn, env)
            submitted = call_action(mod.submit_stock_entry, conn, ns(
                stock_entry_id=doc_id))
            assert is_ok(submitted), submitted
            run = lambda c, i: call_action(  # noqa: E731
                mod.cancel_stock_entry, c, ns(stock_entry_id=i))
        else:
            doc_id = draft_fn(conn, env)
            run = run_fn
        counters = {"insert": 0, "reverse": 0}
        _install_stale_wrapper(monkeypatch, table, doc_id, new, counters)
        before = _snapshot(conn)
        result = run(conn, doc_id)
        assert is_error(result), result
        assert result["message"] == message, result
        assert counters == {"insert": 0, "reverse": 0}, counters
        fresh = get_connection()
        try:
            after = _snapshot(fresh)
        finally:
            fresh.close()
        _assert_only_status_changed(before, after, table, doc_id, old,
                                    new)
        monkeypatch.undo()


# ── test 2: head before the first write (SQLite) ──

def _write_statements(proxy):
    return [s for s in proxy.statements
            if s.lstrip()[:6].upper() in ("INSERT", "UPDATE", "DELETE")]


def _assert_head_first(writes, own_tables):
    assert writes, "expected the action to write"
    first = writes[0]
    assert first.lstrip().upper().startswith("INSERT"), first
    assert "gl_chain_head" in first, first
    for idx, stmt in enumerate(writes):
        if any(t in stmt for t in own_tables):
            assert idx > 0, stmt


def test_2a_submit_takes_head_before_first_write(lite_conn):
    env = _build_lock_env(lite_conn)
    se_id = _draft_issue(lite_conn, env)
    proxy = _RecordingProxy(lite_conn)
    result = call_action(mod.submit_stock_entry, proxy, ns(
        stock_entry_id=se_id))
    assert is_ok(result), result
    _assert_head_first(_write_statements(proxy),
                       ("stock_ledger_entry", "stock_fifo_layer",
                        "stock_entry"))


def test_2b_cancel_takes_head_before_first_write(lite_conn):
    env = _build_lock_env(lite_conn)
    se_id = _draft_issue(lite_conn, env)
    submitted = call_action(mod.submit_stock_entry, lite_conn, ns(
        stock_entry_id=se_id))
    assert is_ok(submitted), submitted
    proxy = _RecordingProxy(lite_conn)
    result = call_action(mod.cancel_stock_entry, proxy, ns(
        stock_entry_id=se_id))
    assert is_ok(result), result
    _assert_head_first(_write_statements(proxy),
                       ("stock_ledger_entry", "stock_fifo_layer",
                        "stock_entry"))


def test_2c_recon_takes_head_before_first_write(lite_conn):
    env = _build_lock_env(lite_conn)
    sr_id = _draft_recon(lite_conn, env)
    proxy = _RecordingProxy(lite_conn)
    result = call_action(mod.submit_stock_reconciliation, proxy, ns(
        stock_reconciliation_id=sr_id))
    assert is_ok(result), result
    _assert_head_first(_write_statements(proxy),
                       ("stock_ledger_entry", "stock_fifo_layer",
                        "stock_reconciliation"))


# ── test 3: happy paths unchanged (SQLite) ──

def test_3a_issue_submit_posts_once(lite_conn):
    env = _build_lock_env(lite_conn)
    se_id = _draft_issue(lite_conn, env)
    result = call_action(mod.submit_stock_entry, lite_conn, ns(
        stock_entry_id=se_id))
    assert is_ok(result), result
    sle = _rows(lite_conn,
                "SELECT actual_qty FROM stock_ledger_entry "
                "WHERE voucher_type = 'stock_entry' AND voucher_id = ? "
                "AND is_cancelled = 0", (se_id,))
    assert len(sle) == 1, sle
    assert sle[0]["actual_qty"] == "-4.00", sle
    legs = _rows(lite_conn,
                 "SELECT account_id, debit, credit FROM gl_entry "
                 "WHERE voucher_type = 'stock_entry' AND voucher_id = ? "
                 "AND is_cancelled = 0", (se_id,))
    assert len(legs) == 2, legs
    debits = [r for r in legs if Decimal(r["debit"]) != 0]
    credits = [r for r in legs if Decimal(r["credit"]) != 0]
    assert len(debits) == 1 and len(credits) == 1, legs
    assert debits[0]["account_id"] == env["cogs_acct"], legs
    assert Decimal(debits[0]["debit"]) == Decimal("80.00"), legs
    assert credits[0]["account_id"] == env["stock_acct"], legs
    assert Decimal(credits[0]["credit"]) == Decimal("80.00"), legs
    status = lite_conn.execute(
        "SELECT status FROM stock_entry WHERE id = ?", (se_id,)).fetchone()
    assert status["status"] == "submitted"


def test_3b_issue_cancel_mirrors_and_restores_balance(lite_conn):
    env = _build_lock_env(lite_conn)
    se_id = _draft_issue(lite_conn, env)
    submitted = call_action(mod.submit_stock_entry, lite_conn, ns(
        stock_entry_id=se_id))
    assert is_ok(submitted), submitted
    result = call_action(mod.cancel_stock_entry, lite_conn, ns(
        stock_entry_id=se_id))
    assert is_ok(result), result
    status = lite_conn.execute(
        "SELECT status FROM stock_entry WHERE id = ?", (se_id,)).fetchone()
    assert status["status"] == "cancelled"
    bals = _rows(lite_conn,
                 "SELECT actual_qty FROM stock_ledger_entry "
                 "WHERE item_id = ? AND warehouse_id = ? "
                 "AND is_cancelled = 0",
                 (env["item"], env["warehouse"]))
    total = sum((Decimal(r["actual_qty"]) for r in bals), Decimal("0"))
    assert total == Decimal("10"), bals


def test_3c_recon_submit_posts_difference(lite_conn):
    env = _build_lock_env(lite_conn)
    sr_id = _draft_recon(lite_conn, env)
    result = call_action(mod.submit_stock_reconciliation, lite_conn, ns(
        stock_reconciliation_id=sr_id))
    assert is_ok(result), result
    sle = _rows(lite_conn,
                "SELECT actual_qty FROM stock_ledger_entry "
                "WHERE voucher_type = 'stock_reconciliation' "
                "AND voucher_id = ? AND is_cancelled = 0", (sr_id,))
    assert len(sle) == 1, sle
    assert sle[0]["actual_qty"] == "2.00", sle
    legs = _rows(lite_conn,
                 "SELECT account_id, debit, credit FROM gl_entry "
                 "WHERE voucher_type = 'stock_reconciliation' "
                 "AND voucher_id = ? AND is_cancelled = 0", (sr_id,))
    assert len(legs) == 2, legs
    debits = [r for r in legs if Decimal(r["debit"]) != 0]
    credits = [r for r in legs if Decimal(r["credit"]) != 0]
    assert len(debits) == 1 and len(credits) == 1, legs
    assert debits[0]["account_id"] == env["stock_acct"], legs
    assert Decimal(debits[0]["debit"]) == Decimal("40.00"), legs
    assert credits[0]["account_id"] == env["stock_adj"], legs
    assert Decimal(credits[0]["credit"]) == Decimal("40.00"), legs
    status = lite_conn.execute(
        "SELECT status FROM stock_reconciliation WHERE id = ?",
        (sr_id,)).fetchone()
    assert status["status"] == "submitted"


# ── PostgreSQL legs ──

def _build_fifo_pg_env(conn):
    env = _build_lock_env(conn, with_naming=False)
    item_t = Table("item")
    fifo_q = (Q.update(item_t).set(item_t.valuation_method, P())
              .where(item_t.id == P()))
    conn.execute(fifo_q.get_sql(), ("fifo", env["item"]))
    seed = conn.execute(
        "SELECT id, voucher_id FROM stock_ledger_entry "
        "WHERE item_id = ? AND warehouse_id = ? AND is_cancelled = 0",
        (env["item"], env["warehouse"])).fetchone()
    assert seed is not None
    conn.execute(
        "INSERT INTO stock_fifo_layer (id, item_id, warehouse_id, "
        "posting_date, qty, rate, remaining_qty, source_voucher_type, "
        "source_voucher_id, created_at) VALUES (?, ?, ?, '2026-01-01', "
        "'10.00', '20.00', '10.00', 'stock_entry', ?, "
        "CAST(CURRENT_TIMESTAMP AS TEXT))",
        (str(uuid.uuid4()), env["item"], env["warehouse"],
         seed["voucher_id"]))
    conn.commit()
    return env


def _submit_cmd(se_id):
    return [sys.executable, _INVENTORY_SCRIPT,
            "--action", "submit-stock-entry", "--stock-entry-id", se_id]


def _cancel_cmd(se_id):
    return [sys.executable, _INVENTORY_SCRIPT,
            "--action", "cancel-stock-entry", "--stock-entry-id", se_id]


def _balance(conn, env):
    bals = _rows(conn,
                 "SELECT actual_qty FROM stock_ledger_entry "
                 "WHERE item_id = ? AND warehouse_id = ? "
                 "AND is_cancelled = 0",
                 (env["item"], env["warehouse"]))
    return sum((Decimal(r["actual_qty"]) for r in bals), Decimal("0"))


def test_4_head_first_pg(pg_conn):
    """A FIFO issue blocks on the head while holding no FIFO row."""
    conn = pg_conn
    env = _build_fifo_pg_env(conn)
    se_id = _draft_issue(conn, env)
    holder = get_connection()
    proc = None
    try:
        _real_take_heads(holder, [env["company_id"]])
        penv = proofs._proc_env(ERPCLAW_PG_LOCK_TIMEOUT="10s")
        proc = subprocess.Popen(
            _submit_cmd(se_id),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=penv)
        try:
            proc.wait(timeout=1.0)
            pytest.fail("submit-stock-entry should block on the held head "
                        "(rc=%s)" % proc.returncode)
        except subprocess.TimeoutExpired:
            assert proc.poll() is None, "submit must still be alive"
        probe = get_connection()
        try:
            probe.execute("SET lock_timeout = '1s'")
            probe.execute(
                "UPDATE stock_fifo_layer SET remaining_qty = remaining_qty "
                "WHERE item_id = ? AND warehouse_id = ?",
                (env["item"], env["warehouse"]))
            probe.rollback()
        finally:
            probe.close()
        holder.rollback()
        out, err_text = proc.communicate(timeout=12)
        assert proc.returncode == 0, (out, err_text)
        status = conn.execute(
            "SELECT status FROM stock_entry WHERE id = ?",
            (se_id,)).fetchone()
        assert status["status"] == "submitted"
    finally:
        try:
            holder.rollback()
        except Exception:  # noqa: BLE001, S110 - best effort
            pass
        holder.close()
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()


def test_5_posted_once_pg(pg_conn):
    """Two concurrent submits post once; two concurrent cancels reverse once."""
    conn = pg_conn
    last = None
    for _ in range(5):
        env = _build_lock_env(conn, with_naming=False)
        se_id = _draft_issue(conn, env)
        penv = proofs._proc_env()
        first = subprocess.Popen(
            _submit_cmd(se_id),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=penv)
        second = subprocess.Popen(
            _submit_cmd(se_id),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=penv)
        out1, err1 = first.communicate(timeout=15)
        out2, err2 = second.communicate(timeout=15)
        assert "deadlock" not in (out1 + err1 + out2 + err2).lower(), \
            (out1, err1, out2, err2)
        ordered = sorted([(first.returncode, out1, err1),
                          (second.returncode, out2, err2)])
        assert [rc for rc, _, _ in ordered] == [0, 1], \
            (out1, err1, out2, err2)
        assert json.loads(ordered[1][1])["message"] == SUBMIT_MSG, \
            (out1, err1, out2, err2)
        active = _rows(conn,
                       "SELECT id FROM stock_ledger_entry "
                       "WHERE voucher_type = 'stock_entry' AND voucher_id = ? "
                       "AND is_cancelled = 0", (se_id,))
        assert len(active) == 1, active
        assert _balance(conn, env) == Decimal("6"), se_id
        last = (env, se_id)
    env, se_id = last
    penv = proofs._proc_env()
    first = subprocess.Popen(
        _cancel_cmd(se_id),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=penv)
    second = subprocess.Popen(
        _cancel_cmd(se_id),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=penv)
    out1, err1 = first.communicate(timeout=15)
    out2, err2 = second.communicate(timeout=15)
    assert "deadlock" not in (out1 + err1 + out2 + err2).lower(), \
        (out1, err1, out2, err2)
    ordered = sorted([(first.returncode, out1, err1),
                      (second.returncode, out2, err2)])
    assert [rc for rc, _, _ in ordered] == [0, 1], \
        (out1, err1, out2, err2)
    assert json.loads(ordered[1][1])["message"] == CANCEL_MSG, \
        (out1, err1, out2, err2)
    rows = _rows(conn,
                 "SELECT id, is_cancelled FROM stock_ledger_entry "
                 "WHERE voucher_type = 'stock_entry' AND voucher_id = ?",
                 (se_id,))
    assert len(rows) == 2, rows
    assert all(int(r["is_cancelled"]) == 1 for r in rows), rows
    assert _balance(conn, env) == Decimal("10"), se_id
    proofs._assert_chain_intact(conn, env["company_id"])
    proofs._assert_contiguous(conn, env["company_id"])


def test_6_final_compare_and_set_pg(pg_conn, monkeypatch):
    """A mid-flight status change trips the final compare-and-set."""
    conn = pg_conn
    env = _build_lock_env(conn, with_naming=False)
    se_id = _draft_issue(conn, env)
    real_build = mod.create_perpetual_inventory_gl

    def sneaky(*args, **kwargs):
        built = real_build(*args, **kwargs)
        other = get_connection()
        try:
            flip_t = Table("stock_entry")
            flip_q = (Q.update(flip_t).set(flip_t.status, P())
                      .where(flip_t.id == P()))
            other.execute(flip_q.get_sql(), ("submitted", se_id))
            other.commit()
        finally:
            other.close()
        return built

    monkeypatch.setattr(mod, "create_perpetual_inventory_gl", sneaky,
                        raising=False)
    result = call_action(mod.submit_stock_entry, conn, ns(
        stock_entry_id=se_id))
    assert is_error(result), result
    assert result["message"] == SUBMIT_MSG, result
    fresh = get_connection()
    try:
        sle = _rows(fresh,
                    "SELECT id FROM stock_ledger_entry "
                    "WHERE voucher_type = 'stock_entry' AND voucher_id = ?",
                    (se_id,))
        gle = _rows(fresh,
                    "SELECT id FROM gl_entry "
                    "WHERE voucher_type = 'stock_entry' AND voucher_id = ?",
                    (se_id,))
        assert sle == [], sle
        assert gle == [], gle
    finally:
        fresh.close()


# ── test 7: rollback on a refusal after the stock writes (SQLite) ──

def test_7_gl_refusal_rolls_back_stock_writes(lite_conn, monkeypatch):
    env = _build_lock_env(lite_conn)
    se_id = _draft_issue(lite_conn, env)

    def boom(conn, entries, **kwargs):
        raise ValueError("planted")

    monkeypatch.setattr(mod, "insert_gl_entries", boom, raising=False)
    result = call_action(mod.submit_stock_entry, lite_conn, ns(
        stock_entry_id=se_id))
    assert is_error(result), result
    assert result["message"] == "GL posting failed: planted", result
    sle = _rows(lite_conn,
                "SELECT id FROM stock_ledger_entry "
                "WHERE voucher_type = 'stock_entry' AND voucher_id = ?",
                (se_id,))
    assert sle == [], sle
    status = lite_conn.execute(
        "SELECT status FROM stock_entry WHERE id = ?", (se_id,)).fetchone()
    assert status["status"] == "draft"


@pytest.fixture
def lite_conn(lite_db):
    old_dialect = os.environ.get("ERPCLAW_DB_DIALECT")
    os.environ["ERPCLAW_DB_DIALECT"] = "sqlite"
    conn = get_conn(lite_db)
    try:
        yield conn
    finally:
        conn.close()
        if old_dialect is None:
            os.environ.pop("ERPCLAW_DB_DIALECT", None)
        else:
            os.environ["ERPCLAW_DB_DIALECT"] = old_dialect


@pytest.fixture
def pg_conn():
    proofs._pg_only()
    old_url = os.environ.get("ERPCLAW_DB_URL")
    old_path = os.environ.get("ERPCLAW_DB_PATH")
    old_dialect = os.environ.get("ERPCLAW_DB_DIALECT")
    test_url = os.environ.get("ERPCLAW_PG_TEST_URL")
    assert test_url, "ERPCLAW_PG_TEST_URL is not set"
    os.environ["ERPCLAW_DB_URL"] = test_url
    os.environ.pop("ERPCLAW_DB_PATH", None)
    os.environ["ERPCLAW_DB_DIALECT"] = "postgresql"
    helpers_pg = _load("payments_helpers_pg", _PAYMENTS_HELPERS_PATH)
    helpers_pg.init_all_tables(None)
    conn = get_connection()
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001, S110 - best effort
            pass
        if old_url is None:
            os.environ.pop("ERPCLAW_DB_URL", None)
        else:
            os.environ["ERPCLAW_DB_URL"] = old_url
        if old_path is None:
            os.environ.pop("ERPCLAW_DB_PATH", None)
        else:
            os.environ["ERPCLAW_DB_PATH"] = old_path
        if old_dialect is None:
            os.environ.pop("ERPCLAW_DB_DIALECT", None)
        else:
            os.environ["ERPCLAW_DB_DIALECT"] = old_dialect
