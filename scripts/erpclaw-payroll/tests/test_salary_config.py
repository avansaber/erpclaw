"""Tests for erpclaw-payroll salary component, structure, and assignment actions.

Actions tested:
  - add-salary-component
  - list-salary-components
  - add-salary-structure
  - get-salary-structure
  - list-salary-structures
  - add-salary-assignment
  - list-salary-assignments
"""
import json
import pytest
from payroll_helpers import (
    call_action, ns, is_error, is_ok,
    seed_company, seed_employee, load_db_query,
)

mod = load_db_query()


# ──────────────────────────────────────────────────────────────────────────────
# add-salary-component
# ──────────────────────────────────────────────────────────────────────────────

class TestAddSalaryComponent:
    def test_basic_earning(self, conn):
        """Create a basic earning component."""
        result = call_action(mod.add_salary_component, conn, ns(
            name="Basic Salary",
            component_type="earning",
            is_tax_applicable=None,
            is_statutory=None,
            is_pre_tax=None,
            variable_based_on_taxable_salary=None,
            depends_on_payment_days=None,
            gl_account_id=None,
            description=None,
        ))
        assert is_ok(result)
        assert result["name"] == "Basic Salary"
        assert result["component_type"] == "earning"
        assert "salary_component_id" in result

        # Verify in DB
        row = conn.execute(
            "SELECT * FROM salary_component WHERE id = ?",
            (result["salary_component_id"],),
        ).fetchone()
        assert row is not None
        assert row["name"] == "Basic Salary"
        assert row["component_type"] == "earning"
        assert row["is_tax_applicable"] == 1  # default

    def test_basic_deduction(self, conn):
        """Create a deduction component."""
        result = call_action(mod.add_salary_component, conn, ns(
            name="Health Insurance",
            component_type="deduction",
            is_tax_applicable="0",
            is_statutory="0",
            is_pre_tax="1",
            variable_based_on_taxable_salary=None,
            depends_on_payment_days=None,
            gl_account_id=None,
            description="Employee health insurance premium",
        ))
        assert is_ok(result)
        assert result["component_type"] == "deduction"

        row = conn.execute(
            "SELECT * FROM salary_component WHERE id = ?",
            (result["salary_component_id"],),
        ).fetchone()
        assert row["is_pre_tax"] == 1
        assert row["is_tax_applicable"] == 0

    def test_missing_name_fails(self, conn):
        """Omitting --name should fail."""
        result = call_action(mod.add_salary_component, conn, ns(
            name=None,
            component_type="earning",
            is_tax_applicable=None,
            is_statutory=None,
            is_pre_tax=None,
            variable_based_on_taxable_salary=None,
            depends_on_payment_days=None,
            gl_account_id=None,
            description=None,
        ))
        assert is_error(result)


# ──────────────────────────────────────────────────────────────────────────────
# list-salary-components
# ──────────────────────────────────────────────────────────────────────────────

class TestListSalaryComponents:
    def test_list(self, conn):
        """List salary components after creating a few."""
        # Create two components
        call_action(mod.add_salary_component, conn, ns(
            name="Base Pay", component_type="earning",
            is_tax_applicable=None, is_statutory=None, is_pre_tax=None,
            variable_based_on_taxable_salary=None, depends_on_payment_days=None,
            gl_account_id=None, description=None,
        ))
        call_action(mod.add_salary_component, conn, ns(
            name="401k Deduction", component_type="deduction",
            is_tax_applicable=None, is_statutory=None, is_pre_tax=None,
            variable_based_on_taxable_salary=None, depends_on_payment_days=None,
            gl_account_id=None, description=None,
        ))

        result = call_action(mod.list_salary_components, conn, ns(
            component_type=None,
            limit=20,
            offset=0,
            search=None,
        ))
        assert is_ok(result)
        assert result["count"] >= 2
        names = [c["name"] for c in result["components"]]
        assert "Base Pay" in names
        assert "401k Deduction" in names


# ──────────────────────────────────────────────────────────────────────────────
# add-salary-structure
# ──────────────────────────────────────────────────────────────────────────────

class TestAddSalaryStructure:
    def test_basic_create(self, conn, env):
        """Create a salary structure with one earning component."""
        # First create a salary component
        comp_result = call_action(mod.add_salary_component, conn, ns(
            name="Monthly Base", component_type="earning",
            is_tax_applicable=None, is_statutory=None, is_pre_tax=None,
            variable_based_on_taxable_salary=None, depends_on_payment_days=None,
            gl_account_id=None, description=None,
        ))
        assert is_ok(comp_result)
        comp_id = comp_result["salary_component_id"]

        components_json = json.dumps([
            {"salary_component_id": comp_id, "amount": "5000"},
        ])

        result = call_action(mod.add_salary_structure, conn, ns(
            name="Standard Monthly",
            company_id=env["company_id"],
            components=components_json,
            payroll_frequency=None,
        ))
        assert is_ok(result)
        assert result["name"] == "Standard Monthly"
        assert result["payroll_frequency"] == "monthly"
        assert result["component_count"] == 1
        assert "salary_structure_id" in result

    def test_missing_name_fails(self, conn, env):
        """Omitting --name should fail."""
        result = call_action(mod.add_salary_structure, conn, ns(
            name=None,
            company_id=env["company_id"],
            components='[{"salary_component_id": "fake", "amount": "5000"}]',
            payroll_frequency=None,
        ))
        assert is_error(result)


# ──────────────────────────────────────────────────────────────────────────────
# get-salary-structure
# ──────────────────────────────────────────────────────────────────────────────

