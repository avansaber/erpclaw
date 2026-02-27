"""Tests for erpclaw-projects: timesheet, profitability, gantt, and utilization.

Covers:
  - add-timesheet, get-timesheet, list-timesheets
  - submit-timesheet (validates, updates task hours)
  - submit-timesheet invalid (reject hours=0)
  - bill-timesheet (submitted->billed, updates project costs)
  - project-profitability (employee breakdown)
  - gantt-data (tasks with dependencies)
  - resource-utilization (hours by employee)
"""
import json
import os
import sys

import pytest

TESTS_DIR = os.path.dirname(__file__)
SCRIPTS_DIR = os.path.join(os.path.dirname(TESTS_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

from decimal import Decimal

from db_query import ACTIONS
from helpers import (
    _call_action,
    setup_projects_environment,
    create_test_project,
    create_test_task,
    create_test_milestone,
    create_test_timesheet,
    create_test_employee,
)


# ---------------------------------------------------------------------------
# 1. test_add_timesheet
# ---------------------------------------------------------------------------

class TestAddTimesheet:

    def test_add_timesheet(self, fresh_db):
        """Create a draft timesheet with detail items."""
        env = setup_projects_environment(fresh_db)
        project_id = create_test_project(fresh_db, env["company_id"])
        task_id = create_test_task(fresh_db, project_id, name="Dev Work")

        items = [
            {
                "project_id": project_id,
                "task_id": task_id,
                "activity_type": "development",
                "hours": "8",
                "billing_rate": "150",
                "billable": 1,
                "date": "2026-02-03",
                "description": "Backend coding",
            },
            {
                "project_id": project_id,
                "task_id": task_id,
                "activity_type": "consulting",
                "hours": "2",
                "billing_rate": "200",
                "billable": 1,
                "date": "2026-02-04",
                "description": "Client meeting",
            },
        ]

        result = _call_action(
            ACTIONS["add-timesheet"], fresh_db,
            company_id=env["company_id"],
            employee_id=env["employee_id"],
            start_date="2026-02-01",
            end_date="2026-02-07",
            items=json.dumps(items),
        )

        assert result["status"] == "ok"
        ts = result["timesheet"]
        assert ts["status"] == "draft"
        assert ts["employee_id"] == env["employee_id"]
        assert ts["company_id"] == env["company_id"]
        # total_hours = 8 + 2 = 10
        assert ts["total_hours"] == "10.00"
        # total_billable_hours = 10 (both billable)
        assert ts["total_billable_hours"] == "10.00"
        # total_billable_amount = 8*150 + 2*200 = 1200 + 400 = 1600
        assert ts["total_billable_amount"] == "1600.00"
        # total_cost = same as billable amount for billable items
        assert ts["total_cost"] == "1600.00"
        assert ts["total_billed_hours"] == "0"

        # Detail rows
        assert len(ts["items"]) == 2
        assert ts["items"][0]["hours"] == "8.00"
        assert ts["items"][0]["billing_rate"] == "150.00"
        assert ts["items"][1]["hours"] == "2.00"
        assert ts["items"][1]["billing_rate"] == "200.00"
        assert ts["naming_series"] is not None


# ---------------------------------------------------------------------------
# 2. test_get_timesheet
# ---------------------------------------------------------------------------

class TestGetTimesheet:

    def test_get_timesheet(self, fresh_db):
        """Get a timesheet with detail rows."""
        env = setup_projects_environment(fresh_db)
        project_id = create_test_project(fresh_db, env["company_id"])
        task_id = create_test_task(fresh_db, project_id, name="Test Task")

        timesheet_id = create_test_timesheet(
            fresh_db, env["company_id"], env["employee_id"],
            project_id, task_id=task_id,
        )

        result = _call_action(
            ACTIONS["get-timesheet"], fresh_db,
            timesheet_id=timesheet_id,
        )

        assert result["status"] == "ok"
        ts = result["timesheet"]
        assert ts["id"] == timesheet_id
        assert ts["status"] == "draft"
        assert len(ts["items"]) == 1
        assert ts["items"][0]["project_id"] == project_id
        assert ts["items"][0]["task_id"] == task_id


# ---------------------------------------------------------------------------
# 3. test_list_timesheets
# ---------------------------------------------------------------------------

class TestListTimesheets:

    def test_list_timesheets(self, fresh_db):
        """List timesheets with employee and status filters."""
        env = setup_projects_environment(fresh_db)
        project_id = create_test_project(fresh_db, env["company_id"])

        # Create a second employee
        emp2_id = create_test_employee(
            fresh_db, env["company_id"], first_name="Jane", last_name="Smith",
        )

        # Create timesheets for different employees
        ts1_id = create_test_timesheet(
            fresh_db, env["company_id"], env["employee_id"], project_id,
            start_date="2026-02-01", end_date="2026-02-07",
        )
        ts2_id = create_test_timesheet(
            fresh_db, env["company_id"], emp2_id, project_id,
            start_date="2026-02-01", end_date="2026-02-07",
        )

        # List all
        result = _call_action(
            ACTIONS["list-timesheets"], fresh_db,
            company_id=env["company_id"],
        )
        assert result["status"] == "ok"
        assert result["total"] == 2

        # Filter by employee
        result = _call_action(
            ACTIONS["list-timesheets"], fresh_db,
            employee_id=env["employee_id"],
        )
        assert result["total"] == 1
        assert result["timesheets"][0]["employee_id"] == env["employee_id"]

        # Filter by status
        result = _call_action(
            ACTIONS["list-timesheets"], fresh_db,
            status="draft",
        )
        assert result["total"] == 2


# ---------------------------------------------------------------------------
# 4. test_submit_timesheet
# ---------------------------------------------------------------------------

class TestSubmitTimesheet:

    def test_submit_timesheet(self, fresh_db):
        """Submit a timesheet: validates, updates status, updates task hours."""
        env = setup_projects_environment(fresh_db)
        project_id = create_test_project(fresh_db, env["company_id"])
        task_id = create_test_task(fresh_db, project_id, name="Coding Task")

        items = [
            {
                "project_id": project_id,
                "task_id": task_id,
                "activity_type": "development",
                "hours": "16",
                "billing_rate": "150",
                "billable": 1,
                "date": "2026-02-03",
                "description": "Sprint work",
            },
        ]
        timesheet_id = create_test_timesheet(
            fresh_db, env["company_id"], env["employee_id"], project_id,
            items=items, task_id=task_id,
        )

        # Submit
        result = _call_action(
            ACTIONS["submit-timesheet"], fresh_db,
            timesheet_id=timesheet_id,
        )

        assert result["status"] == "ok"
        assert result["timesheet"]["status"] == "submitted"

        # Verify task actual_hours was updated
        task_row = fresh_db.execute(
            "SELECT actual_hours FROM task WHERE id = ?", (task_id,),
        ).fetchone()
        assert task_row["actual_hours"] == "16.00"

    def test_submit_timesheet_invalid(self, fresh_db):
        """Reject submit if timesheet is not in draft status."""
        env = setup_projects_environment(fresh_db)
        project_id = create_test_project(fresh_db, env["company_id"])
        task_id = create_test_task(fresh_db, project_id, name="Some Task")

        timesheet_id = create_test_timesheet(
            fresh_db, env["company_id"], env["employee_id"], project_id,
            task_id=task_id,
        )

        # Submit once (should succeed)
        result1 = _call_action(
            ACTIONS["submit-timesheet"], fresh_db,
            timesheet_id=timesheet_id,
        )
        assert result1["status"] == "ok"

        # Try to submit again (should fail since status is now 'submitted')
        result2 = _call_action(
            ACTIONS["submit-timesheet"], fresh_db,
            timesheet_id=timesheet_id,
        )
        assert result2["status"] == "error"
        assert "submitted" in result2["message"].lower() or "draft" in result2["message"].lower()

    def test_submit_timesheet_accumulates_task_hours(self, fresh_db):
        """Multiple timesheets accumulate hours on the same task."""
        env = setup_projects_environment(fresh_db)
        project_id = create_test_project(fresh_db, env["company_id"])
        task_id = create_test_task(fresh_db, project_id, name="Long Task")

        # First timesheet: 8 hours
        items1 = [{
            "project_id": project_id,
            "task_id": task_id,
            "activity_type": "development",
            "hours": "8",
            "billing_rate": "100",
            "billable": 1,
            "date": "2026-02-03",
        }]
        ts1_id = create_test_timesheet(
            fresh_db, env["company_id"], env["employee_id"], project_id,
            items=items1, task_id=task_id,
        )
        _call_action(ACTIONS["submit-timesheet"], fresh_db, timesheet_id=ts1_id)

        # Second timesheet: 12 hours
        items2 = [{
            "project_id": project_id,
            "task_id": task_id,
            "activity_type": "development",
            "hours": "12",
            "billing_rate": "100",
            "billable": 1,
            "date": "2026-02-10",
        }]
        ts2_id = create_test_timesheet(
            fresh_db, env["company_id"], env["employee_id"], project_id,
            start_date="2026-02-08", end_date="2026-02-14",
            items=items2, task_id=task_id,
        )
        _call_action(ACTIONS["submit-timesheet"], fresh_db, timesheet_id=ts2_id)

        # Task should now have 8 + 12 = 20 actual_hours
        task_row = fresh_db.execute(
            "SELECT actual_hours FROM task WHERE id = ?", (task_id,),
        ).fetchone()
        assert task_row["actual_hours"] == "20.00"


# ---------------------------------------------------------------------------
# 5. test_bill_timesheet
# ---------------------------------------------------------------------------

class TestBillTimesheet:

    def test_bill_timesheet(self, fresh_db):
        """Bill a submitted timesheet: updates project costs."""
        env = setup_projects_environment(fresh_db)
        project_id = create_test_project(
            fresh_db, env["company_id"],
            name="Billable Project",
            billing_type="time_and_material",
            estimated_cost="10000",
        )
        task_id = create_test_task(fresh_db, project_id, name="Billable Task")

        items = [
            {
                "project_id": project_id,
                "task_id": task_id,
                "activity_type": "consulting",
                "hours": "10",
                "billing_rate": "200",
                "billable": 1,
                "date": "2026-02-05",
            },
            {
                "project_id": project_id,
                "task_id": task_id,
                "activity_type": "admin",
                "hours": "2",
                "billing_rate": "100",
                "billable": 0,
                "date": "2026-02-06",
            },
        ]

        timesheet_id = create_test_timesheet(
            fresh_db, env["company_id"], env["employee_id"], project_id,
            items=items, task_id=task_id,
        )

        # Submit first
        _call_action(ACTIONS["submit-timesheet"], fresh_db, timesheet_id=timesheet_id)

        # Bill
        result = _call_action(
            ACTIONS["bill-timesheet"], fresh_db,
            timesheet_id=timesheet_id,
        )

        assert result["status"] == "ok"
        ts = result["timesheet"]
        assert ts["status"] == "billed"
        # total_billable_hours = 10 (only the billable item)
        assert ts["total_billable_hours"] == "10.00"
        # total_billed_hours should match total_billable_hours after billing
        assert ts["total_billed_hours"] == "10.00"

        # Check project was updated:
        # cost = 10*200 + 2*100 = 2200 (all items contribute to cost)
        # billed = 10*200 = 2000 (only billable items)
        project_row = fresh_db.execute(
            "SELECT actual_cost, total_billed, profit_margin FROM project WHERE id = ?",
            (project_id,),
        ).fetchone()
        assert project_row["actual_cost"] == "2200.00"
        assert project_row["total_billed"] == "2000.00"

        # profit_margin = ((2000 - 2200) / 2000) * 100 = -10.00
        assert project_row["profit_margin"] == "-10.00"

    def test_bill_timesheet_rejects_draft(self, fresh_db):
        """Cannot bill a draft timesheet."""
        env = setup_projects_environment(fresh_db)
        project_id = create_test_project(fresh_db, env["company_id"])

        timesheet_id = create_test_timesheet(
            fresh_db, env["company_id"], env["employee_id"], project_id,
        )

        result = _call_action(
            ACTIONS["bill-timesheet"], fresh_db,
            timesheet_id=timesheet_id,
        )

        assert result["status"] == "error"
        assert "draft" in result["message"].lower()


# ---------------------------------------------------------------------------
# 6. test_project_profitability
# ---------------------------------------------------------------------------

class TestProjectProfitability:

    def test_project_profitability(self, fresh_db):
        """Project profitability shows employee breakdown after billing."""
        env = setup_projects_environment(fresh_db)
        project_id = create_test_project(
            fresh_db, env["company_id"],
            name="Profitable Project",
            billing_type="time_and_material",
            estimated_cost="20000",
        )

        # Create two employees
        emp2_id = create_test_employee(
            fresh_db, env["company_id"], first_name="Jane", last_name="Smith",
        )

        # Timesheet for employee 1: 20 hours @ $150
        items1 = [{
            "project_id": project_id,
            "activity_type": "development",
            "hours": "20",
            "billing_rate": "150",
            "billable": 1,
            "date": "2026-02-03",
        }]
        ts1 = create_test_timesheet(
            fresh_db, env["company_id"], env["employee_id"], project_id,
            items=items1,
        )
        _call_action(ACTIONS["submit-timesheet"], fresh_db, timesheet_id=ts1)
        _call_action(ACTIONS["bill-timesheet"], fresh_db, timesheet_id=ts1)

        # Timesheet for employee 2: 10 hours @ $200
        items2 = [{
            "project_id": project_id,
            "activity_type": "consulting",
            "hours": "10",
            "billing_rate": "200",
            "billable": 1,
            "date": "2026-02-05",
        }]
        ts2 = create_test_timesheet(
            fresh_db, env["company_id"], emp2_id, project_id,
            start_date="2026-02-03", end_date="2026-02-09",
            items=items2,
        )
        _call_action(ACTIONS["submit-timesheet"], fresh_db, timesheet_id=ts2)
        _call_action(ACTIONS["bill-timesheet"], fresh_db, timesheet_id=ts2)

        # Check profitability
        result = _call_action(
            ACTIONS["project-profitability"], fresh_db,
            project_id=project_id,
        )

        assert result["status"] == "ok"
        assert result["project_id"] == project_id
        assert result["project_name"] == "Profitable Project"
        assert result["estimated_cost"] == "20000.00"

        # actual_cost = 20*150 + 10*200 = 3000 + 2000 = 5000
        assert result["actual_cost"] == "5000.00"
        # total_billed = same since all billable
        assert result["total_billed"] == "5000.00"
        # profit = 5000 - 5000 = 0
        assert result["profit"] == "0.00"
        # margin = 0%
        assert result["margin_percent"] == "0.00"
        # cost_variance = 20000 - 5000 = 15000
        assert result["cost_variance"] == "15000.00"

        # Employee breakdown
        employees = result["employees"]
        assert len(employees) == 2

        # Sorted by total_hours DESC, so employee 1 (20h) first
        emp1_data = next(e for e in employees if e["employee_id"] == env["employee_id"])
        assert emp1_data["total_hours"] == "20.00"
        assert emp1_data["billable_hours"] == "20.00"
        assert emp1_data["total_cost"] == "3000.00"
        assert emp1_data["billable_amount"] == "3000.00"

        emp2_data = next(e for e in employees if e["employee_id"] == emp2_id)
        assert emp2_data["total_hours"] == "10.00"
        assert emp2_data["billable_hours"] == "10.00"
        assert emp2_data["total_cost"] == "2000.00"
        assert emp2_data["billable_amount"] == "2000.00"


# ---------------------------------------------------------------------------
# 7. test_gantt_data
# ---------------------------------------------------------------------------

class TestGanttData:

    def test_gantt_data(self, fresh_db):
        """Gantt data returns tasks with dependencies and milestones."""
        env = setup_projects_environment(fresh_db)
        project_id = create_test_project(
            fresh_db, env["company_id"],
            name="Gantt Project",
            start_date="2026-01-01",
            end_date="2026-06-30",
        )

        # Create tasks with dependency chain
        task1_id = create_test_task(
            fresh_db, project_id, name="Design",
            start_date="2026-01-15", end_date="2026-02-15",
            estimated_hours="80",
        )
        task2_id = create_test_task(
            fresh_db, project_id, name="Development",
            start_date="2026-02-16", end_date="2026-04-30",
            estimated_hours="200",
            depends_on=json.dumps([task1_id]),
        )
        task3_id = create_test_task(
            fresh_db, project_id, name="Testing",
            start_date="2026-05-01", end_date="2026-06-15",
            estimated_hours="60",
            depends_on=json.dumps([task2_id]),
        )

        # Add a milestone
        milestone_id = create_test_milestone(
            fresh_db, project_id, name="Beta Release", target_date="2026-05-01"
        )

        result = _call_action(
            ACTIONS["gantt-data"], fresh_db,
            project_id=project_id,
        )

        assert result["status"] == "ok"
        assert result["project_id"] == project_id
        assert result["project_name"] == "Gantt Project"
        assert result["start_date"] == "2026-01-01"
        assert result["end_date"] == "2026-06-30"

        # Tasks
        tasks = result["tasks"]
        assert len(tasks) == 3

        # Verify order (by start_date)
        assert tasks[0]["name"] == "Design"
        assert tasks[1]["name"] == "Development"
        assert tasks[2]["name"] == "Testing"

        # Verify dependencies
        assert tasks[0]["depends_on"] is None
        assert tasks[1]["depends_on"] == [task1_id]
        assert tasks[2]["depends_on"] == [task2_id]

        # Verify other fields
        assert tasks[0]["estimated_hours"] == "80.00"
        assert tasks[1]["estimated_hours"] == "200.00"
        assert tasks[2]["estimated_hours"] == "60.00"

        # Milestones
        milestones = result["milestones"]
        assert len(milestones) == 1
        assert milestones[0]["name"] == "Beta Release"
        assert milestones[0]["target_date"] == "2026-05-01"
        assert milestones[0]["status"] == "pending"
        assert milestones[0]["completion_date"] is None


# ---------------------------------------------------------------------------
# 8. test_resource_utilization
# ---------------------------------------------------------------------------

class TestResourceUtilization:

    def test_resource_utilization(self, fresh_db):
        """Resource utilization shows hours by employee with utilization %."""
        env = setup_projects_environment(fresh_db)
        project_id = create_test_project(fresh_db, env["company_id"])

        emp2_id = create_test_employee(
            fresh_db, env["company_id"], first_name="Alice", last_name="Johnson",
        )

        # Employee 1: 20 billable + 5 non-billable = 25 total
        items1 = [
            {
                "project_id": project_id,
                "activity_type": "development",
                "hours": "20",
                "billing_rate": "150",
                "billable": 1,
                "date": "2026-02-03",
            },
            {
                "project_id": project_id,
                "activity_type": "admin",
                "hours": "5",
                "billing_rate": "0",
                "billable": 0,
                "date": "2026-02-04",
            },
        ]
        ts1 = create_test_timesheet(
            fresh_db, env["company_id"], env["employee_id"], project_id,
            items=items1,
        )
        _call_action(ACTIONS["submit-timesheet"], fresh_db, timesheet_id=ts1)

        # Employee 2: 15 billable hours
        items2 = [{
            "project_id": project_id,
            "activity_type": "consulting",
            "hours": "15",
            "billing_rate": "200",
            "billable": 1,
            "date": "2026-02-05",
        }]
        ts2 = create_test_timesheet(
            fresh_db, env["company_id"], emp2_id, project_id,
            start_date="2026-02-03", end_date="2026-02-09",
            items=items2,
        )
        _call_action(ACTIONS["submit-timesheet"], fresh_db, timesheet_id=ts2)

        result = _call_action(
            ACTIONS["resource-utilization"], fresh_db,
            company_id=env["company_id"],
        )

        assert result["status"] == "ok"
        assert result["company_id"] == env["company_id"]

        employees = result["employees"]
        assert len(employees) == 2

        # Find each employee's data
        emp1_data = next(e for e in employees if e["employee_id"] == env["employee_id"])
        emp2_data = next(e for e in employees if e["employee_id"] == emp2_id)

        # Employee 1: 25 total, 20 billable, 5 non-billable
        assert emp1_data["total_hours"] == "25.00"
        assert emp1_data["billable_hours"] == "20.00"
        assert emp1_data["non_billable_hours"] == "5.00"
        # utilization = 20/25 * 100 = 80.00
        assert emp1_data["utilization_percent"] == "80.00"
        assert emp1_data["project_count"] == 1
        # billable_amount = 20 * 150 = 3000
        assert emp1_data["billable_amount"] == "3000.00"

        # Employee 2: 15 total, 15 billable, 0 non-billable
        assert emp2_data["total_hours"] == "15.00"
        assert emp2_data["billable_hours"] == "15.00"
        assert emp2_data["non_billable_hours"] == "0.00"
        # utilization = 15/15 * 100 = 100.00
        assert emp2_data["utilization_percent"] == "100.00"
        assert emp2_data["project_count"] == 1
        # billable_amount = 15 * 200 = 3000
        assert emp2_data["billable_amount"] == "3000.00"

        # Summary
        summary = result["summary"]
        assert summary["total_employees"] == 2
        # total_hours = 25 + 15 = 40
        assert summary["total_hours"] == "40.00"
        # total_billable_hours = 20 + 15 = 35
        assert summary["total_billable_hours"] == "35.00"
        # overall_utilization = 35/40 * 100 = 87.50
        assert summary["overall_utilization_percent"] == "87.50"

    def test_resource_utilization_with_date_filter(self, fresh_db):
        """Resource utilization can be filtered by date range."""
        env = setup_projects_environment(fresh_db)
        project_id = create_test_project(fresh_db, env["company_id"])

        # Timesheet in February
        items_feb = [{
            "project_id": project_id,
            "activity_type": "development",
            "hours": "40",
            "billing_rate": "100",
            "billable": 1,
            "date": "2026-02-15",
        }]
        ts_feb = create_test_timesheet(
            fresh_db, env["company_id"], env["employee_id"], project_id,
            start_date="2026-02-09", end_date="2026-02-15",
            items=items_feb,
        )
        _call_action(ACTIONS["submit-timesheet"], fresh_db, timesheet_id=ts_feb)

        # Timesheet in March
        items_mar = [{
            "project_id": project_id,
            "activity_type": "development",
            "hours": "30",
            "billing_rate": "100",
            "billable": 1,
            "date": "2026-03-10",
        }]
        ts_mar = create_test_timesheet(
            fresh_db, env["company_id"], env["employee_id"], project_id,
            start_date="2026-03-09", end_date="2026-03-15",
            items=items_mar,
        )
        _call_action(ACTIONS["submit-timesheet"], fresh_db, timesheet_id=ts_mar)

        # Filter to February only
        result = _call_action(
            ACTIONS["resource-utilization"], fresh_db,
            company_id=env["company_id"],
            from_date="2026-02-01",
            to_date="2026-02-28",
        )

        assert result["status"] == "ok"
        assert len(result["employees"]) == 1
        assert result["employees"][0]["total_hours"] == "40.00"
        assert result["summary"]["total_hours"] == "40.00"
