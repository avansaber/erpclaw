"""Status leave summary counts from each employee's own company year.

SQLite only: hr test helpers are SQLite-only (hr_helpers.get_conn).

Dates are relative to today with wide margins (decoy year covers today
+/- 400 days, own year today -30/+330 days) so a date rollover between runs
cannot flip the result.
"""
import uuid
from datetime import date, timedelta

from hr_helpers import (
    build_hr_env,
    call_action,
    is_ok,
    load_db_query,
    ns,
    seed_company,
    seed_fiscal_year,
)

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


def _insert_draft(conn, employee_id, leave_type_id, from_date):
    conn.execute(
        """INSERT INTO leave_application
           (id, naming_series, employee_id, leave_type_id, from_date, to_date,
            total_days, half_day, half_day_date, reason, status,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (str(uuid.uuid4()), "LA-1", employee_id, leave_type_id,
         from_date, from_date,
         "1", 0, None, None, "draft",
         "2026-01-01 00:00:00", "2026-01-01 00:00:00"))
    conn.commit()


def _company_year_setup(conn):
    t = date.today()
    decoy = seed_company(conn, name="Decoy Co", abbr="DC")
    seed_fiscal_year(
        conn, decoy, name="FY-DECOY",
        start=(t - timedelta(days=400)).isoformat(),
        end=(t + timedelta(days=400)).isoformat())
    env = build_hr_env(conn)
    conn.execute(
        "UPDATE fiscal_year SET name = 'FY-A', start_date = ?, end_date = ?"
        " WHERE company_id = ?",
        ((t - timedelta(days=30)).isoformat(),
         (t + timedelta(days=330)).isoformat(),
         env["company_id"]))
    conn.commit()
    return decoy, env


def test_company_summary_counts_from_own_year_start(conn):
    _decoy, env = _company_year_setup(conn)
    t = date.today()
    emp_id = _add_employee(conn, env["company_id"], "EmpA")
    lt_id = _create_leave_type(conn)
    _insert_draft(conn, emp_id, lt_id, (t - timedelta(days=100)).isoformat())
    _insert_draft(conn, emp_id, lt_id, (t - timedelta(days=10)).isoformat())
    result = call_action(mod.status_action, conn, ns(
        company_id=env["company_id"],
    ))
    assert is_ok(result), result
    assert result["leave_summary"] == {"draft": 1}
    assert result["fiscal_year"] == "FY-A"


def test_all_company_summary_uses_each_employee_company_year(conn):
    decoy, env = _company_year_setup(conn)
    t = date.today()
    emp_a = _add_employee(conn, env["company_id"], "EmpA")
    lt_id = _create_leave_type(conn)
    _insert_draft(conn, emp_a, lt_id, (t - timedelta(days=100)).isoformat())
    _insert_draft(conn, emp_a, lt_id, (t - timedelta(days=10)).isoformat())
    emp_b = _add_employee(conn, decoy, "EmpB")
    _insert_draft(conn, emp_b, lt_id, (t - timedelta(days=100)).isoformat())
    result = call_action(mod.status_action, conn, ns(
        company_id=None,
    ))
    assert is_ok(result), result
    assert result["leave_summary"] == {"draft": 2}
    assert result["fiscal_year"] is None


def test_employee_without_covering_year_is_not_counted(conn):
    _decoy, env = _company_year_setup(conn)
    t = date.today()
    lt_id = _create_leave_type(conn)
    company_c = seed_company(conn, name="C Co", abbr="CC")
    emp_c = _add_employee(conn, company_c, "EmpC")
    _insert_draft(conn, emp_c, lt_id, (t - timedelta(days=10)).isoformat())
    scoped = call_action(mod.status_action, conn, ns(
        company_id=company_c,
    ))
    assert is_ok(scoped), scoped
    assert scoped["leave_summary"] == {}
    assert scoped["fiscal_year"] is None
    unscoped = call_action(mod.status_action, conn, ns(
        company_id=None,
    ))
    assert is_ok(unscoped), unscoped
    assert unscoped["leave_summary"] == {}
    assert unscoped["fiscal_year"] is None
