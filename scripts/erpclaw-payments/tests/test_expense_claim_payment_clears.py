"""m805b: paying an approved expense claim clears what is owed to the employee.

An approved claim is money owed to the employee (m805a writes one
payment-ledger row under the claim's own voucher). A submitted pay-to-employee
payment allocated to the claim for its full total must clear it: outstanding
drops to zero and the claim reads `paid`. Cancelling that payment restores
`approved` and the outstanding amount.

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


_INV_PATH = os.path.join(_TESTS_DIR, "..", "..", "..", "..", "..", "testing", "invariant_engine.py")
if os.path.exists(_INV_PATH):
    _spec = importlib.util.spec_from_file_location("invariant_engine_ecc",
                                                   _INV_PATH)
    inv_engine = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(inv_engine)
else:
    inv_engine = None


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


hr = _load(_HR_PATH, "db_query_hr_ecc")

CLAIM_DATE = "2026-03-02"
PAY_DATE = "2026-06-02"


# ── environment ────────────────────────────────────────────────────────────

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


def _submitted_claim(conn, company_id, employee_id, amount):
    return _claim(conn, company_id, employee_id, amount)


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
             paid_to=None, party_type="employee", party_id=None,
             allocations="default", deductions=None):
    if allocations == "default":
        allocs = [{"voucher_type": "expense_claim", "voucher_id": claim_id,
                   "allocated_amount": alloc_amount if alloc_amount is not None else amount}]
    elif allocations is None:
        allocs = None
    else:
        allocs = allocations
    return call_action(pay.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="pay",
        posting_date=PAY_DATE, party_type=party_type,
        party_id=party_id if party_id is not None else employee_id,
        paid_from_account=env["bank"],
        paid_to_account=paid_to or env["payable"],
        paid_amount=amount, exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None,
        allocations=json.dumps(allocs) if allocs is not None else None,
        deductions=json.dumps(deductions) if deductions is not None else None))


def _submit(conn, pe_id):
    return call_action(pay.submit_payment, conn, ns(payment_entry_id=pe_id))


def _cancel(conn, pe_id):
    return call_action(pay.cancel_payment, conn, ns(payment_entry_id=pe_id))


def _outstanding(conn, employee_id):
    r = call_action(pay.get_outstanding, conn, ns(
        party_type="employee", party_id=employee_id,
        voucher_type=None, voucher_id=None,
        company_id=None, company_name=None))
    assert is_ok(r), r
    return r


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


def _ple_net(conn, party_id):
    total = Decimal("0")
    for r in conn.execute(
            "SELECT voucher_type, amount, delinked FROM payment_ledger_entry "
            "WHERE party_type = 'employee' AND party_id = ?", (party_id,)):
        if r["voucher_type"] == "payment_entry" or (r["delinked"] or 0) == 0:
            total += Decimal(str(r["amount"]))
    return total


def _gl_payable_net(conn, account_id, party_id):
    total = Decimal("0")
    for r in conn.execute(
            "SELECT debit, credit FROM gl_entry WHERE account_id = ? "
            "AND party_type = 'employee' AND party_id = ? AND is_cancelled = 0",
            (account_id, party_id)):
        total += Decimal(str(r["debit"])) - Decimal(str(r["credit"]))
    return total


def _inv27(conn):
    if inv_engine is None:
        pytest.fail(f"testing/invariant_engine.py not found at {_INV_PATH}")
    inv_engine._ensure_decimal_sum(conn)
    return inv_engine._check_inv27_party_level_residual(conn)


def _scenario(conn, amount="150.00"):
    env = _env(conn)
    claimant = _employee(conn, env["company_id"], "Owed", "Employee")
    approver = _employee(conn, env["company_id"], "Mara", "Approver")
    other = _employee(conn, env["company_id"], "Other", "Employee")
    claim_id = _approved_claim(conn, env["company_id"], claimant, approver, amount)
    return env, claimant, approver, other, claim_id


# ── clears ─────────────────────────────────────────────────────────────────

def test_pay_clears_claim_and_marks_paid(conn):
    env, claimant, _approver, _other, claim_id = _scenario(conn)
    assert _outstanding(conn, claimant)["outstanding"] == "150.00"

    created = _add_pay(conn, env, claimant, "150.00", claim_id)
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    res = _submit(conn, pe_id)
    assert is_ok(res), res

    assert _outstanding(conn, claimant)["outstanding"] == "0.00"
    row = _claim_row(conn, claim_id)
    assert row["status"] == "paid"
    assert row["payment_entry_id"] == pe_id
    assert _ple_net(conn, claimant) == Decimal("0.00")
    assert _gl_payable_net(conn, env["payable"], claimant) == Decimal("0.00")
    assert _gl_payable_net(conn, env["payable"], claimant) == _ple_net(conn, claimant)
    assert _inv27(conn) is None


def test_cancel_restores_approved_and_owed(conn):
    env, claimant, _approver, _other, claim_id = _scenario(conn)
    created = _add_pay(conn, env, claimant, "150.00", claim_id)
    pe_id = created["payment_entry_id"]
    assert is_ok(_submit(conn, pe_id)), pe_id

    cancelled = _cancel(conn, pe_id)
    assert is_ok(cancelled), cancelled

    row = _claim_row(conn, claim_id)
    assert row["status"] == "approved"
    assert row["payment_entry_id"] is None
    assert _outstanding(conn, claimant)["outstanding"] == "150.00"
    assert _inv27(conn) is None


# ── refusals write nothing ─────────────────────────────────────────────────

def test_partial_allocation_is_refused(conn):
    env, claimant, _approver, _other, claim_id = _scenario(conn)
    before = _counts(conn)
    created = _add_pay(conn, env, claimant, "150.00", claim_id, alloc_amount="100.00")
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]

    res = _submit(conn, pe_id)
    assert is_error(res)
    assert res["message"] == (
        "Payment allocation failed: An expense claim is paid in full: "
        f"allocate exactly 150.00 to expense claim {claim_id}")
    assert _pe_status(conn, pe_id) == "draft"
    assert _claim_row(conn, claim_id)["status"] == "approved"
    assert _counts(conn) == before


def test_submitted_claim_is_refused(conn):
    env = _env(conn)
    claimant = _employee(conn, env["company_id"], "Owed", "Employee")
    _approver = _employee(conn, env["company_id"], "Mara", "Approver")
    claim_id = _submitted_claim(conn, env["company_id"], claimant, "150.00")
    s = call_action(hr.submit_expense_claim, conn, ns(expense_claim_id=claim_id))
    assert is_ok(s), s
    before = _counts(conn)

    created = _add_pay(conn, env, claimant, "150.00", claim_id)
    assert is_ok(created), created
    res = _submit(conn, created["payment_entry_id"])
    assert is_error(res)
    assert res["message"] == (
        "Payment allocation failed: Cannot apply payment: expense claim is "
        f"'submitted' (must be 'approved')")
    assert _pe_status(conn, created["payment_entry_id"]) == "draft"
    assert _claim_row(conn, claim_id)["status"] == "submitted"
    assert _counts(conn) == before


def test_payment_to_another_employee_is_refused(conn):
    env, claimant, _approver, other, claim_id = _scenario(conn)
    before = _counts(conn)
    created = _add_pay(conn, env, other, "150.00", claim_id)
    assert is_ok(created), created
    res = _submit(conn, created["payment_entry_id"])
    assert is_error(res)
    assert res["message"] == (
        "Payment allocation failed: "
        f"Expense claim {claim_id} belongs to another employee or company")
    assert _pe_status(conn, created["payment_entry_id"]) == "draft"
    assert _claim_row(conn, claim_id)["status"] == "approved"
    assert _counts(conn) == before


def test_supplier_party_payment_is_refused(conn):
    env, claimant, _approver, _other, claim_id = _scenario(conn)
    before = _counts(conn)
    created = _add_pay(conn, env, claimant, "150.00", claim_id,
                       party_type="supplier", party_id=env["supplier"])
    assert is_ok(created), created
    res = _submit(conn, created["payment_entry_id"])
    assert is_error(res)
    assert res["message"] == (
        "Payment allocation failed: "
        "An expense claim can only be paid to its employee")
    assert _pe_status(conn, created["payment_entry_id"]) == "draft"
    assert _claim_row(conn, claim_id)["status"] == "approved"
    assert _counts(conn) == before


def test_wrong_paid_to_account_is_refused(conn):
    env, claimant, _approver, _other, claim_id = _scenario(conn)
    before = _counts(conn)
    created = _add_pay(conn, env, claimant, "150.00", claim_id,
                       paid_to=env["bank"])
    assert is_ok(created), created
    res = _submit(conn, created["payment_entry_id"])
    assert is_error(res)
    assert res["message"] == (
        "Payment allocation failed: paid-to-account must be the payable "
        f"account of expense claim {claim_id}")
    assert _pe_status(conn, created["payment_entry_id"]) == "draft"
    assert _claim_row(conn, claim_id)["status"] == "approved"
    assert _counts(conn) == before


def test_payment_with_deduction_is_refused(conn):
    env, claimant, _approver, _other, claim_id = _scenario(conn)
    before = _counts(conn)
    created = _add_pay(conn, env, claimant, "160.00", claim_id,
                       alloc_amount="150.00",
                       deductions=[{"account_id": env["tds"], "amount": "10.00",
                                    "type": "tds"}])
    assert is_ok(created), created
    res = _submit(conn, created["payment_entry_id"])
    assert is_error(res)
    assert res["message"] == "Deductions are not supported on a payment to an expense claim"
    assert _pe_status(conn, created["payment_entry_id"]) == "draft"
    assert _claim_row(conn, claim_id)["status"] == "approved"
    assert _counts(conn) == before


def test_second_payment_against_paid_claim_is_refused(conn):
    env, claimant, _approver, _other, claim_id = _scenario(conn)
    first = _add_pay(conn, env, claimant, "150.00", claim_id)
    assert is_ok(first), first
    assert is_ok(_submit(conn, first["payment_entry_id"]))
    assert _claim_row(conn, claim_id)["status"] == "paid"
    before = _counts(conn)

    second = _add_pay(conn, env, claimant, "150.00", claim_id)
    assert is_ok(second), second
    res = _submit(conn, second["payment_entry_id"])
    assert is_error(res)
    assert res["message"] == (
        "Payment allocation failed: Cannot apply payment: expense claim is "
        "'paid' (must be 'approved')")
    assert _pe_status(conn, second["payment_entry_id"]) == "draft"
    row = _claim_row(conn, claim_id)
    assert row["status"] == "paid"
    assert row["payment_entry_id"] == first["payment_entry_id"]
    assert _counts(conn) == before


# ── allocate later ─────────────────────────────────────────────────────────

def test_allocate_later_clears_and_cancel_restores(conn):
    env, claimant, _approver, _other, claim_id = _scenario(conn)
    created = _add_pay(conn, env, claimant, "150.00", claim_id, allocations=None)
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    assert is_ok(_submit(conn, pe_id)), pe_id
    assert _claim_row(conn, claim_id)["status"] == "approved"
    # The unallocated advance nets the party total to zero, but the claim
    # bucket still shows the full amount owed until it is allocated.
    buckets = {(v["voucher_type"], v["voucher_id"]): v["outstanding_amount"]
               for v in _outstanding(conn, claimant)["vouchers"]}
    assert buckets[("expense_claim", claim_id)] == "150.00"

    alloc = call_action(pay.allocate_payment, conn, ns(
        payment_entry_id=pe_id, voucher_type="expense_claim",
        voucher_id=claim_id, allocated_amount="150.00"))
    assert is_ok(alloc), alloc

    assert _claim_row(conn, claim_id)["status"] == "paid"
    assert _claim_row(conn, claim_id)["payment_entry_id"] == pe_id
    assert _outstanding(conn, claimant)["outstanding"] == "0.00"
    assert _inv27(conn) is None

    assert is_ok(_cancel(conn, pe_id)), pe_id
    row = _claim_row(conn, claim_id)
    assert row["status"] == "approved"
    assert row["payment_entry_id"] is None
    assert _outstanding(conn, claimant)["outstanding"] == "150.00"


# ── a paid claim cannot move ────────────────────────────────────────────────

def test_paid_claim_cannot_move(conn):
    env, claimant, _approver, _other, claim_id = _scenario(conn)
    created = _add_pay(conn, env, claimant, "150.00", claim_id)
    assert is_ok(_submit(conn, created["payment_entry_id"]))
    assert _claim_row(conn, claim_id)["status"] == "paid"

    res = call_action(hr.update_expense_claim_status, conn, ns(
        expense_claim_id=claim_id, status="cancelled", payment_entry_id=None))
    assert is_error(res)
    assert res["message"] == (
        f"Expense claim {claim_id} cannot change from 'paid' to 'cancelled'. "
        "Allowed from 'paid': none")
    assert _claim_row(conn, claim_id)["status"] == "paid"


# ── manual paid is refused ─────────────────────────────────────────────────

def test_manual_paid_status_is_refused(conn):
    env, claimant, _approver, _other, claim_id = _scenario(conn)
    audits_before = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE entity_type = 'expense_claim' "
        "AND entity_id = ? AND action = 'update-expense-claim-status'",
        (claim_id,)).fetchone()[0]

    res = call_action(hr.update_expense_claim_status, conn, ns(
        expense_claim_id=claim_id, status="paid", payment_entry_id="PAY-0042"))
    assert is_error(res)
    assert res["message"] == (
        f"Expense claim {claim_id} is owed to the employee in the payment "
        "ledger; it is marked paid when a submitted payment is allocated to it")
    row = _claim_row(conn, claim_id)
    assert row["status"] == "approved"
    assert row["payment_entry_id"] is None
    audits_after = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE entity_type = 'expense_claim' "
        "AND entity_id = ? AND action = 'update-expense-claim-status'",
        (claim_id,)).fetchone()[0]
    assert audits_after == audits_before


# ── helpers for the no-payable and paid-claim pins ─────────────────────────

def _unallocated(conn, pe_id):
    row = conn.execute(
        "SELECT unallocated_amount FROM payment_entry WHERE id = ?",
        (pe_id,)).fetchone()
    assert row is not None
    return row["unallocated_amount"]


def _audit_count(conn, claim_id, action):
    return conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE entity_type = 'expense_claim' "
        "AND entity_id = ? AND action = ?", (claim_id, action)).fetchone()[0]


def _delete_claim_payable(conn, claim_id):
    conn.execute(
        "DELETE FROM payment_ledger_entry WHERE voucher_type = 'expense_claim' "
        "AND voucher_id = ?", (claim_id,))
    conn.commit()


_NO_PAYABLE_TAIL = ("has no payable in the payment ledger; it cannot be paid "
                    "through a payment allocation")


# ── approved claim with no payable of its own ──────────────────────────────

def test_approved_claim_without_payable_is_refused(conn):
    env, claimant, _approver, _other, claim_id = _scenario(conn)
    _delete_claim_payable(conn, claim_id)
    before = _counts(conn)

    created = _add_pay(conn, env, claimant, "150.00", claim_id)
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]

    res = _submit(conn, pe_id)
    assert is_error(res)
    assert res["message"] == (
        f"Payment allocation failed: Expense claim {claim_id} {_NO_PAYABLE_TAIL}")
    assert _pe_status(conn, pe_id) == "draft"
    row = _claim_row(conn, claim_id)
    assert row["status"] == "approved"
    assert row["payment_entry_id"] is None
    assert _counts(conn) == before


def test_allocate_later_without_payable_is_refused(conn):
    env, claimant, _approver, _other, claim_id = _scenario(conn)
    _delete_claim_payable(conn, claim_id)

    created = _add_pay(conn, env, claimant, "150.00", claim_id, allocations=None)
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    assert is_ok(_submit(conn, pe_id)), pe_id
    assert _claim_row(conn, claim_id)["status"] == "approved"

    unallocated_before = _unallocated(conn, pe_id)
    assert unallocated_before == "150.00"
    before = _counts(conn)

    alloc = call_action(pay.allocate_payment, conn, ns(
        payment_entry_id=pe_id, voucher_type="expense_claim",
        voucher_id=claim_id, allocated_amount="150.00"))
    assert is_error(alloc)
    assert alloc["message"] == (
        f"Payment allocation failed: Expense claim {claim_id} {_NO_PAYABLE_TAIL}")
    assert _unallocated(conn, pe_id) == unallocated_before
    assert _counts(conn) == before
    row = _claim_row(conn, claim_id)
    assert row["status"] == "approved"
    assert row["payment_entry_id"] is None


# ── restored hr pins: a genuinely paid claim cannot move ───────────────────

def test_reject_paid_claim_is_refused(conn):
    env, claimant, _approver, _other, claim_id = _scenario(conn)
    created = _add_pay(conn, env, claimant, "150.00", claim_id)
    assert is_ok(_submit(conn, created["payment_entry_id"]))
    assert _claim_row(conn, claim_id)["status"] == "paid"
    audits_before = _audit_count(conn, claim_id, "reject-expense-claim")

    res = call_action(hr.reject_expense_claim, conn, ns(
        expense_claim_id=claim_id, reason="Too late"))
    assert is_error(res)
    assert res["message"] == (
        f"Expense claim {claim_id} cannot be rejected. "
        "Current status: paid (must be 'submitted')")
    assert _claim_row(conn, claim_id)["status"] == "paid"
    assert _audit_count(conn, claim_id, "reject-expense-claim") == audits_before


def test_paid_claim_cannot_move_to_approved(conn):
    env, claimant, _approver, _other, claim_id = _scenario(conn)
    created = _add_pay(conn, env, claimant, "150.00", claim_id)
    assert is_ok(_submit(conn, created["payment_entry_id"]))
    assert _claim_row(conn, claim_id)["status"] == "paid"
    audits_before = _audit_count(conn, claim_id, "update-expense-claim-status")

    res = call_action(hr.update_expense_claim_status, conn, ns(
        expense_claim_id=claim_id, status="approved", payment_entry_id=None))
    assert is_error(res)
    assert res["message"] == (
        f"Expense claim {claim_id} cannot change from 'paid' to 'approved'. "
        "Allowed from 'paid': none")
    assert _claim_row(conn, claim_id)["status"] == "paid"
    assert _audit_count(conn, claim_id, "update-expense-claim-status") == audits_before


def test_paid_claim_cannot_move_to_draft(conn):
    env, claimant, _approver, _other, claim_id = _scenario(conn)
    created = _add_pay(conn, env, claimant, "150.00", claim_id)
    assert is_ok(_submit(conn, created["payment_entry_id"]))
    assert _claim_row(conn, claim_id)["status"] == "paid"
    audits_before = _audit_count(conn, claim_id, "update-expense-claim-status")

    res = call_action(hr.update_expense_claim_status, conn, ns(
        expense_claim_id=claim_id, status="draft", payment_entry_id=None))
    assert is_error(res)
    assert res["message"] == (
        f"Expense claim {claim_id} cannot change from 'paid' to 'draft'. "
        "Allowed from 'paid': none")
    assert _claim_row(conn, claim_id)["status"] == "paid"
    assert _audit_count(conn, claim_id, "update-expense-claim-status") == audits_before


# ── reverse no-op guard ────────────────────────────────────────────────────

def test_reverse_by_other_payment_is_noop_on_paid_claim(conn):
    env, claimant, _approver, _other, claim_id = _scenario(conn)
    first = _add_pay(conn, env, claimant, "150.00", claim_id)
    assert is_ok(_submit(conn, first["payment_entry_id"]))
    pe_a = first["payment_entry_id"]
    assert _claim_row(conn, claim_id)["status"] == "paid"
    before = _counts(conn)

    res = payment_clearing.reverse_payment_on_expense_claim(
        conn, claim_id, "PAY-OTHER-0001")
    assert res["applied"] is False
    row = _claim_row(conn, claim_id)
    assert row["status"] == "paid"
    assert row["payment_entry_id"] == pe_a
    assert _counts(conn) == before


def test_reverse_is_noop_on_approved_claim(conn):
    env, claimant, _approver, _other, claim_id = _scenario(conn)
    assert _claim_row(conn, claim_id)["status"] == "approved"
    before = _counts(conn)

    res = payment_clearing.reverse_payment_on_expense_claim(
        conn, claim_id, "PAY-OTHER-0001")
    assert res["applied"] is False
    row = _claim_row(conn, claim_id)
    assert row["status"] == "approved"
    assert row["payment_entry_id"] is None
    assert _counts(conn) == before


def test_cancel_unrelated_payment_leaves_paid_claim(conn):
    env, claimant, _approver, _other, claim_id = _scenario(conn)
    first = _add_pay(conn, env, claimant, "150.00", claim_id)
    assert is_ok(_submit(conn, first["payment_entry_id"]))
    pe_a = first["payment_entry_id"]
    assert _claim_row(conn, claim_id)["status"] == "paid"

    second = _add_pay(conn, env, claimant, "50.00", claim_id, allocations=None)
    assert is_ok(second), second
    pe_b = second["payment_entry_id"]
    assert is_ok(_submit(conn, pe_b)), pe_b
    assert _claim_row(conn, claim_id)["status"] == "paid"

    assert is_ok(_cancel(conn, pe_b)), pe_b
    row = _claim_row(conn, claim_id)
    assert row["status"] == "paid"
    assert row["payment_entry_id"] == pe_a


# ── decoy ──────────────────────────────────────────────────────────────────

def test_other_company_claim_is_untouched(conn):
    env, claimant, _approver, _other, claim_id = _scenario(conn)
    env2 = _env(conn, name="Other Co")
    claimant2 = _employee(conn, env2["company_id"], "Decoy", "Employee")
    approver2 = _employee(conn, env2["company_id"], "Decoy", "Approver")
    decoy_id = _approved_claim(conn, env2["company_id"], claimant2, approver2, "80.00")

    created = _add_pay(conn, env, claimant, "150.00", claim_id)
    assert is_ok(_submit(conn, created["payment_entry_id"]))

    row = _claim_row(conn, decoy_id)
    assert row["status"] == "approved"
    assert row["payment_entry_id"] is None
    assert _outstanding(conn, claimant2)["outstanding"] == "80.00"
