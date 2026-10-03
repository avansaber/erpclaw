"""Cost center defaults: the company default wins, claims follow the employee.

Ledger legs that need a default cost center use the company's configured
default cost center, falling back to the oldest non-group cost center.
An expense claim's expense leg follows the claiming employee's own cost
center instead: the employee's payroll cost center first, then the
employee's department cost center, otherwise the company default. A "stop"
budget on the employee's cost center can therefore refuse a claim that the
company default would have let through.
"""
import json
import uuid

from hr_helpers import (
    build_hr_env,
    call_action,
    is_error,
    is_ok,
    load_db_query,
    ns,
    seed_company,
    seed_cost_center,
    seed_naming_series,
)
from erpclaw_lib.query import P, Q, Table
from erpclaw_lib.query_helpers import get_default_cost_center

mod = load_db_query()


def _ok(result):
    assert is_ok(result), result
    return result


def _emp_ns(**overrides):
    defaults = dict(
        first_name="TestEmp",
        last_name=None,
        date_of_birth=None,
        gender=None,
        date_of_joining="2025-01-01",
        employment_type=None,
        company_id=None,
        department_id=None,
        designation_id=None,
        employee_grade_id=None,
        branch=None,
        reporting_to=None,
        company_email=None,
        personal_email=None,
        cell_phone=None,
        emergency_contact=None,
        bank_details=None,
        ssn=None,
        federal_filing_status=None,
        w4_allowances=None,
        holiday_list_id=None,
        payroll_cost_center_id=None,
    )
    defaults.update(overrides)
    return ns(**defaults)


def _add_employee(conn, company_id, first_name="Emp", **kwargs):
    return _ok(call_action(mod.add_employee, conn, _emp_ns(
        first_name=first_name, company_id=company_id, **kwargs)))["employee_id"]


def _add_department(conn, company_id, name, cost_center_id=None):
    return _ok(call_action(mod.add_department, conn, ns(
        name=name,
        company_id=company_id,
        parent_id=None,
        cost_center_id=cost_center_id,
    )))["department_id"]


def _claim_150(conn, company_id, employee_id, expense_account):
    items = json.dumps([{
        "expense_type": "travel",
        "description": "Trip",
        "amount": "150.00",
        "account_id": expense_account,
    }])
    return _ok(call_action(mod.add_expense_claim, conn, ns(
        employee_id=employee_id,
        expense_date="2026-03-01",
        company_id=company_id,
        items=items,
    )))["expense_claim_id"]


def _submit(conn, claim_id):
    return _ok(call_action(mod.submit_expense_claim, conn, ns(
        expense_claim_id=claim_id,
    )))


def _approve(conn, claim_id, approver_id):
    return call_action(mod.approve_expense_claim, conn, ns(
        expense_claim_id=claim_id,
        approved_by=approver_id,
    ))


def _gl_legs(conn, claim_id):
    gl = Table("gl_entry")
    q = (Q.from_(gl)
         .select(gl.account_id, gl.debit, gl.credit, gl.cost_center_id)
         .where(gl.voucher_id == P()))
    return [dict(r) for r in conn.execute(q.get_sql(), (claim_id,)).fetchall()]


def _claim_status(conn, claim_id):
    ec = Table("expense_claim")
    q = Q.from_(ec).select(ec.status).where(ec.id == P())
    return conn.execute(q.get_sql(), (claim_id,)).fetchone()["status"]


def _gl_count(conn, claim_id):
    gl = Table("gl_entry")
    q = Q.from_(gl).select(gl.id).where(gl.voucher_id == P())
    return len(conn.execute(q.get_sql(), (claim_id,)).fetchall())


def _set_company_default(conn, company_id, cost_center_id):
    co = Table("company")
    q = Q.update(co).set(co.default_cost_center_id, P()).where(co.id == P())
    conn.execute(q.get_sql(), (cost_center_id, company_id))
    conn.commit()


