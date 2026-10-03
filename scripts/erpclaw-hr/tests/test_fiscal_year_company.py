"""HR uses the employee/claim company's own fiscal year.

Multi-company regression tests: with a decoy company's year inserted first
(and starting earlier), the old company-blind lookup returned the decoy
year. Every lookup below must use the acting company's own year.

SQLite only: hr test helpers are SQLite-only (hr_helpers.get_conn).

The balance/detail/summary tests use dates relative to today with wide
margins (decoy year covers today +/- 400 days, own year today -30/+330
days) so a date rollover between runs cannot flip the result.
"""
import json
import uuid
from datetime import date, timedelta

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


def _create_employee(conn, env, first_name="TestEmp"):
    return _add_employee(conn, env["company_id"], first_name)


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


def _create_allocation(conn, employee_id, leave_type_id, total_leaves, fy_name):
    result = call_action(mod.add_leave_allocation, conn, ns(
        employee_id=employee_id,
        leave_type_id=leave_type_id,
        total_leaves=total_leaves,
        fiscal_year=fy_name,
    ))
    assert is_ok(result), result
    return result["allocation_id"]


def _claim(conn, company_id, employee_id, expense_account, expense_date):
    items = json.dumps([{
        "expense_type": "travel",
        "description": "Trip",
        "amount": "150.00",
        "account_id": expense_account,
    }])
    result = call_action(mod.add_expense_claim, conn, ns(
        employee_id=employee_id,
        expense_date=expense_date,
        company_id=company_id,
        items=items,
    ))
    assert is_ok(result), result
    return result["expense_claim_id"]


def _submit(conn, claim_id):
    result = call_action(mod.submit_expense_claim, conn, ns(
        expense_claim_id=claim_id,
    ))
    assert is_ok(result), result
    return result


def _approve_claim(conn, claim_id, approver_id):
    return call_action(mod.approve_expense_claim, conn, ns(
        expense_claim_id=claim_id,
        approved_by=approver_id,
    ))


def _claim_fiscal_years(conn, claim_id):
    return [r["fiscal_year"] for r in conn.execute(
        "SELECT fiscal_year FROM gl_entry WHERE voucher_id = ?",
        (claim_id,)).fetchall()]


def _fixed_year_setup(conn):
    """Decoy year first (starts earlier), then company A with its own year."""
    decoy = seed_company(conn, name="Decoy Co", abbr="DC")
    seed_fiscal_year(conn, decoy, name="FY-DECOY",
                     start="2025-07-01", end="2026-12-31")
    env = build_hr_env(conn)
    conn.execute("UPDATE fiscal_year SET name = 'FY-A-2026' WHERE company_id = ?",
                 (env["company_id"],))
    conn.commit()
    return decoy, env


def _relative_year_setup(conn):
    """Same as above but with years relative to today (wide margins)."""
    today = date.today()
    decoy = seed_company(conn, name="Decoy Co", abbr="DC")
    seed_fiscal_year(
        conn, decoy, name="FY-DECOY",
        start=(today - timedelta(days=400)).isoformat(),
        end=(today + timedelta(days=400)).isoformat())
    env = build_hr_env(conn)
    conn.execute(
        "UPDATE fiscal_year SET name = 'FY-A-2026', start_date = ?, end_date = ?"
        " WHERE company_id = ?",
        ((today - timedelta(days=30)).isoformat(),
         (today + timedelta(days=330)).isoformat(),
         env["company_id"]))
    conn.commit()
    return decoy, env


def test_expense_claim_approval_stamps_own_company_year(conn):
    _decoy, env = _fixed_year_setup(conn)
    emp_id = _create_employee(conn, env, "ClaimEmp")
    approver_id = _create_employee(conn, env, "Approver")
    claim_id = _claim(conn, env["company_id"], emp_id,
                      env["expense_account"], "2026-03-15")
    _submit(conn, claim_id)
    result = _approve_claim(conn, claim_id, approver_id)
    assert is_ok(result), result
    years = _claim_fiscal_years(conn, claim_id)
    assert years, "expected GL legs for the approved claim"
    assert all(y == "FY-A-2026" for y in years), years


