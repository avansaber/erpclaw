"""Tests for attendance marking and holiday lists."""
import json
import pytest

from helpers import (
    _call_action,
    setup_hr_environment,
    create_test_employee,
)
from db_query import ACTIONS


# ---------------------------------------------------------------------------
# 1. test_mark_attendance
# ---------------------------------------------------------------------------

def test_mark_attendance(fresh_db):
    """Mark a single employee as present on a given date."""
    env = setup_hr_environment(fresh_db)
    emp_id = create_test_employee(fresh_db, env["company_id"], first_name="Alice")

    result = _call_action(
        ACTIONS["mark-attendance"], fresh_db,
        employee_id=emp_id,
        date="2026-03-15",
        status="present",
    )

    assert result["status"] == "ok"
    assert "attendance_id" in result
    assert result["employee_id"] == emp_id
    assert result["date"] == "2026-03-15"

    # Verify DB row
    att = fresh_db.execute(
        "SELECT * FROM attendance WHERE id = ?",
        (result["attendance_id"],),
    ).fetchone()
    assert att is not None
    assert att["employee_id"] == emp_id
    assert att["attendance_date"] == "2026-03-15"
    assert att["status"] == "present"


# ---------------------------------------------------------------------------
# 2. test_mark_attendance_duplicate
# ---------------------------------------------------------------------------

def test_mark_attendance_duplicate(fresh_db):
    """Marking the same employee+date twice should fail."""
    env = setup_hr_environment(fresh_db)
    emp_id = create_test_employee(fresh_db, env["company_id"], first_name="Bob")

    # First mark -- should succeed
    result1 = _call_action(
        ACTIONS["mark-attendance"], fresh_db,
        employee_id=emp_id,
        date="2026-03-15",
        status="present",
    )
    assert result1["status"] == "ok"

    # Second mark on the same date -- should fail
    result2 = _call_action(
        ACTIONS["mark-attendance"], fresh_db,
        employee_id=emp_id,
        date="2026-03-15",
        status="absent",
    )
    assert result2["status"] == "error"


# ---------------------------------------------------------------------------
# 3. test_bulk_mark_attendance
# ---------------------------------------------------------------------------

def test_bulk_mark_attendance(fresh_db):
    """Bulk-mark two employees in one call."""
    env = setup_hr_environment(fresh_db)
    emp1 = create_test_employee(fresh_db, env["company_id"], first_name="Carol")
    emp2 = create_test_employee(fresh_db, env["company_id"], first_name="Dave")

    entries = json.dumps([
        {"employee_id": emp1, "status": "present"},
        {"employee_id": emp2, "status": "absent"},
    ])

    result = _call_action(
        ACTIONS["bulk-mark-attendance"], fresh_db,
        date="2026-03-16",
        entries=entries,
    )

    assert result["status"] == "ok"
    assert result["created"] == 2
    assert result["total"] == 2
    assert result["skipped_duplicates"] == 0

    # Verify individual rows in DB
    att1 = fresh_db.execute(
        "SELECT status FROM attendance WHERE employee_id = ? AND attendance_date = ?",
        (emp1, "2026-03-16"),
    ).fetchone()
    assert att1 is not None
    assert att1["status"] == "present"

    att2 = fresh_db.execute(
        "SELECT status FROM attendance WHERE employee_id = ? AND attendance_date = ?",
        (emp2, "2026-03-16"),
    ).fetchone()
    assert att2 is not None
    assert att2["status"] == "absent"


# ---------------------------------------------------------------------------
# 4. test_list_attendance
# ---------------------------------------------------------------------------

def test_list_attendance(fresh_db):
    """Mark attendance on 3 days, then list with a date range filter."""
    env = setup_hr_environment(fresh_db)
    emp_id = create_test_employee(fresh_db, env["company_id"], first_name="Eve")

    dates = ["2026-03-10", "2026-03-11", "2026-03-12"]
    for d in dates:
        r = _call_action(
            ACTIONS["mark-attendance"], fresh_db,
            employee_id=emp_id,
            date=d,
            status="present",
        )
        assert r["status"] == "ok"

    # List all 3 days
    list_result = _call_action(
        ACTIONS["list-attendance"], fresh_db,
        employee_id=emp_id,
        from_date="2026-03-10",
        to_date="2026-03-12",
    )

    assert list_result["status"] == "ok"
    assert list_result["total_count"] == 3
    assert len(list_result["attendance"]) == 3

    # List a subset (only 2 days)
    list_subset = _call_action(
        ACTIONS["list-attendance"], fresh_db,
        employee_id=emp_id,
        from_date="2026-03-10",
        to_date="2026-03-11",
    )

    assert list_subset["status"] == "ok"
    assert list_subset["total_count"] == 2
    assert len(list_subset["attendance"]) == 2


# ---------------------------------------------------------------------------
# 5. test_add_holiday_list
# ---------------------------------------------------------------------------

def test_add_holiday_list(fresh_db):
    """Create a holiday list with two holidays."""
    env = setup_hr_environment(fresh_db)

    holidays = json.dumps([
        {"date": "2026-01-01", "description": "New Year"},
        {"date": "2026-07-04", "description": "Independence Day"},
    ])

    result = _call_action(
        ACTIONS["add-holiday-list"], fresh_db,
        name="US Holidays 2026",
        company_id=env["company_id"],
        from_date="2026-01-01",
        to_date="2026-12-31",
        holidays=holidays,
    )

    assert result["status"] == "ok"
    assert "holiday_list_id" in result
    assert result["name"] == "US Holidays 2026"
    assert result["holiday_count"] == 2

    # Verify DB: holiday_list row
    hl = fresh_db.execute(
        "SELECT * FROM holiday_list WHERE id = ?",
        (result["holiday_list_id"],),
    ).fetchone()
    assert hl is not None
    assert hl["name"] == "US Holidays 2026"
    assert hl["from_date"] == "2026-01-01"
    assert hl["to_date"] == "2026-12-31"

    # Verify DB: holiday child rows
    holidays_rows = fresh_db.execute(
        "SELECT * FROM holiday WHERE holiday_list_id = ? ORDER BY holiday_date",
        (result["holiday_list_id"],),
    ).fetchall()
    assert len(holidays_rows) == 2
    assert holidays_rows[0]["holiday_date"] == "2026-01-01"
    assert holidays_rows[0]["description"] == "New Year"
    assert holidays_rows[1]["holiday_date"] == "2026-07-04"
    assert holidays_rows[1]["description"] == "Independence Day"