def _insert_cc(conn, company_id, name, created_at=None, is_group=0):
    cc = Table("cost_center")
    cc_id = str(uuid.uuid4())
    if created_at is None:
        q = (Q.into(cc)
             .columns(cc.id, cc.name, cc.company_id, cc.is_group)
             .insert(P(), P(), P(), P()))
        conn.execute(q.get_sql(), (cc_id, name, company_id, is_group))
    else:
        q = (Q.into(cc)
             .columns(cc.id, cc.name, cc.company_id, cc.is_group, cc.created_at)
             .insert(P(), P(), P(), P(), P()))
        conn.execute(q.get_sql(), (cc_id, name, company_id, is_group, created_at))
    conn.commit()
    return cc_id


def test_default_is_the_company_default(conn):
    cid = seed_company(conn)
    seed_cost_center(conn, cid, "Other CC")
    main = seed_cost_center(conn, cid, "Main CC")
    _set_company_default(conn, cid, main)
    assert get_default_cost_center(conn, cid) == main
    assert mod._get_cost_center(conn, cid) == main


def test_default_without_company_default_is_ordered(conn):
    cid = seed_company(conn)
    _insert_cc(conn, cid, "Alpha CC", "2026-01-02 00:00:00")
    zeta = _insert_cc(conn, cid, "Zeta CC", "2026-01-01 00:00:00")
    assert get_default_cost_center(conn, cid) == zeta

    cid2 = seed_company(conn)
    _insert_cc(conn, cid2, "Beta CC", "2026-05-01 00:00:00")
    alpha2 = _insert_cc(conn, cid2, "Alpha CC", "2026-05-01 00:00:00")
    assert get_default_cost_center(conn, cid2) == alpha2

    _insert_cc(conn, cid, "Group CC", "2025-01-01 00:00:00", is_group=1)
    assert get_default_cost_center(conn, cid) == zeta

    other_co = seed_company(conn)
    _insert_cc(conn, other_co, "Other Co CC", "2020-01-01 00:00:00")
    assert get_default_cost_center(conn, cid) == zeta

    cid3 = seed_company(conn)
    assert get_default_cost_center(conn, cid3) is None


def test_company_default_that_is_a_group_is_skipped(conn):
    cid = seed_company(conn)
    group = _insert_cc(conn, cid, "Group CC", is_group=1)
    old = _insert_cc(conn, cid, "Old Leaf", "2026-01-01 00:00:00")
    _insert_cc(conn, cid, "New Leaf", "2026-02-01 00:00:00")
    _set_company_default(conn, cid, group)
    assert get_default_cost_center(conn, cid) == old


def test_expense_claim_uses_the_payroll_cost_center(conn, env):
    sales = seed_cost_center(conn, env["company_id"], "Sales CC")
    claimant = _add_employee(conn, env["company_id"], "Claimant",
                             payroll_cost_center_id=sales)
    approver = _add_employee(conn, env["company_id"], "Approver")
    claim_id = _claim_150(conn, env["company_id"], claimant, env["expense_account"])
    _submit(conn, claim_id)
    result = _approve(conn, claim_id, approver)
    assert is_ok(result), result

    legs = _gl_legs(conn, claim_id)
    expense = [leg for leg in legs if leg["debit"] == "150.00"]
    payable = [leg for leg in legs if leg["credit"] == "150.00"]
    assert len(expense) == 1
    assert expense[0]["cost_center_id"] == sales
    assert len(payable) == 1
    assert payable[0]["cost_center_id"] is None


def test_expense_claim_uses_the_department_cost_center(conn, env):
    sales = seed_cost_center(conn, env["company_id"], "Sales CC")
    dept = _add_department(conn, env["company_id"], "Sales Dept", sales)
    claimant = _add_employee(conn, env["company_id"], "Claimant",
                             department_id=dept)
    approver = _add_employee(conn, env["company_id"], "Approver")
    claim_id = _claim_150(conn, env["company_id"], claimant, env["expense_account"])
    _submit(conn, claim_id)
    result = _approve(conn, claim_id, approver)
    assert is_ok(result), result

    legs = _gl_legs(conn, claim_id)
    expense = [leg for leg in legs if leg["debit"] == "150.00"]
    assert len(expense) == 1
    assert expense[0]["cost_center_id"] == sales


