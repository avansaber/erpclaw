"""Expense-claim payables carry the company currency, and refusals guide repair.

An approved claim writes one payment-ledger row under its own voucher; that
row must carry the claim company's default currency. A payment in another
currency cannot clear the claim. Marking a claim paid by hand is refused when
the claim has no payable row of its own, and the payment refusal names the
repair step as a suggestion.

Money discipline: Decimal in Python, TEXT columns, exact two-place string
comparisons. Never float.
"""
import importlib.util
import json
import os
import uuid
from decimal import Decimal

import pytest

from payments_helpers import (call_action, is_error, is_ok, load_db_query, ns,
                              seed_account)

from erpclaw_lib import payment_clearing

pay = load_db_query()

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_HR_PATH = os.path.join(os.path.dirname(os.path.dirname(_TESTS_DIR)), "erpclaw-hr", "db_query.py")
_MIGRATION_PATH = os.path.normpath(os.path.join(
    _TESTS_DIR, "..", "..", "erpclaw-setup", "migrations",
    "051_backfill_expense_claim_ple.py"))


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


hr = _load(_HR_PATH, "db_query_hr_eccs")
mig = _load(_MIGRATION_PATH, "migration_051_eccs")

CLAIM_DATE = "2026-03-02"
PAY_DATE = "2026-06-02"

REMEDY = ("Run the expense-claim backfill migration, or repair the claim's "
          "approval ledger if the migration skipped it")


def _env(conn, name="EC Co"):
    cid = str(uuid.uuid4())
    conn.execute("INSERT INTO company (id, name, abbr) VALUES (?, ?, ?)",
                 (cid, f"{name} {cid[:6]}", f"EC{cid[:4]}"))
    conn.execute(
        "INSERT INTO fiscal_year (id, name, start_date, end_date, is_closed, company_id) "
        "VALUES (?, ?, '2026-01-01', '2026-12-31', 0, ?)",
        (str(uuid.uuid4()), f"FY-{cid[:6]}", cid))
    ccid = str(uuid.uuid4())
    conn.execute("INSERT INTO cost_center (id, name, company_id, is_group) "
                 "VALUES (?, ?, ?, 0)", (ccid, f"Main CC {cid[:6]}", cid))
    conn.execute("UPDATE company SET default_cost_center_id = ? WHERE id = ?",
                 (ccid, cid))
    bank = seed_account(conn, cid, "Bank", "asset", "bank")
    payable = seed_account(conn, cid, "Payables", "liability", "payable")
    expense = seed_account(conn, cid, "Expense", "expense", "expense")
    tds = seed_account(conn, cid, "TDS", "liability")
    conn.execute("UPDATE company SET default_payable_account_id = ?, "
                 "default_expense_account_id = ? WHERE id = ?",
                 (payable, expense, cid))
    supp = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO supplier (id, name, supplier_type, status, company_id) "
        "VALUES (?, 'Acme', 'company', 'active', ?)", (supp, cid))
    conn.commit()
    return {"company_id": cid, "cc": ccid, "bank": bank, "payable": payable,
            "expense": expense, "tds": tds, "supplier": supp}


def _eur_env(conn, name="EC Co"):
    env = _env(conn, name=name)
    conn.execute("UPDATE company SET default_currency = 'EUR' WHERE id = ?",
                 (env["company_id"],))
    conn.commit()
    return env


def _employee(conn, company_id, first, last):
    r = call_action(hr.add_employee, conn, ns(
        first_name=first, last_name=last, date_of_birth=None, gender=None,
        date_of_joining="2025-01-01", employment_type=None,
        company_id=company_id, department_id=None, designation_id=None,
        employee_grade_id=None, branch=None, reporting_to=None, company_email=None,
        personal_email=None, cell_phone=None, emergency_contact=None,
        bank_details=None, ssn=None, federal_filing_status=None, w4_allowances=None,
        holiday_list_id=None, payroll_cost_center_id=None))
    assert is_ok(r), r
    return r["employee_id"]


def _claim(conn, company_id, employee_id, amount, expense_date=CLAIM_DATE):
    items = [{"expense_type": "travel", "description": "Client visit",
              "amount": amount}]
    r = call_action(hr.add_expense_claim, conn, ns(
        employee_id=employee_id, expense_date=expense_date,
        company_id=company_id, items=json.dumps(items)))
    assert is_ok(r), r
    return r["expense_claim_id"]


