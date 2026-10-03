"""Submitting or cancelling a payroll run takes the company ledger lock first.

Covers m832: ``submit-payroll-run`` and ``cancel-payroll-run`` take the
company's ``gl_chain_head`` before their first write, re-read the run under
the head and refuse on stale state, finish with a status-conditioned run
update, and roll back the head touch plus every partial write when any later
refusal fires. A run therefore posts and reverses exactly once.
"""
import importlib.util
import json
import os
import subprocess
import sys
import time
import uuid
from decimal import Decimal

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import pytest

from payroll_helpers import (
    build_payroll_env, call_action, is_error, is_ok, load_db_query, ns,
    seed_account, seed_company, seed_cost_center, seed_employee,
    seed_fiscal_year,
)
from erpclaw_lib.db import get_connection, get_dialect
from erpclaw_lib.gl_posting import take_chain_heads as _real_take_chain_heads
from erpclaw_lib.query import P, Q, Table

mod = load_db_query()

_SCRIPTS_DIR = os.path.dirname(os.path.dirname(_TESTS_DIR))
_PAYMENTS_TESTS = os.path.join(_SCRIPTS_DIR, "erpclaw-payments", "tests")
_PAYROLL_SCRIPT = os.path.join(_SCRIPTS_DIR, "erpclaw-payroll", "db_query.py")
if _PAYMENTS_TESTS not in sys.path:
    sys.path.insert(0, _PAYMENTS_TESTS)


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_proofs = _load_module(
    "payroll_lock_chain_proofs",
    os.path.join(_PAYMENTS_TESTS, "test_chain_lock_proofs.py"))
_allocmod = _load_module(
    "payroll_lock_alloc_compare",
    os.path.join(_PAYMENTS_TESTS,
                 "test_payment_edit_and_allocation_compare_and_set.py"))
_payments_pg = _load_module(
    "payments_helpers_pg",
    os.path.join(_PAYMENTS_TESTS, "payments_helpers.py"))

_RecordingProxy = _allocmod._RecordingProxy


_SQLITE_ONLY = pytest.mark.skipif(
    get_dialect() == "postgresql", reason="SQLite-only leg")


# ──────────────────────────────────────────────────────────────────────────────
# Builders (same shape as test_garnishment_regeneration.py)
# ──────────────────────────────────────────────────────────────────────────────

def _setup_payroll_ready(conn, env, extra_deduction=None):
    comp = call_action(mod.add_salary_component, conn, ns(
        name="Lock Base %s" % uuid.uuid4().hex[:6], component_type="earning",
        is_tax_applicable=None, is_statutory=None, is_pre_tax=None,
        variable_based_on_taxable_salary=None, depends_on_payment_days=None,
        gl_account_id=None, description=None,
    ))
    assert is_ok(comp), comp
    comp_id = comp["salary_component_id"]
    details = [{"salary_component_id": comp_id, "amount": "5000"}]
    if extra_deduction is not None:
        details.append(extra_deduction)
    ss = call_action(mod.add_salary_structure, conn, ns(
        name="Lock Structure %s" % uuid.uuid4().hex[:6],
        company_id=env["company_id"],
        components=json.dumps(details), payroll_frequency=None,
    ))
    assert is_ok(ss), ss
    sa = call_action(mod.add_salary_assignment, conn, ns(
        employee_id=env["employee_id"], salary_structure_id=ss["salary_structure_id"],
        base_amount="5000.00", effective_from="2026-01-01", effective_to=None,
    ))
    assert is_ok(sa), sa
    fica = call_action(mod.update_fica_config, conn, ns(
        tax_year="2026", ss_wage_base="168600", ss_employee_rate="6.2",
        ss_employer_rate="6.2", medicare_employee_rate="1.45",
        medicare_employer_rate="1.45",
        additional_medicare_threshold="200000", additional_medicare_rate="0.9",
    ))
    assert is_ok(fica), fica
    seed_account(conn, env["company_id"], "Garnishments Payable",
                 root_type="liability", account_type=None)
    return {"component_id": comp_id, "structure_id": ss["salary_structure_id"]}


