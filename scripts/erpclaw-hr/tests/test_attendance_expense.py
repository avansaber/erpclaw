"""Tests for erpclaw-hr attendance and expense claim actions.

Actions tested:
  - mark-attendance
  - list-attendance
  - add-expense-claim
  - submit-expense-claim
  - approve-expense-claim
  - list-expense-claims
  - status
"""
import json
import pytest
from datetime import date, timedelta
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


def _past_date(days_ago=1):
    """Return an ISO date string for a date in the past (avoids future-date guard)."""
    return (date.today() - timedelta(days=days_ago)).isoformat()


# ──────────────────────────────────────────────────────────────────────────────
# mark-attendance accesses: employee_id, date, status, shift, check_in_time,
#   check_out_time, working_hours, late_entry, early_exit, source
# ──────────────────────────────────────────────────────────────────────────────


class TestMarkAttendance:
    def test_mark_present(self, conn, env):
        emp_id = _create_employee(conn, env, "AttEmp")
        att_date = _past_date(1)

        result = call_action(mod.mark_attendance, conn, ns(
            employee_id=emp_id,
            date=att_date,
            status="present",
            shift=None,
            check_in_time=None,
            check_out_time=None,
            working_hours=None,
            late_entry=None,
            early_exit=None,
            source=None,
        ))
        assert is_ok(result)
        assert result["employee_id"] == emp_id
        assert result["date"] == att_date
        assert "attendance_id" in result


class TestListAttendance:
    def test_list(self, conn, env):
        emp_id = _create_employee(conn, env, "ListAttEmp")
        att_date = _past_date(2)

        call_action(mod.mark_attendance, conn, ns(
            employee_id=emp_id,
            date=att_date,
            status="present",
            shift=None,
            check_in_time=None,
            check_out_time=None,
            working_hours=None,
            late_entry=None,
            early_exit=None,
            source=None,
        ))

        result = call_action(mod.list_attendance, conn, ns(
            employee_id=emp_id,
            from_date=None,
            to_date=None,
            status=None,
            limit=None,
            offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1
        assert len(result["attendance"]) >= 1
        assert "summary" in result


# ──────────────────────────────────────────────────────────────────────────────
# add-expense-claim accesses: employee_id, expense_date, company_id, items
# submit-expense-claim accesses: expense_claim_id
# approve-expense-claim accesses: expense_claim_id, approved_by
# list-expense-claims accesses: employee_id, status, company_id, from_date,
#   to_date, limit, offset
# ──────────────────────────────────────────────────────────────────────────────


def _create_expense_claim(conn, env, emp_id):
    """Create a draft expense claim and return its ID."""
    items = json.dumps([
        {
            "expense_type": "travel",
            "description": "Flight to NYC",
            "amount": "500.00",
            "account_id": env["expense_account"],
        },
    ])
    result = call_action(mod.add_expense_claim, conn, ns(
        employee_id=emp_id,
        expense_date="2026-03-01",
        company_id=env["company_id"],
        items=items,
    ))
    assert is_ok(result)
    return result["expense_claim_id"]


class TestAddExpenseClaim:
    def test_basic_create(self, conn, env):
        emp_id = _create_employee(conn, env, "ExpEmp")
        items = json.dumps([
            {
                "expense_type": "meals",
                "description": "Team lunch",
                "amount": "150.00",
                "account_id": env["expense_account"],
            },
        ])
        result = call_action(mod.add_expense_claim, conn, ns(
            employee_id=emp_id,
            expense_date="2026-02-15",
            company_id=env["company_id"],
            items=items,
        ))
        assert is_ok(result)
        assert Decimal(result["total_amount"]) == Decimal("150.00")
        assert result["item_count"] == 1
        assert "expense_claim_id" in result
        assert "naming_series" in result


class TestSubmitExpenseClaim:
    def test_submit(self, conn, env):
        emp_id = _create_employee(conn, env, "SubExpEmp")
        claim_id = _create_expense_claim(conn, env, emp_id)

        result = call_action(mod.submit_expense_claim, conn, ns(
            expense_claim_id=claim_id,
        ))
        assert is_ok(result)

        # Verify DB status
        row = conn.execute(
            "SELECT status FROM expense_claim WHERE id=?",
            (claim_id,)
        ).fetchone()
        assert row["status"] == "submitted"


class TestApproveExpenseClaim:
    def test_approve(self, conn, env):
        emp_id = _create_employee(conn, env, "ApprExpEmp")
        approver_id = _create_employee(conn, env, "ExpApprover")
        claim_id = _create_expense_claim(conn, env, emp_id)

        # Must submit before approving
        call_action(mod.submit_expense_claim, conn, ns(
            expense_claim_id=claim_id,
        ))

        result = call_action(mod.approve_expense_claim, conn, ns(
            expense_claim_id=claim_id,
            approved_by=approver_id,
        ))
        assert is_ok(result)

        # Verify DB status
        row = conn.execute(
            "SELECT status FROM expense_claim WHERE id=?",
            (claim_id,)
        ).fetchone()
        assert row["status"] == "approved"

        # Verify GL entries were created
        assert result["gl_entry_count"] >= 2
        assert len(result["gl_entry_ids"]) >= 2


class TestListExpenseClaims:
    def test_list(self, conn, env):
        emp_id = _create_employee(conn, env, "ListExpEmp")
        _create_expense_claim(conn, env, emp_id)

        result = call_action(mod.list_expense_claims, conn, ns(
            employee_id=emp_id,
            status=None,
            company_id=None,
            from_date=None,
            to_date=None,
            limit=None,
            offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1
        assert len(result["expense_claims"]) >= 1


# ──────────────────────────────────────────────────────────────────────────────
# status accesses: company_id
# ──────────────────────────────────────────────────────────────────────────────


class TestStatus:
    def test_status(self, conn, env):
        # Create some data so there's something to report
        _create_employee(conn, env, "StatusEmp")

        result = call_action(mod.status_action, conn, ns(
            company_id=env["company_id"],
        ))
        assert is_ok(result)
        assert "total_employees" in result
        assert result["total_employees"] >= 1
        assert "total_departments" in result
        assert "employees_by_status" in result
        assert "leave_summary" in result
        assert "expense_claims_by_status" in result
