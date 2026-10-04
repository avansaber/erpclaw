"""Revalue-stock takes the chain head first (task m849).

1. Re-read under the head (SQLite + PostgreSQL).
2. Head before the first write (SQLite).
3. Happy paths unchanged (SQLite).
4. Head first (PostgreSQL only).
5. Revalued once (PostgreSQL only).
6. Rollback on a refusal after the first write (SQLite).
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
    init_all_tables, get_conn,
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
_LOCK_ORDER_PATH = os.path.join(_TESTS_DIR, "test_stock_document_lock_order.py")
_INVENTORY_SCRIPT = os.path.join(
    _SCRIPTS_DIR, "erpclaw-inventory", "db_query.py")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


proofs = _load("payments_chain_lock_proofs_revalue", _PROOFS_PATH)
_cas = _load("payments_cas_proxy_module_revalue", _CAS_PATH)
_lock = _load("stock_document_lock_order_helpers_revalue", _LOCK_ORDER_PATH)
_RecordingProxy = _cas._RecordingProxy

_build_lock_env = _lock._build_lock_env
_build_fifo_pg_env = _lock._build_fifo_pg_env
_rows = _lock._rows


@pytest.fixture
def lite_db(tmp_path):
    old_dialect = os.environ.get("ERPCLAW_DB_DIALECT")
    os.environ["ERPCLAW_DB_DIALECT"] = "sqlite"
    try:
        path = str(tmp_path / "revalue_lock.sqlite")
        init_all_tables(path)
        yield path
    finally:
        if old_dialect is None:
            os.environ.pop("ERPCLAW_DB_DIALECT", None)
        else:
            os.environ["ERPCLAW_DB_DIALECT"] = old_dialect


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
    if os.environ.get("ERPCLAW_DB_DIALECT") != "postgresql" or not os.environ.get("ERPCLAW_PG_TEST_URL"):
        pytest.skip("PostgreSQL-only case: needs the ERPCLAW_PG_TEST_URL lane")
    old_url = os.environ.get("ERPCLAW_DB_URL")
    old_path = os.environ.get("ERPCLAW_DB_PATH")
    old_dialect = os.environ.get("ERPCLAW_DB_DIALECT")
    test_url = os.environ.get("ERPCLAW_PG_TEST_URL")
    os.environ["ERPCLAW_DB_URL"] = test_url
    os.environ.pop("ERPCLAW_DB_PATH", None)
    os.environ["ERPCLAW_DB_DIALECT"] = "postgresql"
    helpers_pg = _load("payments_helpers_pg_revalue", _PAYMENTS_HELPERS_PATH)
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


def _revalue_args(env, new_rate):
    return ns(
        item_id=env["item"], warehouse_id=env["warehouse"],
        new_rate=new_rate, posting_date="2026-06-15",
        reason="Lock test")


def _fiscal_year_name(conn, company_id):
    row = conn.execute(
        "SELECT name FROM fiscal_year WHERE company_id = ?",
        (company_id,)).fetchone()
    if row is None:
        return None
    try:
        return row["name"]
    except Exception:  # noqa: BLE001
        return row[0]


def _plant_valuation_row(conn, env, valuation_rate="22.00",
                         stock_value="220.00", diff="20.00"):
    fy = _fiscal_year_name(conn, env["company_id"])
    sle_t = Table("stock_ledger_entry")
    q = (Q.into(sle_t).columns(
        "id", "posting_date", "item_id", "warehouse_id",
        "actual_qty", "qty_after_transaction", "valuation_rate",
        "stock_value", "stock_value_difference",
        "voucher_type", "voucher_id", "incoming_rate",
        "is_cancelled", "fiscal_year", "created_at",
    ).insert(P(), P(), P(), P(), P(), P(), P(), P(), P(), P(),
             P(), P(), P(), P(), P()))
    conn.execute(
        q.get_sql(),
        (str(uuid.uuid4()), "2026-06-14", env["item"], env["warehouse"],
         "0", "10.00", valuation_rate, stock_value, diff,
         "stock_revaluation", str(uuid.uuid4()), "0",
         0, fy, "2999-01-01 00:00:00"))


def _plant_issue_row(conn, env):
    fy = _fiscal_year_name(conn, env["company_id"])
    sle_t = Table("stock_ledger_entry")
    q = (Q.into(sle_t).columns(
        "id", "posting_date", "item_id", "warehouse_id",
        "actual_qty", "qty_after_transaction", "valuation_rate",
        "stock_value", "stock_value_difference",
        "voucher_type", "voucher_id", "incoming_rate",
        "is_cancelled", "fiscal_year", "created_at",
    ).insert(P(), P(), P(), P(), P(), P(), P(), P(), P(), P(),
             P(), P(), P(), P(), P()))
    conn.execute(
        q.get_sql(),
        (str(uuid.uuid4()), "2026-06-14", env["item"], env["warehouse"],
         "-10.00", "0.00", "20.00", "0.00", "-200.00",
         "stock_entry", str(uuid.uuid4()), "0",
         0, fy, "2999-01-01 00:00:00"))


def _install_plant_valuation(monkeypatch, mode="valuation"):
    def fake_take(conn, company_ids):
        _real_take_heads(conn, company_ids)
        if mode == "valuation":
            _plant_valuation_row(conn, fake_take.env)
        else:
            _plant_issue_row(conn, fake_take.env)
        conn.commit()
        _real_take_heads(conn, company_ids)
    fake_take.env = None
    monkeypatch.setattr(mod, "take_chain_heads", fake_take, raising=False)
    return fake_take


# ── test 1: re-read under the head ──

def test_1a_same_rate_reread_sqlite(lite_conn, lite_db, monkeypatch):
    env = _build_lock_env(lite_conn)
    fake = _install_plant_valuation(monkeypatch, "valuation")
    fake.env = env
    result = call_action(mod.revalue_stock, lite_conn, _revalue_args(env, "22.00"))
    assert is_error(result), result
    assert result["message"] == \
        "New rate (22.00) is the same as current rate (22.00). No revaluation needed.", result
    fresh = get_conn(lite_db)
    try:
        revals = _rows(fresh, "SELECT id FROM stock_revaluation")
        assert revals == [], revals
        sle = _rows(
            fresh,
            "SELECT valuation_rate FROM stock_ledger_entry "
            "WHERE voucher_type = 'stock_revaluation' AND is_cancelled = 0")
        assert len(sle) == 1, sle
        assert sle[0]["valuation_rate"] == "22.00", sle
        gle = _rows(
            fresh,
            "SELECT id FROM gl_entry WHERE voucher_type = 'stock_revaluation'")
        assert gle == [], gle
    finally:
        fresh.close()


def test_1a_same_rate_reread_pg(pg_conn, monkeypatch):
    conn = pg_conn
    env = _build_lock_env(conn, with_naming=False)
    fake = _install_plant_valuation(monkeypatch, "valuation")
    fake.env = env
    result = call_action(mod.revalue_stock, conn, _revalue_args(env, "22.00"))
    assert is_error(result), result
    assert result["message"] == \
        "New rate (22.00) is the same as current rate (22.00). No revaluation needed.", result
    fresh = get_connection()
    try:
        revals = _rows(fresh, "SELECT id FROM stock_revaluation WHERE item_id = ?",
                       (env["item"],))
        assert revals == [], revals
        sle = _rows(
            fresh,
            "SELECT valuation_rate FROM stock_ledger_entry "
            "WHERE voucher_type = 'stock_revaluation' AND is_cancelled = 0 "
            "AND item_id = ?", (env["item"],))
        assert len(sle) == 1, sle
        assert Decimal(str(sle[0]["valuation_rate"])) == Decimal("22.00"), sle
        gle = _rows(
            fresh,
            "SELECT g.id FROM gl_entry g JOIN account a ON a.id = g.account_id "
            "WHERE g.voucher_type = 'stock_revaluation' AND a.company_id = ?",
            (env["company_id"],))
        assert gle == [], gle
    finally:
        fresh.close()


def test_1b_adjustment_from_reread_sqlite(lite_conn, lite_db, monkeypatch):
    env = _build_lock_env(lite_conn)
    fake = _install_plant_valuation(monkeypatch, "valuation")
    fake.env = env
    result = call_action(mod.revalue_stock, lite_conn, _revalue_args(env, "25.00"))
    assert is_ok(result), result
    assert result["old_rate"] == "22.00", result
    assert result["adjustment_amount"] == "30.00", result
    assert result["current_qty"] == "10.00", result
    fresh = get_conn(lite_db)
    try:
        stored = fresh.execute(
            "SELECT adjustment_amount FROM stock_revaluation WHERE id = ?",
            (result["revaluation_id"],)).fetchone()
        assert stored["adjustment_amount"] == "30.00", dict(stored)
        legs = _rows(
            fresh,
            "SELECT account_id, debit, credit FROM gl_entry "
            "WHERE voucher_type = 'stock_revaluation' AND voucher_id = ? "
            "AND is_cancelled = 0", (result["revaluation_id"],))
        assert len(legs) == 2, legs
        by_acct = {r["account_id"]: r for r in legs}
        assert by_acct[env["stock_acct"]]["debit"] == "30.00", legs
        assert by_acct[env["stock_acct"]]["credit"] == "0.00", legs
        assert by_acct[env["stock_adj"]]["debit"] == "0.00", legs
        assert by_acct[env["stock_adj"]]["credit"] == "30.00", legs
    finally:
        fresh.close()


def test_1b_adjustment_from_reread_pg(pg_conn, monkeypatch):
    conn = pg_conn
    env = _build_lock_env(conn, with_naming=False)
    fake = _install_plant_valuation(monkeypatch, "valuation")
    fake.env = env
    result = call_action(mod.revalue_stock, conn, _revalue_args(env, "25.00"))
    assert is_ok(result), result
    assert result["old_rate"] == "22.00", result
    assert result["adjustment_amount"] == "30.00", result
    assert result["current_qty"] == "10.00", result
    fresh = get_connection()
    try:
        stored = fresh.execute(
            "SELECT adjustment_amount FROM stock_revaluation WHERE id = ?",
            (result["revaluation_id"],)).fetchone()
        assert Decimal(str(stored["adjustment_amount"])) == Decimal("30.00"), dict(stored)
        legs = _rows(
            fresh,
            "SELECT account_id, debit, credit FROM gl_entry "
            "WHERE voucher_type = 'stock_revaluation' AND voucher_id = ? "
            "AND is_cancelled = 0", (result["revaluation_id"],))
        assert len(legs) == 2, legs
        by_acct = {r["account_id"]: r for r in legs}
        assert Decimal(str(by_acct[env["stock_acct"]]["debit"])) == Decimal("30.00"), legs
        assert Decimal(str(by_acct[env["stock_adj"]]["credit"])) == Decimal("30.00"), legs
    finally:
        fresh.close()


def test_1c_no_stock_reread_sqlite(lite_conn, lite_db, monkeypatch):
    env = _build_lock_env(lite_conn)
    wh_name = lite_conn.execute(
        "SELECT name FROM warehouse WHERE id = ?", (env["warehouse"],)).fetchone()["name"]
    fake = _install_plant_valuation(monkeypatch, "issue")
    fake.env = env
    result = call_action(mod.revalue_stock, lite_conn, _revalue_args(env, "25.00"))
    assert is_error(result), result
    expected = (
        "Cannot revalue: no stock on hand for item 'Lock Widget' "
        "in warehouse '%s' (qty=0.00)" % wh_name)
    assert result["message"] == expected, result
    fresh = get_conn(lite_db)
    try:
        revals = _rows(fresh, "SELECT id FROM stock_revaluation")
        assert revals == [], revals
        sle = _rows(
            fresh,
            "SELECT id FROM stock_ledger_entry "
            "WHERE voucher_type = 'stock_revaluation'")
        assert sle == [], sle
    finally:
        fresh.close()


def test_1c_no_stock_reread_pg(pg_conn, monkeypatch):
    conn = pg_conn
    env = _build_lock_env(conn, with_naming=False)
    wh_name = conn.execute(
        "SELECT name FROM warehouse WHERE id = ?", (env["warehouse"],)).fetchone()["name"]
    fake = _install_plant_valuation(monkeypatch, "issue")
    fake.env = env
    result = call_action(mod.revalue_stock, conn, _revalue_args(env, "25.00"))
    assert is_error(result), result
    expected = (
        "Cannot revalue: no stock on hand for item 'Lock Widget' "
        "in warehouse '%s' (qty=0.00)" % wh_name)
    assert result["message"] == expected, result
    fresh = get_connection()
    try:
        revals = _rows(fresh, "SELECT id FROM stock_revaluation WHERE item_id = ?",
                       (env["item"],))
        assert revals == [], revals
        sle = _rows(
            fresh,
            "SELECT id FROM stock_ledger_entry "
            "WHERE voucher_type = 'stock_revaluation' AND item_id = ?",
            (env["item"],))
        assert sle == [], sle
    finally:
        fresh.close()


# ── test 2: head before the first write (SQLite) ──

def _write_statements(proxy):
    return [s for s in proxy.statements
            if s.lstrip()[:6].upper() in ("INSERT", "UPDATE", "DELETE")]


def _assert_revalue_head_first(writes, require_fifo=False):
    assert writes, "expected the action to write"
    first = writes[0]
    assert first.lstrip().upper().startswith("INSERT"), first
    assert "gl_chain_head" in first, first
    head_idx = next(
        (idx for idx, stmt in enumerate(writes) if "gl_chain_head" in stmt),
        0)
    required = ["naming_series", "stock_ledger_entry", "stock_revaluation"]
    if require_fifo:
        required.append("stock_fifo_layer")
    for table in required:
        first_idx = next(
            (idx for idx, stmt in enumerate(writes) if table in stmt), None)
        assert first_idx is not None, (table, writes)
        assert head_idx < first_idx, (table, head_idx, first_idx, writes)


def test_2_head_before_first_write_ma_sqlite(lite_conn):
    env = _build_lock_env(lite_conn)
    proxy = _RecordingProxy(lite_conn)
    result = call_action(mod.revalue_stock, proxy, _revalue_args(env, "25.00"))
    assert is_ok(result), result
    _assert_revalue_head_first(_write_statements(proxy))


def test_2_head_before_first_write_fifo_sqlite(lite_conn):
    env = _build_fifo_pg_env(lite_conn)
    proxy = _RecordingProxy(lite_conn)
    result = call_action(mod.revalue_stock, proxy, _revalue_args(env, "25.00"))
    assert is_ok(result), result
    _assert_revalue_head_first(_write_statements(proxy), require_fifo=True)


# ── test 3: happy paths unchanged (SQLite) ──

def test_3a_revalue_up_sqlite(lite_conn):
    env = _build_lock_env(lite_conn)
    result = call_action(mod.revalue_stock, lite_conn, _revalue_args(env, "25.00"))
    assert is_ok(result), result
    assert result["current_qty"] == "10.00", result
    assert result["old_rate"] == "20.00", result
    assert result["new_rate"] == "25.00", result
    assert result["adjustment_amount"] == "50.00", result
    assert result["gl_entries_created"] == 2, result
    sle = _rows(
        lite_conn,
        "SELECT valuation_rate, stock_value, stock_value_difference, actual_qty "
        "FROM stock_ledger_entry WHERE voucher_type = 'stock_revaluation' "
        "AND voucher_id = ? AND is_cancelled = 0", (result["revaluation_id"],))
    assert len(sle) == 1, sle
    assert sle[0]["valuation_rate"] == "25.00", sle
    assert sle[0]["stock_value"] == "250.00", sle
    assert sle[0]["stock_value_difference"] == "50.00", sle
    assert sle[0]["actual_qty"] == "0", sle
    legs = _rows(
        lite_conn,
        "SELECT account_id, debit, credit FROM gl_entry "
        "WHERE voucher_type = 'stock_revaluation' AND voucher_id = ? "
        "AND is_cancelled = 0", (result["revaluation_id"],))
    assert len(legs) == 2, legs
    by_acct = {r["account_id"]: r for r in legs}
    assert by_acct[env["stock_acct"]]["debit"] == "50.00", legs
    assert by_acct[env["stock_acct"]]["credit"] == "0.00", legs
    assert by_acct[env["stock_adj"]]["debit"] == "0.00", legs
    assert by_acct[env["stock_adj"]]["credit"] == "50.00", legs


def test_3b_revalue_down_sqlite(lite_conn):
    env = _build_lock_env(lite_conn)
    result = call_action(mod.revalue_stock, lite_conn, _revalue_args(env, "15.00"))
    assert is_ok(result), result
    assert result["adjustment_amount"] == "-50.00", result
    legs = _rows(
        lite_conn,
        "SELECT account_id, debit, credit FROM gl_entry "
        "WHERE voucher_type = 'stock_revaluation' AND voucher_id = ? "
        "AND is_cancelled = 0", (result["revaluation_id"],))
    assert len(legs) == 2, legs
    by_acct = {r["account_id"]: r for r in legs}
    assert by_acct[env["stock_adj"]]["debit"] == "50.00", legs
    assert by_acct[env["stock_acct"]]["credit"] == "50.00", legs


def test_3c_revalue_fifo_sqlite(lite_conn):
    env = _build_fifo_pg_env(lite_conn)
    result = call_action(mod.revalue_stock, lite_conn, _revalue_args(env, "25.00"))
    assert is_ok(result), result
    layer = lite_conn.execute(
        "SELECT rate FROM stock_fifo_layer WHERE item_id = ? AND warehouse_id = ?",
        (env["item"], env["warehouse"])).fetchone()
    assert layer["rate"] == "25.00", dict(layer)
    again = call_action(mod.revalue_stock, lite_conn, _revalue_args(env, "25.00"))
    assert is_error(again), again
    assert again["message"] == \
        "New rate (25.00) is the same as current rate (25.00). No revaluation needed.", again


# ── test 4: head first (PostgreSQL only) ──

def _revalue_cmd(env):
    return [sys.executable, _INVENTORY_SCRIPT,
            "--action", "revalue-stock",
            "--item-id", env["item"], "--warehouse-id", env["warehouse"],
            "--new-rate", "25.00", "--posting-date", "2026-06-15",
            "--reason", "Lock test"]


def test_4_head_first_pg(pg_conn):
    conn = pg_conn
    env = _build_fifo_pg_env(conn)
    holder = get_connection()
    proc = None
    try:
        _real_take_heads(holder, [env["company_id"]])
        holder_pid = holder.execute(
            "SELECT pg_backend_pid() AS pid").fetchone()["pid"]
        penv = proofs._proc_env(ERPCLAW_PG_LOCK_TIMEOUT="30s")
        proc = subprocess.Popen(
            _revalue_cmd(env),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=penv)
        deadline = time.monotonic() + 10
        seen = False
        while True:
            if proc.poll() is not None:
                out, err_text = proc.communicate(timeout=12)
                pytest.fail(
                    "revalue-stock exited early (rc=%s out=%s err=%s)"
                    % (proc.returncode, out, err_text))
            watcher = get_connection()
            try:
                waiting = watcher.execute(
                    "SELECT COUNT(*) c FROM pg_stat_activity "
                    "WHERE pid <> pg_backend_pid() "
                    "AND ? = ANY(pg_blocking_pids(pid))",
                    (holder_pid,)).fetchone()["c"]
            finally:
                watcher.close()
            if int(waiting) >= 1:
                seen = True
                break
            if time.monotonic() > deadline:
                pytest.fail("revalue-stock never blocked on the head")
            time.sleep(0.1)
        assert seen
        probe = get_connection()
        try:
            probe.execute("SET lock_timeout = '1s'")
            probe.execute(
                "UPDATE stock_fifo_layer SET rate = rate "
                "WHERE item_id = ? AND warehouse_id = ?",
                (env["item"], env["warehouse"]))
            probe.rollback()
        finally:
            probe.close()
        holder.rollback()
        out, err_text = proc.communicate(timeout=12)
        assert proc.returncode == 0, (out, err_text)
        layer = conn.execute(
            "SELECT rate FROM stock_fifo_layer "
            "WHERE item_id = ? AND warehouse_id = ?",
            (env["item"], env["warehouse"])).fetchone()
        assert Decimal(str(layer["rate"])) == Decimal("25.00"), dict(layer)
    finally:
        try:
            holder.rollback()
        except Exception:  # noqa: BLE001, S110 - best effort
            pass
        try:
            holder.close()
        except Exception:  # noqa: BLE001, S110 - best effort
            pass
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()


# ── test 5: revalued once (PostgreSQL only) ──

def test_5_revalued_once_pg(pg_conn):
    conn = pg_conn
    for _ in range(5):
        env = _build_fifo_pg_env(conn)
        penv = proofs._proc_env()
        _spawn_start = time.monotonic()
        first = subprocess.Popen(
            _revalue_cmd(env),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=penv)
        _spawn_mid = time.monotonic()
        second = subprocess.Popen(
            _revalue_cmd(env),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=penv)
        _spawn_end = time.monotonic()
        print("popen gap: %.6f s (second spawn %.6f s)"
              % (_spawn_mid - _spawn_start, _spawn_end - _spawn_mid))
        try:
            out1, err1 = first.communicate(timeout=15)
            out2, err2 = second.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            first.kill()
            second.kill()
            raise
        assert "deadlock" not in (out1 + err1).lower(), (out1, err1)
        assert "deadlock" not in (out2 + err2).lower(), (out2, err2)
        ordered = sorted([(first.returncode, out1, err1),
                          (second.returncode, out2, err2)])
        assert [rc for rc, _, _ in ordered] == [0, 1], (out1, err1, out2, err2)
        assert out1, (out1, err1, out2, err2)
        assert out2, (out1, err1, out2, err2)
        assert json.loads(ordered[1][1])["message"] == \
            "New rate (25.00) is the same as current rate (25.00). No revaluation needed.", \
            (out1, err1, out2, err2)
        revals = _rows(
            conn,
            "SELECT id FROM stock_revaluation WHERE item_id = ?",
            (env["item"],))
        assert len(revals) == 1, revals
        sle = _rows(
            conn,
            "SELECT stock_value_difference, voucher_id FROM stock_ledger_entry "
            "WHERE voucher_type = 'stock_revaluation' AND item_id = ? "
            "AND is_cancelled = 0", (env["item"],))
        assert len(sle) == 1, sle
        assert Decimal(str(sle[0]["stock_value_difference"])) == Decimal("50.00"), sle
        voucher_id = sle[0]["voucher_id"]
        legs = _rows(
            conn,
            "SELECT account_id, debit, credit FROM gl_entry "
            "WHERE voucher_type = 'stock_revaluation' AND voucher_id = ? "
            "AND is_cancelled = 0", (voucher_id,))
        net = sum(
            (Decimal(str(r["debit"])) - Decimal(str(r["credit"]))
             for r in legs if r["account_id"] == env["stock_acct"]),
            Decimal("0"))
        assert net == Decimal("50.00"), legs
        layer = conn.execute(
            "SELECT rate FROM stock_fifo_layer "
            "WHERE item_id = ? AND warehouse_id = ?",
            (env["item"], env["warehouse"])).fetchone()
        assert Decimal(str(layer["rate"])) == Decimal("25.00"), dict(layer)
        proofs._assert_chain_intact(conn, env["company_id"])
        proofs._assert_contiguous(conn, env["company_id"])


# ── test 6: rollback on a refusal after the first write (SQLite) ──

def test_6_gl_refusal_rolls_back_valuation_writes(lite_conn, monkeypatch):
    env = _build_fifo_pg_env(lite_conn)
    before_ns = _rows(lite_conn, "SELECT * FROM naming_series ORDER BY id")

    def boom(conn, entries, **kwargs):
        raise ValueError("planted")

    monkeypatch.setattr(mod, "insert_gl_entries", boom, raising=False)
    result = call_action(mod.revalue_stock, lite_conn, _revalue_args(env, "25.00"))
    assert is_error(result), result
    assert result["message"] == "GL posting failed: planted", result
    revals = _rows(lite_conn, "SELECT id FROM stock_revaluation")
    assert revals == [], revals
    sle = _rows(
        lite_conn,
        "SELECT id FROM stock_ledger_entry "
        "WHERE voucher_type = 'stock_revaluation'")
    assert sle == [], sle
    layer = lite_conn.execute(
        "SELECT rate FROM stock_fifo_layer WHERE item_id = ? AND warehouse_id = ?",
        (env["item"], env["warehouse"])).fetchone()
    assert layer["rate"] == "20.00", dict(layer)
    after_ns = _rows(lite_conn, "SELECT * FROM naming_series ORDER BY id")
    assert after_ns == before_ns, (before_ns, after_ns)