def _add_garnishment(conn, employee_id, order, creditor, total, amount="0.50",
                     gtype="child_support", pct=False):
    res = call_action(mod.add_garnishment, conn, ns(
        employee_id=employee_id, order_number=order, creditor_name=creditor,
        garnishment_type=gtype, amount_or_percentage=amount,
        is_percentage=pct, total_owed=total, start_date="2026-01-01",
        end_date=None,
    ))
    assert is_ok(res), res
    return res["garnishment_id"]


def _create_run(conn, company_id, start="2026-01-01", end="2026-01-31"):
    res = call_action(mod.create_payroll_run, conn, ns(
        company_id=company_id, period_start=start, period_end=end,
        department_id=None, payroll_frequency="monthly",
    ))
    assert is_ok(res), res
    return res["payroll_run_id"]


def _generate(conn, run_id):
    return call_action(mod.generate_salary_slips, conn, ns(payroll_run_id=run_id))


def _garn_row(conn, gid):
    return conn.execute(
        "SELECT * FROM wage_garnishment WHERE id = ?", (gid,)).fetchone()


def _run_slips(conn, run_id, status=None):
    if status is None:
        return conn.execute(
            "SELECT * FROM salary_slip WHERE payroll_run_id = ?", (run_id,)).fetchall()
    return conn.execute(
        "SELECT * FROM salary_slip WHERE payroll_run_id = ? AND status = ?",
        (run_id, status)).fetchall()


def _gl_count(conn, run_id):
    return conn.execute(
        "SELECT COUNT(*) FROM gl_entry WHERE voucher_type = 'payroll_entry'"
        " AND voucher_id = ?",
        (run_id,)).fetchone()[0]


def _gl_debit_total(conn, run_id):
    rows = conn.execute(
        "SELECT debit FROM gl_entry WHERE voucher_type = 'payroll_entry'"
        " AND voucher_id = ?",
        (run_id,)).fetchall()
    total = Decimal("0")
    for row in rows:
        total += Decimal(str(row["debit"]))
    return total


def _fresh_conn():
    return get_connection()


def _build_pg_env(conn):
    # Same environment as build_payroll_env but without seed_naming_series,
    # which uses SQLite-only INSERT OR IGNORE; get_next_name upserts its own
    # naming-series row, so the series seed is unnecessary on PostgreSQL.
    cid = seed_company(conn)
    fyid = seed_fiscal_year(conn, cid)
    ccid = seed_cost_center(conn, cid)
    cash = seed_account(conn, cid, "Cash", root_type="asset", account_type="cash")
    salary_expense = seed_account(conn, cid, "Salary Expense",
                                  root_type="expense", account_type=None)
    payroll_payable = seed_account(conn, cid, "Payroll Payable",
                                   root_type="liability", account_type="payable")
    federal_tax_payable = seed_account(conn, cid, "Federal Income Tax Withheld",
                                       root_type="liability", account_type=None)
    ss_payable = seed_account(conn, cid, "Social Security Payable",
                              root_type="liability", account_type=None)
    medicare_payable = seed_account(conn, cid, "Medicare Payable",
                                    root_type="liability", account_type=None)
    employer_tax_expense = seed_account(conn, cid, "Employer Tax Expense",
                                        root_type="expense", account_type=None)
    emp_id = seed_employee(conn, cid)
    return {
        "company_id": cid,
        "fiscal_year_id": fyid,
        "cost_center_id": ccid,
        "cash": cash,
        "salary_expense": salary_expense,
        "payroll_payable": payroll_payable,
        "federal_tax_payable": federal_tax_payable,
        "ss_payable": ss_payable,
        "medicare_payable": medicare_payable,
        "employer_tax_expense": employer_tax_expense,
        "employee_id": emp_id,
    }


