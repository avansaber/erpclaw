"""Leave allocation must name an open year of the employee company.

Allocations stored under another company year or a closed year are
invisible to later readers, so add-leave-allocation refuses them.
"""
from hr_helpers import (
    build_hr_env,
    call_action,
    is_error,
    is_ok,
    load_db_query,
    ns,
    seed_company,
    seed_fiscal_year,
)
from erpclaw_lib.query import P, Q, Table, fn

mod = load_db_query()


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
    result = call_action(mod.add_employee, conn, _emp_ns(
        first_name=first_name, company_id=company_id, **kwargs))
    assert is_ok(result), result
    return result["employee_id"]


def _create_leave_type(conn, name="Annual Leave", max_days="20"):
    result = call_action(mod.add_leave_type, conn, ns(
        name=name,
        max_days_allowed=max_days,
        is_paid_leave=None,
        is_carry_forward=None,
        max_carry_forward_days=None,
        is_compensatory=None,
        applicable_after_days=None,
    ))
    assert is_ok(result), result
    return result["leave_type_id"]


def _allocate(conn, employee_id, leave_type_id, total_leaves, fy_name):
    return call_action(mod.add_leave_allocation, conn, ns(
        employee_id=employee_id,
        leave_type_id=leave_type_id,
        total_leaves=total_leaves,
        fiscal_year=fy_name,
    ))


def _counts(conn):
    la = Table("leave_allocation")
    q = Q.from_(la).select(fn.Count("*").as_("n"))
    la_count = conn.execute(q.get_sql()).fetchone()["n"]
    al = Table("audit_log")
    q2 = Q.from_(al).select(fn.Count("*").as_("n"))
    al_count = conn.execute(q2.get_sql()).fetchone()["n"]
    return {"leave_allocation": la_count, "audit_log": al_count}


def _allocations_for(conn, employee_id):
    la = Table("leave_allocation")
    q = (Q.from_(la).select(la.star).where(la.employee_id == P()))
    return [dict(r) for r in conn.execute(q.get_sql(), (employee_id,)).fetchall()]


def _close_year(conn, fiscal_year_id):
    fy = Table("fiscal_year")
    q = Q.update(fy).set(fy.is_closed, P()).where(fy.id == P())
    conn.execute(q.get_sql(), (1, fiscal_year_id))
    conn.commit()


def _two_companies(conn):
    b = seed_company(conn, name="Other Co", abbr="OC")
    seed_fiscal_year(conn, b, name="FY-OTHER",
                     start="2026-01-01", end="2026-12-31")
    env = build_hr_env(conn)
    emp_id = _add_employee(conn, env["company_id"], "AllocEmp")
    lt_id = _create_leave_type(conn)
    return b, env, emp_id, lt_id


def test_allocation_refuses_another_company_year(conn):
    _b, env, emp_id, lt_id = _two_companies(conn)
    before = _counts(conn)
    result = _allocate(conn, emp_id, lt_id, "15", "FY-OTHER")
    assert is_error(result), result
    assert result["message"] == "Fiscal year 'FY-OTHER' belongs to another company"
    assert _counts(conn) == before


def test_allocation_refuses_a_closed_own_year(conn, env):
    emp_id = _add_employee(conn, env["company_id"], "ClosedEmp")
    lt_id = _create_leave_type(conn)
    _close_year(conn, env["fiscal_year_id"])
    before = _counts(conn)
    result = _allocate(conn, emp_id, lt_id, "15", env["fiscal_year_name"])
    assert is_error(result), result
    assert result["message"] == "Fiscal year '%s' is closed" % env["fiscal_year_name"]
    assert _counts(conn) == before


def test_allocation_unknown_year_message_unchanged(conn, env):
    emp_id = _add_employee(conn, env["company_id"], "UnknownEmp")
    lt_id = _create_leave_type(conn)
    before = _counts(conn)
    result = _allocate(conn, emp_id, lt_id, "15", "FY-NOPE")
    assert is_error(result), result
    assert result["message"] == "Fiscal year 'FY-NOPE' not found"
    assert _counts(conn) == before


def test_allocation_in_own_open_year_still_works(conn):
    _b, env, emp_id, lt_id = _two_companies(conn)
    result = _allocate(conn, emp_id, lt_id, "15", env["fiscal_year_name"])
    assert is_ok(result), result
    assert result["fiscal_year"] == env["fiscal_year_name"]
    assert result["total_leaves"] == "15"
    assert result["used_leaves"] == "0"
    assert result["remaining_leaves"] == "15"
    rows = _allocations_for(conn, emp_id)
    assert len(rows) == 1
    assert rows[0]["fiscal_year"] == env["fiscal_year_name"]
    assert rows[0]["total_leaves"] == "15"


def test_other_company_employee_uses_its_own_year(conn):
    b, env, _emp_a, _lt_a = _two_companies(conn)
    emp_b = _add_employee(conn, b, "OtherEmp")
    lt_b = _create_leave_type(conn, name="Other Leave")
    result = _allocate(conn, emp_b, lt_b, "15", "FY-OTHER")
    assert is_ok(result), result
    assert result["fiscal_year"] == "FY-OTHER"
    refused = _allocate(conn, emp_b, lt_b, "15", env["fiscal_year_name"])
    assert is_error(refused), refused
    assert refused["message"] == "Fiscal year '%s' belongs to another company" % env["fiscal_year_name"]