def test_expense_claim_falls_back_to_the_company_default(conn, env):
    head = seed_cost_center(conn, env["company_id"], "Head Office CC")
    _set_company_default(conn, env["company_id"], head)
    claimant = _add_employee(conn, env["company_id"], "Claimant")
    approver = _add_employee(conn, env["company_id"], "Approver")
    claim_id = _claim_150(conn, env["company_id"], claimant, env["expense_account"])
    _submit(conn, claim_id)
    result = _approve(conn, claim_id, approver)
    assert is_ok(result), result

    legs = _gl_legs(conn, claim_id)
    expense = [leg for leg in legs if leg["debit"] == "150.00"]
    assert len(expense) == 1
    assert expense[0]["cost_center_id"] == head
    assert expense[0]["cost_center_id"] != env["cost_center_id"]


def test_expense_claim_refuses_an_invalid_employee_cost_center(conn, env):
    other_co = seed_company(conn)
    foreign_cc = seed_cost_center(conn, other_co, "Foreign CC")
    claimant_a = _add_employee(conn, env["company_id"], "ClaimA",
                               payroll_cost_center_id=foreign_cc)
    approver = _add_employee(conn, env["company_id"], "Approver")
    claim_a = _claim_150(conn, env["company_id"], claimant_a, env["expense_account"])
    _submit(conn, claim_a)
    result_a = _approve(conn, claim_a, approver)
    assert is_error(result_a), result_a
    assert result_a["message"] == (
        f"Cannot approve expense claim: employee {claimant_a} has payroll "
        f"cost center {foreign_cc}, which is not a non-group cost center of "
        f"company {env['company_id']}. Correct the employee's or department's "
        "cost center first."
    )
    assert _claim_status(conn, claim_a) == "submitted"
    assert _gl_count(conn, claim_a) == 0

    group = _insert_cc(conn, env["company_id"], "Group CC", is_group=1)
    dept = _add_department(conn, env["company_id"], "Group Dept", group)
    claimant_b = _add_employee(conn, env["company_id"], "ClaimB",
                               department_id=dept)
    claim_b = _claim_150(conn, env["company_id"], claimant_b, env["expense_account"])
    _submit(conn, claim_b)
    result_b = _approve(conn, claim_b, approver)
    assert is_error(result_b), result_b
    assert result_b["message"] == (
        f"Cannot approve expense claim: employee {claimant_b} has department "
        f"cost center {group}, which is not a non-group cost center of "
        f"company {env['company_id']}. Correct the employee's or department's "
        "cost center first."
    )
    assert _claim_status(conn, claim_b) == "submitted"
    assert _gl_count(conn, claim_b) == 0


def test_cross_company_claim_uses_the_claim_company_default(conn):
    env_b = build_hr_env(conn)
    co_a = seed_company(conn)
    seed_naming_series(conn, co_a)
    cc_a = seed_cost_center(conn, co_a, "A CC")
    emp_a = _add_employee(conn, co_a, "CrossA", payroll_cost_center_id=cc_a)
    approver_b = _add_employee(conn, env_b["company_id"], "ApproverB")
    claim_id = _claim_150(conn, env_b["company_id"], emp_a, env_b["expense_account"])
    _submit(conn, claim_id)
    result = _approve(conn, claim_id, approver_b)
    assert is_ok(result), result

    legs = _gl_legs(conn, claim_id)
    expense = [leg for leg in legs if leg["debit"] == "150.00"]
    assert len(expense) == 1
    assert expense[0]["cost_center_id"] == env_b["cost_center_id"]