def _pg_ready_run(conn, order, start="2026-01-01", end="2026-01-31"):
    env = _build_pg_env(conn)
    _setup_payroll_ready(conn, env)
    gid = _add_garnishment(conn, env["employee_id"], order,
                           "Lock Creditor", "600.00", amount="0.50")
    run_id = _create_run(conn, env["company_id"], start=start, end=end)
    assert is_ok(_generate(conn, run_id)), run_id
    return env, gid, run_id


@pytest.fixture
def pg_conn():
    """Fresh PostgreSQL book; skips outside the PostgreSQL lane."""
    _proofs._pg_only()
    base_url = os.environ.get("ERPCLAW_PG_TEST_URL")
    assert base_url, "ERPCLAW_PG_TEST_URL is not set"
    old_url = os.environ.get("ERPCLAW_DB_URL")
    old_path = os.environ.get("ERPCLAW_DB_PATH")
    os.environ["ERPCLAW_DB_URL"] = base_url
    os.environ.pop("ERPCLAW_DB_PATH", None)
    try:
        _payments_pg.init_all_tables(None)
        conn = get_connection()
        yield conn
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
    finally:
        if old_url is None:
            os.environ.pop("ERPCLAW_DB_URL", None)
        else:
            os.environ["ERPCLAW_DB_URL"] = old_url
        if old_path is None:
            os.environ.pop("ERPCLAW_DB_PATH", None)
        else:
            os.environ["ERPCLAW_DB_PATH"] = old_path


# ──────────────────────────────────────────────────────────────────────────────
# 1. Stale state under the head is refused before any write
# ──────────────────────────────────────────────────────────────────────────────

def _wrap_counters(monkeypatch):
    real_insert = mod.insert_gl_entries
    real_reverse = mod.reverse_gl_entries
    insert_calls = []
    reverse_calls = []

    def _count_insert(conn, *args, **kwargs):
        insert_calls.append(1)
        return real_insert(conn, *args, **kwargs)

    def _count_reverse(conn, *args, **kwargs):
        reverse_calls.append(1)
        return real_reverse(conn, *args, **kwargs)

    monkeypatch.setattr(mod, "insert_gl_entries", _count_insert)
    monkeypatch.setattr(mod, "reverse_gl_entries", _count_reverse)
    return insert_calls, reverse_calls


def _check_1a(conn, run_id):
    fresh = _fresh_conn()
    try:
        assert _gl_count(fresh, run_id) == 0
        slips = fresh.execute(
            "SELECT status FROM salary_slip WHERE payroll_run_id = ?",
            (run_id,)).fetchall()
        assert len(slips) >= 1
        assert all(s["status"] == "draft" for s in slips)
    finally:
        fresh.close()


@_SQLITE_ONLY
def test_1a_submit_stale_state_refused(conn, env, monkeypatch):
    """A submit whose run flips after the head take refuses with no posting."""
    _setup_payroll_ready(conn, env)
    run_id = _create_run(conn, env["company_id"])
    assert is_ok(_generate(conn, run_id))
    insert_calls, _ = _wrap_counters(monkeypatch)
    real_head = _real_take_chain_heads

    def _stale(conn2, company_ids):
        real_head(conn2, company_ids)
        conn2.execute("UPDATE payroll_run SET status = 'submitted' WHERE id = ?",
                      (run_id,))
        conn2.commit()
        real_head(conn2, company_ids)

    monkeypatch.setattr(mod, "take_chain_heads", _stale, raising=False)
    res = call_action(mod.submit_payroll_run, conn, ns(
        payroll_run_id=run_id, cost_center_id=env["cost_center_id"]))
    assert is_error(res), res
    assert res["message"] == "Payroll run is 'submitted', must be 'draft' to submit"
    assert len(insert_calls) == 0
    _check_1a(conn, run_id)


