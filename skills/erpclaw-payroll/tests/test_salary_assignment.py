"""Tests for salary assignment actions."""
import json
from helpers import _call_action, setup_payroll_environment
from db_query import ACTIONS


def _create_structure(fresh_db, env):
    """Helper to create a salary structure for assignment tests."""
    basic = _call_action(ACTIONS["add-salary-component"], fresh_db,
                         name="Basic Salary", component_type="earning")
    components = json.dumps([
        {"salary_component_id": basic["salary_component_id"], "amount": "5000"},
    ])
    struct = _call_action(ACTIONS["add-salary-structure"], fresh_db,
                          name="Standard", company_id=env["company_id"],
                          components=components)
    return struct["salary_structure_id"]


def test_add_salary_assignment(fresh_db):
    """Assign a salary structure to an employee."""
    env = setup_payroll_environment(fresh_db)
    struct_id = _create_structure(fresh_db, env)

    result = _call_action(ACTIONS["add-salary-assignment"], fresh_db,
                          employee_id=env["employee_ids"][0],
                          salary_structure_id=struct_id,
                          base_amount="5000",
                          effective_from="2026-01-01",
                          company_id=env["company_id"])
    assert result["status"] == "ok"
    assert "salary_assignment_id" in result


def test_list_salary_assignments(fresh_db):
    """List assignments for an employee."""
    env = setup_payroll_environment(fresh_db)
    struct_id = _create_structure(fresh_db, env)

    _call_action(ACTIONS["add-salary-assignment"], fresh_db,
                 employee_id=env["employee_ids"][0],
                 salary_structure_id=struct_id,
                 base_amount="5000",
                 effective_from="2026-01-01",
                 company_id=env["company_id"])

    result = _call_action(ACTIONS["list-salary-assignments"], fresh_db,
                          employee_id=env["employee_ids"][0])
    assert result["status"] == "ok"
    assert result["count"] == 1


def test_auto_close_previous_assignment(fresh_db):
    """New assignment auto-closes the previous one."""
    env = setup_payroll_environment(fresh_db)
    struct_id = _create_structure(fresh_db, env)
    emp_id = env["employee_ids"][0]

    # First assignment
    _call_action(ACTIONS["add-salary-assignment"], fresh_db,
                 employee_id=emp_id, salary_structure_id=struct_id,
                 base_amount="5000", effective_from="2026-01-01",
                 company_id=env["company_id"])

    # Second assignment — should auto-close first
    _call_action(ACTIONS["add-salary-assignment"], fresh_db,
                 employee_id=emp_id, salary_structure_id=struct_id,
                 base_amount="6000", effective_from="2026-04-01",
                 company_id=env["company_id"])

    result = _call_action(ACTIONS["list-salary-assignments"], fresh_db,
                          employee_id=emp_id)
    assert result["count"] == 2
    # First assignment should have effective_to set to 2026-03-31
    assignments = sorted(result["assignments"], key=lambda a: a["effective_from"])
    assert assignments[0]["effective_to"] == "2026-03-31"
    assert assignments[1]["effective_to"] is None
