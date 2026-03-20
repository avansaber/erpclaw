"""Tests for erpclaw-hr shift management actions (Sprint 6, Feature #20).

Actions tested:
  - add-shift-type
  - list-shift-types
  - update-shift-type
  - assign-shift
  - list-shift-assignments
"""
import json
import pytest
from datetime import date, timedelta
from decimal import Decimal
from hr_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
)

mod = load_db_query()


def _add_employee(conn, env, first_name="ShiftEmp"):
    """Create an employee and return its ID."""
    defaults = dict(
        first_name=first_name,
        last_name=None,
        date_of_birth=None,
        gender=None,
        date_of_joining="2025-01-01",
        employment_type=None,
        company_id=env["company_id"],
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
    result = call_action(mod.add_employee, conn, ns(**defaults))
    assert is_ok(result)
    return result["employee_id"]


# ==============================================================================
# add-shift-type
# ==============================================================================


class TestAddShiftType:
    def test_add_basic_shift_type(self, conn, env):
        """Create a morning shift type successfully."""
        result = call_action(mod.add_shift_type, conn, ns(
            name="Morning Shift",
            start_time="06:00",
            end_time="14:00",
            company_id=env["company_id"],
            status=None,
            limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["name"] == "Morning Shift"
        assert result["start_time"] == "06:00"
        assert result["end_time"] == "14:00"
        assert result["shift_status"] == "active"

    def test_add_shift_type_duplicate_name(self, conn, env):
        """Duplicate shift type name should fail."""
        call_action(mod.add_shift_type, conn, ns(
            name="Evening Shift", start_time="14:00", end_time="22:00",
            company_id=env["company_id"], status=None,
            limit=None, offset=None,
        ))
        result = call_action(mod.add_shift_type, conn, ns(
            name="Evening Shift", start_time="15:00", end_time="23:00",
            company_id=env["company_id"], status=None,
            limit=None, offset=None,
        ))
        assert is_error(result)

    def test_add_shift_type_invalid_time_format(self, conn, env):
        """Invalid time format should fail."""
        result = call_action(mod.add_shift_type, conn, ns(
            name="Bad Shift", start_time="6am", end_time="2pm",
            company_id=env["company_id"], status=None,
            limit=None, offset=None,
        ))
        assert is_error(result)

    def test_add_shift_type_missing_fields(self, conn, env):
        """Missing required fields should fail."""
        result = call_action(mod.add_shift_type, conn, ns(
            name=None, start_time="06:00", end_time="14:00",
            company_id=env["company_id"], status=None,
            limit=None, offset=None,
        ))
        assert is_error(result)

    def test_add_shift_type_with_seconds(self, conn, env):
        """Support HH:MM:SS time format."""
        result = call_action(mod.add_shift_type, conn, ns(
            name="Night Shift", start_time="22:00:00", end_time="06:00:00",
            company_id=env["company_id"], status=None,
            limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["start_time"] == "22:00:00"


# ==============================================================================
# list-shift-types
# ==============================================================================


class TestListShiftTypes:
    def test_list_shift_types(self, conn, env):
        """List shift types returns created types."""
        call_action(mod.add_shift_type, conn, ns(
            name="Day Shift", start_time="08:00", end_time="16:00",
            company_id=env["company_id"], status=None,
            limit=None, offset=None,
        ))
        result = call_action(mod.list_shift_types, conn, ns(
            company_id=env["company_id"], status=None,
            limit="20", offset="0",
        ))
        assert is_ok(result)
        assert result["count"] >= 1
        names = [s["name"] for s in result["shift_types"]]
        assert "Day Shift" in names


# ==============================================================================
# update-shift-type
# ==============================================================================


class TestUpdateShiftType:
    def test_update_shift_type(self, conn, env):
        """Update a shift type's times and status."""
        created = call_action(mod.add_shift_type, conn, ns(
            name="Update Test", start_time="08:00", end_time="16:00",
            company_id=env["company_id"], status=None,
            limit=None, offset=None,
        ))
        assert is_ok(created)
        shift_id = created["shift_type_id"]

        result = call_action(mod.update_shift_type, conn, ns(
            shift_type_id=shift_id,
            name="Updated Shift",
            start_time="09:00",
            end_time="17:00",
            status="inactive",
            limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["name"] == "Updated Shift"
        assert result["start_time"] == "09:00"
        assert result["end_time"] == "17:00"
        assert result["shift_status"] == "inactive"

    def test_update_nonexistent_shift_type(self, conn, env):
        """Updating a nonexistent shift type should fail."""
        result = call_action(mod.update_shift_type, conn, ns(
            shift_type_id="nonexistent-id",
            name="foo", start_time=None, end_time=None, status=None,
            limit=None, offset=None,
        ))
        assert is_error(result)


# ==============================================================================
# assign-shift
# ==============================================================================


class TestAssignShift:
    def test_assign_shift_to_employee(self, conn, env):
        """Assign a shift type to an employee."""
        emp_id = _add_employee(conn, env, "AssignEmp")
        shift = call_action(mod.add_shift_type, conn, ns(
            name="Assign Test Shift", start_time="08:00", end_time="16:00",
            company_id=env["company_id"], status=None,
            limit=None, offset=None,
        ))
        assert is_ok(shift)

        result = call_action(mod.assign_shift, conn, ns(
            employee_id=emp_id,
            shift_type_id=shift["shift_type_id"],
            start_date="2026-01-01",
            end_date="2026-12-31",
            status=None,
            limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["employee_id"] == emp_id
        assert result["shift_type_id"] == shift["shift_type_id"]
        assert result["start_date"] == "2026-01-01"
        assert result["end_date"] == "2026-12-31"

    def test_assign_shift_end_before_start(self, conn, env):
        """End date before start date should fail."""
        emp_id = _add_employee(conn, env, "BadDates")
        shift = call_action(mod.add_shift_type, conn, ns(
            name="BadDate Shift", start_time="08:00", end_time="16:00",
            company_id=env["company_id"], status=None,
            limit=None, offset=None,
        ))
        assert is_ok(shift)

        result = call_action(mod.assign_shift, conn, ns(
            employee_id=emp_id,
            shift_type_id=shift["shift_type_id"],
            start_date="2026-06-01",
            end_date="2026-01-01",
            status=None,
            limit=None, offset=None,
        ))
        assert is_error(result)

    def test_assign_shift_invalid_employee(self, conn, env):
        """Assigning to nonexistent employee should fail."""
        shift = call_action(mod.add_shift_type, conn, ns(
            name="Ghost Shift", start_time="08:00", end_time="16:00",
            company_id=env["company_id"], status=None,
            limit=None, offset=None,
        ))
        assert is_ok(shift)

        result = call_action(mod.assign_shift, conn, ns(
            employee_id="nonexistent-emp",
            shift_type_id=shift["shift_type_id"],
            start_date="2026-01-01",
            end_date=None,
            status=None,
            limit=None, offset=None,
        ))
        assert is_error(result)


# ==============================================================================
# list-shift-assignments
# ==============================================================================


class TestListShiftAssignments:
    def test_list_assignments_by_employee(self, conn, env):
        """List shift assignments filtered by employee."""
        emp_id = _add_employee(conn, env, "ListAsgnEmp")
        shift = call_action(mod.add_shift_type, conn, ns(
            name="ListAsgn Shift", start_time="08:00", end_time="16:00",
            company_id=env["company_id"], status=None,
            limit=None, offset=None,
        ))
        assert is_ok(shift)

        call_action(mod.assign_shift, conn, ns(
            employee_id=emp_id,
            shift_type_id=shift["shift_type_id"],
            start_date="2026-01-01",
            end_date=None,
            status=None,
            limit=None, offset=None,
        ))

        result = call_action(mod.list_shift_assignments, conn, ns(
            employee_id=emp_id,
            shift_type_id=None,
            company_id=None,
            status=None,
            limit="20", offset="0",
        ))
        assert is_ok(result)
        assert result["count"] >= 1
        assert result["shift_assignments"][0]["employee_id"] == emp_id
        assert result["shift_assignments"][0]["shift_name"] == "ListAsgn Shift"