def test_1a_submit_stale_state_refused_pg(pg_conn, monkeypatch):
    """PostgreSQL leg of the stale-submit refusal."""
    conn = pg_conn
    env, _, run_id = _pg_ready_run(conn, "CS-LOCK-1A")
    insert_calls, _ = _wrap_counters(monkeypatch)
    real_head = _real_take_chain_heads

    def _stale(conn2, company_ids):
        real_head(conn2, company_ids)
        conn2.execute("UPDATE payroll_run SET status = 'submitted' WHERE id = ?",
                      (run_id,))
        conn2.commit()
        real_head(conn2, company_ids)

    monkeypatch.setattr(mod, "take_chain_heads", _stale, raising=False)
    res = call_action(mod.submit_payroll_run, conn, ns(
        payroll_run_id=run_id, cost_center_id=env["cost_center_id"]))
    assert is_error(res), res
    assert res["message"] == "Payroll run is 'submitted', must be 'draft' to submit"
    assert len(insert_calls) == 0
    _check_1a(conn, run_id)


def _check_1b(conn, gid, run_id, before_count):
    assert _garn_row(conn, gid)["cumulative_paid"] == "0.50"
    assert _gl_count(conn, run_id) == before_count


@_SQLITE_ONLY
def test_1b_cancel_stale_state_refused(conn, env, monkeypatch):
    """A cancel whose run flips after the head take reverses nothing."""
    _setup_payroll_ready(conn, env)
    gid = _add_garnishment(conn, env["employee_id"], "CS-LOCK-1B",
                           "Lock Creditor", "600.00", amount="0.50")
    run_id = _create_run(conn, env["company_id"])
    assert is_ok(_generate(conn, run_id))
    sub = call_action(mod.submit_payroll_run, conn, ns(
        payroll_run_id=run_id, cost_center_id=env["cost_center_id"]))
    assert is_ok(sub), sub
    assert _garn_row(conn, gid)["cumulative_paid"] == "0.50"
    before_count = _gl_count(conn, run_id)
    assert before_count > 0
    _, reverse_calls = _wrap_counters(monkeypatch)
    real_head = _real_take_chain_heads

    def _stale(conn2, company_ids):
        real_head(conn2, company_ids)
        conn2.execute("UPDATE payroll_run SET status = 'cancelled' WHERE id = ?",
                      (run_id,))
        conn2.commit()
        real_head(conn2, company_ids)

    monkeypatch.setattr(mod, "take_chain_heads", _stale, raising=False)
    res = call_action(mod.cancel_payroll_run, conn, ns(payroll_run_id=run_id))
    assert is_error(res), res
    assert res["message"] == "Payroll run is 'cancelled', must be 'submitted' to cancel"
    assert len(reverse_calls) == 0
    _check_1b(conn, gid, run_id, before_count)


def test_1b_cancel_stale_state_refused_pg(pg_conn, monkeypatch):
    """PostgreSQL leg of the stale-cancel refusal."""
    conn = pg_conn
    env, gid, run_id = _pg_ready_run(conn, "CS-LOCK-1B")
    sub = call_action(mod.submit_payroll_run, conn, ns(
        payroll_run_id=run_id, cost_center_id=env["cost_center_id"]))
    assert is_ok(sub), sub
    assert _garn_row(conn, gid)["cumulative_paid"] == "0.50"
    before_count = _gl_count(conn, run_id)
    assert before_count > 0
    _, reverse_calls = _wrap_counters(monkeypatch)
    real_head = _real_take_chain_heads

    def _stale(conn2, company_ids):
        real_head(conn2, company_ids)
        conn2.execute("UPDATE payroll_run SET status = 'cancelled' WHERE id = ?",
                      (run_id,))
        conn2.commit()
        real_head(conn2, company_ids)

    monkeypatch.setattr(mod, "take_chain_heads", _stale, raising=False)
    res = call_action(mod.cancel_payroll_run, conn, ns(payroll_run_id=run_id))
    assert is_error(res), res
    assert res["message"] == "Payroll run is 'cancelled', must be 'submitted' to cancel"
    assert len(reverse_calls) == 0
    _check_1b(conn, gid, run_id, before_count)


