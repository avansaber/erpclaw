"""Migration 051: approved-before-the-upgrade expense claims read as owed, too.

Approving a claim now writes one payment-ledger row (the business owes the
claimant the total until paid). Claims approved before that change carry no
row, so an upgraded install reads them as owed nothing. This migration writes
the missing row for every claim still ``approved`` -- exactly the shape
approval writes, once, with an audit row -- and refuses to guess where the
claim's own approval ledger does not prove the account.

Every pin runs the REAL migration module against a real database file and
asserts exact two-place strings. Fixtures come from the setup suite
(``conn`` / ``db_path`` plus ``setup_helpers`` seeds); the hr and payments
actions and the invariant engine are loaded by path with importlib, and the
two employees plus the ``EC-`` expense-claim naming series are seeded here.
A pre-upgrade claim is recreated by approving through the real action and
then deleting the row approval wrote (a PyPika delete).

Money discipline: Decimal in Python, TEXT columns, exact string comparisons.
Never float.
"""
import importlib.util
import io
import json
import os
import uuid
from contextlib import redirect_stdout

import pytest

from setup_helpers import (call_action, init_all_tables, is_ok, ns,
                           seed_account, seed_company, seed_cost_center,
                           seed_fiscal_year)

from erpclaw_lib.query import P, Q, Table

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SETUP_DIR = os.path.dirname(_TESTS_DIR)
_MIGRATION = os.path.join(
    _SETUP_DIR, "migrations", "051_backfill_expense_claim_ple.py")
_HR_PATH = os.path.join(_SETUP_DIR, "..", "erpclaw-hr", "db_query.py")
_PAYMENTS_PATH = os.path.join(
    _SETUP_DIR, "..", "erpclaw-payments", "db_query.py")
_ROOT_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(_SETUP_DIR))))
_ENGINE_PATH = os.path.join(_ROOT_DIR, "testing", "invariant_engine.py")

CLAIM_DATE = "2026-03-02"
PAY_DATE = "2026-06-02"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mig = _load("migration_051", _MIGRATION)
hr = _load("db_query_hr_051", os.path.normpath(_HR_PATH))
pay = _load("db_query_payments_051", os.path.normpath(_PAYMENTS_PATH))
engine = _load("invariant_engine_051", os.path.normpath(_ENGINE_PATH))


def _pg_reset_schema():
    from urllib.parse import urlparse
    from erpclaw_lib.db import _resolve_pg_url, get_connection as _gc
    test_url = os.environ.get("ERPCLAW_PG_TEST_URL")
    if not test_url:
        raise RuntimeError("pg target unset")
    expected_db = urlparse(test_url).path.strip("/")
    if not expected_db:
        raise RuntimeError("pg target names no database")
    db_url = _resolve_pg_url(None)
    conn = _gc()
    try:
        resolved_db = conn.execute(
            "SELECT current_database()").fetchone()[0]
        if resolved_db != expected_db:
            raise RuntimeError("pg database mismatch")
        test_parts = urlparse(test_url)
        db_parts = urlparse(db_url)
        if (test_parts.hostname != db_parts.hostname
                or test_parts.port != db_parts.port):
            raise RuntimeError("pg host/port mismatch")
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
    finally:
        conn.close()


def _pg_identity(url):
    from urllib.parse import urlparse
    from erpclaw_lib.db import get_connection as _gc
    conn = _gc()
    try:
        want_dir = os.environ.get("AUTHORITY_PGDATA_CHECK")
        data_dir = None
        if want_dir:
            data_dir = conn.execute(
                "SELECT current_setting('data_directory')").fetchone()[0]
            listen = conn.execute(
                "SELECT current_setting('listen_addresses')").fetchone()[0]
            assert data_dir == want_dir
            assert listen == ""
        server_addr = conn.execute(
            "SELECT inet_server_addr()").fetchone()[0]
        current_db = conn.execute(
            "SELECT current_database()").fetchone()[0]
    finally:
        conn.close()
    assert server_addr is None
    assert current_db == urlparse(url).path.strip("/")
    return data_dir


def _ok(result):
    assert is_ok(result), result
    return result


