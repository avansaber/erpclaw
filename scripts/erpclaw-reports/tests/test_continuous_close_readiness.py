"""Value tests for the continuous-close-readiness report (floor-o039).

Every pin asserts exact TEXT money produced by the REAL action against a
fresh core DB: a ready company, each of the four blocking facts, the exact
500.03 unallocated amount, company isolation, invalid/missing input refusal,
deterministic repeat calls, and a byte-identical snapshot around every read
(business rows and the audit trail), since the report owns no tables.

Money discipline: Decimal in Python, TEXT columns, exact string comparisons.
Never float.
"""
import importlib.util
import json
import os
import uuid

import pytest

from payments_helpers import build_ar_env, call_action, is_error, is_ok, ns

from erpclaw_lib.query import P, Q, Table, insert_row

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_MODULE_DIR = os.path.dirname(_TESTS_DIR)
_SCRIPTS_DIR = os.path.dirname(_MODULE_DIR)


def _load_reports():
    spec = importlib.util.spec_from_file_location(
        "db_query_reports_ccr",
        os.path.join(_SCRIPTS_DIR, "erpclaw-reports", "db_query.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


REP = _load_reports()

AS_OF = "2026-06-30"

_SNAPSHOT_TABLES = ("company", "account", "journal_entry",
                    "journal_entry_line", "gl_entry", "payment_entry",
                    "payment_allocation", "fiscal_year", "audit_log")


def _uuid():
    return str(uuid.uuid4())


def _insert(conn, table, **data):
    markers = {k: P() for k in data}
    sql, cols = insert_row(table, markers)
    conn.execute(sql, [data[c] for c in cols])
    conn.commit()


def _state(conn):
    out = {}
    for table in _SNAPSHOT_TABLES:
        t = Table(table)
        q = Q.from_(t).select(t.star)
        rows = conn.execute(q.get_sql(), ()).fetchall()
        out[table] = sorted(
            json.dumps({k: (None if v is None else str(v))
                        for k, v in dict(r).items()}, sort_keys=True)
            for r in rows)
    return out


def _read(conn, company_id=None, company_name=None, as_of_date=AS_OF):
    return call_action(REP.continuous_close_readiness, conn, ns(
        company_id=company_id, company_name=company_name,
        as_of_date=as_of_date))


def _seed_journal(conn, company_id, status, posting_date="2026-06-01",
                  debit="100.00", credit="100.00"):
    jid = _uuid()
    _insert(conn, "journal_entry", id=jid, posting_date=posting_date,
            total_debit=debit, total_credit=credit, status=status,
            company_id=company_id)
    return jid


def _seed_gl(conn, account_id, voucher_type, voucher_id, debit, credit,
             posting_date="2026-06-01"):
    _insert(conn, "gl_entry", id=_uuid(), posting_date=posting_date,
            account_id=account_id, debit=debit, credit=credit,
            voucher_type=voucher_type, voucher_id=voucher_id,
            is_cancelled=0)


def _seed_payment(conn, env, status="submitted", unallocated="500.03",
                  paid="500.03", posting_date="2026-06-01"):
    pid = _uuid()
    _insert(conn, "payment_entry", id=pid, payment_type="receive",
            posting_date=posting_date, paid_from_account=env["ar"],
            paid_to_account=env["bank"], paid_amount=paid,
            unallocated_amount=unallocated, status=status,
            company_id=env["company_id"])
    return pid


def _by_name(report):
    return {c["name"]: c for c in report["checks"]}


# ── ready ─────────────────────────────────────────────────────────────────

def test_ready_company(conn):
    env = build_ar_env(conn)
    before = _state(conn)
    r = _read(conn, company_id=env["company_id"])
    assert is_ok(r), r
    assert r["company_id"] == env["company_id"]
    assert r["as_of_date"] == AS_OF
    assert r["ready"] is True
    assert r["preview_only"] is True
    assert r["limitation"] == REP._CLOSE_READINESS_LIMITATION
    assert r["draft_journal_count"] == 0
    assert r["draft_journal_ids"] == []
    assert r["imbalanced_voucher_count"] == 0
    assert r["imbalanced_vouchers"] == []
    assert r["unallocated_payment_count"] == 0
    assert r["unallocated_total"] == "0.00"
    assert r["unallocated_payments"] == []
    assert r["open_fiscal_year"] is True
    assert r["fiscal_year_id"] is not None
    assert [c["name"] for c in r["checks"]] == [
        "draft_journal_entries", "balanced_posted_vouchers",
        "allocated_submitted_payments", "open_fiscal_year_contains_date"]
    for check in r["checks"]:
        assert check["matched"] is True
        assert check["result"] == "pass"
        assert isinstance(check["facts"], dict) and check["facts"]
        assert isinstance(check["explanation"], str) and check["explanation"]
        assert len(check["rule"]["conditions"]) == 1
    assert _state(conn) == before


# ── blocking facts ────────────────────────────────────────────────────────

def test_draft_journal_blocks(conn):
    env = build_ar_env(conn)
    first = _seed_journal(conn, env["company_id"], "draft",
                          posting_date="2026-05-01")
    second = _seed_journal(conn, env["company_id"], "draft",
                           posting_date="2026-06-01")
    future = _seed_journal(conn, env["company_id"], "draft",
                           posting_date="2026-12-31")
    r = _read(conn, company_id=env["company_id"])
    assert is_ok(r), r
    assert r["ready"] is False
    assert r["draft_journal_count"] == 2
    assert set(r["draft_journal_ids"]) == {first, second}
    assert r["draft_journal_ids"] == sorted(r["draft_journal_ids"])
    assert future not in r["draft_journal_ids"]
    checks = _by_name(r)
    assert checks["draft_journal_entries"]["matched"] is False
    assert checks["draft_journal_entries"]["result"] == "block"
    assert checks["draft_journal_entries"]["facts"] == {"draft_count": "2"}
    for name, check in checks.items():
        if name != "draft_journal_entries":
            assert check["matched"] is True, (name, check)


def test_submitted_journal_imbalance_blocks(conn):
    env = build_ar_env(conn)
    jid = _seed_journal(conn, env["company_id"], "submitted",
                        debit="100.00", credit="90.00")
    _seed_journal(conn, env["company_id"], "submitted",
                  debit="50.00", credit="50.00")
    r = _read(conn, company_id=env["company_id"])
    assert is_ok(r), r
    assert r["ready"] is False
    assert r["imbalanced_voucher_count"] == 1
    assert r["imbalanced_vouchers"] == [{
        "voucher_type": "journal_entry", "voucher_id": jid,
        "total_debit": "100.00", "total_credit": "90.00"}]
    checks = _by_name(r)
    assert checks["balanced_posted_vouchers"]["matched"] is False
    assert checks["balanced_posted_vouchers"]["facts"] == {
        "imbalanced_voucher_count": "1"}
    for name, check in checks.items():
        if name != "balanced_posted_vouchers":
            assert check["matched"] is True, (name, check)


def test_gl_voucher_imbalance_blocks(conn):
    env = build_ar_env(conn)
    vid = _uuid()
    _seed_gl(conn, env["ar"], "sales_invoice", vid, "200.00", "0")
    _seed_gl(conn, env["bank"], "sales_invoice", vid, "0", "150.00")
    balanced = _uuid()
    _seed_gl(conn, env["ar"], "sales_invoice", balanced, "75.00", "0")
    _seed_gl(conn, env["bank"], "sales_invoice", balanced, "0", "75.00")
    r = _read(conn, company_id=env["company_id"])
    assert is_ok(r), r
    assert r["ready"] is False
    assert r["imbalanced_vouchers"] == [{
        "voucher_type": "sales_invoice", "voucher_id": vid,
        "total_debit": "200.00", "total_credit": "150.00"}]
    checks = _by_name(r)
    assert checks["balanced_posted_vouchers"]["matched"] is False
    for name, check in checks.items():
        if name != "balanced_posted_vouchers":
            assert check["matched"] is True, (name, check)


def test_unallocated_payment_blocks_exact_money(conn):
    env = build_ar_env(conn)
    pid = _seed_payment(conn, env, unallocated="500.03")
    _seed_payment(conn, env, unallocated="0", paid="120.00")
    _seed_payment(conn, env, status="draft", unallocated="999.99")
    _seed_payment(conn, env, unallocated="77.77", posting_date="2026-12-31")
    r = _read(conn, company_id=env["company_id"])
    assert is_ok(r), r
    assert r["ready"] is False
    assert r["unallocated_payment_count"] == 1
    assert r["unallocated_total"] == "500.03"
    assert isinstance(r["unallocated_total"], str)
    assert r["unallocated_payments"] == [{
        "payment_entry_id": pid, "payment_type": "receive",
        "posting_date": "2026-06-01", "paid_amount": "500.03",
        "unallocated_amount": "500.03"}]
    checks = _by_name(r)
    assert checks["allocated_submitted_payments"]["matched"] is False
    assert checks["allocated_submitted_payments"]["result"] == "block"
    assert checks["allocated_submitted_payments"]["facts"] == {
        "unallocated_total": "500.03"}
    for name, check in checks.items():
        if name != "allocated_submitted_payments":
            assert check["matched"] is True, (name, check)


def test_closed_fiscal_year_blocks(conn):
    env = build_ar_env(conn)
    conn.execute("UPDATE fiscal_year SET is_closed = 1 WHERE company_id = ?",
                 (env["company_id"],))
    conn.commit()
    r = _read(conn, company_id=env["company_id"])
    assert is_ok(r), r
    assert r["ready"] is False
    assert r["open_fiscal_year"] is False
    assert r["fiscal_year_id"] is None
    checks = _by_name(r)
    assert checks["open_fiscal_year_contains_date"]["matched"] is False
    assert checks["open_fiscal_year_contains_date"]["facts"] == {
        "open_fiscal_year_count": "0"}
    for name, check in checks.items():
        if name != "open_fiscal_year_contains_date":
            assert check["matched"] is True, (name, check)


# ── isolation, refusal, determinism, no writes ────────────────────────────

def test_company_isolation(conn):
    first = build_ar_env(conn)
    second = build_ar_env(conn)
    _seed_journal(conn, second["company_id"], "draft")
    _seed_payment(conn, second, unallocated="500.03")
    before = _state(conn)
    near = _read(conn, company_id=first["company_id"])
    assert is_ok(near), near
    assert near["ready"] is True
    assert near["draft_journal_ids"] == []
    assert near["imbalanced_vouchers"] == []
    assert near["unallocated_total"] == "0.00"
    far = _read(conn, company_id=second["company_id"])
    assert is_ok(far), far
    assert far["ready"] is False
    assert far["draft_journal_count"] == 1
    assert far["unallocated_total"] == "500.03"
    assert _state(conn) == before


def test_invalid_and_missing_input_refuse(conn):
    env = build_ar_env(conn)
    before = _state(conn)
    bad_date = _read(conn, company_id=env["company_id"],
                     as_of_date="not-a-date")
    assert bad_date == {
        "status": "error",
        "message": "Invalid --as-of-date 'not-a-date': expected YYYY-MM-DD"}
    bad_month = _read(conn, company_id=env["company_id"],
                      as_of_date="2026-13-40")
    assert bad_month == {
        "status": "error",
        "message": "Invalid --as-of-date '2026-13-40': expected YYYY-MM-DD"}
    missing_date = _read(conn, company_id=env["company_id"],
                         as_of_date=None)
    assert missing_date == {
        "status": "error", "message": "--as-of-date is required"}
    missing_company = _read(conn, as_of_date=AS_OF)
    assert missing_company == {
        "status": "error", "message": "--company-id is required"}
    unknown = _read(conn, company_id="bogus", as_of_date=AS_OF)
    assert unknown == {
        "status": "error", "message": "Company not found: bogus"}
    for result in (bad_date, bad_month, missing_date, missing_company,
                   unknown):
        assert is_error(result)
    assert _state(conn) == before


def test_two_identical_calls_match(conn):
    env = build_ar_env(conn)
    _seed_journal(conn, env["company_id"], "draft")
    _seed_payment(conn, env, unallocated="500.03")
    first = _read(conn, company_id=env["company_id"])
    second = _read(conn, company_id=env["company_id"])
    assert is_ok(first) and is_ok(second)
    assert first == second


def test_no_writes_to_business_or_audit_tables(conn):
    env = build_ar_env(conn)
    _seed_journal(conn, env["company_id"], "draft")
    _seed_journal(conn, env["company_id"], "submitted",
                  debit="100.00", credit="90.00")
    _seed_payment(conn, env, unallocated="500.03")
    before = _state(conn)
    r = _read(conn, company_id=env["company_id"])
    assert is_ok(r), r
    assert r["ready"] is False
    assert _state(conn) == before