# ──────────────────────────────────────────────────────────────────────────────
# 2. Head before the first write
# ──────────────────────────────────────────────────────────────────────────────

def _is_write(sql):
    return sql.lstrip()[:6].upper() in ("INSERT", "UPDATE", "DELETE")


def _is_read(sql):
    return sql.lstrip()[:6].upper() == "SELECT"


def _first_write_idx(stmts):
    for i, sql in enumerate(stmts):
        if _is_write(sql):
            return i
    return None


def _first_read_idx_after(stmts, table, after):
    for i in range(after + 1, len(stmts)):
        if _is_read(stmts[i]) and table in stmts[i]:
            return i
    return None


def _submit_cancel_book(conn, env):
    _setup_payroll_ready(conn, env)
    run_id = _create_run(conn, env["company_id"])
    assert is_ok(_generate(conn, run_id))
    return run_id


@_SQLITE_ONLY
def test_2_head_before_first_write(conn, env):
    """The chain-head upsert is each action's first write; re-reads follow."""
    run_id = _submit_cancel_book(conn, env)

    proxy = _RecordingProxy(conn)
    sub = call_action(mod.submit_payroll_run, proxy, ns(
        payroll_run_id=run_id, cost_center_id=env["cost_center_id"]))
    assert is_ok(sub), sub
    stmts = list(proxy.statements)
    fw = _first_write_idx(stmts)
    assert fw is not None, stmts[:10]
    assert _is_write(stmts[fw])
    assert stmts[fw].lstrip().upper().startswith("INSERT")
    assert "gl_chain_head" in stmts[fw]
    r_run = _first_read_idx_after(stmts, "payroll_run", fw)
    assert r_run is not None, [s for s in stmts[fw:fw + 8]]
    r_slip = _first_read_idx_after(stmts, "salary_slip", fw)
    assert r_slip is not None, [s for s in stmts[fw:fw + 8]]
    watched = ("gl_entry", "salary_slip", "retro_pay_adjustment",
               "wage_garnishment", "payroll_run")
    for i, sql in enumerate(stmts):
        if _is_write(sql) and any(t in sql for t in watched):
            assert i > r_run, (i, r_run, sql)
            assert i > r_slip, (i, r_slip, sql)

    proxy2 = _RecordingProxy(conn)
    res = call_action(mod.cancel_payroll_run, proxy2, ns(payroll_run_id=run_id))
    assert is_ok(res), res
    stmts2 = list(proxy2.statements)
    fw2 = _first_write_idx(stmts2)
    assert fw2 is not None, stmts2[:10]
    assert stmts2[fw2].lstrip().upper().startswith("INSERT")
    assert "gl_chain_head" in stmts2[fw2]
    r_run2 = _first_read_idx_after(stmts2, "payroll_run", fw2)
    assert r_run2 is not None, [s for s in stmts2[fw2:fw2 + 8]]
    watched2 = ("gl_entry", "salary_slip", "retro_pay_adjustment",
                "wage_garnishment", "payroll_run")
    for i, sql in enumerate(stmts2):
        if _is_write(sql) and any(t in sql for t in watched2):
            assert i > r_run2, (i, r_run2, sql)


# ──────────────────────────────────────────────────────────────────────────────
# 3. Happy path unchanged
# ──────────────────────────────────────────────────────────────────────────────