def _env(conn, name="Backfill Co", abbr="BC"):
    """One company with payable/expense/bank accounts, fiscal year, cost center."""
    company_id = seed_company(conn, name, abbr)
    seed_fiscal_year(conn, company_id)
    cost_center = seed_cost_center(conn, company_id)
    bank = seed_account(conn, company_id, "Bank", "asset", "bank")
    payable = seed_account(conn, company_id, "Payables", "liability", "payable")
    expense = seed_account(conn, company_id, "Expense", "expense", "expense")
    conn.execute(
        "UPDATE company SET default_payable_account_id = ?, "
        "default_expense_account_id = ?, default_cost_center_id = ? "
        "WHERE id = ?", (payable, expense, cost_center, company_id))
    conn.execute(
        "INSERT INTO naming_series (id, entity_type, prefix, current_value, "
        "company_id) VALUES (?, 'employee', 'EMP-', 0, ?)",
        (str(uuid.uuid4()), company_id))
    conn.execute(
        "INSERT INTO naming_series (id, entity_type, prefix, current_value, "
        "company_id) VALUES (?, 'expense_claim', 'EC-', 0, ?)",
        (str(uuid.uuid4()), company_id))
    conn.commit()
    return {"company_id": company_id, "bank": bank, "payable": payable,
            "expense": expense, "cost_center": cost_center}


def _employee(conn, company_id, first, last):
    return _ok(call_action(hr.add_employee, conn, ns(
        first_name=first, last_name=last, date_of_birth=None, gender=None,
        date_of_joining="2025-01-01", employment_type=None,
        company_id=company_id, department_id=None, designation_id=None,
        employee_grade_id=None, branch=None, reporting_to=None,
        company_email=None, personal_email=None, cell_phone=None,
        emergency_contact=None, bank_details=None, ssn=None,
        federal_filing_status=None, w4_allowances=None, holiday_list_id=None,
        payroll_cost_center_id=None)))["employee_id"]


def _claim(conn, env, employee_id, amount):
    items = [{"expense_type": "travel", "description": "Client visit",
              "amount": amount, "account_id": env["expense"]}]
    return _ok(call_action(hr.add_expense_claim, conn, ns(
        employee_id=employee_id, expense_date=CLAIM_DATE,
        company_id=env["company_id"], items=json.dumps(items))))["expense_claim_id"]


def _submitted(conn, env, employee_id, amount):
    claim_id = _claim(conn, env, employee_id, amount)
    _ok(call_action(hr.submit_expense_claim, conn, ns(expense_claim_id=claim_id)))
    return claim_id


def _approved(conn, env, employee_id, approver_id, amount):
    claim_id = _submitted(conn, env, employee_id, amount)
    result = _ok(call_action(hr.approve_expense_claim, conn, ns(
        expense_claim_id=claim_id, approved_by=approver_id)))
    assert result["total_amount"] == amount
    return claim_id


def _strip_approval_row(conn, claim_id):
    """Delete the row approval wrote: the claim is now pre-upgrade shaped."""
    ple = Table("payment_ledger_entry")
    conn.execute(
        Q.from_(ple).delete()
        .where(ple.voucher_type == P())
        .where(ple.voucher_id == P()).get_sql(),
        ("expense_claim", claim_id))
    conn.commit()


def _pre_upgrade_claim(conn, env, employee_id, approver_id, amount="150.00"):
    claim_id = _approved(conn, env, employee_id, approver_id, amount)
    _strip_approval_row(conn, claim_id)
    assert _ple_rows(conn, claim_id) == []
    return claim_id


def _ple_rows(conn, claim_id):
    return conn.execute(
        "SELECT * FROM payment_ledger_entry WHERE voucher_type = ? "
        "AND voucher_id = ?", ("expense_claim", claim_id)).fetchall()


def _audit_rows(conn, claim_id):
    return conn.execute(
        "SELECT * FROM audit_log WHERE action = ? AND entity_type = ? "
        "AND entity_id = ?",
        ("migration:051_backfill_expense_claim_ple",
         "expense_claim", claim_id)).fetchall()


def _outstanding(conn, employee_id):
    return _ok(call_action(pay.get_outstanding, conn, ns(
        party_type="employee", party_id=employee_id,
        voucher_type=None, voucher_id=None,
        company_id=None, company_name=None)))


def _inv27(conn):
    engine._ensure_decimal_sum(conn)
    return engine._check_inv27_party_level_residual(conn)


def _run(db_path, report_only=False):
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = mig.run_migration(db_path, report_only=report_only)
    return result, buf.getvalue()


# ── the backfill ─────────────────────────────────────────────────────────────

