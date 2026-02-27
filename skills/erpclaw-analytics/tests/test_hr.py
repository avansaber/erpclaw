"""Tests for HR analytics — 4 tests."""
import sys
import os
import uuid
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))
sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))

from helpers import (
    _call_action, create_test_company, create_test_employee,
    create_test_department,
)
from db_query import action_headcount_analytics, action_payroll_analytics, action_leave_utilization


def _create_payroll_run(conn, company_id, period_start, period_end=None,
                        status="submitted"):
    """Insert a payroll_run for testing."""
    pr_id = str(uuid.uuid4())
    if not period_end:
        period_end = period_start
    conn.execute(
        """INSERT INTO payroll_run (id, company_id, period_start, period_end,
           status)
           VALUES (?, ?, ?, ?, ?)""",
        (pr_id, company_id, period_start, period_end, status),
    )
    conn.commit()
    return pr_id


def _create_salary_slip(conn, payroll_run_id, employee_id, company_id,
                         gross, net, deductions):
    """Insert a salary_slip for testing."""
    ss_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO salary_slip (id, payroll_run_id, employee_id,
           period_start, period_end,
           gross_pay, net_pay, total_deductions, company_id, status)
           VALUES (?, ?, ?, '2026-01-01', '2026-01-31', ?, ?, ?, ?, 'submitted')""",
        (ss_id, payroll_run_id, employee_id,
         str(gross), str(net), str(deductions), company_id),
    )
    conn.commit()
    return ss_id


def _create_leave_allocation(conn, employee_id, total_leaves,
                              fiscal_year="2026"):
    """Insert a leave_allocation for testing."""
    la_id = str(uuid.uuid4())
    lt_id = str(uuid.uuid4())
    # Need a leave_type first
    conn.execute(
        "INSERT OR IGNORE INTO leave_type (id, name, max_days_allowed) VALUES (?, 'PTO', '20')",
        (lt_id,),
    )
    conn.execute(
        """INSERT INTO leave_allocation (id, employee_id, leave_type_id,
           fiscal_year, total_leaves)
           VALUES (?, ?, ?, ?, ?)""",
        (la_id, employee_id, lt_id, fiscal_year, str(total_leaves)),
    )
    conn.commit()
    return la_id


def _create_leave_application(conn, employee_id, from_date, to_date, total_days,
                               status="approved"):
    """Insert a leave_application for testing."""
    la_id = str(uuid.uuid4())
    lt_id = conn.execute("SELECT id FROM leave_type LIMIT 1").fetchone()["id"]
    conn.execute(
        """INSERT INTO leave_application (id, employee_id, leave_type_id,
           from_date, to_date, total_days, status)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (la_id, employee_id, lt_id, from_date, to_date, str(total_days), status),
    )
    conn.commit()
    return la_id


class TestHeadcountAnalytics:
    def test_basic_headcount(self, fresh_db):
        """HR-01: Headcount shows correct total and department breakdown."""
        cid = create_test_company(fresh_db)
        eng = create_test_department(fresh_db, cid, "Engineering")
        mktg = create_test_department(fresh_db, cid, "Marketing")

        create_test_employee(fresh_db, cid, "Alice", "Smith", eng)
        create_test_employee(fresh_db, cid, "Bob", "Jones", eng)
        create_test_employee(fresh_db, cid, "Charlie", "Brown", mktg)

        result = _call_action(action_headcount_analytics, fresh_db,
                              company_id=cid, group_by="department")
        assert result["status"] == "ok"
        assert result["total_headcount"] == 3
        assert len(result["breakdown"]) == 2

    def test_empty_headcount(self, fresh_db):
        """HR-02: Returns zero when no employees."""
        cid = create_test_company(fresh_db)
        result = _call_action(action_headcount_analytics, fresh_db, company_id=cid)
        assert result["status"] == "ok"
        assert result["total_headcount"] == 0


class TestPayrollAnalytics:
    def test_basic_payroll(self, fresh_db):
        """HR-03: Payroll analytics shows correct totals."""
        cid = create_test_company(fresh_db)
        eng = create_test_department(fresh_db, cid, "Engineering")
        e1 = create_test_employee(fresh_db, cid, "Alice", "Smith", eng)
        e2 = create_test_employee(fresh_db, cid, "Bob", "Jones", eng)

        pr = _create_payroll_run(fresh_db, cid, "2026-01-01", "2026-01-31")
        _create_salary_slip(fresh_db, pr, e1, cid, 8000, 6500, 1500)
        _create_salary_slip(fresh_db, pr, e2, cid, 9000, 7200, 1800)

        result = _call_action(action_payroll_analytics, fresh_db,
                              company_id=cid, from_date="2026-01-01", to_date="2026-01-31")
        assert result["status"] == "ok"
        assert result["slip_count"] == 2
        assert Decimal(result["total_gross"]) == Decimal("17000.00")
        assert Decimal(result["total_net"]) == Decimal("13700.00")


class TestLeaveUtilization:
    def test_basic_utilization(self, fresh_db):
        """HR-04: Leave utilization shows correct allocated vs used."""
        cid = create_test_company(fresh_db)
        e1 = create_test_employee(fresh_db, cid, "Alice", "Smith")

        _create_leave_allocation(fresh_db, e1, 20)
        _create_leave_application(fresh_db, e1, "2026-01-10", "2026-01-14", 5)

        result = _call_action(action_leave_utilization, fresh_db,
                              company_id=cid, from_date="2026-01-01", to_date="2026-12-31")
        assert result["status"] == "ok"
        assert Decimal(result["total_allocated"]) == Decimal("20.00")
        assert Decimal(result["total_used"]) == Decimal("5.00")
        assert result["utilization"] == "25.0%"