@_SQLITE_ONLY
def test_3_submit_then_cancel_happy_path(conn, env):
    """Submit then cancel keeps the base payloads, ledger and give-back."""
    _setup_payroll_ready(conn, env)
    gid = _add_garnishment(conn, env["employee_id"], "CS-LOCK-003",
                           "Lock Creditor", "600.00", amount="0.50")
    run_id = _create_run(conn, env["company_id"])
    assert is_ok(_generate(conn, run_id))
    assert _garn_row(conn, gid)["cumulative_paid"] == "0.50"

    sub = call_action(mod.submit_payroll_run, conn, ns(
        payroll_run_id=run_id, cost_center_id=env["cost_center_id"]))
    assert is_ok(sub), sub
    assert sub["payroll_run_id"] == run_id
    assert sub["gl_entries"] > 0
    assert conn.execute(
        "SELECT status FROM payroll_run WHERE id = ?",
        (run_id,)).fetchone()["status"] == "submitted"
    assert conn.execute(
        "SELECT COUNT(*) FROM salary_slip WHERE payroll_run_id = ? AND status = 'draft'",
        (run_id,)).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM salary_slip WHERE payroll_run_id = ? AND status = 'submitted'",
        (run_id,)).fetchone()[0] >= 1
    gl_before = _gl_count(conn, run_id)
    assert gl_before > 0
    totals = conn.execute(
        "SELECT debit, credit FROM gl_entry WHERE voucher_type = 'payroll_entry'"
        " AND voucher_id = ?",
        (run_id,)).fetchall()
    debit = sum((Decimal(str(r["debit"])) for r in totals), Decimal("0"))
    credit = sum((Decimal(str(r["credit"])) for r in totals), Decimal("0"))
    assert abs(debit - credit) < Decimal("0.02")

    res = call_action(mod.cancel_payroll_run, conn, ns(payroll_run_id=run_id))
    assert is_ok(res), res
    assert res["payroll_run_id"] == run_id
    assert res["reversed_entries"] > 0
    assert res["garnishments_reverted"] == 1
    assert conn.execute(
        "SELECT status FROM payroll_run WHERE id = ?",
        (run_id,)).fetchone()["status"] == "cancelled"
    assert conn.execute(
        "SELECT COUNT(*) FROM salary_slip WHERE payroll_run_id = ? AND status = 'submitted'",
        (run_id,)).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM salary_slip WHERE payroll_run_id = ? AND status = 'cancelled'",
        (run_id,)).fetchone()[0] >= 1
    assert _gl_count(conn, run_id) > gl_before
    row = _garn_row(conn, gid)
    assert row["cumulative_paid"] == "0.00"
    assert row["status"] == "active"


# ──────────────────────────────────────────────────────────────────────────────
# 7. Rollback on a refusal after the head
# ──────────────────────────────────────────────────────────────────────────────

@_SQLITE_ONLY
def test_7_refusal_after_head_rolls_back(conn, env, monkeypatch):
    """A refusal after the head take leaves no head row and draft slips."""
    _setup_payroll_ready(conn, env)
    run_id = _create_run(conn, env["company_id"])
    assert is_ok(_generate(conn, run_id))
    assert conn.execute(
        "SELECT * FROM gl_chain_head WHERE company_id = ?",
        (env["company_id"],)).fetchone() is None

    def _boom(conn2, company_id):
        raise ValueError("planted")

    monkeypatch.setattr(mod, "_find_payroll_accounts", _boom)
    res = call_action(mod.submit_payroll_run, conn, ns(
        payroll_run_id=run_id, cost_center_id=env["cost_center_id"]))
    assert is_error(res), res
    assert res["message"] == "planted"
    assert conn.execute(
        "SELECT * FROM gl_chain_head WHERE company_id = ?",
        (env["company_id"],)).fetchone() is None
    slips = conn.execute(
        "SELECT status FROM salary_slip WHERE payroll_run_id = ?",
        (run_id,)).fetchall()
    assert len(slips) >= 1
    assert all(s["status"] == "draft" for s in slips)


# ──────────────────────────────────────────────────────────────────────────────
# PostgreSQL-only legs: head first, posted once, final compare-and-set
# ──────────────────────────────────────────────────────────────────────────────