def test_backfills_missing_row_with_approval_shape(conn, db_path):
    env = _env(conn)
    claimant = _employee(conn, env["company_id"], "Owed", "Employee")
    approver = _employee(conn, env["company_id"], "Mara", "Approver")
    claim_id = _pre_upgrade_claim(conn, env, claimant, approver)

    result, out = _run(db_path)

    assert len(result["written"]) == 1
    assert result["written"][0]["expense_claim_id"] == claim_id
    assert result["already_present"] == []
    assert result["skipped"] == []
    assert "appended 1 missing row(s)" in out

    rows = _ple_rows(conn, claim_id)
    assert len(rows) == 1
    row = dict(rows[0])
    series = conn.execute(
        "SELECT naming_series FROM expense_claim WHERE id = ?",
        (claim_id,)).fetchone()["naming_series"]
    assert series.startswith("EC-")
    assert row["posting_date"] == CLAIM_DATE
    assert row["account_id"] == env["payable"]
    assert (row["party_type"], row["party_id"]) == ("employee", claimant)
    assert (row["voucher_type"], row["voucher_id"]) == ("expense_claim", claim_id)
    assert (row["against_voucher_type"], row["against_voucher_id"]) == (
        "expense_claim", claim_id)
    assert row["amount"] == "150.00"
    assert row["amount_in_account_currency"] == "150.00"
    assert row["remarks"] == \
        "Expense claim %s approved (backfilled by migration 051)" % series

    audits = _audit_rows(conn, claim_id)
    assert len(audits) == 1
    new_values = json.loads(dict(audits[0])["new_values"])
    assert new_values == {"payment_ledger_entry_id": row["id"],
                          "account_id": env["payable"], "amount": "150.00",
                          "currency": "USD"}

    assert _outstanding(conn, claimant)["outstanding"] == "150.00"


def test_inv27_red_before_green_after(conn, db_path):
    env = _env(conn)
    claimant = _employee(conn, env["company_id"], "Owed", "Employee")
    approver = _employee(conn, env["company_id"], "Mara", "Approver")
    _pre_upgrade_claim(conn, env, claimant, approver)
    _approved(conn, env, claimant, approver, "150.00")

    before = _inv27(conn)
    assert before is not None
    assert "employee:" in before

    _run(db_path)

    assert _inv27(conn) is None


def test_second_run_writes_nothing(conn, db_path):
    env = _env(conn)
    claimant = _employee(conn, env["company_id"], "Owed", "Employee")
    approver = _employee(conn, env["company_id"], "Mara", "Approver")
    claim_id = _pre_upgrade_claim(conn, env, claimant, approver)

    first, _ = _run(db_path)
    assert len(first["written"]) == 1
    ple_count = conn.execute(
        "SELECT COUNT(*) FROM payment_ledger_entry").fetchone()[0]
    audit_count = conn.execute(
        "SELECT COUNT(*) FROM audit_log").fetchone()[0]

    second, _ = _run(db_path)

    assert second["written"] == []
    assert second["skipped"] == []
    assert [w["expense_claim_id"] for w in second["already_present"]] == [claim_id]
    assert conn.execute(
        "SELECT COUNT(*) FROM payment_ledger_entry").fetchone()[0] == ple_count
    assert conn.execute(
        "SELECT COUNT(*) FROM audit_log").fetchone()[0] == audit_count


def test_already_present_claim_is_untouched(conn, db_path):
    env = _env(conn)
    claimant = _employee(conn, env["company_id"], "Owed", "Employee")
    approver = _employee(conn, env["company_id"], "Mara", "Approver")
    claim_id = _approved(conn, env, claimant, approver, "150.00")

    before = [dict(r) for r in _ple_rows(conn, claim_id)]
    assert len(before) == 1

    result, _ = _run(db_path)

    assert result["written"] == []
    assert [w["expense_claim_id"] for w in result["already_present"]] == [claim_id]
    assert [dict(r) for r in _ple_rows(conn, claim_id)] == before
    assert _audit_rows(conn, claim_id) == []
    assert _inv27(conn) is None