def _approved_claim(conn, company_id, employee_id, approver_id, amount):
    claim_id = _claim(conn, company_id, employee_id, amount)
    s = call_action(hr.submit_expense_claim, conn, ns(expense_claim_id=claim_id))
    assert is_ok(s), s
    a = call_action(hr.approve_expense_claim, conn, ns(
        expense_claim_id=claim_id, approved_by=approver_id))
    assert is_ok(a), a
    assert a["total_amount"] == amount
    return claim_id


def _add_pay(conn, env, employee_id, amount, claim_id, alloc_amount=None,
             paid_to=None, allocations="default", payment_currency=None):
    if allocations == "default":
        allocs = [{"voucher_type": "expense_claim", "voucher_id": claim_id,
                   "allocated_amount": alloc_amount if alloc_amount is not None else amount}]
    elif allocations is None:
        allocs = None
    else:
        allocs = allocations
    return call_action(pay.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="pay",
        posting_date=PAY_DATE, party_type="employee",
        party_id=employee_id,
        paid_from_account=env["bank"],
        paid_to_account=paid_to or env["payable"],
        paid_amount=amount, exchange_rate=None,
        payment_currency=payment_currency,
        reference_number=None, reference_date=None,
        allocations=json.dumps(allocs) if allocs is not None else None,
        deductions=None))


def _submit(conn, pe_id):
    return call_action(pay.submit_payment, conn, ns(payment_entry_id=pe_id))


def _cancel(conn, pe_id):
    return call_action(pay.cancel_payment, conn, ns(payment_entry_id=pe_id))


def _claim_row(conn, claim_id):
    row = conn.execute(
        "SELECT status, total_amount, payment_entry_id FROM expense_claim "
        "WHERE id = ?", (claim_id,)).fetchone()
    assert row is not None
    return {"status": row["status"], "total_amount": row["total_amount"],
            "payment_entry_id": row["payment_entry_id"]}


def _pe_status(conn, pe_id):
    return conn.execute(
        "SELECT status FROM payment_entry WHERE id = ?", (pe_id,)).fetchone()["status"]


def _counts(conn):
    ple = conn.execute("SELECT COUNT(*) FROM payment_ledger_entry").fetchone()[0]
    gl = conn.execute("SELECT COUNT(*) FROM gl_entry").fetchone()[0]
    return ple, gl


def _unallocated(conn, pe_id):
    row = conn.execute(
        "SELECT unallocated_amount FROM payment_entry WHERE id = ?",
        (pe_id,)).fetchone()
    assert row is not None
    return row["unallocated_amount"]


def _payable_row(conn, claim_id):
    return conn.execute(
        "SELECT * FROM payment_ledger_entry WHERE voucher_type = ? "
        "AND voucher_id = ?", ("expense_claim", claim_id)).fetchone()


def _delete_claim_payable(conn, claim_id):
    conn.execute(
        "DELETE FROM payment_ledger_entry WHERE voucher_type = 'expense_claim' "
        "AND voucher_id = ?", (claim_id,))
    conn.commit()


def _audit_count(conn, claim_id, action):
    return conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE entity_type = 'expense_claim' "
        "AND entity_id = ? AND action = ?", (claim_id, action)).fetchone()[0]


def _outstanding(conn, employee_id):
    r = call_action(pay.get_outstanding, conn, ns(
        party_type="employee", party_id=employee_id,
        voucher_type=None, voucher_id=None,
        company_id=None, company_name=None))
    assert is_ok(r), r
    return r


def test_approval_payable_row_carries_company_currency(conn):
    env = _eur_env(conn)
    claimant = _employee(conn, env["company_id"], "Owed", "Employee")
    approver = _employee(conn, env["company_id"], "Mara", "Approver")
    claim_id = _approved_claim(conn, env["company_id"], claimant, approver, "150.00")
    row = _payable_row(conn, claim_id)
    assert row is not None
    assert dict(row)["currency"] == "EUR"


def test_backfill_row_carries_company_currency(conn, db_path):
    env = _eur_env(conn)
    claimant = _employee(conn, env["company_id"], "Owed", "Employee")
    approver = _employee(conn, env["company_id"], "Mara", "Approver")
    claim_id = _approved_claim(conn, env["company_id"], claimant, approver, "150.00")
    _delete_claim_payable(conn, claim_id)
    result = mig.run_migration(db_path)
    assert [w["expense_claim_id"] for w in result["written"]] == [claim_id]
    row = _payable_row(conn, claim_id)
    assert row is not None
    assert dict(row)["currency"] == "EUR"
    audits = conn.execute(
        "SELECT * FROM audit_log WHERE action = ? AND entity_type = ? "
        "AND entity_id = ?",
        ("migration:051_backfill_expense_claim_ple",
         "expense_claim", claim_id)).fetchall()
    assert len(audits) == 1
    new_values = json.loads(dict(audits[0])["new_values"])
    assert new_values["currency"] == "EUR"


