"""Tests for salary structure actions."""
import json
from decimal import Decimal
from helpers import _call_action, setup_payroll_environment
from db_query import ACTIONS


def test_add_salary_structure(fresh_db):
    """Create a salary structure with components."""
    env = setup_payroll_environment(fresh_db)

    # Create components first
    basic = _call_action(ACTIONS["add-salary-component"], fresh_db,
                         name="Basic Salary", component_type="earning")
    hra = _call_action(ACTIONS["add-salary-component"], fresh_db,
                       name="HRA", component_type="earning")

    components = json.dumps([
        {"salary_component_id": basic["salary_component_id"], "amount": "5000"},
        {"salary_component_id": hra["salary_component_id"], "percentage": "40",
         "base_component_id": basic["salary_component_id"]},
    ])

    result = _call_action(ACTIONS["add-salary-structure"], fresh_db,
                          name="Standard Monthly",
                          company_id=env["company_id"],
                          components=components)
    assert result["status"] == "ok"
    assert result["component_count"] == 2


def test_get_salary_structure_with_details(fresh_db):
    """Get a structure with its component breakdown."""
    env = setup_payroll_environment(fresh_db)
    basic = _call_action(ACTIONS["add-salary-component"], fresh_db,
                         name="Basic Salary", component_type="earning")

    components = json.dumps([
        {"salary_component_id": basic["salary_component_id"], "amount": "8000"},
    ])
    struct = _call_action(ACTIONS["add-salary-structure"], fresh_db,
                          name="Executive", company_id=env["company_id"],
                          components=components)

    result = _call_action(ACTIONS["get-salary-structure"], fresh_db,
                          salary_structure_id=struct["salary_structure_id"])
    assert result["status"] == "ok"
    ss = result["salary_structure"]
    assert ss["name"] == "Executive"
    assert len(ss["components"]) == 1
    assert ss["components"][0]["amount"] == "8000"


def test_list_salary_structures(fresh_db):
    """List structures for a company."""
    env = setup_payroll_environment(fresh_db)
    basic = _call_action(ACTIONS["add-salary-component"], fresh_db,
                         name="Basic Salary", component_type="earning")

    for name in ["Standard", "Executive"]:
        components = json.dumps([
            {"salary_component_id": basic["salary_component_id"], "amount": "5000"},
        ])
        _call_action(ACTIONS["add-salary-structure"], fresh_db,
                     name=name, company_id=env["company_id"],
                     components=components)

    result = _call_action(ACTIONS["list-salary-structures"], fresh_db,
                          company_id=env["company_id"])
    assert result["status"] == "ok"
    assert result["count"] == 2


def test_add_structure_invalid_component(fresh_db):
    """Error when referencing non-existent component."""
    env = setup_payroll_environment(fresh_db)
    components = json.dumps([
        {"salary_component_id": "non-existent-id", "amount": "5000"},
    ])
    result = _call_action(ACTIONS["add-salary-structure"], fresh_db,
                          name="Bad Structure", company_id=env["company_id"],
                          components=components)
    assert result["status"] == "error"