def test_non_approved_claims_are_untouched(conn, db_path):
    env = _env(conn)
    claimant = _employee(conn, env["company_id"], "Owed", "Employee")
    approver = _employee(conn, env["company_id"], "Mara", "Approver")
    submitted = _submitted(conn, env, claimant, "40.00")
    rejected = _submitted(conn, env, claimant, "25.00")
    _ok(call_action(hr.reject_expense_claim, conn, ns(
        expense_claim_id=rejected, reason="Duplicate")))
    hand_paid = _pre_upgrade_claim(conn, env, claimant, approver)
    conn.execute("UPDATE expense_claim SET status = 'paid' WHERE id = ?", (hand_paid,))
    conn.commit()

    result, _ = _run(db_path)

    assert result["written"] == []
    assert result["skipped"] == []
    for claim_id in (submitted, rejected, hand_paid):
        assert _ple_rows(conn, claim_id) == []
        assert _audit_rows(conn, claim_id) == []
    assert conn.execute(
        "SELECT status FROM expense_claim WHERE id = ?",
        (hand_paid,)).fetchone()["status"] == "paid"


def test_paid_before_the_upgrade_nets_to_zero(conn, db_path):
    env = _env(conn)
    claimant = _employee(conn, env["company_id"], "Owed", "Employee")
    approver = _employee(conn, env["company_id"], "Mara", "Approver")
    claim_id = _pre_upgrade_claim(conn, env, claimant, approver)
    created = _ok(call_action(pay.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="pay",
        posting_date=PAY_DATE, party_type="employee", party_id=claimant,
        paid_from_account=env["bank"], paid_to_account=env["payable"],
        paid_amount="150.00", exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None,
        allocations=None, deductions=None)))
    _ok(call_action(pay.submit_payment, conn,
                    ns(payment_entry_id=created["payment_entry_id"])))

    result, _ = _run(db_path)

    assert [w["expense_claim_id"] for w in result["written"]] == [claim_id]
    assert _outstanding(conn, claimant)["outstanding"] == "0.00"
    assert _inv27(conn) is None


# ── skips: nothing written for any ───────────────────────────────────────────

def _skip_env(conn):
    env = _env(conn)
    claimant = _employee(conn, env["company_id"], "Skip", "Claimant")
    approver = _employee(conn, env["company_id"], "Mara", "Approver")
    return env, claimant, approver


def test_skip_when_approval_ledger_is_cancelled(conn, db_path):
    env, claimant, approver = _skip_env(conn)
    claim_id = _pre_upgrade_claim(conn, env, claimant, approver)
    conn.execute(
        "UPDATE gl_entry SET is_cancelled = 1 WHERE voucher_type = ? "
        "AND voucher_id = ?", ("expense_claim", claim_id))
    conn.commit()

    result, out = _run(db_path)

    assert result["written"] == []
    assert len(result["skipped"]) == 1
    assert result["skipped"][0]["expense_claim_id"] == claim_id
    assert result["skipped"][0]["reason"] == "no approval ledger credit"
    assert "no approval ledger credit" in out
    assert _ple_rows(conn, claim_id) == []
    assert _audit_rows(conn, claim_id) == []


def test_skip_when_credit_does_not_equal_total(conn, db_path):
    env, claimant, approver = _skip_env(conn)
    claim_id = _pre_upgrade_claim(conn, env, claimant, approver)
    conn.execute(
        "UPDATE gl_entry SET credit = '149.00' WHERE voucher_type = ? "
        "AND voucher_id = ? AND CAST(credit AS NUMERIC) > 0",
        ("expense_claim", claim_id))
    conn.commit()

    result, _ = _run(db_path)

    assert result["written"] == []
    assert [s["reason"] for s in result["skipped"]] == [
        "approval credit 149.00 does not equal claim total 150.00"]
    assert _ple_rows(conn, claim_id) == []
    assert _audit_rows(conn, claim_id) == []


def test_skip_when_credit_account_is_another_companys(conn, db_path):
    env, claimant, approver = _skip_env(conn)
    claim_id = _pre_upgrade_claim(conn, env, claimant, approver)
    other = _env(conn, "Other Co", "OC")
    conn.execute(
        "UPDATE gl_entry SET account_id = ? WHERE voucher_type = ? "
        "AND voucher_id = ? AND CAST(credit AS NUMERIC) > 0",
        (other["payable"], "expense_claim", claim_id))
    conn.commit()

    result, _ = _run(db_path)

    assert result["written"] == []
    assert [s["reason"] for s in result["skipped"]] == [
        "approval credit account %s belongs to another company"
        % other["payable"]]
    assert _ple_rows(conn, claim_id) == []
    assert _audit_rows(conn, claim_id) == []