def _pg_submit_proc(run_id, cost_center_id, **env_overrides):
    return subprocess.Popen(
        [sys.executable, _PAYROLL_SCRIPT,
         "--action", "submit-payroll-run", "--payroll-run-id", run_id,
         "--cost-center-id", cost_center_id],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=_proofs._proc_env(**env_overrides))


def _pg_cancel_proc(run_id, **env_overrides):
    return subprocess.Popen(
        [sys.executable, _PAYROLL_SCRIPT,
         "--action", "cancel-payroll-run", "--payroll-run-id", run_id],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=_proofs._proc_env(**env_overrides))


def test_4_cancel_waits_on_held_head(pg_conn):
    """A held head blocks cancel; untouched rows stay writable meanwhile."""
    conn = pg_conn
    env, gid, run_id = _pg_ready_run(conn, "CS-LOCK-T4")
    sub = call_action(mod.submit_payroll_run, conn, ns(
        payroll_run_id=run_id, cost_center_id=env["cost_center_id"]))
    assert is_ok(sub), sub
    from erpclaw_lib.gl_posting import take_chain_heads
    holder = get_connection()
    try:
        take_chain_heads(holder, [env["company_id"]])
        proc = _pg_cancel_proc(run_id, ERPCLAW_PG_LOCK_TIMEOUT="10s")
        try:
            # The fixed cancel blocks on the held head; on the base it never
            # takes the head and finishes. Poll long enough that a base-speed
            # cancel would have exited: an early exit means "did not block".
            blocked = True
            for _ in range(40):
                if proc.poll() is not None:
                    blocked = False
                    break
                time.sleep(0.2)
            assert blocked, (
                "cancel-payroll-run finished without blocking on the held head"
                " (rc=%s)" % proc.returncode)
            probe = get_connection()
            try:
                probe.execute("SET lock_timeout = '1s'")
                probe.execute(
                    "UPDATE wage_garnishment SET cumulative_paid = cumulative_paid"
                    " WHERE id = ?",
                    (gid,))
                probe.execute(
                    "UPDATE payroll_run SET status = status WHERE id = ?",
                    (run_id,))
                probe.rollback()
            finally:
                probe.close()
        finally:
            holder.rollback()
        out, err = proc.communicate(timeout=12)
        assert proc.returncode == 0, (out, err)
    finally:
        try:
            holder.rollback()
        except Exception:
            pass
        holder.close()


def _pg_seq_submit(conn, order, start="2026-01-01", end="2026-01-31", timeout=30):
    env, gid, run_id = _pg_ready_run(conn, order, start=start, end=end)
    proc = subprocess.run(
        [sys.executable, _PAYROLL_SCRIPT,
         "--action", "submit-payroll-run", "--payroll-run-id", run_id,
         "--cost-center-id", env["cost_center_id"]],
        capture_output=True, text=True, timeout=timeout,
        env=_proofs._proc_env())
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    return env, gid, run_id


