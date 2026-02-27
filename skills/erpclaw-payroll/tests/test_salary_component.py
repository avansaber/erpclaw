"""Tests for salary component actions."""
from decimal import Decimal
from helpers import _call_action, setup_payroll_environment
from db_query import ACTIONS


def test_add_earning_component(fresh_db):
    """Add a basic earning component."""
    result = _call_action(ACTIONS["add-salary-component"], fresh_db,
                          name="Basic Salary", component_type="earning")
    assert result["status"] == "ok"
    assert result["name"] == "Basic Salary"
    assert "salary_component_id" in result


def test_add_deduction_component(fresh_db):
    """Add a statutory deduction component."""
    result = _call_action(ACTIONS["add-salary-component"], fresh_db,
                          name="Federal Income Tax", component_type="deduction",
                          is_statutory="1", is_tax_applicable="0")
    assert result["status"] == "ok"
    assert result["name"] == "Federal Income Tax"


def test_list_components_with_filter(fresh_db):
    """List components filtered by type."""
    _call_action(ACTIONS["add-salary-component"], fresh_db,
                 name="Basic Salary", component_type="earning")
    _call_action(ACTIONS["add-salary-component"], fresh_db,
                 name="Federal Income Tax", component_type="deduction")
    _call_action(ACTIONS["add-salary-component"], fresh_db,
                 name="HRA", component_type="earning")

    result = _call_action(ACTIONS["list-salary-components"], fresh_db,
                          component_type="earning")
    assert result["status"] == "ok"
    assert result["count"] == 2
    names = [c["name"] for c in result["components"]]
    assert "Basic Salary" in names
    assert "HRA" in names
