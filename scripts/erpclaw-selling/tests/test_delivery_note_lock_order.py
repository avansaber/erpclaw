"""Submitting and cancelling a delivery note take the chain head first (m829).

Every action that posts to the ledger and changes a document's state takes
the company's ledger chain head before its first write, then decides on
state re-read under the head. ``submit-delivery-note`` and
``cancel-delivery-note`` now do the same, and both status flips are
compare-and-set, so a second submit (or cancel) waits on the head, sees the
fresh status, and is refused with the existing message while writing nothing.
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

from selling_helpers import (
    build_selling_env, call_action, get_conn, is_error, is_ok, load_db_query,
    ns, seed_item, seed_stock_entry,
)

try:
    from erpclaw_lib.db import get_connection, get_dialect
except ImportError:  # lib not on path in some minimal contexts
    def get_dialect():
        return os.environ.get("ERPCLAW_DB_DIALECT", "sqlite")
    get_connection = None

from erpclaw_lib.query import P, Q, Table

mod = load_db_query()

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_MODULE_DIR = os.path.dirname(_TESTS_DIR)
_SCRIPTS_DIR = os.path.dirname(_MODULE_DIR)
_SELLING_SCRIPT = os.path.join(_MODULE_DIR, "db_query.py")
_PAY_TESTS = os.path.join(_SCRIPTS_DIR, "erpclaw-payments", "tests")
_CHAIN_PROOFS_PATH = os.path.join(_PAY_TESTS, "test_chain_lock_proofs.py")
_RECORDING_PATH = os.path.join(
    _PAY_TESTS, "test_payment_edit_and_allocation_compare_and_set.py")

SUBMIT_STALE_MSG = (
    "Cannot submit: delivery note is 'submitted' (must be 'draft')")
CANCEL_STALE_MSG = (
    "Cannot cancel: delivery note is 'cancelled' (must be 'submitted')")

SNAPSHOT_TABLES = (
    "stock_ledger_entry",
    "gl_entry",
    "delivery_note",
    "sales_order_item",
    "serial_number",
)

_WRITE_KINDS = ("INSERT", "UPDATE", "DELETE")
_TRACKED_WRITES = (
    "stock_ledger_entry",
    "serial_number",
    "delivery_note",
    "sales_order_item",
)


def _load_chain_proofs():
    if _PAY_TESTS not in sys.path:
        sys.path.insert(0, _PAY_TESTS)
    spec = importlib.util.spec_from_file_location(
        "payments_chain_lock_proofs", _CHAIN_PROOFS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_recording_proxy():
    if _PAY_TESTS not in sys.path:
        sys.path.insert(0, _PAY_TESTS)
    spec = importlib.util.spec_from_file_location(
        "payments_recording_proxy", _RECORDING_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._RecordingProxy


chain_proofs = _load_chain_proofs()
_RecordingProxy = _load_recording_proxy()


def _setup_draft_note(conn, valuation_rate="20.00", qty="4", rate="100.00"):
    """Company + stock item (10 units) + customer + confirmed SO + draft DN."""
    env = build_selling_env(conn)
    item = seed_item(conn, "DN Widget")
    seed_stock_entry(conn, item, env["warehouse"], "10", valuation_rate)
    conn.commit()
    items = json.dumps([{
        "item_id": item, "qty": qty, "rate": rate,
        "warehouse_id": env["warehouse"],
    }])
    so = call_action(mod.add_sales_order, conn, ns(
        customer_id=env["customer"], company_id=env["company_id"],
        posting_date="2026-06-15", items=items,
        delivery_date="2026-07-01", tax_template_id=None,
    ))
    assert is_ok(so), so
    so_id = so["sales_order_id"]
    submit = call_action(mod.submit_sales_order, conn, ns(
        sales_order_id=so_id))
    assert is_ok(submit), submit
    dn = call_action(mod.create_delivery_note, conn, ns(
        sales_order_id=so_id, posting_date="2026-06-20",
        items=None,
    ))
    assert is_ok(dn), dn
    conn.commit()
    return env, item, so_id, dn["delivery_note_id"]


def _snapshot(conn):
    snap = {}
    for table in SNAPSHOT_TABLES:
        rows = conn.execute("SELECT * FROM %s ORDER BY id" % table).fetchall()
        norm = []
        for row in rows:
            record = dict(row)
            norm.append(tuple(sorted((k, str(v)) for k, v in record.items())))
        norm.sort()
        snap[table] = norm
    return snap


def _snapshot_ignoring_dn_status(snap):
    out = dict(snap)
    normed = []
    for row in snap["delivery_note"]:
        normed.append(tuple((k, "*" if k == "status" else v) for k, v in row))
    normed.sort()
    out["delivery_note"] = normed
    return out


def _fresh_conn(db_path):
    if get_dialect() == "postgresql":
        return get_connection()
    return get_conn(db_path)


def _dn_status(conn, dn_id):
    return conn.execute(
        "SELECT status FROM delivery_note WHERE id = ?",
        (dn_id,)).fetchone()["status"]


def _so_item(conn, so_id):
    return conn.execute(
        "SELECT id, delivered_qty FROM sales_order_item "
        "WHERE sales_order_id = ?", (so_id,)).fetchone()


def _sle_count(conn, dn_id, only_active=False):
    query = ("SELECT COUNT(*) AS c FROM stock_ledger_entry "
             "WHERE voucher_type = 'delivery_note' AND voucher_id = ?")
    if only_active:
        query += " AND is_cancelled = 0"
    return conn.execute(query, (dn_id,)).fetchone()["c"]


def _stock_balance(conn, item_id, warehouse_id):
    rows = conn.execute(
        "SELECT actual_qty FROM stock_ledger_entry "
        "WHERE item_id = ? AND warehouse_id = ? AND is_cancelled = 0",
        (item_id, warehouse_id)).fetchall()
    total = Decimal("0")
    for row in rows:
        total += Decimal(str(row["actual_qty"]))
    return total


def _assert_head_first_order(statements):
    writes = []
    for sql in statements:
        parts = sql.lstrip().split(None, 1)
        if parts and parts[0].upper() in _WRITE_KINDS:
            writes.append(sql)
    assert writes, "expected writes, got: %r" % (statements[:10],)
    assert writes[0].lstrip().upper().startswith("INSERT"), writes[0]
    assert "gl_chain_head" in writes[0], writes[0]
    head_pos = statements.index(writes[0])
    for sql in statements[:head_pos]:
        parts = sql.lstrip().split(None, 1)
        assert not (parts and parts[0].upper() in _WRITE_KINDS), sql
    after = statements[head_pos + 1:]
    tracked_after = [
        sql for sql in after
        if sql.lstrip().split(None, 1)[0].upper() in _WRITE_KINDS
        and any(table in sql for table in _TRACKED_WRITES)
    ]
    assert any("stock_ledger_entry" in sql for sql in tracked_after), after[:5]
    assert any("delivery_note" in sql for sql in tracked_after), after[:5]


def _pg_only():
    chain_proofs._pg_only()


def _sqlite_only():
    if get_dialect() == "postgresql":
        pytest.skip("SQLite-only case")


# ── 1. stale status under the head is refused before any write ──

def test_submit_stale_status_under_head_refused(db_path, conn, monkeypatch):
    from erpclaw_lib.gl_posting import take_chain_heads as real_take
    env, item, so_id, dn_id = _setup_draft_note(conn)
    before = _snapshot(conn)

    def wrapper(c, company_ids):
        real_take(c, company_ids)
        table = Table("delivery_note")
        uq = Q.update(table).set(table.status, P()).where(table.id == P())
        c.execute(uq.get_sql(), ("submitted", dn_id))
        c.commit()
        real_take(c, company_ids)

    monkeypatch.setattr(mod, "take_chain_heads", wrapper, raising=False)
    insert_calls = []
    reverse_calls = []
    real_insert = mod.insert_sle_entries
    real_reverse = mod.reverse_sle_entries

    def counting_insert(*args, **kwargs):
        insert_calls.append(1)
        return real_insert(*args, **kwargs)

    def counting_reverse(*args, **kwargs):
        reverse_calls.append(1)
        return real_reverse(*args, **kwargs)

    monkeypatch.setattr(mod, "insert_sle_entries", counting_insert)
    monkeypatch.setattr(mod, "reverse_sle_entries", counting_reverse)

    result = call_action(mod.submit_delivery_note, conn,
                         ns(delivery_note_id=dn_id))
    assert is_error(result), result
    assert result.get("message") == SUBMIT_STALE_MSG, result
    assert insert_calls == [], insert_calls
    assert reverse_calls == [], reverse_calls
    fresh = _fresh_conn(db_path)
    try:
        assert _dn_status(fresh, dn_id) == "submitted"
        assert (_snapshot_ignoring_dn_status(_snapshot(fresh))
                == _snapshot_ignoring_dn_status(before))
    finally:
        fresh.close()


def test_cancel_stale_status_under_head_refused(db_path, conn, monkeypatch):
    from erpclaw_lib.gl_posting import take_chain_heads as real_take
    env, item, so_id, dn_id = _setup_draft_note(conn)
    submit = call_action(mod.submit_delivery_note, conn,
                         ns(delivery_note_id=dn_id))
    assert is_ok(submit), submit
    conn.commit()
    before = _snapshot(conn)

    def wrapper(c, company_ids):
        real_take(c, company_ids)
        table = Table("delivery_note")
        uq = Q.update(table).set(table.status, P()).where(table.id == P())
        c.execute(uq.get_sql(), ("cancelled", dn_id))
        c.commit()
        real_take(c, company_ids)

    monkeypatch.setattr(mod, "take_chain_heads", wrapper, raising=False)
    insert_calls = []
    reverse_calls = []
    real_insert = mod.insert_sle_entries
    real_reverse = mod.reverse_sle_entries

    def counting_insert(*args, **kwargs):
        insert_calls.append(1)
        return real_insert(*args, **kwargs)

    def counting_reverse(*args, **kwargs):
        reverse_calls.append(1)
        return real_reverse(*args, **kwargs)

    monkeypatch.setattr(mod, "insert_sle_entries", counting_insert)
    monkeypatch.setattr(mod, "reverse_sle_entries", counting_reverse)

    result = call_action(mod.cancel_delivery_note, conn,
                         ns(delivery_note_id=dn_id))
    assert is_error(result), result
    assert result.get("message") == CANCEL_STALE_MSG, result
    assert insert_calls == [], insert_calls
    assert reverse_calls == [], reverse_calls
    fresh = _fresh_conn(db_path)
    try:
        assert _dn_status(fresh, dn_id) == "cancelled"
        assert (_snapshot_ignoring_dn_status(_snapshot(fresh))
                == _snapshot_ignoring_dn_status(before))
    finally:
        fresh.close()


# ── 2. head before the first write (SQLite) ──

def test_submit_head_before_first_write(db_path, conn):
    _sqlite_only()
    env, item, so_id, dn_id = _setup_draft_note(conn)
    proxy = _RecordingProxy(conn)
    result = call_action(mod.submit_delivery_note, proxy,
                         ns(delivery_note_id=dn_id))
    assert is_ok(result), result
    _assert_head_first_order(proxy.statements)


def test_cancel_head_before_first_write(db_path, conn):
    _sqlite_only()
    env, item, so_id, dn_id = _setup_draft_note(conn)
    submit = call_action(mod.submit_delivery_note, conn,
                         ns(delivery_note_id=dn_id))
    assert is_ok(submit), submit
    conn.commit()
    proxy = _RecordingProxy(conn)
    result = call_action(mod.cancel_delivery_note, proxy,
                         ns(delivery_note_id=dn_id))
    assert is_ok(result), result
    _assert_head_first_order(proxy.statements)


# ── 3. happy paths unchanged ──

def test_submit_happy_path(db_path, conn):
    pg = get_dialect() == "postgresql"
    env, item, so_id, dn_id = _setup_draft_note(conn)
    result = call_action(mod.submit_delivery_note, conn,
                         ns(delivery_note_id=dn_id))
    assert is_ok(result), result
    assert result["sle_entries_created"] == 1, result
    assert result["gl_entries_created"] == 2, result

    sle = conn.execute(
        "SELECT actual_qty, valuation_rate FROM stock_ledger_entry "
        "WHERE voucher_type = 'delivery_note' AND voucher_id = ? "
        "AND is_cancelled = 0", (dn_id,)).fetchall()
    assert len(sle) == 1, [dict(r) for r in sle]
    if pg:
        assert Decimal(str(sle[0]["actual_qty"])) == Decimal("-4")
    else:
        assert sle[0]["actual_qty"] == "-4.00", dict(sle[0])
    assert Decimal(str(sle[0]["valuation_rate"])) == Decimal("20.00")

    legs = conn.execute(
        "SELECT account_id, debit, credit FROM gl_entry "
        "WHERE voucher_type = 'delivery_note' AND voucher_id = ?",
        (dn_id,)).fetchall()
    assert len(legs) == 2, [dict(r) for r in legs]
    debit = [r for r in legs if Decimal(str(r["debit"])) != 0]
    credit = [r for r in legs if Decimal(str(r["credit"])) != 0]
    assert len(debit) == 1 and len(credit) == 1, [dict(r) for r in legs]
    assert debit[0]["account_id"] == env["cogs"], dict(debit[0])
    assert credit[0]["account_id"] == env["stock_acct"], dict(credit[0])
    assert Decimal(str(debit[0]["debit"])) == Decimal("80.00")
    assert Decimal(str(debit[0]["credit"])) == Decimal("0")
    assert Decimal(str(credit[0]["credit"])) == Decimal("80.00")
    assert Decimal(str(credit[0]["debit"])) == Decimal("0")
    if not pg:
        assert debit[0]["debit"] == "80.00", dict(debit[0])
        assert debit[0]["credit"] == "0.00", dict(debit[0])
        assert credit[0]["credit"] == "80.00", dict(credit[0])
        assert credit[0]["debit"] == "0.00", dict(credit[0])

    assert _dn_status(conn, dn_id) == "submitted"
    line = _so_item(conn, so_id)
    if pg:
        assert Decimal(str(line["delivered_qty"])) == Decimal("4")
    else:
        assert line["delivered_qty"] == "4", dict(line)


def test_cancel_happy_path(db_path, conn):
    pg = get_dialect() == "postgresql"
    env, item, so_id, dn_id = _setup_draft_note(conn)
    submit = call_action(mod.submit_delivery_note, conn,
                         ns(delivery_note_id=dn_id))
    assert is_ok(submit), submit
    result = call_action(mod.cancel_delivery_note, conn,
                         ns(delivery_note_id=dn_id))
    assert is_ok(result), result

    rows = conn.execute(
        "SELECT actual_qty, is_cancelled FROM stock_ledger_entry "
        "WHERE voucher_type = 'delivery_note' AND voucher_id = ? "
        "ORDER BY id", (dn_id,)).fetchall()
    assert len(rows) == 2, [dict(r) for r in rows]
    assert all(int(r["is_cancelled"]) == 1 for r in rows)
    qtys = sorted(Decimal(str(r["actual_qty"])) for r in rows)
    assert qtys == [Decimal("-4"), Decimal("4")], [dict(r) for r in rows]

    assert _dn_status(conn, dn_id) == "cancelled"
    line = _so_item(conn, so_id)
    if pg:
        assert Decimal(str(line["delivered_qty"])) == Decimal("0")
    else:
        assert line["delivered_qty"] == "0", dict(line)


# ── 4. head first (PostgreSQL only) ──

def _setup_fifo_note(conn):
    env, item, so_id, dn_id = _setup_draft_note(conn)
    conn.execute("UPDATE item SET valuation_method = ? WHERE id = ?",
                 ("fifo", item))
    conn.execute(
        "INSERT INTO stock_fifo_layer (id, item_id, warehouse_id, posting_date,"
        " qty, rate, remaining_qty, source_voucher_type, source_voucher_id)"
        " VALUES (?, ?, ?, '2026-01-01', '10', '20.00', '10',"
        " 'stock_entry', ?)",
        (uuid.uuid4().hex, item, env["warehouse"], uuid.uuid4().hex))
    conn.commit()
    return env, item, so_id, dn_id


def test_submit_waits_on_head_before_touching_fifo(db_path):
    _pg_only()
    from erpclaw_lib.gl_posting import take_chain_heads as real_take
    conn = get_connection()
    holder = get_connection()
    probe = get_connection()
    proc = None
    try:
        env, item, so_id, dn_id = _setup_fifo_note(conn)
        real_take(holder, [env["company_id"]])
        penv = chain_proofs._proc_env(ERPCLAW_PG_LOCK_TIMEOUT="10s")
        proc = subprocess.Popen(
            [sys.executable, _SELLING_SCRIPT,
             "--action", "submit-delivery-note",
             "--delivery-note-id", dn_id],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=penv)
        try:
            proc.wait(timeout=1.0)
            pytest.fail("submit-delivery-note should block on the held head "
                        "(rc=%s)" % (proc.returncode,))
        except subprocess.TimeoutExpired:
            assert proc.poll() is None, "submit must still be running"
        probe.execute("SET lock_timeout = '1s'")
        probe.execute(
            "UPDATE stock_fifo_layer SET remaining_qty = remaining_qty "
            "WHERE item_id = ? AND warehouse_id = ?",
            (item, env["warehouse"]))
        probe.rollback()
        holder.rollback()
        out, err = proc.communicate(timeout=12)
        assert proc.returncode == 0, (out, err)
        check = get_connection()
        try:
            assert _dn_status(check, dn_id) == "submitted"
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


# ── 5. submitted once at zero valuation (PostgreSQL only) ──

def _popen_dn_action(action, dn_id, penv):
    return subprocess.Popen(
        [sys.executable, _SELLING_SCRIPT,
         "--action", action, "--delivery-note-id", dn_id],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=penv)


def _staggered_pair(action, dn_id, penv):
    start = time.monotonic()
    first = _popen_dn_action(action, dn_id, penv)
    elapsed = time.monotonic() - start
    if elapsed < 0.05:
        time.sleep(0.05 - elapsed)
    second = _popen_dn_action(action, dn_id, penv)
    return first, second


def _join_pair(action, pair):
    results = []
    for proc in pair:
        try:
            out, err = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            pytest.fail("%s timed out (out=%r err=%r)" % (action, out, err))
        results.append((proc.returncode, out, err))
    return results


def _is_action_json(out):
    try:
        data = json.loads(out)
    except Exception:
        return False
    return isinstance(data, dict) and "status" in data


def _is_infra_crash(result):
    """A proc that died setting its connection up, before the action ran.

    ``erpclaw_lib.db._ensure_pg_decimal_sum`` re-runs its CREATE OR REPLACE
    DDL on every fresh PostgreSQL connection; two connections opened at once
    can collide with ``tuple concurrently updated`` and the child exits 1
    with a traceback and no action JSON. That is foundation trouble outside
    this task (recorded in CHANGES.md): the proc never took the head and
    never ran the action, so the round is re-run with a fresh company.
    """
    code, out, err = result
    return (code != 0 and not _is_action_json(out)
            and "tuple concurrently updated" in err
            and "_ensure_pg_decimal_sum" in err)


def _assert_one_winner(results, expected_message, action):
    for _, out, err in results:
        assert "deadlock" not in (out + err).lower(), (out, err)
    codes = sorted(r[0] for r in results)
    assert codes == [0, 1], [(r[0], r[1], r[2]) for r in results]
    loser_out = [r[1] for r in results if r[0] == 1][0]
    assert _is_action_json(loser_out), (action, results)
    assert json.loads(loser_out)["message"] == expected_message, loser_out


def _fresh_zero_valuation_round():
    conn = get_connection()
    try:
        return _setup_draft_note(conn, valuation_rate="0.00")
    finally:
        conn.close()


def test_concurrent_submits_and_cancels_post_once(db_path):
    _pg_only()
    penv = chain_proofs._proc_env()
    clean_rounds = 0
    attempts = 0
    while clean_rounds < 5:
        attempts += 1
        assert attempts <= 10, "too many infrastructure retries"
        env, item, so_id, dn_id = _fresh_zero_valuation_round()

        submitted = _join_pair(
            "submit-delivery-note",
            _staggered_pair("submit-delivery-note", dn_id, penv))
        if any(_is_infra_crash(r) for r in submitted):
            continue

        _assert_one_winner(submitted, SUBMIT_STALE_MSG,
                           "submit-delivery-note")

        check = get_connection()
        try:
            assert _sle_count(check, dn_id, only_active=True) == 1
            line = check.execute(
                "SELECT delivered_qty FROM sales_order_item "
                "WHERE sales_order_id = ?", (so_id,)).fetchone()
            assert Decimal(str(line["delivered_qty"])) == Decimal("4")
        finally:
            check.close()

        cancelled = _join_pair(
            "cancel-delivery-note",
            _staggered_pair("cancel-delivery-note", dn_id, penv))
        if any(_is_infra_crash(r) for r in cancelled):
            continue

        _assert_one_winner(cancelled, CANCEL_STALE_MSG,
                           "cancel-delivery-note")

        check = get_connection()
        try:
            rows = check.execute(
                "SELECT actual_qty, is_cancelled FROM stock_ledger_entry "
                "WHERE voucher_type = 'delivery_note' AND voucher_id = ?",
                (dn_id,)).fetchall()
            assert len(rows) == 2, [dict(r) for r in rows]
            assert all(int(r["is_cancelled"]) == 1 for r in rows), \
                [dict(r) for r in rows]
            assert _stock_balance(check, item, env["warehouse"]) == Decimal("10")
            line = check.execute(
                "SELECT delivered_qty FROM sales_order_item "
                "WHERE sales_order_id = ?", (so_id,)).fetchone()
            assert Decimal(str(line["delivered_qty"])) == Decimal("0")
            chain_proofs._assert_chain_intact(check, env["company_id"])
            chain_proofs._assert_contiguous(check, env["company_id"])
        finally:
            check.close()
        clean_rounds += 1


# ── 6. final compare-and-set (PostgreSQL only) ──

def test_submit_final_compare_and_set(db_path, conn, monkeypatch):
    _pg_only()
    env, item, so_id, dn_id = _setup_draft_note(conn)
    real_create = mod.create_perpetual_inventory_gl

    def wrapped(*args, **kwargs):
        result = real_create(*args, **kwargs)
        other = get_connection()
        try:
            table = Table("delivery_note")
            uq = Q.update(table).set(table.status, P()).where(table.id == P())
            other.execute(uq.get_sql(), ("submitted", dn_id))
            other.commit()
        finally:
            other.close()
        return result

    monkeypatch.setattr(mod, "create_perpetual_inventory_gl", wrapped)
    result = call_action(mod.submit_delivery_note, conn,
                         ns(delivery_note_id=dn_id))
    assert is_error(result), result
    assert result.get("message") == SUBMIT_STALE_MSG, result
    fresh = get_connection()
    try:
        assert fresh.execute(
            "SELECT id FROM stock_ledger_entry "
            "WHERE voucher_type = 'delivery_note' AND voucher_id = ?",
            (dn_id,)).fetchall() == []
        assert fresh.execute(
            "SELECT id FROM gl_entry "
            "WHERE voucher_type = 'delivery_note' AND voucher_id = ?",
            (dn_id,)).fetchall() == []
    finally:
        fresh.close()


# ── 7. rollback on a refusal after the stock writes (SQLite) ──

def test_submit_rolls_back_stock_when_gl_refused(db_path, conn, monkeypatch):
    _sqlite_only()
    env, item, so_id, dn_id = _setup_draft_note(conn)
    line_before = _so_item(conn, so_id)

    def _boom(*args, **kwargs):
        raise ValueError("planted")

    monkeypatch.setattr(mod, "insert_gl_entries", _boom)
    result = call_action(mod.submit_delivery_note, conn,
                         ns(delivery_note_id=dn_id))
    assert is_error(result), result
    assert result.get("message") == "GL posting failed: planted", result
    assert conn.execute(
        "SELECT id FROM stock_ledger_entry "
        "WHERE voucher_type = 'delivery_note' AND voucher_id = ?",
        (dn_id,)).fetchall() == []
    assert _dn_status(conn, dn_id) == "draft"
    line_after = _so_item(conn, so_id)
    assert line_after["delivered_qty"] == line_before["delivered_qty"]
