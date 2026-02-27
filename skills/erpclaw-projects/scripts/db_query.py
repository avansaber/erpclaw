#!/usr/bin/env python3
"""ERPClaw Projects Skill -- db_query.py

Projects, tasks, milestones, timesheets, and project reports.
All 18 actions are routed through this single entry point.

Usage: python3 db_query.py --action <action-name> [--flags ...]
Output: JSON to stdout, exit 0 on success, exit 1 on error.
"""
import argparse
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

# Add shared lib to path
try:
    sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
    from erpclaw_lib.db import get_connection, ensure_db_exists, DEFAULT_DB_PATH
    from erpclaw_lib.decimal_utils import to_decimal, round_currency
    from erpclaw_lib.naming import get_next_name
    from erpclaw_lib.validation import check_input_lengths
    from erpclaw_lib.response import ok, err, row_to_dict
    from erpclaw_lib.audit import audit
    from erpclaw_lib.dependencies import check_required_tables
except ImportError:
    import json as _json
    print(_json.dumps({"status": "error", "error": "ERPClaw foundation not installed. Install erpclaw-setup first: clawhub install erpclaw-setup", "suggestion": "clawhub install erpclaw-setup"}))
    sys.exit(1)

REQUIRED_TABLES = ["company"]

VALID_PROJECT_TYPES = ("internal", "external", "service", "product")
VALID_PROJECT_STATUSES = ("open", "in_progress", "completed", "cancelled", "on_hold")
VALID_PROJECT_PRIORITIES = ("low", "medium", "high", "critical")
VALID_BILLING_TYPES = ("fixed_price", "time_and_material", "non_billable")
VALID_TASK_STATUSES = ("open", "in_progress", "completed", "cancelled", "blocked")
VALID_TASK_PRIORITIES = ("low", "medium", "high", "critical")
VALID_MILESTONE_STATUSES = ("pending", "completed", "missed")
VALID_TIMESHEET_STATUSES = ("draft", "submitted", "billed", "cancelled")
VALID_ACTIVITY_TYPES = ("development", "design", "consulting", "support", "admin")


def _parse_json_arg(value, name):
    if value is None:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        err(f"Invalid JSON for --{name}: {value}")


def _validate_company_exists(conn, company_id: str):
    """Validate that a company exists and return the row, or error."""
    company = conn.execute(
        "SELECT id FROM company WHERE id = ?", (company_id,),
    ).fetchone()
    if not company:
        err(f"Company {company_id} not found")
    return company


def _validate_project_exists(conn, project_id: str):
    """Validate that a project exists and return the row, or error."""
    project = conn.execute(
        "SELECT * FROM project WHERE id = ?", (project_id,),
    ).fetchone()
    if not project:
        err(f"Project {project_id} not found",
             suggestion="Use 'list projects' to see available projects.")
    return project


def _validate_task_exists(conn, task_id: str):
    """Validate that a task exists and return the row, or error."""
    task = conn.execute(
        "SELECT * FROM task WHERE id = ?", (task_id,),
    ).fetchone()
    if not task:
        err(f"Task {task_id} not found")
    return task


def _validate_milestone_exists(conn, milestone_id: str):
    """Validate that a milestone exists and return the row, or error."""
    milestone = conn.execute(
        "SELECT * FROM milestone WHERE id = ?", (milestone_id,),
    ).fetchone()
    if not milestone:
        err(f"Milestone {milestone_id} not found")
    return milestone


def _validate_timesheet_exists(conn, timesheet_id: str):
    """Validate that a timesheet exists and return the row, or error."""
    ts = conn.execute(
        "SELECT * FROM timesheet WHERE id = ?", (timesheet_id,),
    ).fetchone()
    if not ts:
        err(f"Timesheet {timesheet_id} not found")
    return ts


def _validate_employee_exists(conn, employee_id: str):
    """Validate that an employee exists and return the row, or error."""
    emp = conn.execute(
        "SELECT * FROM employee WHERE id = ?", (employee_id,),
    ).fetchone()
    if not emp:
        err(f"Employee {employee_id} not found")
    return emp


def _validate_customer_exists(conn, customer_id: str):
    """Validate that a customer exists and return the row, or error."""
    cust = conn.execute(
        "SELECT * FROM customer WHERE id = ?", (customer_id,),
    ).fetchone()
    if not cust:
        err(f"Customer {customer_id} not found")
    return cust


# ---------------------------------------------------------------------------
# 1. add-project
# ---------------------------------------------------------------------------

def add_project(conn, args):
    """Create a new project.

    Required: --company-id, --name
    Optional: --customer-id, --project-type, --status, --priority,
              --start-date, --end-date, --estimated-cost, --billing-type,
              --cost-center-id, --description
    """
    if not args.company_id:
        err("--company-id is required")
    if not args.name:
        err("--name is required")

    _validate_company_exists(conn, args.company_id)

    if args.customer_id:
        _validate_customer_exists(conn, args.customer_id)

    project_type = args.project_type or "internal"
    if project_type not in VALID_PROJECT_TYPES:
        err(f"Invalid --project-type: {project_type}. Must be one of {VALID_PROJECT_TYPES}")

    status = args.status or "open"
    if status not in VALID_PROJECT_STATUSES:
        err(f"Invalid --status: {status}. Must be one of {VALID_PROJECT_STATUSES}")

    priority = args.priority or "medium"
    if priority not in VALID_PROJECT_PRIORITIES:
        err(f"Invalid --priority: {priority}. Must be one of {VALID_PROJECT_PRIORITIES}")

    billing_type = args.billing_type or "non_billable"
    if billing_type not in VALID_BILLING_TYPES:
        err(f"Invalid --billing-type: {billing_type}. Must be one of {VALID_BILLING_TYPES}")

    estimated_cost = "0"
    if args.estimated_cost:
        estimated_cost = str(round_currency(to_decimal(args.estimated_cost)))
        if to_decimal(estimated_cost) < 0:
            err("--estimated-cost must be >= 0")

    if args.cost_center_id:
        cc = conn.execute(
            "SELECT id FROM cost_center WHERE id = ? OR name = ?",
            (args.cost_center_id, args.cost_center_id),
        ).fetchone()
        if not cc:
            err(f"Cost center {args.cost_center_id} not found")
        args.cost_center_id = cc["id"]

    project_id = str(uuid.uuid4())
    naming = get_next_name(conn, "project", company_id=args.company_id)

    conn.execute(
        """INSERT INTO project
           (id, naming_series, project_name, customer_id, project_type, status,
            priority, start_date, end_date, estimated_cost, actual_cost,
            billing_type, total_billed, profit_margin, percent_complete,
            cost_center_id, company_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '0', ?, '0', '0', '0', ?, ?)""",
        (project_id, naming, args.name, args.customer_id, project_type,
         status, priority, args.start_date, args.end_date, estimated_cost,
         billing_type, args.cost_center_id, args.company_id),
    )
    audit(conn, "erpclaw-projects", "add-project", "project", project_id,
           new_values={"project_name": args.name, "company_id": args.company_id})
    conn.commit()

    project = conn.execute("SELECT * FROM project WHERE id = ?", (project_id,)).fetchone()
    ok({"project": row_to_dict(project)})


