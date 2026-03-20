"""Tests for erpclaw-hr organization structure actions.

Actions tested:
  - add-department
  - list-departments
  - add-designation
  - list-designations
"""
import pytest
from hr_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
)

mod = load_db_query()


# ──────────────────────────────────────────────────────────────────────────────
# add-department accesses: args.name, args.company_id, args.parent_id,
#                          args.cost_center_id
# list-departments accesses: args.company_id, args.parent_id,
#                            args.limit, args.offset
# ──────────────────────────────────────────────────────────────────────────────


class TestAddDepartment:
    def test_basic_create(self, conn, env):
        result = call_action(mod.add_department, conn, ns(
            name="Sales",
            company_id=env["company_id"],
            parent_id=None,
            cost_center_id=None,
        ))
        assert is_ok(result)
        assert result["name"] == "Sales"
        assert "department_id" in result

    def test_missing_name_fails(self, conn, env):
        result = call_action(mod.add_department, conn, ns(
            name=None,
            company_id=env["company_id"],
            parent_id=None,
            cost_center_id=None,
        ))
        assert is_error(result)


class TestListDepartments:
    def test_list(self, conn, env):
        call_action(mod.add_department, conn, ns(
            name="HR Dept",
            company_id=env["company_id"],
            parent_id=None,
            cost_center_id=None,
        ))
        call_action(mod.add_department, conn, ns(
            name="Finance Dept",
            company_id=env["company_id"],
            parent_id=None,
            cost_center_id=None,
        ))

        result = call_action(mod.list_departments, conn, ns(
            company_id=env["company_id"],
            parent_id=None,
            limit=None,
            offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 2
        assert len(result["departments"]) >= 2


# ──────────────────────────────────────────────────────────────────────────────
# add-designation accesses: args.name, args.description
# list-designations accesses: args.limit, args.offset
# ──────────────────────────────────────────────────────────────────────────────


class TestAddDesignation:
    def test_basic_create(self, conn, env):
        result = call_action(mod.add_designation, conn, ns(
            name="Software Engineer",
            description="Writes code",
        ))
        assert is_ok(result)
        assert result["name"] == "Software Engineer"
        assert "designation_id" in result

    def test_missing_name_fails(self, conn, env):
        result = call_action(mod.add_designation, conn, ns(
            name=None,
            description=None,
        ))
        assert is_error(result)


class TestListDesignations:
    def test_list(self, conn, env):
        call_action(mod.add_designation, conn, ns(
            name="Manager",
            description=None,
        ))
        call_action(mod.add_designation, conn, ns(
            name="Director",
            description=None,
        ))

        result = call_action(mod.list_designations, conn, ns(
            limit=None,
            offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 2
        assert len(result["designations"]) >= 2