def test_leave_application_uses_own_company_allocation(conn):
    _decoy, env = _fixed_year_setup(conn)
    emp_id = _create_employee(conn, env, "LeaveEmp")
    approver_id = _create_employee(conn, env, "Approver")
    lt_id = _create_leave_type(conn)
    _create_allocation(conn, emp_id, lt_id, "10", "FY-A-2026")
    app = call_action(mod.add_leave_application, conn, ns(
        employee_id=emp_id,
        leave_type_id=lt_id,
        from_date="2026-03-16",
        to_date="2026-03-17",
        half_day=None,
        half_day_date=None,
        reason=None,
    ))
    assert is_ok(app), app
    assert app["total_days"] == "2"
    result = call_action(mod.approve_leave, conn, ns(
        leave_application_id=app["leave_application_id"],
        approved_by=approver_id,
    ))
    assert is_ok(result), result
    alloc = conn.execute(
        "SELECT remaining_leaves FROM leave_allocation"
        " WHERE employee_id = ? AND leave_type_id = ? AND fiscal_year = ?",
        (emp_id, lt_id, "FY-A-2026")).fetchone()
    assert alloc["remaining_leaves"] == "8"


def test_leave_balance_defaults_to_own_company_year(conn):
    _decoy, env = _relative_year_setup(conn)
    emp_id = _create_employee(conn, env, "BalEmp")
    lt_id = _create_leave_type(conn, "Balance Leave", "20")
    _create_allocation(conn, emp_id, lt_id, "12", "FY-A-2026")
    result = call_action(mod.get_leave_balance, conn, ns(
        employee_id=emp_id,
        leave_type_id=None,
        fiscal_year=None,
    ))
    assert is_ok(result), result
    assert result["fiscal_year"] == "FY-A-2026"
    assert len(result["balances"]) == 1
    assert result["balances"][0]["remaining_leaves"] == "12"


def test_employee_detail_shows_own_company_leave(conn):
    _decoy, env = _relative_year_setup(conn)
    emp_id = _create_employee(conn, env, "DetailEmp")
    lt_id = _create_leave_type(conn, "Detail Leave", "20")
    _create_allocation(conn, emp_id, lt_id, "12", "FY-A-2026")
    result = call_action(mod.get_employee, conn, ns(employee_id=emp_id))
    assert is_ok(result), result
    balances = result["employee"]["leave_balances"]
    assert len(balances) == 1
    assert balances[0]["fiscal_year"] == "FY-A-2026"
    assert balances[0]["remaining_leaves"] == "12"


def test_status_leave_summary_uses_own_company_year_start(conn):
    _decoy, env = _relative_year_setup(conn)
    emp_id = _create_employee(conn, env, "StatusEmp")
    lt_id = _create_leave_type(conn, "Status Leave", "20")
    old_day = (date.today() - timedelta(days=100)).isoformat()
    conn.execute(
        """INSERT INTO leave_application
           (id, naming_series, employee_id, leave_type_id, from_date, to_date,
            total_days, half_day, half_day_date, reason, status,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (str(uuid.uuid4()), "LA-1", emp_id, lt_id, old_day, old_day,
         "1", 0, None, None, "draft",
         "2026-01-01 00:00:00", "2026-01-01 00:00:00"))
    conn.commit()
    result = call_action(mod.status_action, conn, ns(
        company_id=env["company_id"],
    ))
    assert is_ok(result), result
    assert result["leave_summary"].get("draft", 0) == 0


def test_expense_claim_without_own_company_year_is_refused(conn):
    decoy = seed_company(conn, name="Decoy Co", abbr="DC")
    seed_fiscal_year(conn, decoy, name="FY-DECOY",
                     start="2025-07-01", end="2026-12-31")
    env = build_hr_env(conn)
    conn.execute("DELETE FROM fiscal_year WHERE company_id = ?",
                 (env["company_id"],))
    conn.commit()
    emp_id = _create_employee(conn, env, "ClaimEmp")
    approver_id = _create_employee(conn, env, "Approver")
    claim_id = _claim(conn, env["company_id"], emp_id,
                      env["expense_account"], "2026-03-15")
    _submit(conn, claim_id)
    result = _approve_claim(conn, claim_id, approver_id)
    assert is_error(result), result
    assert result["message"].endswith(
        "No open fiscal year found for posting date 2026-03-15"), result
    assert conn.execute(
        "SELECT id FROM gl_entry WHERE voucher_id = ?",
        (claim_id,)).fetchall() == []
    assert conn.execute(
        "SELECT status FROM expense_claim WHERE id = ?",
        (claim_id,)).fetchone()["status"] == "submitted"
