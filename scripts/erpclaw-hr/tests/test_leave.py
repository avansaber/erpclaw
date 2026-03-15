"""Tests for erpclaw-hr leave management actions.

Actions tested:
  - add-leave-type
  - list-leave-types
  - add-leave-allocation
  - get-leave-balance
  - add-leave-application
  - approve-leave
  - reject-leave
  - list-leave-applications
"""
import json
import pytest
from decimal import Decimal
from hr_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
)

mod = load_db_query()


def _add_employee_ns(**overrides):
    """Build a full namespace for add-employee with sensible defaults."""
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
        federal_filing_status=None,
        w4_allowances=None,
        holiday_list_id=None,
        payroll_cost_center_id=None,
    )
    defaults.update(overrides)
    return ns(**defaults)


def _create_employee(conn, env, first_name="TestEmp"):
    """Create an employee and return its ID."""
    result = call_action(mod.add_employee, conn, _add_employee_ns(
        first_name=first_name,
        company_id=env["company_id"],
    ))
    assert is_ok(result)
    return result["employee_id"]


def _create_leave_type(conn, name="Annual Leave", max_days="20"):
    """Create a leave type and return its ID."""
    result = call_action(mod.add_leave_type, conn, ns(
        name=name,
        max_days_allowed=max_days,
        is_paid_leave=None,
        is_carry_forward=None,
        max_carry_forward_days=None,
        is_compensatory=None,
        applicable_after_days=None,
    ))
    assert is_ok(result)
    return result["leave_type_id"]


def _create_allocation(conn, employee_id, leave_type_id, total_leaves, fy_name):
    """Create a leave allocation and return its ID."""
    result = call_action(mod.add_leave_allocation, conn, ns(
        employee_id=employee_id,
        leave_type_id=leave_type_id,
        total_leaves=total_leaves,
        fiscal_year=fy_name,
    ))
    assert is_ok(result)
    return result["allocation_id"]


class TestAddLeaveType:
    def test_basic_create(self, conn, env):
        result = call_action(mod.add_leave_type, conn, ns(
            name="Sick Leave",
            max_days_allowed="10",
            is_paid_leave=None,
            is_carry_forward=None,
            max_carry_forward_days=None,
            is_compensatory=None,
            applicable_after_days=None,
        ))
        assert is_ok(result)
        assert result["name"] == "Sick Leave"
        assert "leave_type_id" in result
        assert Decimal(result["max_days_allowed"]) == Decimal("10")

    def test_missing_name_fails(self, conn, env):
        result = call_action(mod.add_leave_type, conn, ns(
            name=None,
            max_days_allowed="10",
            is_paid_leave=None,
            is_carry_forward=None,
            max_carry_forward_days=None,
            is_compensatory=None,
            applicable_after_days=None,
        ))
        assert is_error(result)