class TestGetSalaryStructure:
    def _create_structure(self, conn, env):
        """Helper: create a component and structure, return structure ID."""
        comp = call_action(mod.add_salary_component, conn, ns(
            name="Get Test Base", component_type="earning",
            is_tax_applicable=None, is_statutory=None, is_pre_tax=None,
            variable_based_on_taxable_salary=None, depends_on_payment_days=None,
            gl_account_id=None, description=None,
        ))
        comp_id = comp["salary_component_id"]

        ss = call_action(mod.add_salary_structure, conn, ns(
            name="Get Test Structure",
            company_id=env["company_id"],
            components=json.dumps([{"salary_component_id": comp_id, "amount": "6000"}]),
            payroll_frequency=None,
        ))
        return ss["salary_structure_id"]

    def test_get(self, conn, env):
        """Retrieve a salary structure with component details."""
        ss_id = self._create_structure(conn, env)

        result = call_action(mod.get_salary_structure, conn, ns(
            salary_structure_id=ss_id,
        ))
        assert is_ok(result)
        ss = result["salary_structure"]
        assert ss["id"] == ss_id
        assert ss["name"] == "Get Test Structure"
        assert ss["component_count"] == 1
        assert len(ss["components"]) == 1
        assert ss["components"][0]["component_name"] == "Get Test Base"

    def test_get_nonexistent_fails(self, conn):
        """Getting a non-existent structure should fail."""
        result = call_action(mod.get_salary_structure, conn, ns(
            salary_structure_id="00000000-0000-0000-0000-000000000000",
        ))
        assert is_error(result)


# ──────────────────────────────────────────────────────────────────────────────
# list-salary-structures
# ──────────────────────────────────────────────────────────────────────────────

class TestListSalaryStructures:
    def test_list(self, conn, env):
        """List salary structures after creating one."""
        # Create a component and structure
        comp = call_action(mod.add_salary_component, conn, ns(
            name="List Test Base", component_type="earning",
            is_tax_applicable=None, is_statutory=None, is_pre_tax=None,
            variable_based_on_taxable_salary=None, depends_on_payment_days=None,
            gl_account_id=None, description=None,
        ))
        call_action(mod.add_salary_structure, conn, ns(
            name="List Test Structure",
            company_id=env["company_id"],
            components=json.dumps([
                {"salary_component_id": comp["salary_component_id"], "amount": "5000"},
            ]),
            payroll_frequency=None,
        ))

        result = call_action(mod.list_salary_structures, conn, ns(
            company_id=env["company_id"],
            limit=20,
            offset=0,
            search=None,
        ))
        assert is_ok(result)
        assert result["count"] >= 1
        names = [s["name"] for s in result["structures"]]
        assert "List Test Structure" in names


# ──────────────────────────────────────────────────────────────────────────────
# add-salary-assignment
# ──────────────────────────────────────────────────────────────────────────────

class TestAddSalaryAssignment:
    def test_basic_create(self, conn, env):
        """Assign a salary structure to an employee."""
        # Create component and structure
        comp = call_action(mod.add_salary_component, conn, ns(
            name="Assign Test Base", component_type="earning",
            is_tax_applicable=None, is_statutory=None, is_pre_tax=None,
            variable_based_on_taxable_salary=None, depends_on_payment_days=None,
            gl_account_id=None, description=None,
        ))
        ss = call_action(mod.add_salary_structure, conn, ns(
            name="Assign Test Structure",
            company_id=env["company_id"],
            components=json.dumps([
                {"salary_component_id": comp["salary_component_id"], "amount": "5000"},
            ]),
            payroll_frequency=None,
        ))
        ss_id = ss["salary_structure_id"]

        result = call_action(mod.add_salary_assignment, conn, ns(
            employee_id=env["employee_id"],
            salary_structure_id=ss_id,
            base_amount="5000.00",
            effective_from="2026-01-01",
            effective_to=None,
        ))
        assert is_ok(result)
        assert result["employee_id"] == env["employee_id"]
        assert result["salary_structure_id"] == ss_id
        assert result["base_amount"] == "5000.00"
        assert "salary_assignment_id" in result

        # Verify in DB
        row = conn.execute(
            "SELECT * FROM salary_assignment WHERE id = ?",
            (result["salary_assignment_id"],),
        ).fetchone()
        assert row is not None
        assert row["base_amount"] == "5000.00"


# ──────────────────────────────────────────────────────────────────────────────
# list-salary-assignments
# ──────────────────────────────────────────────────────────────────────────────

class TestListSalaryAssignments:
    def test_list(self, conn, env):
        """List salary assignments after creating one."""
        # Create component, structure, assignment
        comp = call_action(mod.add_salary_component, conn, ns(
            name="ListAssign Base", component_type="earning",
            is_tax_applicable=None, is_statutory=None, is_pre_tax=None,
            variable_based_on_taxable_salary=None, depends_on_payment_days=None,
            gl_account_id=None, description=None,
        ))
        ss = call_action(mod.add_salary_structure, conn, ns(
            name="ListAssign Structure",
            company_id=env["company_id"],
            components=json.dumps([
                {"salary_component_id": comp["salary_component_id"], "amount": "4000"},
            ]),
            payroll_frequency=None,
        ))
        call_action(mod.add_salary_assignment, conn, ns(
            employee_id=env["employee_id"],
            salary_structure_id=ss["salary_structure_id"],
            base_amount="4000.00",
            effective_from="2026-01-01",
            effective_to=None,
        ))

        result = call_action(mod.list_salary_assignments, conn, ns(
            employee_id=env["employee_id"],
            company_id=None,
            limit=20,
            offset=0,
            from_date=None,
            to_date=None,
        ))
        assert is_ok(result)
        assert result["count"] >= 1
        assert len(result["assignments"]) >= 1
        assert result["assignments"][0]["employee_id"] == env["employee_id"]
