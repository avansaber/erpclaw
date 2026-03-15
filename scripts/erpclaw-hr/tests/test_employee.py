"""Tests for erpclaw-hr employee management actions.

Actions tested:
  - add-employee
  - update-employee
  - get-employee
  - list-employees
"""
import pytest
from hr_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
    seed_company,
)

mod = load_db_query()


# ── All args accessed by add_employee ──
# Required: first_name, date_of_joining, company_id
# Optional: last_name, date_of_birth, gender, employment_type,
#           department_id, designation_id, employee_grade_id,
#           branch, reporting_to, company_email, personal_email,
#           cell_phone, emergency_contact, bank_details,
#           federal_filing_status, w4_allowances,
#           holiday_list_id, payroll_cost_center_id

def _add_employee_ns(**overrides):
    """Build a full namespace for add-employee with sensible defaults."""
    defaults = dict(
        first_name="John",
        last_name=None,
        date_of_birth=None,
        gender=None,
        date_of_joining="2026-01-15",
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


class TestAddEmployee:
    def test_basic_create(self, conn, env):
        result = call_action(mod.add_employee, conn, _add_employee_ns(
            company_id=env["company_id"],
        ))
        assert is_ok(result)
        assert result["full_name"] == "John"
        assert "employee_id" in result
        assert "naming_series" in result

    def test_with_department(self, conn, env):
        # Create a department first
        dept_result = call_action(mod.add_department, conn, ns(
            name="Engineering",
            company_id=env["company_id"],
            parent_id=None,
            cost_center_id=None,
        ))
        assert is_ok(dept_result)

        result = call_action(mod.add_employee, conn, _add_employee_ns(
            first_name="Jane",
            last_name="Doe",
            company_id=env["company_id"],
            department_id=dept_result["department_id"],
        ))
        assert is_ok(result)
        assert result["full_name"] == "Jane Doe"

    def test_missing_name_fails(self, conn, env):
        result = call_action(mod.add_employee, conn, _add_employee_ns(
            first_name=None,
            company_id=env["company_id"],
        ))
        assert is_error(result)

    def test_missing_company_fails(self, conn, env):
        result = call_action(mod.add_employee, conn, _add_employee_ns(
            company_id=None,
        ))
        assert is_error(result)


class TestUpdateEmployee:
    def _create_employee(self, conn, env):
        result = call_action(mod.add_employee, conn, _add_employee_ns(
            company_id=env["company_id"],
        ))
        assert is_ok(result)
        return result["employee_id"]

    def test_update_name(self, conn, env):
        eid = self._create_employee(conn, env)
        result = call_action(mod.update_employee, conn, ns(
            employee_id=eid,
            first_name="Updated",
            last_name="Name",
            date_of_birth=None,
            gender=None,
            date_of_joining=None,
            date_of_exit=None,
            employment_type=None,
            status=None,
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
            w4_additional_withholding=None,
            state_filing_status=None,
            state_withholding_allowances=None,
            employee_401k_rate=None,
            hsa_contribution=None,
            is_exempt_from_fica=None,
            salary_structure_id=None,
            leave_policy_id=None,
            shift_id=None,
            attendance_device_id=None,
            holiday_list_id=None,
            payroll_cost_center_id=None,
        ))
        assert is_ok(result)
        assert "first_name" in result["updated_fields"]
        assert "last_name" in result["updated_fields"]

        row = conn.execute(
            "SELECT full_name FROM employee WHERE id=?", (eid,)
        ).fetchone()
        assert row["full_name"] == "Updated Name"

    def test_no_fields_fails(self, conn, env):
        eid = self._create_employee(conn, env)
        result = call_action(mod.update_employee, conn, ns(
            employee_id=eid,
            first_name=None,
            last_name=None,
            date_of_birth=None,
            gender=None,
            date_of_joining=None,
            date_of_exit=None,
            employment_type=None,
            status=None,
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
            w4_additional_withholding=None,
            state_filing_status=None,
            state_withholding_allowances=None,
            employee_401k_rate=None,
            hsa_contribution=None,
            is_exempt_from_fica=None,
            salary_structure_id=None,
            leave_policy_id=None,
            shift_id=None,
            attendance_device_id=None,
            holiday_list_id=None,
            payroll_cost_center_id=None,
        ))
        assert is_error(result)


class TestGetEmployee:
    def test_get(self, conn, env):
        create = call_action(mod.add_employee, conn, _add_employee_ns(
            first_name="Alice",
            last_name="Smith",
            company_id=env["company_id"],
        ))
        assert is_ok(create)

        result = call_action(mod.get_employee, conn, ns(
            employee_id=create["employee_id"],
        ))
        assert is_ok(result)
        assert result["employee"]["full_name"] == "Alice Smith"
        assert "leave_balances" in result["employee"]
        assert "attendance_summary" in result["employee"]

    def test_get_nonexistent_fails(self, conn, env):
        result = call_action(mod.get_employee, conn, ns(
            employee_id="fake-id-does-not-exist",
        ))
        assert is_error(result)


class TestListEmployees:
    def test_list(self, conn, env):
        # Create two employees
        call_action(mod.add_employee, conn, _add_employee_ns(
            first_name="Bob", company_id=env["company_id"],
        ))
        call_action(mod.add_employee, conn, _add_employee_ns(
            first_name="Carol", company_id=env["company_id"],
        ))

        result = call_action(mod.list_employees, conn, ns(
            company_id=env["company_id"],
            department_id=None,
            designation_id=None,
            status=None,
            employment_type=None,
            search=None,
            limit=None,
            offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 2
        assert len(result["employees"]) >= 2

    def test_list_search(self, conn, env):
        call_action(mod.add_employee, conn, _add_employee_ns(
            first_name="Searchable", last_name="Person",
            company_id=env["company_id"],
        ))

        result = call_action(mod.list_employees, conn, ns(
            company_id=env["company_id"],
            department_id=None,
            designation_id=None,
            status=None,
            employment_type=None,
            search="Searchable",
            limit=None,
            offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1
        names = [e["full_name"] for e in result["employees"]]
        assert any("Searchable" in n for n in names)