# ---------------------------------------------------------------------------
# 2. update-project
# ---------------------------------------------------------------------------

def update_project(conn, args):
    """Update project fields.

    Required: --project-id
    Optional: --name, --customer-id, --project-type, --status, --priority,
              --start-date, --end-date, --estimated-cost, --billing-type,
              --cost-center-id, --percent-complete, --actual-cost, --total-billed,
              --description
    """
    if not args.project_id:
        err("--project-id is required")

    project = _validate_project_exists(conn, args.project_id)
    old_values = row_to_dict(project)

    updates = []
    params = []

    if args.name is not None:
        updates.append("project_name = ?")
        params.append(args.name)

    if args.customer_id is not None:
        if args.customer_id:
            _validate_customer_exists(conn, args.customer_id)
        updates.append("customer_id = ?")
        params.append(args.customer_id if args.customer_id else None)

    if args.project_type is not None:
        if args.project_type not in VALID_PROJECT_TYPES:
            err(f"Invalid --project-type: {args.project_type}. Must be one of {VALID_PROJECT_TYPES}")
        updates.append("project_type = ?")
        params.append(args.project_type)

    if args.status is not None:
        if args.status not in VALID_PROJECT_STATUSES:
            err(f"Invalid --status: {args.status}. Must be one of {VALID_PROJECT_STATUSES}")
        updates.append("status = ?")
        params.append(args.status)

    if args.priority is not None:
        if args.priority not in VALID_PROJECT_PRIORITIES:
            err(f"Invalid --priority: {args.priority}. Must be one of {VALID_PROJECT_PRIORITIES}")
        updates.append("priority = ?")
        params.append(args.priority)

    if args.start_date is not None:
        updates.append("start_date = ?")
        params.append(args.start_date)

    if args.end_date is not None:
        updates.append("end_date = ?")
        params.append(args.end_date)

    if args.estimated_cost is not None:
        val = str(round_currency(to_decimal(args.estimated_cost)))
        if to_decimal(val) < 0:
            err("--estimated-cost must be >= 0")
        updates.append("estimated_cost = ?")
        params.append(val)

    if args.actual_cost is not None:
        val = str(round_currency(to_decimal(args.actual_cost)))
        if to_decimal(val) < 0:
            err("--actual-cost must be >= 0")
        updates.append("actual_cost = ?")
        params.append(val)

    if args.total_billed is not None:
        val = str(round_currency(to_decimal(args.total_billed)))
        if to_decimal(val) < 0:
            err("--total-billed must be >= 0")
        updates.append("total_billed = ?")
        params.append(val)

    if args.billing_type is not None:
        if args.billing_type not in VALID_BILLING_TYPES:
            err(f"Invalid --billing-type: {args.billing_type}. Must be one of {VALID_BILLING_TYPES}")
        updates.append("billing_type = ?")
        params.append(args.billing_type)

    if args.cost_center_id is not None:
        if args.cost_center_id:
            cc = conn.execute(
                "SELECT id FROM cost_center WHERE id = ? OR name = ?",
                (args.cost_center_id, args.cost_center_id),
            ).fetchone()
            if not cc:
                err(f"Cost center {args.cost_center_id} not found")
            args.cost_center_id = cc["id"]
        updates.append("cost_center_id = ?")
        params.append(args.cost_center_id if args.cost_center_id else None)

    if args.percent_complete is not None:
        val = to_decimal(args.percent_complete)
        if val < 0 or val > Decimal("100"):
            err("--percent-complete must be between 0 and 100")
        updates.append("percent_complete = ?")
        params.append(str(round_currency(val)))

    if not updates:
        err("No fields to update")

    # Recalculate profit_margin if actual_cost or total_billed changed
    # We always recalculate from the resulting values
    updates.append("updated_at = datetime('now')")

    sql = f"UPDATE project SET {', '.join(updates)} WHERE id = ?"
    params.append(args.project_id)
    conn.execute(sql, params)

    # Recalculate profit_margin from current values
    updated = conn.execute("SELECT * FROM project WHERE id = ?", (args.project_id,)).fetchone()
    total_billed = to_decimal(updated["total_billed"])
    actual_cost = to_decimal(updated["actual_cost"])
    if total_billed > 0:
        profit_margin = round_currency(
            ((total_billed - actual_cost) / total_billed) * Decimal("100")
        )
    else:
        profit_margin = Decimal("0")

    conn.execute(
        "UPDATE project SET profit_margin = ? WHERE id = ?",
        (str(profit_margin), args.project_id),
    )

    audit(conn, "erpclaw-projects", "update-project", "project", args.project_id,
           old_values=old_values, description="Project updated")
    conn.commit()

    project = conn.execute("SELECT * FROM project WHERE id = ?", (args.project_id,)).fetchone()
    ok({"project": row_to_dict(project)})


# ---------------------------------------------------------------------------
# 3. get-project
# ---------------------------------------------------------------------------

def get_project(conn, args):
    """Return project with tasks, milestones, and timesheet summary.

    Required: --project-id
    """
    if not args.project_id:
        err("--project-id is required")

    project = _validate_project_exists(conn, args.project_id)
    project_dict = row_to_dict(project)

    # Fetch tasks
    tasks = conn.execute(
        "SELECT * FROM task WHERE project_id = ? ORDER BY start_date, task_name",
        (args.project_id,),
    ).fetchall()
    project_dict["tasks"] = [row_to_dict(t) for t in tasks]

    # Fetch milestones
    milestones = conn.execute(
        "SELECT * FROM milestone WHERE project_id = ? ORDER BY target_date",
        (args.project_id,),
    ).fetchall()
    project_dict["milestones"] = [row_to_dict(m) for m in milestones]

    # Timesheet summary: aggregate from timesheet_detail for this project
    ts_summary = conn.execute(
        """SELECT
               COALESCE(decimal_sum(td.hours), '0') AS total_hours,
               COALESCE(SUM(CASE WHEN td.billable = 1 THEN td.hours + 0 ELSE 0 END), 0) AS billable_hours,
               COALESCE(SUM(CASE WHEN td.billable = 1 THEN (td.hours + 0) * (td.billing_rate + 0) ELSE 0 END), 0) AS billable_amount,
               COUNT(DISTINCT td.timesheet_id) AS timesheet_count
           FROM timesheet_detail td
           JOIN timesheet ts ON td.timesheet_id = ts.id
           WHERE td.project_id = ? AND ts.status IN ('submitted', 'billed')""",
        (args.project_id,),
    ).fetchone()
    project_dict["timesheet_summary"] = {
        "total_hours": str(round_currency(to_decimal(str(ts_summary["total_hours"])))),
        "billable_hours": str(round_currency(to_decimal(str(ts_summary["billable_hours"])))),
        "billable_amount": str(round_currency(to_decimal(str(ts_summary["billable_amount"])))),
        "timesheet_count": ts_summary["timesheet_count"],
    }

    ok({"project": project_dict})