def test_claim_payment_in_another_currency_is_refused(conn):
    env = _eur_env(conn)
    claimant = _employee(conn, env["company_id"], "Owed", "Employee")
    approver = _employee(conn, env["company_id"], "Mara", "Approver")
    claim_id = _approved_claim(conn, env["company_id"], claimant, approver, "150.00")

    created = _add_pay(conn, env, claimant, "150.00", claim_id)
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    before = _counts(conn)
    res = _submit(conn, pe_id)
    assert is_error(res)
    assert res["message"] == (
        f"Payment allocation failed: Expense claim {claim_id} is owed in EUR, "
        f"but payment {pe_id} is in USD; pay it in EUR")
    assert _pe_status(conn, pe_id) == "draft"
    assert _claim_row(conn, claim_id)["status"] == "approved"
    assert _counts(conn) == before

    created2 = _add_pay(conn, env, claimant, "150.00", claim_id,
                        allocations=None)
    assert is_ok(created2), created2
    pe2 = created2["payment_entry_id"]
    assert is_ok(_submit(conn, pe2)), pe2
    unallocated_before = _unallocated(conn, pe2)
    before2 = _counts(conn)
    alloc = call_action(pay.allocate_payment, conn, ns(
        payment_entry_id=pe2, voucher_type="expense_claim",
        voucher_id=claim_id, allocated_amount="150.00"))
    assert is_error(alloc)
    assert alloc["message"] == (
        f"Payment allocation failed: Expense claim {claim_id} is owed in EUR, "
        f"but payment {pe2} is in USD; pay it in EUR")
    assert _unallocated(conn, pe2) == unallocated_before
    assert _counts(conn) == before2
    assert is_ok(_cancel(conn, pe2))

    created3 = _add_pay(conn, env, claimant, "150.00", claim_id,
                        payment_currency="EUR")
    assert is_ok(created3), created3
    assert is_ok(_submit(conn, created3["payment_entry_id"]))
    assert _claim_row(conn, claim_id)["status"] == "paid"
    assert _outstanding(conn, claimant)["outstanding"] == "0.00"


def test_hand_marked_paid_without_payable_is_refused(conn):
    env = _env(conn)
    claimant = _employee(conn, env["company_id"], "Owed", "Employee")
    approver = _employee(conn, env["company_id"], "Mara", "Approver")
    claim_id = _approved_claim(conn, env["company_id"], claimant, approver, "150.00")
    _delete_claim_payable(conn, claim_id)
    audits_before = _audit_count(conn, claim_id, "update-expense-claim-status")
    res = call_action(hr.update_expense_claim_status, conn, ns(
        expense_claim_id=claim_id, status="paid", payment_entry_id=None))
    assert is_error(res)
    assert res["message"] == (
        f"Expense claim {claim_id} has no payable in the payment ledger; "
        "it cannot be marked paid. " + REMEDY)
    assert _claim_row(conn, claim_id)["status"] == "approved"
    assert _audit_count(conn, claim_id, "update-expense-claim-status") == audits_before


def test_no_payable_refusal_names_the_remedy(conn):
    env = _env(conn)
    claimant = _employee(conn, env["company_id"], "Owed", "Employee")
    approver = _employee(conn, env["company_id"], "Mara", "Approver")
    claim_id = _approved_claim(conn, env["company_id"], claimant, approver, "150.00")
    _delete_claim_payable(conn, claim_id)

    created = _add_pay(conn, env, claimant, "150.00", claim_id)
    assert is_ok(created), created
    res = _submit(conn, created["payment_entry_id"])
    assert is_error(res)
    assert res["message"] == (
        f"Payment allocation failed: Expense claim {claim_id} has no payable "
        "in the payment ledger; it cannot be paid through a payment allocation")
    assert res.get("suggestion") == REMEDY

    created2 = _add_pay(conn, env, claimant, "150.00", claim_id,
                        allocations=None)
    assert is_ok(created2), created2
    pe2 = created2["payment_entry_id"]
    assert is_ok(_submit(conn, pe2)), pe2
    alloc = call_action(pay.allocate_payment, conn, ns(
        payment_entry_id=pe2, voucher_type="expense_claim",
        voucher_id=claim_id, allocated_amount="150.00"))
    assert is_error(alloc)
    assert alloc["message"] == (
        f"Payment allocation failed: Expense claim {claim_id} has no payable "
        "in the payment ledger; it cannot be paid through a payment allocation")
    assert alloc.get("suggestion") == REMEDY
