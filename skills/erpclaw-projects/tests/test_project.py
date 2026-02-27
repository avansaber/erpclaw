"""Tests for erpclaw-projects: project, task, milestone, and status actions.

Covers:
  - add-project, update-project, get-project, list-projects
  - add-task, update-task, list-tasks (with dependencies)
  - add-milestone, update-milestone
  - status dashboard
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
    create_test_customer,
)


# ---------------------------------------------------------------------------
# 1. test_add_project
# ---------------------------------------------------------------------------

class TestAddProject:

    def test_add_project(self, fresh_db):
        """Create a basic project, verify default fields."""
        env = setup_projects_environment(fresh_db)

        result = _call_action(
            ACTIONS["add-project"], fresh_db,
            company_id=env["company_id"],
            name="Website Redesign",
            project_type="external",
            billing_type="time_and_material",
            estimated_cost="50000",
            start_date="2026-03-01",
            end_date="2026-09-30",
        )

        assert result["status"] == "ok"
        proj = result["project"]
        assert proj["project_name"] == "Website Redesign"
        assert proj["project_type"] == "external"
        assert proj["billing_type"] == "time_and_material"
        assert proj["estimated_cost"] == "50000.00"
        assert proj["actual_cost"] == "0"
        assert proj["total_billed"] == "0"
        assert proj["profit_margin"] == "0"
        assert proj["percent_complete"] == "0"
        assert proj["status"] == "open"
        assert proj["priority"] == "medium"
        assert proj["company_id"] == env["company_id"]
        assert proj["start_date"] == "2026-03-01"
        assert proj["end_date"] == "2026-09-30"
        assert proj["naming_series"] is not None
        assert proj["id"] is not None
        # No customer assigned
        assert proj["customer_id"] is None

    def test_add_project_with_customer(self, fresh_db):
        """Create a project linked to a customer."""
        env = setup_projects_environment(fresh_db)

        result = _call_action(
            ACTIONS["add-project"], fresh_db,
            company_id=env["company_id"],
            name="Client Portal",
            customer_id=env["customer_id"],
            project_type="service",
            billing_type="fixed_price",
            estimated_cost="100000",
            start_date="2026-04-01",
            end_date="2026-12-31",
            cost_center_id=env["cost_center_id"],
        )

        assert result["status"] == "ok"
        proj = result["project"]
        assert proj["customer_id"] == env["customer_id"]
        assert proj["project_type"] == "service"
        assert proj["billing_type"] == "fixed_price"
        assert proj["estimated_cost"] == "100000.00"
        assert proj["cost_center_id"] == env["cost_center_id"]


# ---------------------------------------------------------------------------
# 2. test_update_project
# ---------------------------------------------------------------------------

class TestUpdateProject:

    def test_update_project(self, fresh_db):
        """Update project status and percent_complete."""
        env = setup_projects_environment(fresh_db)
        project_id = create_test_project(fresh_db, env["company_id"])

        result = _call_action(
            ACTIONS["update-project"], fresh_db,
            project_id=project_id,
            status="in_progress",
            percent_complete="35",
        )

        assert result["status"] == "ok"
        proj = result["project"]
        assert proj["status"] == "in_progress"
        assert proj["percent_complete"] == "35.00"


# ---------------------------------------------------------------------------
# 3. test_get_project
# ---------------------------------------------------------------------------

class TestGetProject:

    def test_get_project(self, fresh_db):
        """Get project with nested tasks, milestones, and timesheet summary."""
        env = setup_projects_environment(fresh_db)
        project_id = create_test_project(fresh_db, env["company_id"])

        # Add a task and a milestone
        task_id = create_test_task(fresh_db, project_id, name="Design Phase")
        milestone_id = create_test_milestone(
            fresh_db, project_id, name="Design Complete", target_date="2026-03-31"
        )

        result = _call_action(
            ACTIONS["get-project"], fresh_db,
            project_id=project_id,
        )

        assert result["status"] == "ok"
        proj = result["project"]
        assert proj["id"] == project_id

        # Tasks nested
        assert len(proj["tasks"]) == 1
        assert proj["tasks"][0]["task_name"] == "Design Phase"
        assert proj["tasks"][0]["id"] == task_id

        # Milestones nested
        assert len(proj["milestones"]) == 1
        assert proj["milestones"][0]["milestone_name"] == "Design Complete"
        assert proj["milestones"][0]["id"] == milestone_id

        # Timesheet summary (no timesheets yet)
        ts_summary = proj["timesheet_summary"]
        assert ts_summary["total_hours"] == "0.00"
        assert ts_summary["billable_hours"] == "0.00"
        assert ts_summary["billable_amount"] == "0.00"
        assert ts_summary["timesheet_count"] == 0


# ---------------------------------------------------------------------------
# 4. test_list_projects
# ---------------------------------------------------------------------------

class TestListProjects:

    def test_list_projects(self, fresh_db):
        """List projects with status and search filters."""
        env = setup_projects_environment(fresh_db)

        # Create multiple projects
        p1 = create_test_project(fresh_db, env["company_id"], name="Alpha Project")
        p2 = create_test_project(fresh_db, env["company_id"], name="Beta Project")
        p3 = create_test_project(fresh_db, env["company_id"], name="Gamma Project")

        # Update one to in_progress
        _call_action(
            ACTIONS["update-project"], fresh_db,
            project_id=p2,
            status="in_progress",
        )

        # List all
        result = _call_action(
            ACTIONS["list-projects"], fresh_db,
            company_id=env["company_id"],
        )
        assert result["status"] == "ok"
        assert result["total"] == 3
        assert len(result["projects"]) == 3

        # Filter by status
        result = _call_action(
            ACTIONS["list-projects"], fresh_db,
            company_id=env["company_id"],
            status="open",
        )
        assert result["total"] == 2

        # Search by name
        result = _call_action(
            ACTIONS["list-projects"], fresh_db,
            company_id=env["company_id"],
            search="Beta",
        )
        assert result["total"] == 1
        assert result["projects"][0]["project_name"] == "Beta Project"


# ---------------------------------------------------------------------------
# 5. test_add_task
# ---------------------------------------------------------------------------

class TestAddTask:

    def test_add_task(self, fresh_db):
        """Create a task in a project, verify fields."""
        env = setup_projects_environment(fresh_db)
        project_id = create_test_project(fresh_db, env["company_id"])

        result = _call_action(
            ACTIONS["add-task"], fresh_db,
            project_id=project_id,
            name="Backend API",
            priority="high",
            start_date="2026-02-01",
            end_date="2026-04-30",
            estimated_hours="160",
            assigned_to=env["employee_id"],
            description="Implement REST API endpoints",
        )

        assert result["status"] == "ok"
        task = result["task"]
        assert task["task_name"] == "Backend API"
        assert task["priority"] == "high"
        assert task["status"] == "open"
        assert task["start_date"] == "2026-02-01"
        assert task["end_date"] == "2026-04-30"
        assert task["estimated_hours"] == "160.00"
        assert task["actual_hours"] == "0"
        assert task["assigned_to"] == env["employee_id"]
        assert task["project_id"] == project_id
        assert task["naming_series"] is not None
        assert task["depends_on"] is None

    def test_add_task_with_dependencies(self, fresh_db):
        """Create a task that depends on another task."""
        env = setup_projects_environment(fresh_db)
        project_id = create_test_project(fresh_db, env["company_id"])

        # Create the prerequisite task
        dep_task_id = create_test_task(
            fresh_db, project_id, name="Database Schema",
            start_date="2026-01-15", end_date="2026-02-15",
        )

        # Create a task that depends on it
        result = _call_action(
            ACTIONS["add-task"], fresh_db,
            project_id=project_id,
            name="Backend API",
            start_date="2026-02-16",
            end_date="2026-04-30",
            depends_on=json.dumps([dep_task_id]),
        )

        assert result["status"] == "ok"
        task = result["task"]
        assert task["task_name"] == "Backend API"
        depends = json.loads(task["depends_on"])
        assert isinstance(depends, list)
        assert dep_task_id in depends


# ---------------------------------------------------------------------------
# 6. test_update_task
# ---------------------------------------------------------------------------

class TestUpdateTask:

    def test_update_task(self, fresh_db):
        """Update task status and actual_hours."""
        env = setup_projects_environment(fresh_db)
        project_id = create_test_project(fresh_db, env["company_id"])
        task_id = create_test_task(fresh_db, project_id, name="Frontend Work")

        result = _call_action(
            ACTIONS["update-task"], fresh_db,
            task_id=task_id,
            status="in_progress",
            actual_hours="24.5",
            priority="high",
        )

        assert result["status"] == "ok"
        task = result["task"]
        assert task["status"] == "in_progress"
        assert task["actual_hours"] == "24.50"
        assert task["priority"] == "high"


# ---------------------------------------------------------------------------
# 7. test_list_tasks
# ---------------------------------------------------------------------------

class TestListTasks:

    def test_list_tasks(self, fresh_db):
        """List tasks for a project with status filter."""
        env = setup_projects_environment(fresh_db)
        project_id = create_test_project(fresh_db, env["company_id"])

        # Create multiple tasks
        t1 = create_test_task(fresh_db, project_id, name="Task A",
                              start_date="2026-01-10", end_date="2026-02-10")
        t2 = create_test_task(fresh_db, project_id, name="Task B",
                              start_date="2026-02-15", end_date="2026-03-15")
        t3 = create_test_task(fresh_db, project_id, name="Task C",
                              start_date="2026-03-20", end_date="2026-04-20")

        # Mark one as completed
        _call_action(
            ACTIONS["update-task"], fresh_db,
            task_id=t2,
            status="completed",
        )

        # List all for this project
        result = _call_action(
            ACTIONS["list-tasks"], fresh_db,
            project_id=project_id,
        )
        assert result["status"] == "ok"
        assert result["total"] == 3

        # Filter by status
        result = _call_action(
            ACTIONS["list-tasks"], fresh_db,
            project_id=project_id,
            status="open",
        )
        assert result["total"] == 2

        result = _call_action(
            ACTIONS["list-tasks"], fresh_db,
            project_id=project_id,
            status="completed",
        )
        assert result["total"] == 1
        assert result["tasks"][0]["task_name"] == "Task B"


# ---------------------------------------------------------------------------
# 8. test_add_milestone
# ---------------------------------------------------------------------------

class TestAddMilestone:

    def test_add_milestone(self, fresh_db):
        """Create a milestone and verify fields."""
        env = setup_projects_environment(fresh_db)
        project_id = create_test_project(fresh_db, env["company_id"])

        result = _call_action(
            ACTIONS["add-milestone"], fresh_db,
            project_id=project_id,
            name="MVP Release",
            target_date="2026-06-30",
            description="Minimum viable product launch",
        )

        assert result["status"] == "ok"
        ms = result["milestone"]
        assert ms["milestone_name"] == "MVP Release"
        assert ms["target_date"] == "2026-06-30"
        assert ms["status"] == "pending"
        assert ms["completion_date"] is None
        assert ms["project_id"] == project_id


# ---------------------------------------------------------------------------
# 9. test_update_milestone
# ---------------------------------------------------------------------------

class TestUpdateMilestone:

    def test_update_milestone(self, fresh_db):
        """Mark a milestone as completed."""
        env = setup_projects_environment(fresh_db)
        project_id = create_test_project(fresh_db, env["company_id"])
        milestone_id = create_test_milestone(
            fresh_db, project_id, name="Phase 1 Complete", target_date="2026-04-15"
        )

        result = _call_action(
            ACTIONS["update-milestone"], fresh_db,
            milestone_id=milestone_id,
            status="completed",
            completion_date="2026-04-10",
        )

        assert result["status"] == "ok"
        ms = result["milestone"]
        assert ms["status"] == "completed"
        assert ms["completion_date"] == "2026-04-10"


# ---------------------------------------------------------------------------
# 10. test_status_dashboard
# ---------------------------------------------------------------------------

class TestStatusDashboard:

    def test_status_dashboard(self, fresh_db):
        """Status dashboard returns correct counts."""
        env = setup_projects_environment(fresh_db)

        # Create projects with different statuses
        p1 = create_test_project(fresh_db, env["company_id"], name="Open Project 1")
        p2 = create_test_project(fresh_db, env["company_id"], name="Open Project 2")
        p3 = create_test_project(fresh_db, env["company_id"], name="IP Project")

        _call_action(
            ACTIONS["update-project"], fresh_db,
            project_id=p3,
            status="in_progress",
        )

        # Create an overdue task (end_date in the past -- use a past date)
        _call_action(
            ACTIONS["add-task"], fresh_db,
            project_id=p1,
            name="Overdue Task",
            start_date="2025-01-01",
            end_date="2025-06-01",
            estimated_hours="10",
        )

        # Create a milestone
        create_test_milestone(
            fresh_db, p1, name="Upcoming Milestone", target_date="2028-03-15"
        )

        result = _call_action(
            ACTIONS["status"], fresh_db,
            company_id=env["company_id"],
        )

        assert result["status"] == "ok"
        # 2 open + 1 in_progress = 3 active
        assert result["active_projects"] == 3

        status_counts = result["projects_by_status"]
        assert status_counts.get("open", 0) == 2
        assert status_counts.get("in_progress", 0) == 1

        # Overdue tasks: task with end_date < today and status in (open, in_progress)
        assert result["overdue_tasks_count"] >= 1

        # Hours this month (no timesheets yet)
        assert result["hours_this_month"]["total"] == "0.00"
        assert result["hours_this_month"]["billable"] == "0.00"