def test_5_concurrent_submits_post_once(pg_conn):
    """Two racing submits post one entry; two racing cancels reverse once."""
    conn = pg_conn
    base_env, _, base_run = _pg_seq_submit(conn, "CS-LOCK-BASE")
    base_count = _gl_count(conn, base_run)
    base_debit = _gl_debit_total(conn, base_run)
    assert base_count > 0

    for round_no in range(5):
        env, _, run_id = _pg_ready_run(conn, "CS-LOCK-R%d" % round_no)
        p1 = _pg_submit_proc(run_id, env["cost_center_id"])
        p2 = _pg_submit_proc(run_id, env["cost_center_id"])
        o1, e1 = p1.communicate(timeout=15)
        o2, e2 = p2.communicate(timeout=15)
        assert "deadlock" not in (o1 + e1).lower(), (o1, e1)
        assert "deadlock" not in (o2 + e2).lower(), (o2, e2)
        rcs = sorted([p1.returncode, p2.returncode])
        assert rcs == [0, 1], ((p1.returncode, o1, e1), (p2.returncode, o2, e2))
        loser_out = o1 if p1.returncode == 1 else o2
        assert json.loads(loser_out) == {
            "status": "error",
            "message": "Payroll run is 'submitted', must be 'draft' to submit",
        }, loser_out
        assert _gl_count(conn, run_id) == base_count, round_no
        assert _gl_debit_total(conn, run_id) == base_debit, round_no
        _proofs._assert_chain_intact(conn, env["company_id"])
        _proofs._assert_contiguous(conn, env["company_id"])

    env, gid, run1 = _pg_ready_run(conn, "CS-LOCK-C1")
    proc1 = subprocess.run(
        [sys.executable, _PAYROLL_SCRIPT,
         "--action", "submit-payroll-run", "--payroll-run-id", run1,
         "--cost-center-id", env["cost_center_id"]],
        capture_output=True, text=True, timeout=30,
        env=_proofs._proc_env())
    assert proc1.returncode == 0, (proc1.stdout, proc1.stderr)
    run2 = _create_run(conn, env["company_id"],
                       start="2026-02-01", end="2026-02-28")
    assert is_ok(_generate(conn, run2)), run2
    proc2 = subprocess.run(
        [sys.executable, _PAYROLL_SCRIPT,
         "--action", "submit-payroll-run", "--payroll-run-id", run2,
         "--cost-center-id", env["cost_center_id"]],
        capture_output=True, text=True, timeout=30,
        env=_proofs._proc_env())
    assert proc2.returncode == 0, (proc2.stdout, proc2.stderr)
    assert _garn_row(conn, gid)["cumulative_paid"] == "1.00"
    primary = _gl_count(conn, run1)
    assert primary == base_count

    c1 = _pg_cancel_proc(run1)
    c2 = _pg_cancel_proc(run1)
    co1, ce1 = c1.communicate(timeout=15)
    co2, ce2 = c2.communicate(timeout=15)
    assert sorted([c1.returncode, c2.returncode]) == [0, 1], (
        (c1.returncode, co1, ce1), (c2.returncode, co2, ce2))
    loser_out = co1 if c1.returncode == 1 else co2
    assert json.loads(loser_out)["message"] == (
        "Payroll run is 'cancelled', must be 'submitted' to cancel"), loser_out
    assert _garn_row(conn, gid)["cumulative_paid"] == "0.50"
    rows = conn.execute(
        "SELECT id, remarks FROM gl_entry WHERE voucher_type = 'payroll_entry'"
        " AND voucher_id = ?",
        (run1,)).fetchall()
    assert len(rows) == 2 * primary
    mirrors = [r for r in rows if str(r["remarks"]).startswith("Reversal of ")]
    assert len(mirrors) == primary


def test_6_submit_final_compare_and_set(pg_conn, monkeypatch):
    """A run cancelled after posting refuses and leaves no ledger rows."""
    conn = pg_conn
    env, _, run_id = _pg_ready_run(conn, "CS-LOCK-T6")
    real_insert = mod.insert_gl_entries

    def _tamper(conn2, *args, **kwargs):
        posted = real_insert(conn2, *args, **kwargs)
        other = get_connection()
        try:
            other.execute("UPDATE payroll_run SET status = 'cancelled' WHERE id = ?",
                          (run_id,))
            other.commit()
        finally:
            other.close()
        return posted

    monkeypatch.setattr(mod, "insert_gl_entries", _tamper)
    res = call_action(mod.submit_payroll_run, conn, ns(
        payroll_run_id=run_id, cost_center_id=env["cost_center_id"]))
    assert is_error(res), res
    assert res["message"] == "Payroll run is 'cancelled', must be 'draft' to submit"
    fresh = get_connection()
    try:
        assert fresh.execute(
            "SELECT COUNT(*) FROM gl_entry WHERE voucher_type = 'payroll_entry'"
            " AND voucher_id = ?",
            (run_id,)).fetchone()[0] == 0
    finally:
        fresh.close()