def test_skip_when_credits_name_two_accounts(conn, db_path):
    env, claimant, approver = _skip_env(conn)
    claim_id = _pre_upgrade_claim(conn, env, claimant, approver)
    second = seed_account(conn, env["company_id"], "Second Payable",
                          "liability", "payable")
    conn.execute(
        "INSERT INTO gl_entry (id, posting_date, account_id, party_type, "
        "party_id, debit, credit, voucher_type, voucher_id, remarks, "
        "fiscal_year, is_cancelled) VALUES (?, ?, ?, 'employee', ?, '0', "
        "'150.00', 'expense_claim', ?, 'second credit', 'FY-2026', 0)",
        (str(uuid.uuid4()), CLAIM_DATE, second, claimant, claim_id))
    conn.commit()

    result, _ = _run(db_path)

    assert result["written"] == []
    assert [s["reason"] for s in result["skipped"]] == [
        "approval credits more than one account"]
    assert _ple_rows(conn, claim_id) == []
    assert _audit_rows(conn, claim_id) == []


# ── report-only, second company, data class ──────────────────────────────────

def test_report_only_plans_but_writes_nothing(conn, db_path):
    env = _env(conn)
    claimant = _employee(conn, env["company_id"], "Owed", "Employee")
    approver = _employee(conn, env["company_id"], "Mara", "Approver")
    claim_id = _pre_upgrade_claim(conn, env, claimant, approver)
    doomed = _pre_upgrade_claim(conn, env, claimant, approver)
    conn.execute(
        "UPDATE gl_entry SET is_cancelled = 1 WHERE voucher_type = ? "
        "AND voucher_id = ?", ("expense_claim", doomed))
    conn.commit()
    ple_before = conn.execute(
        "SELECT COUNT(*) FROM payment_ledger_entry").fetchone()[0]
    audit_before = conn.execute(
        "SELECT COUNT(*) FROM audit_log").fetchone()[0]

    result, out = _run(db_path, report_only=True)

    assert result["report_only"] is True
    assert [w["expense_claim_id"] for w in result["planned"]] == [claim_id]
    assert result["written"] == []
    assert [s["expense_claim_id"] for s in result["skipped"]] == [doomed]
    assert "would append 1 missing row(s)" in out
    assert "no approval ledger credit" in out
    assert conn.execute(
        "SELECT COUNT(*) FROM payment_ledger_entry").fetchone()[0] == ple_before
    assert conn.execute(
        "SELECT COUNT(*) FROM audit_log").fetchone()[0] == audit_before


def test_second_company_claim_uses_its_own_payable(conn, db_path):
    first = _env(conn, "First Co", "FC")
    first_claimant = _employee(conn, first["company_id"], "First", "Claimant")
    first_approver = _employee(conn, first["company_id"], "First", "Approver")
    first_claim = _pre_upgrade_claim(
        conn, first, first_claimant, first_approver)
    second = _env(conn, "Second Co", "SC")
    second_claimant = _employee(conn, second["company_id"], "Second", "Claimant")
    second_approver = _employee(conn, second["company_id"], "Second", "Approver")
    second_claim = _pre_upgrade_claim(
        conn, second, second_claimant, second_approver)

    result, _ = _run(db_path)

    assert sorted(w["expense_claim_id"] for w in result["written"]) == \
        sorted([first_claim, second_claim])
    for claim_id, env in ((first_claim, first), (second_claim, second)):
        rows = _ple_rows(conn, claim_id)
        assert len(rows) == 1
        assert dict(rows[0])["account_id"] == env["payable"]
    assert _inv27(conn) is None


def test_migration_data_class_and_id():
    assert mig.MIGRATION_DATA_CLASS == "rows"
    assert mig.MIGRATION_ID == "051_backfill_expense_claim_ple"


def test_missing_credited_account_is_its_own_reason(conn, db_path):
    env = _env(conn)
    claimant = _employee(conn, env["company_id"], "Owed", "Employee")
    approver = _employee(conn, env["company_id"], "Mara", "Approver")
    claim_id = _pre_upgrade_claim(conn, env, claimant, approver)
    credit = conn.execute(
        "SELECT account_id FROM gl_entry WHERE voucher_type = ? AND voucher_id = ? "
        "AND CAST(credit AS NUMERIC) > 0",
        ("expense_claim", claim_id)).fetchone()["account_id"]

    real_execute = conn.execute

    class _MissingAccountResult:
        def fetchone(self):
            return None

    class _PlanWrapper:
        def execute(self, statement, params=()):
            if statement == mig._SELECT_ACCOUNT_COMPANY:
                return _MissingAccountResult()
            return real_execute(statement, params)

    writes, already_present, skips = mig._plan(_PlanWrapper())

    assert writes == []
    assert len(skips) == 1
    assert skips[0]["expense_claim_id"] == claim_id
    assert skips[0]["reason"] == "approval credit account %s does not exist" % credit


