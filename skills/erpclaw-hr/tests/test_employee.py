"""Tests for employee CRUD and org structure."""
from helpers import (
    _call_action,
    setup_hr_environment,
    create_test_department,
    create_test_designation,
    create_test_employee,
)


# ---------------------------------------------------------------------------
# 1. test_add_department -- add department, verify status=ok
# ---------------------------------------------------------------------------

def test_add_department(fresh_db):
    """Add a department and verify the response."""
    from db_query import ACTIONS
    env = setup_hr_environment(fresh_db)

    result = _call_action(
        ACTIONS["add-department"], fresh_db,
        name="Marketing",
        company_id=env["company_id"],
    )

    assert result["status"] == "ok"
    assert "department_id" in result
    assert result["name"] == "Marketing"


# ---------------------------------------------------------------------------
# 2. test_add_designation -- add designation, verify ok
# ---------------------------------------------------------------------------

def test_add_designation(fresh_db):
    """Add a designation and verify the response."""
    from db_query import ACTIONS

    # setup_hr_environment already creates "Engineer", so use a different name
    setup_hr_environment(fresh_db)

    result = _call_action(
        ACTIONS["add-designation"], fresh_db,
        name="Senior Engineer",
    )

    assert result["status"] == "ok"
    assert "designation_id" in result
    assert result["name"] == "Senior Engineer"


# ---------------------------------------------------------------------------
# 3. test_add_employee -- add employee with department and designation
# ---------------------------------------------------------------------------

def test_add_employee(fresh_db):
    """Add an employee with department and designation, verify all fields."""
    from db_query import ACTIONS
    env = setup_hr_environment(fresh_db)

    result = _call_action(
        ACTIONS["add-employee"], fresh_db,
        first_name="Alice",
        last_name="Smith",
        date_of_joining="2026-02-01",
        company_id=env["company_id"],
        department_id=env["department_id"],
        designation_id=env["designation_id"],
        gender="female",
        employment_type="full_time",
    )

    assert result["status"] == "ok"
    assert "employee_id" in result
    assert result["full_name"] == "Alice Smith"
    assert result["naming_series"].startswith("EMP-")


# ---------------------------------------------------------------------------
# 4. test_get_employee -- add then get, verify all fields match
# ---------------------------------------------------------------------------

def test_get_employee(fresh_db):
    """Add an employee, then get by ID and verify fields match."""
    from db_query import ACTIONS
    env = setup_hr_environment(fresh_db)

    emp_id = create_test_employee(
        fresh_db, env["company_id"],
        first_name="Bob", last_name="Jones",
        date_of_joining="2026-01-20",
        department_id=env["department_id"],
        designation_id=env["designation_id"],
    )

    result = _call_action(
        ACTIONS["get-employee"], fresh_db,
        employee_id=emp_id,
    )

    assert result["status"] == "ok"
    emp = result["employee"]
    assert emp["id"] == emp_id
    assert emp["first_name"] == "Bob"
    assert emp["last_name"] == "Jones"
    assert emp["full_name"] == "Bob Jones"
    assert emp["date_of_joining"] == "2026-01-20"
    assert emp["department_name"] == "Engineering"
    assert emp["designation_name"] == "Engineer"
    assert emp["status"] == "active"
    assert emp["employment_type"] == "full_time"
    assert emp["company_id"] == env["company_id"]


# ---------------------------------------------------------------------------
# 5. test_update_employee -- add then update, verify changes
# ---------------------------------------------------------------------------

def test_update_employee(fresh_db):
    """Add an employee then update last_name and branch, verify changes."""
    from db_query import ACTIONS
    env = setup_hr_environment(fresh_db)

    emp_id = create_test_employee(
        fresh_db, env["company_id"],
        first_name="Carol", last_name="Davis",
    )

    result = _call_action(
        ACTIONS["update-employee"], fresh_db,
        employee_id=emp_id,
        last_name="Wilson",
        branch="West Coast",
    )

    assert result["status"] == "ok"
    assert "last_name" in result["updated_fields"]
    assert "branch" in result["updated_fields"]

    # Verify via get-employee
    get_result = _call_action(
        ACTIONS["get-employee"], fresh_db,
        employee_id=emp_id,
    )
    emp = get_result["employee"]
    assert emp["last_name"] == "Wilson"
    assert emp["full_name"] == "Carol Wilson"
    assert emp["branch"] == "West Coast"