# ---------------------------------------------------------------------------
# 4. list-projects
# ---------------------------------------------------------------------------

def list_projects(conn, args):
    """List projects with optional filters.

    Optional: --company-id, --status, --from-date, --to-date, --search,
              --customer-id, --limit, --offset
    """
    conditions = []
    params = []

    if args.company_id:
        conditions.append("company_id = ?")
        params.append(args.company_id)

    if args.status:
        if args.status not in VALID_PROJECT_STATUSES:
            err(f"Invalid --status: {args.status}. Must be one of {VALID_PROJECT_STATUSES}")
        conditions.append("status = ?")
        params.append(args.status)

    if args.customer_id:
        conditions.append("customer_id = ?")
        params.append(args.customer_id)

    if args.from_date:
        conditions.append("start_date >= ?")
        params.append(args.from_date)

    if args.to_date:
        conditions.append("(end_date <= ? OR end_date IS NULL)")
        params.append(args.to_date)

    if args.search:
        conditions.append("project_name LIKE ?")
        params.append(f"%{args.search}%")

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    limit = int(args.limit or "20")
    offset = int(args.offset or "0")

    # Count
    count_row = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM project {where}", params,
    ).fetchone()
    total = count_row["cnt"]

    # Fetch
    rows = conn.execute(
        f"SELECT * FROM project {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()

    ok({
        "projects": [row_to_dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < total,
    })


# ---------------------------------------------------------------------------
# 5. add-task
# ---------------------------------------------------------------------------

def add_task(conn, args):
    """Create a task under a project.

    Required: --project-id, --name
    Optional: --assigned-to, --priority, --status, --start-date, --end-date,
              --estimated-hours, --depends-on (JSON array of task IDs),
              --parent-task-id, --description
    """
    if not args.project_id:
        err("--project-id is required")
    if not args.name:
        err("--name is required")

    project = _validate_project_exists(conn, args.project_id)

    status = args.status or "open"
    if status not in VALID_TASK_STATUSES:
        err(f"Invalid --status: {status}. Must be one of {VALID_TASK_STATUSES}")

    priority = args.priority or "medium"
    if priority not in VALID_TASK_PRIORITIES:
        err(f"Invalid --priority: {priority}. Must be one of {VALID_TASK_PRIORITIES}")

    estimated_hours = "0"
    if args.estimated_hours:
        estimated_hours = str(round_currency(to_decimal(args.estimated_hours)))
        if to_decimal(estimated_hours) < 0:
            err("--estimated-hours must be >= 0")

    # Validate parent task if provided
    if args.parent_task_id:
        parent = _validate_task_exists(conn, args.parent_task_id)
        if parent["project_id"] != args.project_id:
            err(f"Parent task {args.parent_task_id} belongs to a different project")

    # Validate depends_on task IDs
    depends_on = None
    if args.depends_on:
        dep_ids = _parse_json_arg(args.depends_on, "depends-on")
        if not isinstance(dep_ids, list):
            err("--depends-on must be a JSON array of task IDs")
        for dep_id in dep_ids:
            dep_task = conn.execute(
                "SELECT id, project_id FROM task WHERE id = ?", (dep_id,),
            ).fetchone()
            if not dep_task:
                err(f"Dependency task {dep_id} not found")
            if dep_task["project_id"] != args.project_id:
                err(f"Dependency task {dep_id} belongs to a different project")
        depends_on = json.dumps(dep_ids)

    task_id = str(uuid.uuid4())
    # Use project's company_id for naming
    naming = get_next_name(conn, "task", company_id=project["company_id"])

    conn.execute(
        """INSERT INTO task
           (id, naming_series, project_id, task_name, parent_task_id,
            assigned_to, status, priority, start_date, end_date,
            estimated_hours, actual_hours, depends_on, description)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '0', ?, ?)""",
        (task_id, naming, args.project_id, args.name, args.parent_task_id,
         args.assigned_to, status, priority, args.start_date, args.end_date,
         estimated_hours, depends_on, args.description),
    )
    audit(conn, "erpclaw-projects", "add-task", "task", task_id,
           new_values={"task_name": args.name, "project_id": args.project_id})
    conn.commit()

    task = conn.execute("SELECT * FROM task WHERE id = ?", (task_id,)).fetchone()
    ok({"task": row_to_dict(task)})


# ---------------------------------------------------------------------------
# 6. update-task
# ---------------------------------------------------------------------------

def update_task(conn, args):
    """Update task fields.

    Required: --task-id
    Optional: --name, --assigned-to, --priority, --status, --start-date,
              --end-date, --estimated-hours, --actual-hours, --depends-on,
              --description
    """
    if not args.task_id:
        err("--task-id is required")

    task = _validate_task_exists(conn, args.task_id)
    old_values = row_to_dict(task)

    updates = []
    params = []

    if args.name is not None:
        updates.append("task_name = ?")
        params.append(args.name)

    if args.assigned_to is not None:
        updates.append("assigned_to = ?")
        params.append(args.assigned_to if args.assigned_to else None)

    if args.priority is not None:
        if args.priority not in VALID_TASK_PRIORITIES:
            err(f"Invalid --priority: {args.priority}. Must be one of {VALID_TASK_PRIORITIES}")
        updates.append("priority = ?")
        params.append(args.priority)

    if args.status is not None:
        if args.status not in VALID_TASK_STATUSES:
            err(f"Invalid --status: {args.status}. Must be one of {VALID_TASK_STATUSES}")
        updates.append("status = ?")
        params.append(args.status)

    if args.start_date is not None:
        updates.append("start_date = ?")
        params.append(args.start_date)

    if args.end_date is not None:
        updates.append("end_date = ?")
        params.append(args.end_date)

    if args.estimated_hours is not None:
        val = str(round_currency(to_decimal(args.estimated_hours)))
        if to_decimal(val) < 0:
            err("--estimated-hours must be >= 0")
        updates.append("estimated_hours = ?")
        params.append(val)

    if args.actual_hours is not None:
        val = str(round_currency(to_decimal(args.actual_hours)))
        if to_decimal(val) < 0:
            err("--actual-hours must be >= 0")
        updates.append("actual_hours = ?")
        params.append(val)

    if args.depends_on is not None:
        if args.depends_on == "":
            updates.append("depends_on = ?")
            params.append(None)
        else:
            dep_ids = _parse_json_arg(args.depends_on, "depends-on")
            if not isinstance(dep_ids, list):
                err("--depends-on must be a JSON array of task IDs")
            project_id = task["project_id"]
            for dep_id in dep_ids:
                dep_task = conn.execute(
                    "SELECT id, project_id FROM task WHERE id = ?", (dep_id,),
                ).fetchone()
                if not dep_task:
                    err(f"Dependency task {dep_id} not found")
                if dep_task["project_id"] != project_id:
                    err(f"Dependency task {dep_id} belongs to a different project")
            updates.append("depends_on = ?")
            params.append(json.dumps(dep_ids))

    if args.description is not None:
        updates.append("description = ?")
        params.append(args.description if args.description else None)

    if not updates:
        err("No fields to update")

    updates.append("updated_at = datetime('now')")

    sql = f"UPDATE task SET {', '.join(updates)} WHERE id = ?"
    params.append(args.task_id)
    conn.execute(sql, params)

    audit(conn, "erpclaw-projects", "update-task", "task", args.task_id,
           old_values=old_values, description="Task updated")
    conn.commit()

    task = conn.execute("SELECT * FROM task WHERE id = ?", (args.task_id,)).fetchone()
    ok({"task": row_to_dict(task)})


# ---------------------------------------------------------------------------
# 7. list-tasks
# ---------------------------------------------------------------------------

def list_tasks(conn, args):
    """List tasks for a project with optional filters.

    Required: --project-id
    Optional: --status, --assigned-to, --priority, --limit, --offset
    """
    if not args.project_id:
        err("--project-id is required")

    _validate_project_exists(conn, args.project_id)

    conditions = ["project_id = ?"]
    params = [args.project_id]

    if args.status:
        if args.status not in VALID_TASK_STATUSES:
            err(f"Invalid --status: {args.status}. Must be one of {VALID_TASK_STATUSES}")
        conditions.append("status = ?")
        params.append(args.status)

    if args.assigned_to:
        conditions.append("assigned_to = ?")
        params.append(args.assigned_to)

    if args.priority:
        if args.priority not in VALID_TASK_PRIORITIES:
            err(f"Invalid --priority: {args.priority}. Must be one of {VALID_TASK_PRIORITIES}")
        conditions.append("priority = ?")
        params.append(args.priority)

    where = f"WHERE {' AND '.join(conditions)}"
    limit = int(args.limit or "20")
    offset = int(args.offset or "0")

    count_row = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM task {where}", params,
    ).fetchone()
    total = count_row["cnt"]

    rows = conn.execute(
        f"SELECT * FROM task {where} ORDER BY start_date, task_name LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()

    ok({
        "tasks": [row_to_dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < total,
    })


# ---------------------------------------------------------------------------
# 8. add-milestone
# ---------------------------------------------------------------------------

def add_milestone(conn, args):
    """Create a milestone for a project.

    Required: --project-id, --name, --target-date
    Optional: --description
    """
    if not args.project_id:
        err("--project-id is required")
    if not args.name:
        err("--name is required")
    if not args.target_date:
        err("--target-date is required")

    _validate_project_exists(conn, args.project_id)

    milestone_id = str(uuid.uuid4())

    conn.execute(
        """INSERT INTO milestone
           (id, project_id, milestone_name, target_date, status, description)
           VALUES (?, ?, ?, ?, 'pending', ?)""",
        (milestone_id, args.project_id, args.name, args.target_date,
         args.description),
    )
    audit(conn, "erpclaw-projects", "add-milestone", "milestone", milestone_id,
           new_values={"milestone_name": args.name, "project_id": args.project_id})
    conn.commit()

    milestone = conn.execute(
        "SELECT * FROM milestone WHERE id = ?", (milestone_id,),
    ).fetchone()
    ok({"milestone": row_to_dict(milestone)})


# ---------------------------------------------------------------------------
# 9. update-milestone
# ---------------------------------------------------------------------------

def update_milestone(conn, args):
    """Update milestone fields.

    Required: --milestone-id
    Optional: --name, --target-date, --completion-date, --status, --description
    """
    if not args.milestone_id:
        err("--milestone-id is required")

    milestone = _validate_milestone_exists(conn, args.milestone_id)
    old_values = row_to_dict(milestone)

    updates = []
    params = []

    if args.name is not None:
        updates.append("milestone_name = ?")
        params.append(args.name)

    if args.target_date is not None:
        updates.append("target_date = ?")
        params.append(args.target_date)

    if args.completion_date is not None:
        updates.append("completion_date = ?")
        params.append(args.completion_date if args.completion_date else None)

    if args.status is not None:
        if args.status not in VALID_MILESTONE_STATUSES:
            err(f"Invalid --status: {args.status}. Must be one of {VALID_MILESTONE_STATUSES}")
        updates.append("status = ?")
        params.append(args.status)

    if args.description is not None:
        updates.append("description = ?")
        params.append(args.description if args.description else None)

    if not updates:
        err("No fields to update")

    updates.append("updated_at = datetime('now')")

    sql = f"UPDATE milestone SET {', '.join(updates)} WHERE id = ?"
    params.append(args.milestone_id)
    conn.execute(sql, params)

    audit(conn, "erpclaw-projects", "update-milestone", "milestone", args.milestone_id,
           old_values=old_values, description="Milestone updated")
    conn.commit()

    milestone = conn.execute(
        "SELECT * FROM milestone WHERE id = ?", (args.milestone_id,),
    ).fetchone()
    ok({"milestone": row_to_dict(milestone)})


# ---------------------------------------------------------------------------
# 10. add-timesheet
# ---------------------------------------------------------------------------

def add_timesheet(conn, args):
    """Create a draft timesheet with line items.

    Required: --company-id, --employee-id, --start-date, --end-date,
              --items (JSON array of detail rows)
    Each item: {project_id, task_id?, activity_type?, hours, billing_rate?,
                billable?, description?, date}
    """
    if not args.company_id:
        err("--company-id is required")
    if not args.employee_id:
        err("--employee-id is required")
    if not args.start_date:
        err("--start-date is required")
    if not args.end_date:
        err("--end-date is required")
    if not args.items:
        err("--items is required (JSON array of timesheet detail rows)")

    _validate_company_exists(conn, args.company_id)
    _validate_employee_exists(conn, args.employee_id)

    items = _parse_json_arg(args.items, "items")
    if not items or not isinstance(items, list):
        err("--items must be a non-empty JSON array")

    timesheet_id = str(uuid.uuid4())
    naming = get_next_name(conn, "timesheet", company_id=args.company_id)

    total_hours = Decimal("0")
    total_billable_hours = Decimal("0")
    total_billable_amount = Decimal("0")
    total_cost = Decimal("0")
    detail_rows = []

    for i, item in enumerate(items):
        # Validate project_id
        proj_id = item.get("project_id")
        if not proj_id:
            err(f"Item {i}: project_id is required")
        _validate_project_exists(conn, proj_id)

        # Validate task_id if provided
        task_id = item.get("task_id")
        if task_id:
            task = _validate_task_exists(conn, task_id)
            if task["project_id"] != proj_id:
                err(f"Item {i}: task {task_id} does not belong to project {proj_id}")

        # Validate activity_type
        activity_type = item.get("activity_type")
        if activity_type and activity_type not in VALID_ACTIVITY_TYPES:
            err(f"Item {i}: invalid activity_type '{activity_type}'. Must be one of {VALID_ACTIVITY_TYPES}")

        # Parse hours
        hours = to_decimal(item.get("hours", "0"))
        if hours <= 0:
            err(f"Item {i}: hours must be > 0")

        # Parse billing rate and billable flag
        billing_rate = to_decimal(item.get("billing_rate", "0"))
        billable = int(item.get("billable", 1))
        if billable not in (0, 1):
            err(f"Item {i}: billable must be 0 or 1")

        # Validate date
        item_date = item.get("date")
        if not item_date:
            err(f"Item {i}: date is required")

        detail_id = str(uuid.uuid4())
        detail_rows.append({
            "id": detail_id,
            "timesheet_id": timesheet_id,
            "project_id": proj_id,
            "task_id": task_id,
            "activity_type": activity_type,
            "hours": str(round_currency(hours)),
            "billing_rate": str(round_currency(billing_rate)),
            "billable": billable,
            "description": item.get("description"),
            "date": item_date,
        })

        total_hours += hours
        if billable:
            total_billable_hours += hours
            total_billable_amount += round_currency(hours * billing_rate)
        # Cost = hours * billing_rate (or could be costing rate; for simplicity cost = billable amount)
        total_cost += round_currency(hours * billing_rate)

    total_hours = round_currency(total_hours)
    total_billable_hours = round_currency(total_billable_hours)
    total_billable_amount = round_currency(total_billable_amount)
    total_cost = round_currency(total_cost)

    # Insert timesheet header
    conn.execute(
        """INSERT INTO timesheet
           (id, naming_series, employee_id, start_date, end_date,
            total_hours, total_billable_hours, total_billed_hours,
            total_cost, total_billable_amount, status, company_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, '0', ?, ?, 'draft', ?)""",
        (timesheet_id, naming, args.employee_id, args.start_date,
         args.end_date, str(total_hours), str(total_billable_hours),
         str(total_cost), str(total_billable_amount), args.company_id),
    )

    # Insert detail rows
    for d in detail_rows:
        conn.execute(
            """INSERT INTO timesheet_detail
               (id, timesheet_id, project_id, task_id, activity_type,
                hours, billing_rate, billable, description, date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (d["id"], d["timesheet_id"], d["project_id"], d["task_id"],
             d["activity_type"], d["hours"], d["billing_rate"], d["billable"],
             d["description"], d["date"]),
        )

    audit(conn, "erpclaw-projects", "add-timesheet", "timesheet", timesheet_id,
           new_values={"employee_id": args.employee_id,
                       "total_hours": str(total_hours),
                       "items_count": len(detail_rows)})
    conn.commit()

    ts = conn.execute("SELECT * FROM timesheet WHERE id = ?", (timesheet_id,)).fetchone()
    details = conn.execute(
        "SELECT * FROM timesheet_detail WHERE timesheet_id = ? ORDER BY date",
        (timesheet_id,),
    ).fetchall()

    result = row_to_dict(ts)
    result["items"] = [row_to_dict(d) for d in details]
    ok({"timesheet": result})


# ---------------------------------------------------------------------------
# 11. get-timesheet
# ---------------------------------------------------------------------------

def get_timesheet(conn, args):
    """Return timesheet with detail rows.

    Required: --timesheet-id
    """
    if not args.timesheet_id:
        err("--timesheet-id is required")

    ts = _validate_timesheet_exists(conn, args.timesheet_id)
    result = row_to_dict(ts)

    details = conn.execute(
        "SELECT * FROM timesheet_detail WHERE timesheet_id = ? ORDER BY date",
        (args.timesheet_id,),
    ).fetchall()
    result["items"] = [row_to_dict(d) for d in details]

    # Include employee name
    emp = conn.execute(
        "SELECT full_name AS employee_name FROM employee WHERE id = ?", (ts["employee_id"],),
    ).fetchone()
    if emp:
        result["employee_name"] = emp["employee_name"]

    ok({"timesheet": result})


# ---------------------------------------------------------------------------
# 12. list-timesheets
# ---------------------------------------------------------------------------

def list_timesheets(conn, args):
    """List timesheets with optional filters.

    Optional: --company-id, --employee-id, --project-id, --status,
              --from-date, --to-date, --limit, --offset
    """
    conditions = []
    params = []

    if args.company_id:
        conditions.append("ts.company_id = ?")
        params.append(args.company_id)

    if args.employee_id:
        conditions.append("ts.employee_id = ?")
        params.append(args.employee_id)

    if args.status:
        if args.status not in VALID_TIMESHEET_STATUSES:
            err(f"Invalid --status: {args.status}. Must be one of {VALID_TIMESHEET_STATUSES}")
        conditions.append("ts.status = ?")
        params.append(args.status)

    if args.from_date:
        conditions.append("ts.start_date >= ?")
        params.append(args.from_date)

    if args.to_date:
        conditions.append("ts.end_date <= ?")
        params.append(args.to_date)

    # If --project-id filter, join to detail to find timesheets for that project
    join_clause = ""
    if args.project_id:
        join_clause = "JOIN timesheet_detail td ON ts.id = td.timesheet_id"
        conditions.append("td.project_id = ?")
        params.append(args.project_id)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    limit = int(args.limit or "20")
    offset = int(args.offset or "0")

    # Use DISTINCT when joining to detail
    distinct = "DISTINCT" if args.project_id else ""

    count_row = conn.execute(
        f"SELECT COUNT({distinct} ts.id) AS cnt FROM timesheet ts {join_clause} {where}",
        params,
    ).fetchone()
    total = count_row["cnt"]

    rows = conn.execute(
        f"""SELECT {distinct} ts.* FROM timesheet ts {join_clause} {where}
            ORDER BY ts.created_at DESC LIMIT ? OFFSET ?""",
        params + [limit, offset],
    ).fetchall()

    ok({
        "timesheets": [row_to_dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < total,
    })


# ---------------------------------------------------------------------------
# 13. submit-timesheet
# ---------------------------------------------------------------------------

def submit_timesheet(conn, args):
    """Move timesheet from draft to submitted.

    Required: --timesheet-id
    Validates all detail rows have valid project_ids and hours > 0.
    Updates task actual_hours by adding timesheet hours.
    """
    if not args.timesheet_id:
        err("--timesheet-id is required")

    ts = _validate_timesheet_exists(conn, args.timesheet_id)
    if ts["status"] != "draft":
        err(f"Timesheet is '{ts['status']}', can only submit from 'draft'")

    # Validate detail rows
    details = conn.execute(
        "SELECT * FROM timesheet_detail WHERE timesheet_id = ?",
        (args.timesheet_id,),
    ).fetchall()
    if not details:
        err("Timesheet has no detail rows")

    for d in details:
        d_dict = row_to_dict(d)
        # Validate project exists
        proj = conn.execute(
            "SELECT id FROM project WHERE id = ?", (d_dict["project_id"],),
        ).fetchone()
        if not proj:
            err(f"Detail row {d_dict['id']}: project {d_dict['project_id']} not found")

        hours = to_decimal(d_dict["hours"])
        if hours <= 0:
            err(f"Detail row {d_dict['id']}: hours must be > 0, got {hours}")

    # Update status
    conn.execute(
        "UPDATE timesheet SET status = 'submitted', updated_at = datetime('now') WHERE id = ?",
        (args.timesheet_id,),
    )

    # Update task actual_hours for each detail row that references a task
    for d in details:
        d_dict = row_to_dict(d)
        task_id = d_dict.get("task_id")
        if task_id:
            hours = to_decimal(d_dict["hours"])
            task = conn.execute(
                "SELECT actual_hours FROM task WHERE id = ?", (task_id,),
            ).fetchone()
            if task:
                new_hours = round_currency(to_decimal(task["actual_hours"]) + hours)
                conn.execute(
                    "UPDATE task SET actual_hours = ?, updated_at = datetime('now') WHERE id = ?",
                    (str(new_hours), task_id),
                )

    audit(conn, "erpclaw-projects", "submit-timesheet", "timesheet", args.timesheet_id,
           old_values={"status": "draft"}, new_values={"status": "submitted"},
           description="Timesheet submitted")
    conn.commit()

    ts = conn.execute("SELECT * FROM timesheet WHERE id = ?", (args.timesheet_id,)).fetchone()
    ok({"timesheet": row_to_dict(ts)})


# ---------------------------------------------------------------------------
# 14. bill-timesheet
# ---------------------------------------------------------------------------

def bill_timesheet(conn, args):
    """Move timesheet from submitted to billed.

    Required: --timesheet-id
    Marks total_billed_hours = total_billable_hours.
    Updates project total_billed and actual_cost.
    """
    if not args.timesheet_id:
        err("--timesheet-id is required")

    ts = _validate_timesheet_exists(conn, args.timesheet_id)
    if ts["status"] != "submitted":
        err(f"Timesheet is '{ts['status']}', can only bill from 'submitted'")

    total_billable_hours = to_decimal(ts["total_billable_hours"])
    total_billable_amount = to_decimal(ts["total_billable_amount"])
    total_cost = to_decimal(ts["total_cost"])

    # Mark as billed, set total_billed_hours
    conn.execute(
        """UPDATE timesheet SET
           status = 'billed',
           total_billed_hours = ?,
           updated_at = datetime('now')
           WHERE id = ?""",
        (str(total_billable_hours), args.timesheet_id),
    )

    # Aggregate amounts per project from detail rows and update each project
    details = conn.execute(
        "SELECT * FROM timesheet_detail WHERE timesheet_id = ?",
        (args.timesheet_id,),
    ).fetchall()

    project_costs = {}  # project_id -> {cost, billed}
    for d in details:
        d_dict = row_to_dict(d)
        proj_id = d_dict["project_id"]
        hours = to_decimal(d_dict["hours"])
        rate = to_decimal(d_dict["billing_rate"])
        billable = int(d_dict["billable"])

        if proj_id not in project_costs:
            project_costs[proj_id] = {"cost": Decimal("0"), "billed": Decimal("0")}

        line_amount = round_currency(hours * rate)
        project_costs[proj_id]["cost"] += line_amount
        if billable:
            project_costs[proj_id]["billed"] += line_amount

    for proj_id, amounts in project_costs.items():
        project = conn.execute(
            "SELECT actual_cost, total_billed FROM project WHERE id = ?",
            (proj_id,),
        ).fetchone()
        if project:
            new_actual = round_currency(
                to_decimal(project["actual_cost"]) + amounts["cost"]
            )
            new_billed = round_currency(
                to_decimal(project["total_billed"]) + amounts["billed"]
            )
            # Recalculate profit margin
            if new_billed > 0:
                profit_margin = round_currency(
                    ((new_billed - new_actual) / new_billed) * Decimal("100")
                )
            else:
                profit_margin = Decimal("0")

            conn.execute(
                """UPDATE project SET
                   actual_cost = ?, total_billed = ?, profit_margin = ?,
                   updated_at = datetime('now')
                   WHERE id = ?""",
                (str(new_actual), str(new_billed), str(profit_margin), proj_id),
            )

    audit(conn, "erpclaw-projects", "bill-timesheet", "timesheet", args.timesheet_id,
           old_values={"status": "submitted"},
           new_values={"status": "billed",
                       "total_billed_hours": str(total_billable_hours)},
           description="Timesheet billed")
    conn.commit()

    ts = conn.execute("SELECT * FROM timesheet WHERE id = ?", (args.timesheet_id,)).fetchone()
    ok({"timesheet": row_to_dict(ts)})


# ---------------------------------------------------------------------------
# 15. project-profitability
# ---------------------------------------------------------------------------

def project_profitability(conn, args):
    """Project profitability report.

    Required: --project-id
    Returns estimated_cost, actual_cost, total_billed, profit, margin %,
    and breakdown by employee from timesheets.
    """
    if not args.project_id:
        err("--project-id is required")

    project = _validate_project_exists(conn, args.project_id)
    p = row_to_dict(project)

    estimated_cost = to_decimal(p["estimated_cost"])
    actual_cost = to_decimal(p["actual_cost"])
    total_billed = to_decimal(p["total_billed"])
    profit = round_currency(total_billed - actual_cost)
    margin = Decimal("0")
    if total_billed > 0:
        margin = round_currency((profit / total_billed) * Decimal("100"))

    # Employee breakdown from submitted/billed timesheets
    employee_rows = conn.execute(
        """SELECT
               ts.employee_id,
               e.full_name AS employee_name,
               decimal_sum(td.hours) AS total_hours,
               SUM(CASE WHEN td.billable = 1 THEN td.hours + 0 ELSE 0 END) AS billable_hours,
               SUM((td.hours + 0) * (td.billing_rate + 0)) AS total_cost,
               SUM(CASE WHEN td.billable = 1 THEN (td.hours + 0) * (td.billing_rate + 0) ELSE 0 END) AS billable_amount
           FROM timesheet_detail td
           JOIN timesheet ts ON td.timesheet_id = ts.id
           LEFT JOIN employee e ON ts.employee_id = e.id
           WHERE td.project_id = ? AND ts.status IN ('submitted', 'billed')
           GROUP BY ts.employee_id
           ORDER BY total_hours DESC""",
        (args.project_id,),
    ).fetchall()

    employees = []
    for row in employee_rows:
        r = row_to_dict(row)
        employees.append({
            "employee_id": r["employee_id"],
            "employee_name": r["employee_name"],
            "total_hours": str(round_currency(to_decimal(str(r["total_hours"])))),
            "billable_hours": str(round_currency(to_decimal(str(r["billable_hours"])))),
            "total_cost": str(round_currency(to_decimal(str(r["total_cost"])))),
            "billable_amount": str(round_currency(to_decimal(str(r["billable_amount"])))),
        })

    # Cost variance
    cost_variance = round_currency(estimated_cost - actual_cost)

    ok({
        "project_id": args.project_id,
        "project_name": p["project_name"],
        "estimated_cost": str(estimated_cost),
        "actual_cost": str(actual_cost),
        "total_billed": str(total_billed),
        "profit": str(profit),
        "margin_percent": str(margin),
        "cost_variance": str(cost_variance),
        "employees": employees,
    })


# ---------------------------------------------------------------------------
# 16. gantt-data
# ---------------------------------------------------------------------------

def gantt_data(conn, args):
    """Return tasks with dependencies for Gantt chart rendering.

    Required: --project-id
    Returns list of tasks with id, name, start_date, end_date, depends_on,
    status, assigned_to.
    """
    if not args.project_id:
        err("--project-id is required")

    project = _validate_project_exists(conn, args.project_id)

    tasks = conn.execute(
        """SELECT id, task_name, start_date, end_date, depends_on,
                  status, assigned_to, priority, estimated_hours, actual_hours,
                  parent_task_id
           FROM task WHERE project_id = ?
           ORDER BY start_date, task_name""",
        (args.project_id,),
    ).fetchall()

    gantt_items = []
    for t in tasks:
        td = row_to_dict(t)
        depends = None
        if td["depends_on"]:
            try:
                depends = json.loads(td["depends_on"])
            except (json.JSONDecodeError, TypeError):
                depends = None

        gantt_items.append({
            "id": td["id"],
            "name": td["task_name"],
            "start_date": td["start_date"],
            "end_date": td["end_date"],
            "depends_on": depends,
            "status": td["status"],
            "assigned_to": td["assigned_to"],
            "priority": td["priority"],
            "estimated_hours": td["estimated_hours"],
            "actual_hours": td["actual_hours"],
            "parent_task_id": td["parent_task_id"],
        })

    # Include milestones as markers
    milestones = conn.execute(
        """SELECT id, milestone_name, target_date, completion_date, status
           FROM milestone WHERE project_id = ?
           ORDER BY target_date""",
        (args.project_id,),
    ).fetchall()

    milestone_items = []
    for m in milestones:
        md = row_to_dict(m)
        milestone_items.append({
            "id": md["id"],
            "name": md["milestone_name"],
            "target_date": md["target_date"],
            "completion_date": md["completion_date"],
            "status": md["status"],
        })

    ok({
        "project_id": args.project_id,
        "project_name": row_to_dict(project)["project_name"],
        "start_date": row_to_dict(project)["start_date"],
        "end_date": row_to_dict(project)["end_date"],
        "tasks": gantt_items,
        "milestones": milestone_items,
    })


# ---------------------------------------------------------------------------
# 17. resource-utilization
# ---------------------------------------------------------------------------

def resource_utilization(conn, args):
    """Resource utilization report — hours by employee across projects.

    Required: --company-id
    Optional: --from-date, --to-date
    Returns total hours, billable hours, utilization % per employee.
    """
    if not args.company_id:
        err("--company-id is required")

    _validate_company_exists(conn, args.company_id)

    conditions = ["ts.company_id = ?", "ts.status IN ('submitted', 'billed')"]
    params = [args.company_id]

    if args.from_date:
        conditions.append("td.date >= ?")
        params.append(args.from_date)

    if args.to_date:
        conditions.append("td.date <= ?")
        params.append(args.to_date)

    where = f"WHERE {' AND '.join(conditions)}"

    rows = conn.execute(
        f"""SELECT
               ts.employee_id,
               e.full_name AS employee_name,
               decimal_sum(td.hours) AS total_hours,
               SUM(CASE WHEN td.billable = 1 THEN td.hours + 0 ELSE 0 END) AS billable_hours,
               SUM(CASE WHEN td.billable = 0 THEN td.hours + 0 ELSE 0 END) AS non_billable_hours,
               COUNT(DISTINCT td.project_id) AS project_count,
               SUM(CASE WHEN td.billable = 1 THEN (td.hours + 0) * (td.billing_rate + 0) ELSE 0 END) AS billable_amount
           FROM timesheet_detail td
           JOIN timesheet ts ON td.timesheet_id = ts.id
           LEFT JOIN employee e ON ts.employee_id = e.id
           {where}
           GROUP BY ts.employee_id
           ORDER BY total_hours DESC""",
        params,
    ).fetchall()

    employees = []
    for row in rows:
        r = row_to_dict(row)
        total_h = to_decimal(str(r["total_hours"]))
        billable_h = to_decimal(str(r["billable_hours"]))
        utilization = Decimal("0")
        if total_h > 0:
            utilization = round_currency((billable_h / total_h) * Decimal("100"))

        employees.append({
            "employee_id": r["employee_id"],
            "employee_name": r["employee_name"],
            "total_hours": str(round_currency(total_h)),
            "billable_hours": str(round_currency(billable_h)),
            "non_billable_hours": str(round_currency(to_decimal(str(r["non_billable_hours"])))),
            "utilization_percent": str(utilization),
            "project_count": r["project_count"],
            "billable_amount": str(round_currency(to_decimal(str(r["billable_amount"])))),
        })

    # Summary totals
    total_hours_all = sum(to_decimal(e["total_hours"]) for e in employees)
    total_billable_all = sum(to_decimal(e["billable_hours"]) for e in employees)
    overall_utilization = Decimal("0")
    if total_hours_all > 0:
        overall_utilization = round_currency(
            (total_billable_all / total_hours_all) * Decimal("100")
        )

    ok({
        "company_id": args.company_id,
        "from_date": args.from_date,
        "to_date": args.to_date,
        "employees": employees,
        "summary": {
            "total_employees": len(employees),
            "total_hours": str(round_currency(total_hours_all)),
            "total_billable_hours": str(round_currency(total_billable_all)),
            "overall_utilization_percent": str(overall_utilization),
        },
    })


# ---------------------------------------------------------------------------
# 18. status
# ---------------------------------------------------------------------------

def status_action(conn, args):
    """Dashboard status for a company.

    Required: --company-id
    Returns active projects count, overdue tasks, upcoming milestones,
    recent timesheets.
    """
    company_id = args.company_id
    if not company_id:
        row = conn.execute("SELECT id FROM company LIMIT 1").fetchone()
        if not row:
            err("No company found. Create one with erpclaw-setup first.",
                 suggestion="Run 'tutorial' to create a demo company, or 'setup company' to create your own.")
        company_id = row["id"]

    _validate_company_exists(conn, company_id)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Active projects (open or in_progress)
    active_projects = conn.execute(
        """SELECT COUNT(*) AS cnt FROM project
           WHERE company_id = ? AND status IN ('open', 'in_progress')""",
        (company_id,),
    ).fetchone()

    # Total projects by status
    project_by_status = conn.execute(
        """SELECT status, COUNT(*) AS cnt FROM project
           WHERE company_id = ? GROUP BY status""",
        (company_id,),
    ).fetchall()
    status_counts = {r["status"]: r["cnt"] for r in project_by_status}

    # Overdue tasks: open/in_progress tasks with end_date < today
    overdue_tasks = conn.execute(
        """SELECT t.id, t.task_name, t.end_date, t.assigned_to,
                  t.status, t.project_id, p.project_name
           FROM task t
           JOIN project p ON t.project_id = p.id
           WHERE p.company_id = ?
             AND t.status IN ('open', 'in_progress')
             AND t.end_date IS NOT NULL
             AND t.end_date < ?
           ORDER BY t.end_date
           LIMIT 20""",
        (company_id, today),
    ).fetchall()

    # Upcoming milestones: pending milestones within next 30 days
    upcoming_milestones = conn.execute(
        """SELECT m.id, m.milestone_name, m.target_date, m.status,
                  m.project_id, p.project_name
           FROM milestone m
           JOIN project p ON m.project_id = p.id
           WHERE p.company_id = ?
             AND m.status = 'pending'
             AND m.target_date >= ?
             AND m.target_date <= date(?, '+30 days')
           ORDER BY m.target_date
           LIMIT 20""",
        (company_id, today, today),
    ).fetchall()

    # Missed milestones: pending milestones past target date
    missed_milestones = conn.execute(
        """SELECT m.id, m.milestone_name, m.target_date,
                  m.project_id, p.project_name
           FROM milestone m
           JOIN project p ON m.project_id = p.id
           WHERE p.company_id = ?
             AND m.status = 'pending'
             AND m.target_date < ?
           ORDER BY m.target_date
           LIMIT 10""",
        (company_id, today),
    ).fetchall()

    # Recent timesheets (last 10)
    recent_timesheets = conn.execute(
        """SELECT ts.id, ts.naming_series, ts.employee_id, e.full_name AS employee_name,
                  ts.start_date, ts.end_date, ts.total_hours,
                  ts.total_billable_amount, ts.status
           FROM timesheet ts
           LEFT JOIN employee e ON ts.employee_id = e.id
           WHERE ts.company_id = ?
           ORDER BY ts.created_at DESC
           LIMIT 10""",
        (company_id,),
    ).fetchall()

    # Hours summary this month
    month_start = datetime.now(timezone.utc).strftime("%Y-%m-01")
    hours_this_month = conn.execute(
        """SELECT
               COALESCE(decimal_sum(td.hours), '0') AS total_hours,
               COALESCE(SUM(CASE WHEN td.billable = 1 THEN td.hours + 0 ELSE 0 END), 0) AS billable_hours
           FROM timesheet_detail td
           JOIN timesheet ts ON td.timesheet_id = ts.id
           WHERE ts.company_id = ?
             AND ts.status IN ('submitted', 'billed')
             AND td.date >= ?""",
        (company_id, month_start),
    ).fetchone()

    ok({
        "company_id": company_id,
        "date": today,
        "active_projects": active_projects["cnt"],
        "projects_by_status": status_counts,
        "overdue_tasks": [row_to_dict(r) for r in overdue_tasks],
        "overdue_tasks_count": len(overdue_tasks),
        "upcoming_milestones": [row_to_dict(r) for r in upcoming_milestones],
        "missed_milestones": [row_to_dict(r) for r in missed_milestones],
        "recent_timesheets": [row_to_dict(r) for r in recent_timesheets],
        "hours_this_month": {
            "total": str(round_currency(to_decimal(str(hours_this_month["total_hours"])))),
            "billable": str(round_currency(to_decimal(str(hours_this_month["billable_hours"])))),
        },
    })


# ---------------------------------------------------------------------------
# Action dispatch
# ---------------------------------------------------------------------------

ACTIONS = {
    "add-project": add_project,
    "update-project": update_project,
    "get-project": get_project,
    "list-projects": list_projects,
    "add-task": add_task,
    "update-task": update_task,
    "list-tasks": list_tasks,
    "add-milestone": add_milestone,
    "update-milestone": update_milestone,
    "add-timesheet": add_timesheet,
    "get-timesheet": get_timesheet,
    "list-timesheets": list_timesheets,
    "submit-timesheet": submit_timesheet,
    "bill-timesheet": bill_timesheet,
    "project-profitability": project_profitability,
    "gantt-data": gantt_data,
    "resource-utilization": resource_utilization,
    "status": status_action,
}


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="ERPClaw Projects Skill")
    parser.add_argument("--action", required=True, choices=sorted(ACTIONS.keys()))
    parser.add_argument("--db-path", default=None)

    # Entity IDs
    parser.add_argument("--company-id")
    parser.add_argument("--project-id")
    parser.add_argument("--task-id")
    parser.add_argument("--milestone-id")
    parser.add_argument("--timesheet-id")
    parser.add_argument("--employee-id")
    parser.add_argument("--customer-id")
    parser.add_argument("--cost-center-id")

    # Common fields
    parser.add_argument("--name")
    parser.add_argument("--description")

    # Project fields
    parser.add_argument("--project-type")
    parser.add_argument("--billing-type")
    parser.add_argument("--estimated-cost")
    parser.add_argument("--actual-cost")
    parser.add_argument("--total-billed")
    parser.add_argument("--percent-complete")

    # Task fields
    parser.add_argument("--assigned-to")
    parser.add_argument("--estimated-hours")
    parser.add_argument("--actual-hours")
    parser.add_argument("--depends-on")
    parser.add_argument("--parent-task-id")

    # Milestone fields
    parser.add_argument("--target-date")
    parser.add_argument("--completion-date")

    # Timesheet fields
    parser.add_argument("--items")  # JSON array

    # Dates
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")

    # Filters
    parser.add_argument("--status")
    parser.add_argument("--priority")
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    parser.add_argument("--search")
    parser.add_argument("--limit", default="20")
    parser.add_argument("--offset", default="0")

    args, _unknown = parser.parse_known_args()
    check_input_lengths(args)

    db_path = args.db_path or DEFAULT_DB_PATH
    ensure_db_exists(db_path)
    conn = get_connection(db_path)

    # Dependency check
    _dep = check_required_tables(conn, REQUIRED_TABLES)
    if _dep:
        _dep["suggestion"] = "clawhub install " + " ".join(_dep.get("missing_skills", []))
        print(json.dumps(_dep, indent=2))
        conn.close()
        sys.exit(1)

    try:
        ACTIONS[args.action](conn, args)
    except Exception as e:
        conn.rollback()
        sys.stderr.write(f"[erpclaw-projects] {e}\n")
        err("An unexpected error occurred")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