@pytest.mark.skipif(not os.environ.get("ERPCLAW_PG_TEST_URL"),
                    reason="PG lane runs on the gate's PostgreSQL leg")
def test_backfill_on_postgresql(monkeypatch):
    url = os.environ.get("ERPCLAW_PG_TEST_URL")
    from erpclaw_lib import seam
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", url)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    seam.dispose_engines()
    try:
        _pg_identity(url)
        _pg_reset_schema()
        init_all_tables(None)
        from erpclaw_lib.db import get_connection
        conn = get_connection(None)
        try:
            tag = uuid.uuid4().hex[:6]
            env = _env(conn, f"Backfill Co {tag}", f"BC{tag[:2]}")
            claimant = _employee(conn, env["company_id"], "Owed", "Employee")
            approver = _employee(conn, env["company_id"], "Mara", "Approver")
            claim_id = _pre_upgrade_claim(conn, env, claimant, approver)

            result, _ = _run(None)

            assert claim_id in [w["expense_claim_id"] for w in result["written"]]
            rows = _ple_rows(conn, claim_id)
            assert len(rows) == 1
            row = dict(rows[0])
            series = conn.execute(
                "SELECT naming_series FROM expense_claim WHERE id = ?",
                (claim_id,)).fetchone()["naming_series"]
            assert row["posting_date"] == CLAIM_DATE
            assert row["account_id"] == env["payable"]
            assert (row["party_type"], row["party_id"]) == ("employee", claimant)
            assert (row["voucher_type"], row["voucher_id"]) == ("expense_claim", claim_id)
            assert (row["against_voucher_type"], row["against_voucher_id"]) == (
                "expense_claim", claim_id)
            assert row["amount"] == "150.00"
            assert row["amount_in_account_currency"] == "150.00"
            assert row["currency"] == "USD"
            assert row["remarks"] == \
                "Expense claim %s approved (backfilled by migration 051)" % series
            audits = _audit_rows(conn, claim_id)
            assert len(audits) == 1
            new_values = json.loads(dict(audits[0])["new_values"])
            assert new_values == {"payment_ledger_entry_id": row["id"],
                                  "account_id": env["payable"], "amount": "150.00",
                                  "currency": "USD"}
        finally:
            conn.close()
    finally:
        seam.dispose_engines()


@pytest.mark.skipif(not os.environ.get("ERPCLAW_PG_TEST_URL"),
                    reason="PG lane runs on the gate's PostgreSQL leg")
def test_run_migration_none_matches_url_on_postgresql(monkeypatch):
    url = os.environ.get("ERPCLAW_PG_TEST_URL")
    from erpclaw_lib import seam
    from erpclaw_lib.db import get_connection as _get_conn
    for target in (None, url):
        monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
        monkeypatch.setenv("ERPCLAW_DB_URL", url)
        monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
        seam.dispose_engines()
        try:
            _pg_identity(url)
            _pg_reset_schema()
            init_all_tables(None)
            conn = _get_conn(None)
            try:
                tag = uuid.uuid4().hex[:6]
                env = _env(conn, f"Backfill Co {tag}", f"BC{tag[:2]}")
                claimant = _employee(conn, env["company_id"], "Owed", "Employee")
                approver = _employee(conn, env["company_id"], "Mara", "Approver")
                claim_id = _pre_upgrade_claim(conn, env, claimant, approver)

                result, _ = _run(target)

                assert [w["expense_claim_id"] for w in result["written"]] == [claim_id]
                rows = _ple_rows(conn, claim_id)
                assert len(rows) == 1
                row = dict(rows[0])
                assert row["account_id"] == env["payable"]
                assert row["amount"] == "150.00"
                assert row["amount_in_account_currency"] == "150.00"
                assert row["currency"] == "USD"
                assert result["skipped"] == []
                assert result["already_present"] == []
            finally:
                conn.close()
        finally:
            seam.dispose_engines()