# ---------------------------------------------------------------------------
# 6. test_list_employees -- add 2 employees, list, verify count=2
# ---------------------------------------------------------------------------

def test_list_employees(fresh_db):
    """Add 2 employees and verify list returns count=2."""
    from db_query import ACTIONS
    env = setup_hr_environment(fresh_db)

    create_test_employee(
        fresh_db, env["company_id"],
        first_name="Dave", last_name="Alpha",
    )
    create_test_employee(
        fresh_db, env["company_id"],
        first_name="Eve", last_name="Bravo",
    )

    result = _call_action(
        ACTIONS["list-employees"], fresh_db,
        company_id=env["company_id"],
    )

    assert result["status"] == "ok"
    assert result["total_count"] == 2
    assert len(result["employees"]) == 2


# ---------------------------------------------------------------------------
# 7. test_list_employees_with_filters -- filter by department
# ---------------------------------------------------------------------------

def test_list_employees_with_filters(fresh_db):
    """Add 2 employees in different departments, filter by one department."""
    from db_query import ACTIONS
    env = setup_hr_environment(fresh_db)

    # env already has "Engineering" department; create a second one
    sales_dept_id = create_test_department(
        fresh_db, env["company_id"], name="Sales",
    )

    create_test_employee(
        fresh_db, env["company_id"],
        first_name="Frank", last_name="Engineer",
        department_id=env["department_id"],
    )
    create_test_employee(
        fresh_db, env["company_id"],
        first_name="Grace", last_name="Salesperson",
        department_id=sales_dept_id,
    )

    # Filter by Sales department
    result = _call_action(
        ACTIONS["list-employees"], fresh_db,
        company_id=env["company_id"],
        department_id=sales_dept_id,
    )

    assert result["status"] == "ok"
    assert result["total_count"] == 1
    assert result["employees"][0]["full_name"] == "Grace Salesperson"


# ---------------------------------------------------------------------------
# 8. test_record_lifecycle_event -- promotion then separation
# ---------------------------------------------------------------------------

def test_record_lifecycle_event(fresh_db):
    """Record a promotion event (no status change), then a separation event
    which should update employee status to 'left'."""
    from db_query import ACTIONS
    env = setup_hr_environment(fresh_db)

    emp_id = create_test_employee(
        fresh_db, env["company_id"],
        first_name="Hank", last_name="Lifecycle",
    )

    # Record promotion -- should not change status
    promo_result = _call_action(
        ACTIONS["record-lifecycle-event"], fresh_db,
        employee_id=emp_id,
        event_type="promotion",
        event_date="2026-06-01",
    )

    assert promo_result["status"] == "ok"
    assert promo_result["event_type"] == "promotion"
    assert "employee_status_updated" not in promo_result

    # Verify employee is still active
    get_result = _call_action(
        ACTIONS["get-employee"], fresh_db,
        employee_id=emp_id,
    )
    assert get_result["employee"]["status"] == "active"

    # Record separation -- should update status to 'left'
    sep_result = _call_action(
        ACTIONS["record-lifecycle-event"], fresh_db,
        employee_id=emp_id,
        event_type="separation",
        event_date="2026-09-30",
    )

    assert sep_result["status"] == "ok"
    assert sep_result["event_type"] == "separation"
    assert sep_result["employee_status_updated"] is True
    assert sep_result["new_employee_status"] == "left"
    assert sep_result["date_of_exit"] == "2026-09-30"

    # Verify employee status is now 'left'
    get_result2 = _call_action(
        ACTIONS["get-employee"], fresh_db,
        employee_id=emp_id,
    )
    assert get_result2["employee"]["status"] == "left"
    assert get_result2["employee"]["date_of_exit"] == "2026-09-30"