class TestListLeaveTypes:
    def test_list(self, conn, env):
        _create_leave_type(conn, "PTO Type A", "15")
        _create_leave_type(conn, "PTO Type B", "10")

        result = call_action(mod.list_leave_types, conn, ns(
            limit=None,
            offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 2
        assert len(result["leave_types"]) >= 2


class TestAddLeaveAllocation:
    def test_basic_create(self, conn, env):
        emp_id = _create_employee(conn, env, "AllocEmp")
        lt_id = _create_leave_type(conn, "Alloc Leave", "20")

        result = call_action(mod.add_leave_allocation, conn, ns(
            employee_id=emp_id,
            leave_type_id=lt_id,
            total_leaves="15",
            fiscal_year=env["fiscal_year_name"],
        ))
        assert is_ok(result)
        assert result["total_leaves"] == "15"
        assert result["used_leaves"] == "0"
        assert result["remaining_leaves"] == "15"
        assert "allocation_id" in result


class TestGetLeaveBalance:
    def test_balance(self, conn, env):
        emp_id = _create_employee(conn, env, "BalEmp")
        lt_id = _create_leave_type(conn, "Balance Leave", "20")
        _create_allocation(conn, emp_id, lt_id, "18", env["fiscal_year_name"])

        result = call_action(mod.get_leave_balance, conn, ns(
            employee_id=emp_id,
            leave_type_id=None,
            fiscal_year=env["fiscal_year_name"],
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1
        bal = result["balances"][0]
        assert Decimal(bal["total_leaves"]) == Decimal("18")
        assert Decimal(bal["remaining_leaves"]) == Decimal("18")


class TestAddLeaveApplication:
    def test_basic_create(self, conn, env):
        emp_id = _create_employee(conn, env, "LeaveAppEmp")
        lt_id = _create_leave_type(conn, "App Leave", "20")
        _create_allocation(conn, emp_id, lt_id, "20", env["fiscal_year_name"])

        result = call_action(mod.add_leave_application, conn, ns(
            employee_id=emp_id,
            leave_type_id=lt_id,
            from_date="2026-03-02",
            to_date="2026-03-06",
            half_day=None,
            half_day_date=None,
            reason=None,
        ))
        assert is_ok(result)
        assert "leave_application_id" in result
        # 2026-03-02 (Mon) to 2026-03-06 (Fri) = 5 business days
        assert Decimal(result["total_days"]) == Decimal("5")


class TestApproveLeave:
    def test_approve(self, conn, env):
        emp_id = _create_employee(conn, env, "ApprEmp")
        approver_id = _create_employee(conn, env, "Approver")
        lt_id = _create_leave_type(conn, "Appr Leave", "20")
        _create_allocation(conn, emp_id, lt_id, "20", env["fiscal_year_name"])

        app = call_action(mod.add_leave_application, conn, ns(
            employee_id=emp_id,
            leave_type_id=lt_id,
            from_date="2026-04-06",
            to_date="2026-04-10",
            half_day=None,
            half_day_date=None,
            reason=None,
        ))
        assert is_ok(app)

        result = call_action(mod.approve_leave, conn, ns(
            leave_application_id=app["leave_application_id"],
            approved_by=approver_id,
        ))
        assert is_ok(result)

        # Verify the leave application status in DB
        row = conn.execute(
            "SELECT status FROM leave_application WHERE id=?",
            (app["leave_application_id"],)
        ).fetchone()
        assert row["status"] == "approved"

        # Verify leave balance was deducted
        alloc = conn.execute(
            """SELECT used_leaves, remaining_leaves FROM leave_allocation
               WHERE employee_id=? AND leave_type_id=? AND fiscal_year=?""",
            (emp_id, lt_id, env["fiscal_year_name"])
        ).fetchone()
        assert Decimal(alloc["used_leaves"]) == Decimal("5")
        assert Decimal(alloc["remaining_leaves"]) == Decimal("15")


class TestRejectLeave:
    def test_reject(self, conn, env):
        emp_id = _create_employee(conn, env, "RejectEmp")
        lt_id = _create_leave_type(conn, "Reject Leave", "20")
        _create_allocation(conn, emp_id, lt_id, "20", env["fiscal_year_name"])

        app = call_action(mod.add_leave_application, conn, ns(
            employee_id=emp_id,
            leave_type_id=lt_id,
            from_date="2026-05-04",
            to_date="2026-05-08",
            half_day=None,
            half_day_date=None,
            reason=None,
        ))
        assert is_ok(app)

        result = call_action(mod.reject_leave, conn, ns(
            leave_application_id=app["leave_application_id"],
            reason="Not enough coverage",
        ))
        assert is_ok(result)

        # Verify the status in DB
        row = conn.execute(
            "SELECT status FROM leave_application WHERE id=?",
            (app["leave_application_id"],)
        ).fetchone()
        assert row["status"] == "rejected"

        # Verify leave balance was NOT deducted (reject does not touch allocation)
        alloc = conn.execute(
            """SELECT used_leaves, remaining_leaves FROM leave_allocation
               WHERE employee_id=? AND leave_type_id=? AND fiscal_year=?""",
            (emp_id, lt_id, env["fiscal_year_name"])
        ).fetchone()
        assert Decimal(alloc["used_leaves"]) == Decimal("0")
        assert Decimal(alloc["remaining_leaves"]) == Decimal("20")


class TestListLeaveApplications:
    def test_list(self, conn, env):
        emp_id = _create_employee(conn, env, "ListLeaveEmp")
        lt_id = _create_leave_type(conn, "List Leave", "20")
        _create_allocation(conn, emp_id, lt_id, "20", env["fiscal_year_name"])

        call_action(mod.add_leave_application, conn, ns(
            employee_id=emp_id,
            leave_type_id=lt_id,
            from_date="2026-06-01",
            to_date="2026-06-05",
            half_day=None,
            half_day_date=None,
            reason=None,
        ))

        result = call_action(mod.list_leave_applications, conn, ns(
            employee_id=emp_id,
            status=None,
            from_date=None,
            to_date=None,
            leave_type_id=None,
            limit=None,
            offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1
        assert len(result["leave_applications"]) >= 1
