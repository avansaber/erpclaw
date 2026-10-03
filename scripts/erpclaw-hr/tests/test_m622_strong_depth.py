"""Strong behavioural depth for ten erpclaw-hr actions (packet m622).

Each action below already had a behavioural test that only asserted on
response content. The tests here deepen that coverage without deleting or
weakening anything: every writer is read back through the seam with
PyPika-built queries on a connection from ``erpclaw_lib.db.get_connection``
comparing exact stored values, every filtered read seeds decoys that differ
in exactly the filtered column (plus a second-company decoy wherever the
action takes a company), every action pins one refusal with its exact
message and a byte-identical database afterwards, and every test states
what should NOT have changed (ledgers plus the audit trail when the action
should not touch them).

None of the ten actions touches an amount, a rate or a quantity, so no
money literal applies; see CHANGES.md for that per-action note.

Covered actions: add-department, add-designation, add-shift-type,
update-shift-type, assign-shift, mark-attendance, bulk-mark-attendance,
list-attendance, list-shift-assignments, list-shift-types.
"""
import json
import os
import sys
from datetime import date, datetime, timedelta

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from hr_helpers import (  # noqa: E402
    build_hr_env,
    call_action,
    is_error,
    is_ok,
    load_db_query,
    ns,
    seed_company,
    seed_cost_center,
    seed_naming_series,
)
from erpclaw_lib.db import get_connection  # noqa: E402
from erpclaw_lib.query import Field, Order, P, Q, Table, fn  # noqa: E402

H = load_db_query()

ATT_STATUSES_SUFFIX = "('present', 'absent', 'half_day', 'on_leave', 'work_from_home')"
SOURCE_SUFFIX = "('manual', 'biometric', 'app')"


@pytest.fixture
def conn(db_path):
    connection = get_connection(db_path)
    yield connection
    connection.close()


@pytest.fixture
def env(conn):
    return build_hr_env(conn)


def _msg(result):
    return result.get("message", "")


def _row(conn, table, row_id):
    t = Table(table)
    q = Q.from_(t).select(t.star).where(t.id == P())
    found = conn.execute(q.get_sql(), (row_id,)).fetchone()
    assert found is not None, "%s %s not found" % (table, row_id)
    return dict(found)


def _where(conn, table, **filters):
    t = Table(table)
    q = Q.from_(t).select(t.star)
    params = []
    for column, value in filters.items():
        q = q.where(Field(column) == P())
        params.append(value)
    return [dict(r) for r in conn.execute(q.get_sql(), params).fetchall()]


def _all(conn, table):
    t = Table(table)
    q = Q.from_(t).select(t.star).orderby(t.id)
    return [dict(r) for r in conn.execute(q.get_sql()).fetchall()]


def _snapshot(conn, tables):
    return {name: _all(conn, name) for name in tables}


def _audit(conn, action, entity_id):
    rows = _where(conn, "audit_log", action=action, entity_id=entity_id)
    mine = [r for r in rows if r["skill"] == "erpclaw-hr"]
    assert len(mine) == 1, "expected one %s audit row, saw %d" % (action, len(mine))
    return mine[0]


def _parseable(value):
    datetime.fromisoformat(value)
    return True


def _audit_many(conn, action, entity_ids):
    rows = _where(conn, "audit_log", action=action)
    mine = [r for r in rows if r["skill"] == "erpclaw-hr"]
    got = sorted(r["entity_id"] for r in mine)
    assert got == sorted(entity_ids), (
        "expected %s audit rows for %s, saw %s" % (entity_ids, action, got))
    return {r["entity_id"]: r for r in mine}


_HR_TABLES = (
    "department",
    "designation",
    "shift_type",
    "shift_assignment",
    "attendance",
    "employee",
    "audit_log",
)
_LEDGERS = ("gl_entry", "payment_ledger_entry", "stock_ledger_entry")


def _employee(conn, env, first, last="Strong", company_id=None):
    r = call_action(H.add_employee, conn, ns(
        first_name=first, last_name=last, date_of_birth=None, gender=None,
        date_of_joining="2025-01-01", employment_type=None,
        company_id=company_id or env["company_id"],
        department_id=None, designation_id=None, employee_grade_id=None,
        branch=None, reporting_to=None, company_email=None, personal_email=None,
        cell_phone=None, emergency_contact=None, bank_details=None, ssn=None,
        federal_filing_status=None, w4_allowances=None, holiday_list_id=None,
        payroll_cost_center_id=None))
    assert is_ok(r), r
    return r["employee_id"]


def _second_company(conn):
    other = seed_company(conn, name="Other Co", abbr="OC")
    seed_naming_series(conn, other)
    return other


class TestAddDepartmentStrong:
    def test_creates_row_audit_and_message(self, conn, env):
        other = _second_company(conn)
        same_name = "Strong Sales"
        other_dept = call_action(H.add_department, conn, ns(
            name=same_name, company_id=other,
            parent_id=None, cost_center_id=None))
        assert is_ok(other_dept), other_dept
        other_row_before = _row(conn, "department", other_dept["department_id"])
        ledgers_before = _snapshot(conn, _LEDGERS)
        audit_before = _snapshot(conn, ("audit_log",))

        parent = call_action(H.add_department, conn, ns(
            name="Strong Parent", company_id=env["company_id"],
            parent_id=None, cost_center_id=None))
        assert is_ok(parent), parent

        r = call_action(H.add_department, conn, ns(
            name=same_name, company_id=env["company_id"],
            parent_id=parent["department_id"],
            cost_center_id=env["cost_center_id"]))
        assert is_ok(r), r
        assert r["message"] == "Department '%s' created successfully" % same_name

        row = _row(conn, "department", r["department_id"])
        assert (row["name"], row["company_id"], row["parent_id"],
                row["cost_center_id"]) == (
            same_name, env["company_id"], parent["department_id"],
            env["cost_center_id"])
        assert r["department_id"] == row["id"]

        audit = _audit(conn, "add-department", r["department_id"])
        assert audit["entity_type"] == "department"
        assert json.loads(audit["new_values"]) == {
            "name": same_name, "company_id": env["company_id"]}

        assert _row(conn, "department", other_dept["department_id"]) == other_row_before
        assert _snapshot(conn, _LEDGERS) == ledgers_before
        assert len(_all(conn, "audit_log")) == len(audit_before["audit_log"]) + 2

    def test_duplicate_refusal_leaves_db_identical(self, conn, env):
        first = call_action(H.add_department, conn, ns(
            name="Pinned Dept", company_id=env["company_id"],
            parent_id=None, cost_center_id=None))
        assert is_ok(first), first
        snapshot = _snapshot(conn, _HR_TABLES + _LEDGERS)

        r = call_action(H.add_department, conn, ns(
            name="Pinned Dept", company_id=env["company_id"],
            parent_id=None, cost_center_id=None))
        assert is_error(r)
        assert _msg(r) == (
            "Department 'Pinned Dept' already exists in this company "
            "(id: %s)" % first["department_id"])

        r = call_action(H.add_department, conn, ns(
            name=None, company_id=env["company_id"],
            parent_id=None, cost_center_id=None))
        assert is_error(r)
        assert _msg(r) == "--name is required"

        assert _snapshot(conn, _HR_TABLES + _LEDGERS) == snapshot


class TestAddDesignationStrong:
    def test_creates_row_audit_and_message(self, conn, env):
        decoy = call_action(H.add_designation, conn, ns(
            name="Other Title", description="Untouched"))
        assert is_ok(decoy), decoy
        decoy_before = _row(conn, "designation", decoy["designation_id"])
        ledgers_before = _snapshot(conn, _LEDGERS)

        r = call_action(H.add_designation, conn, ns(
            name="Strong Welder", description="Welds frames"))
        assert is_ok(r), r
        assert r["message"] == "Designation 'Strong Welder' created successfully"

        row = _row(conn, "designation", r["designation_id"])
        assert (row["name"], row["description"]) == ("Strong Welder", "Welds frames")
        assert r["designation_id"] == row["id"]

        audit = _audit(conn, "add-designation", r["designation_id"])
        assert audit["entity_type"] == "designation"
        assert json.loads(audit["new_values"]) == {"name": "Strong Welder"}

        assert _row(conn, "designation", decoy["designation_id"]) == decoy_before
        assert _snapshot(conn, _LEDGERS) == ledgers_before

    def test_duplicate_refusal_leaves_db_identical(self, conn, env):
        first = call_action(H.add_designation, conn, ns(
            name="Pinned Title", description=None))
        assert is_ok(first), first
        snapshot = _snapshot(conn, _HR_TABLES + _LEDGERS)

        r = call_action(H.add_designation, conn, ns(
            name="Pinned Title", description="Different words"))
        assert is_error(r)
        assert _msg(r) == (
            "Designation 'Pinned Title' already exists "
            "(id: %s)" % first["designation_id"])

        assert _snapshot(conn, _HR_TABLES + _LEDGERS) == snapshot


class TestAddShiftTypeStrong:
    def test_creates_row_with_default_status(self, conn, env):
        other = _second_company(conn)
        other_shift = call_action(H.add_shift_type, conn, ns(
            name="OtherCo Cover", start_time="10:00", end_time="18:00",
            company_id=other, status="inactive", limit=None, offset=None))
        assert is_ok(other_shift), other_shift
        other_before = _row(conn, "shift_type", other_shift["shift_type_id"])
        ledgers_before = _snapshot(conn, _LEDGERS)

        r = call_action(H.add_shift_type, conn, ns(
            name="Strong Morning", start_time="06:00", end_time="14:00",
            company_id=env["company_id"], status=None, limit=None, offset=None))
        assert is_ok(r), r
        assert r["shift_status"] == "active"

        row = _row(conn, "shift_type", r["shift_type_id"])
        assert row["name"] == "Strong Morning"
        assert row["start_time"] == "06:00"
        assert row["end_time"] == "14:00"
        assert row["company_id"] == env["company_id"]
        assert row["status"] == "active"
        assert _parseable(row["created_at"])
        assert _parseable(row["updated_at"])

        audit = _audit(conn, "add-shift-type", r["shift_type_id"])
        assert audit["entity_type"] == "shift_type"
        assert json.loads(audit["new_values"]) == {
            "name": "Strong Morning", "company_id": env["company_id"]}

        assert _row(conn, "shift_type", other_shift["shift_type_id"]) == other_before
        assert _snapshot(conn, _LEDGERS) == ledgers_before

    def test_duplicate_refusal_leaves_db_identical(self, conn, env):
        first = call_action(H.add_shift_type, conn, ns(
            name="Pinned Shift", start_time="08:00", end_time="16:00",
            company_id=env["company_id"], status=None, limit=None, offset=None))
        assert is_ok(first), first
        snapshot = _snapshot(conn, _HR_TABLES + _LEDGERS)

        r = call_action(H.add_shift_type, conn, ns(
            name="Pinned Shift", start_time="09:00", end_time="17:00",
            company_id=env["company_id"], status=None, limit=None, offset=None))
        assert is_error(r)
        assert _msg(r) == "Shift type 'Pinned Shift' already exists"

        r = call_action(H.add_shift_type, conn, ns(
            name="Bad Time Shift", start_time="6am", end_time="2pm",
            company_id=env["company_id"], status=None, limit=None, offset=None))
        assert is_error(r)
        assert _msg(r) == "Invalid start_time '6am'. Use HH:MM or HH:MM:SS format"

        assert _snapshot(conn, _HR_TABLES + _LEDGERS) == snapshot


class TestUpdateShiftTypeStrong:
    def test_status_transition_changes_only_allowed_columns(self, conn, env):
        created = call_action(H.add_shift_type, conn, ns(
            name="Mutable Shift", start_time="08:00", end_time="16:00",
            company_id=env["company_id"], status=None, limit=None, offset=None))
        assert is_ok(created), created
        shift_id = created["shift_type_id"]
        decoy = call_action(H.add_shift_type, conn, ns(
            name="Untouched Shift", start_time="20:00", end_time="23:00",
            company_id=env["company_id"], status=None, limit=None, offset=None))
        assert is_ok(decoy), decoy
        decoy_before = _row(conn, "shift_type", decoy["shift_type_id"])
        before = _row(conn, "shift_type", shift_id)
        assert before["status"] == "active"
        ledgers_before = _snapshot(conn, _LEDGERS)

        r = call_action(H.update_shift_type, conn, ns(
            shift_type_id=shift_id, name="Renamed Shift",
            start_time="09:00", end_time="17:00", status="inactive",
            limit=None, offset=None))
        assert is_ok(r), r
        assert r["shift_status"] == "inactive"

        after = _row(conn, "shift_type", shift_id)
        allowed = {"name", "start_time", "end_time", "status", "updated_at"}
        assert ({k: v for k, v in before.items() if k not in allowed} ==
                {k: v for k, v in after.items() if k not in allowed})
        assert (after["name"], after["start_time"], after["end_time"],
                after["status"]) == ("Renamed Shift", "09:00", "17:00", "inactive")
        for column in ("name", "start_time", "end_time", "status",
                       "updated_at"):
            assert after[column] != before[column], column
        assert _parseable(after["updated_at"])

        audit = _audit(conn, "update-shift-type", shift_id)
        assert audit["entity_type"] == "shift_type"
        assert json.loads(audit["old_values"]) == {
            "name": "Mutable Shift", "start_time": "08:00",
            "end_time": "16:00", "status": "active"}
        assert json.loads(audit["new_values"]) == {
            "name": "Renamed Shift", "start_time": "09:00",
            "end_time": "17:00", "status": "inactive"}

        assert _row(conn, "shift_type", decoy["shift_type_id"]) == decoy_before
        assert _snapshot(conn, _LEDGERS) == ledgers_before

    def test_unknown_id_refusal_leaves_db_identical(self, conn, env):
        created = call_action(H.add_shift_type, conn, ns(
            name="Kept Shift", start_time="08:00", end_time="16:00",
            company_id=env["company_id"], status=None, limit=None, offset=None))
        assert is_ok(created), created
        snapshot = _snapshot(conn, _HR_TABLES + _LEDGERS)

        r = call_action(H.update_shift_type, conn, ns(
            shift_type_id="no-such-shift-xyz", name="Ghost",
            start_time=None, end_time=None, status=None,
            limit=None, offset=None))
        assert is_error(r)
        assert _msg(r) == "Shift type no-such-shift-xyz not found"

        r = call_action(H.update_shift_type, conn, ns(
            shift_type_id=created["shift_type_id"], name=None,
            start_time=None, end_time=None, status=None,
            limit=None, offset=None))
        assert is_error(r)
        assert _msg(r) == ("No fields to update. Provide at least one of: "
                           "--name, --start-time, --end-time, --status")

        assert _snapshot(conn, _HR_TABLES + _LEDGERS) == snapshot


class TestAssignShiftStrong:
    def test_assign_stores_row_with_default_status(self, conn, env):
        emp_id = _employee(conn, env, "Assign", "Strong")
        quiet_id = _employee(conn, env, "Quiet", "Colleague")
        shift = call_action(H.add_shift_type, conn, ns(
            name="Assign Day", start_time="08:00", end_time="16:00",
            company_id=env["company_id"], status=None, limit=None, offset=None))
        assert is_ok(shift), shift
        ledgers_before = _snapshot(conn, _LEDGERS)

        r = call_action(H.assign_shift, conn, ns(
            employee_id=emp_id, shift_type_id=shift["shift_type_id"],
            start_date="2026-01-01", end_date="2026-12-31",
            status=None, limit=None, offset=None))
        assert is_ok(r), r
        assert r["assignment_status"] == "active"

        row = _row(conn, "shift_assignment", r["shift_assignment_id"])
        assert row["employee_id"] == emp_id
        assert row["shift_type_id"] == shift["shift_type_id"]
        assert row["start_date"] == "2026-01-01"
        assert row["end_date"] == "2026-12-31"
        assert row["status"] == "active"
        assert _parseable(row["created_at"])
        assert _parseable(row["updated_at"])

        audit = _audit(conn, "assign-shift", r["shift_assignment_id"])
        assert audit["entity_type"] == "shift_assignment"
        assert json.loads(audit["new_values"]) == {
            "employee_id": emp_id, "shift_type_id": shift["shift_type_id"],
            "start_date": "2026-01-01", "end_date": "2026-12-31",
            "status": "active"}

        assert _where(conn, "shift_assignment", employee_id=quiet_id) == []
        assert _snapshot(conn, _LEDGERS) == ledgers_before

    def test_cross_company_assignment_refused_leaves_db_identical(
            self, conn, env):
        other = _second_company(conn)
        home_emp = _employee(conn, env, "Home", "Worker")
        away_emp = _employee(conn, env, "Away", "Worker", company_id=other)
        home_shift = call_action(H.add_shift_type, conn, ns(
            name="Home Day", start_time="08:00", end_time="16:00",
            company_id=env["company_id"], status=None, limit=None, offset=None))
        assert is_ok(home_shift), home_shift
        away_shift = call_action(H.add_shift_type, conn, ns(
            name="Away Day", start_time="08:00", end_time="16:00",
            company_id=other, status=None, limit=None, offset=None))
        assert is_ok(away_shift), away_shift
        snapshot = _snapshot(conn, _HR_TABLES + _LEDGERS)

        r = call_action(H.assign_shift, conn, ns(
            employee_id=away_emp, shift_type_id=home_shift["shift_type_id"],
            start_date="2026-01-01", end_date=None,
            status=None, limit=None, offset=None))
        assert is_error(r)
        assert _msg(r) == (
            "Employee %s and shift type %s belong to different companies"
            % (away_emp, home_shift["shift_type_id"]))

        r = call_action(H.assign_shift, conn, ns(
            employee_id=home_emp, shift_type_id=away_shift["shift_type_id"],
            start_date="2026-01-01", end_date=None,
            status=None, limit=None, offset=None))
        assert is_error(r)
        assert _msg(r) == (
            "Employee %s and shift type %s belong to different companies"
            % (home_emp, away_shift["shift_type_id"]))

        assert _snapshot(conn, _HR_TABLES + _LEDGERS) == snapshot

    def test_end_before_start_refusal_leaves_db_identical(self, conn, env):
        emp_id = _employee(conn, env, "Assign", "Pinned")
        shift = call_action(H.add_shift_type, conn, ns(
            name="Pinned Day", start_time="08:00", end_time="16:00",
            company_id=env["company_id"], status=None, limit=None, offset=None))
        assert is_ok(shift), shift
        snapshot = _snapshot(conn, _HR_TABLES + _LEDGERS)

        r = call_action(H.assign_shift, conn, ns(
            employee_id=emp_id, shift_type_id=shift["shift_type_id"],
            start_date="2026-06-01", end_date="2026-01-01",
            status=None, limit=None, offset=None))
        assert is_error(r)
        assert _msg(r) == "end_date cannot be before start_date"

        r = call_action(H.assign_shift, conn, ns(
            employee_id="no-such-employee", shift_type_id=shift["shift_type_id"],
            start_date="2026-01-01", end_date=None,
            status=None, limit=None, offset=None))
        assert is_error(r)
        assert _msg(r) == "Employee no-such-employee not found"

        assert _snapshot(conn, _HR_TABLES + _LEDGERS) == snapshot


class TestMarkAttendanceStrong:
    def test_mark_stores_row_audit_and_defaults(self, conn, env):
        emp_id = _employee(conn, env, "Mark", "Strong")
        quiet_id = _employee(conn, env, "Quiet", "Witness")
        att_date = "2026-02-10"
        ledgers_before = _snapshot(conn, _LEDGERS)

        r = call_action(H.mark_attendance, conn, ns(
            employee_id=emp_id, date=att_date, status="present",
            shift=None, check_in_time=None, check_out_time=None,
            working_hours=None, late_entry=None, early_exit=None, source=None))
        assert is_ok(r), r
        assert r["source"] == "manual"
        assert r["employee_name"] == "Mark Strong"

        row = _row(conn, "attendance", r["attendance_id"])
        assert (row["employee_id"], row["attendance_date"], row["status"],
                row["shift"], row["late_entry"], row["early_exit"],
                row["source"]) == (
            emp_id, att_date, "present", None, 0, 0, "manual")
        assert r["attendance_id"] == row["id"]
        assert r["date"] == row["attendance_date"]

        audit = _audit(conn, "mark-attendance", r["attendance_id"])
        assert audit["entity_type"] == "attendance"
        assert json.loads(audit["new_values"]) == {
            "employee_id": emp_id, "date": att_date, "status": "present"}

        assert _where(conn, "attendance", employee_id=quiet_id) == []
        assert _snapshot(conn, _LEDGERS) == ledgers_before

    def test_duplicate_refusal_leaves_db_identical(self, conn, env):
        emp_id = _employee(conn, env, "Mark", "Pinned")
        att_date = "2026-02-11"
        first = call_action(H.mark_attendance, conn, ns(
            employee_id=emp_id, date=att_date, status="present",
            shift=None, check_in_time=None, check_out_time=None,
            working_hours=None, late_entry=None, early_exit=None, source=None))
        assert is_ok(first), first
        snapshot = _snapshot(conn, _HR_TABLES + _LEDGERS)

        r = call_action(H.mark_attendance, conn, ns(
            employee_id=emp_id, date=att_date, status="absent",
            shift=None, check_in_time=None, check_out_time=None,
            working_hours=None, late_entry=None, early_exit=None, source=None))
        assert is_error(r)
        assert _msg(r) == (
            "Attendance already marked for employee %s on %s "
            "(id: %s)" % (emp_id, att_date, first["attendance_id"]))

        r = call_action(H.mark_attendance, conn, ns(
            employee_id=emp_id, date=att_date, status="sleeping",
            shift=None, check_in_time=None, check_out_time=None,
            working_hours=None, late_entry=None, early_exit=None, source=None))
        assert is_error(r)
        assert _msg(r) == (
            "Invalid attendance status 'sleeping'. Valid: %s" % ATT_STATUSES_SUFFIX)

        assert _snapshot(conn, _HR_TABLES + _LEDGERS) == snapshot


class TestMarkAttendanceWorkingHours:
    def test_valid_quantities_stored_with_two_places(self, conn, env):
        emp_id = _employee(conn, env, "Mark", "Hours")
        ledgers_before = _snapshot(conn, _LEDGERS)

        r = call_action(H.mark_attendance, conn, ns(
            employee_id=emp_id, date="2026-02-10", status="present",
            shift=None, check_in_time=None, check_out_time=None,
            working_hours="7.50", late_entry=None, early_exit=None,
            source=None))
        assert is_ok(r), r
        assert _row(conn, "attendance", r["attendance_id"])["working_hours"] == "7.50"

        r = call_action(H.mark_attendance, conn, ns(
            employee_id=emp_id, date="2026-02-11", status="present",
            shift=None, check_in_time=None, check_out_time=None,
            working_hours="7.5", late_entry=None, early_exit=None,
            source=None))
        assert is_ok(r), r
        assert _row(conn, "attendance", r["attendance_id"])["working_hours"] == "7.50"

        r = call_action(H.mark_attendance, conn, ns(
            employee_id=emp_id, date="2026-02-12", status="present",
            shift=None, check_in_time=None, check_out_time=None,
            working_hours="8", late_entry=None, early_exit=None,
            source=None))
        assert is_ok(r), r
        assert _row(conn, "attendance", r["attendance_id"])["working_hours"] == "8.00"

        assert _snapshot(conn, _LEDGERS) == ledgers_before

    def test_invalid_quantities_refused_leaves_db_identical(self, conn, env):
        emp_id = _employee(conn, env, "Mark", "PinnedHours")
        att_date = "2026-02-13"
        snapshot = _snapshot(conn, _HR_TABLES + _LEDGERS)

        for bad in ("abc", "-1", "7.555", "NaN"):
            r = call_action(H.mark_attendance, conn, ns(
                employee_id=emp_id, date=att_date, status="present",
                shift=None, check_in_time=None, check_out_time=None,
                working_hours=bad, late_entry=None, early_exit=None,
                source=None))
            assert is_error(r), bad
            assert _msg(r) == (
                "Invalid working_hours '%s'. Must be a non-negative number "
                "with at most two decimal places" % bad)

        assert _snapshot(conn, _HR_TABLES + _LEDGERS) == snapshot

    def test_scientific_and_huge_quantities_refused_leaves_db_identical(
            self, conn, env):
        emp_id = _employee(conn, env, "Mark", "StrictHours")
        snapshot = _snapshot(conn, _HR_TABLES + _LEDGERS)

        cases = (
            ("2026-02-14", "1E+1"),
            ("2026-02-15", "-0"),
            ("2026-02-16", "1E+30"),
            ("2026-02-17", "100000000000000000000000000000"),
        )
        for att_date, bad in cases:
            r = call_action(H.mark_attendance, conn, ns(
                employee_id=emp_id, date=att_date, status="present",
                shift=None, check_in_time=None, check_out_time=None,
                working_hours=bad, late_entry=None, early_exit=None,
                source=None))
            assert is_error(r), (bad, r)
            assert _msg(r) == (
                "Invalid working_hours '%s'. Must be a non-negative number "
                "with at most two decimal places" % bad)

        assert _snapshot(conn, _HR_TABLES + _LEDGERS) == snapshot


class TestShiftTypeCompanyScope:
    @pytest.mark.xfail(
        strict=True,
        reason="shift-type names are unique across ALL companies; "
               "per-company uniqueness needs a schema change")
    def test_second_company_reuses_shift_type_name(self, conn, env):
        other = _second_company(conn)
        first = call_action(H.add_shift_type, conn, ns(
            name="Shared Morning", start_time="06:00", end_time="14:00",
            company_id=env["company_id"], status=None, limit=None, offset=None))
        assert is_ok(first), first

        r = call_action(H.add_shift_type, conn, ns(
            name="Shared Morning", start_time="07:00", end_time="15:00",
            company_id=other, status=None, limit=None, offset=None))
        assert is_ok(r), r
        assert _row(conn, "shift_type", r["shift_type_id"])["company_id"] == other


class TestBulkMarkAttendanceStrong:
    def test_bulk_creates_rows_and_counts_duplicates(self, conn, env):
        amy = _employee(conn, env, "Amy", "Bulk")
        ben = _employee(conn, env, "Ben", "Bulk")
        att_date = "2026-04-04"
        ledgers_before = _snapshot(conn, _LEDGERS)

        r = call_action(H.bulk_mark_attendance, conn, ns(
            date=att_date,
            entries=json.dumps([
                {"employee_id": amy, "status": "present"},
                {"employee_id": ben, "status": "absent"},
            ]),
            source="biometric"))
        assert is_ok(r), r
        assert (r["date"], r["total"], r["created"],
                r["skipped_duplicates"], r["errors"]) == (
            att_date, 2, 2, 0, [])

        rows = _where(conn, "attendance", attendance_date=att_date)
        assert sorted(
            (x["employee_id"], x["status"], x["source"],
             x["late_entry"], x["early_exit"]) for x in rows) == sorted([
            (amy, "present", "biometric", 0, 0),
            (ben, "absent", "biometric", 0, 0),
        ])
        by_emp = {x["employee_id"]: x for x in rows}
        for emp_id, status in ((amy, "present"), (ben, "absent")):
            stored = by_emp[emp_id]
            assert stored["attendance_date"] == att_date
            assert stored["status"] == status
            assert stored["shift"] is None
            assert stored["check_in_time"] is None
            assert stored["check_out_time"] is None
            assert stored["working_hours"] is None
            assert stored["late_entry"] == 0
            assert stored["early_exit"] == 0
            assert stored["source"] == "biometric"
            assert _parseable(stored["created_at"])

        audits = _audit_many(
            conn, "bulk-mark-attendance",
            [by_emp[amy]["id"], by_emp[ben]["id"]])
        assert audits[by_emp[amy]["id"]]["entity_type"] == "attendance"
        assert json.loads(audits[by_emp[amy]["id"]]["new_values"]) == {
            "employee_id": amy, "date": att_date, "status": "present"}
        assert json.loads(audits[by_emp[ben]["id"]]["new_values"]) == {
            "employee_id": ben, "date": att_date, "status": "absent"}

        audit_count = len(_all(conn, "audit_log"))
        again = call_action(H.bulk_mark_attendance, conn, ns(
            date=att_date,
            entries=json.dumps([
                {"employee_id": amy, "status": "present"},
                {"employee_id": ben, "status": "absent"},
            ]),
            source="biometric"))
        assert is_ok(again), again
        assert (again["created"], again["skipped_duplicates"],
                again["errors"]) == (0, 2, [])
        assert len(_where(conn, "attendance", attendance_date=att_date)) == 2
        assert len(_all(conn, "audit_log")) == audit_count

        assert _snapshot(conn, _LEDGERS) == ledgers_before

    def test_future_date_refusal_leaves_db_identical(self, conn, env):
        amy = _employee(conn, env, "Amy", "Future")
        ben = _employee(conn, env, "Ben", "Future")
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        today = date.today().isoformat()
        snapshot = _snapshot(conn, _HR_TABLES + _LEDGERS)

        r = call_action(H.bulk_mark_attendance, conn, ns(
            date=tomorrow,
            entries=json.dumps([
                {"employee_id": amy, "status": "present"},
                {"employee_id": ben, "status": "absent"},
            ]),
            source="biometric"))
        assert is_error(r)
        assert _msg(r) == (
            "Cannot mark attendance for a future date (%s). Today is %s"
            % (tomorrow, today))

        assert _snapshot(conn, _HR_TABLES + _LEDGERS) == snapshot

    def test_today_date_writes_entries_with_exact_counts(self, conn, env):
        amy = _employee(conn, env, "Amy", "Today")
        ben = _employee(conn, env, "Ben", "Today")
        today = date.today().isoformat()

        r = call_action(H.bulk_mark_attendance, conn, ns(
            date=today,
            entries=json.dumps([
                {"employee_id": amy, "status": "present"},
                {"employee_id": ben, "status": "absent"},
            ]),
            source="biometric"))
        assert is_ok(r), r
        assert (r["date"], r["total"], r["created"],
                r["skipped_duplicates"], r["errors"]) == (
            today, 2, 2, 0, [])

        rows = _where(conn, "attendance", attendance_date=today)
        assert sorted(
            (x["employee_id"], x["status"]) for x in rows) == sorted([
            (amy, "present"), (ben, "absent")])
        audits = _audit_many(
            conn, "bulk-mark-attendance", [x["id"] for x in rows])
        for stored in rows:
            assert audits[stored["id"]]["entity_type"] == "attendance"
            assert json.loads(audits[stored["id"]]["new_values"]) == {
                "employee_id": stored["employee_id"], "date": today,
                "status": stored["status"]}

    def test_missing_entries_refusal_leaves_db_identical(self, conn, env):
        amy = _employee(conn, env, "Amy", "Pinned")
        good = json.dumps([{"employee_id": amy, "status": "present"}])
        snapshot = _snapshot(conn, _HR_TABLES + _LEDGERS)

        r = call_action(H.bulk_mark_attendance, conn, ns(
            date="2026-04-04", entries=None, source=None))
        assert is_error(r)
        assert _msg(r) == "--entries is required (JSON array)"

        r = call_action(H.bulk_mark_attendance, conn, ns(
            date="2026-04-04", entries=good, source="pigeon"))
        assert is_error(r)
        assert _msg(r) == (
            "Invalid attendance source 'pigeon'. Valid: %s" % SOURCE_SUFFIX)

        assert _snapshot(conn, _HR_TABLES + _LEDGERS) == snapshot


class TestListAttendanceStrong:
    def test_filter_by_employee_and_status_with_summary(self, conn, env):
        keeper = _employee(conn, env, "Keep", "Listed")
        decoy_emp = _employee(conn, env, "Decoy", "Listed")
        d1, d2, d3 = "2026-01-05", "2026-01-06", "2026-01-07"

        def _mark(emp, day, status):
            res = call_action(H.mark_attendance, conn, ns(
                employee_id=emp, date=day, status=status,
                shift=None, check_in_time=None, check_out_time=None,
                working_hours=None, late_entry=None, early_exit=None,
                source=None))
            assert is_ok(res), res
            return res["attendance_id"]

        keep_present_early = _mark(keeper, d1, "present")
        _mark(keeper, d2, "absent")
        keep_present_late = _mark(keeper, d3, "present")
        decoy_row = _mark(decoy_emp, d1, "present")
        snapshot = _snapshot(conn, _HR_TABLES + _LEDGERS)

        r = call_action(H.list_attendance, conn, ns(
            employee_id=keeper, from_date=None, to_date=None,
            status="present", limit=None, offset=None))
        assert is_ok(r), r
        assert r["total_count"] == 2
        assert len(r["attendance"]) == 2
        got_ids = [row["id"] for row in r["attendance"]]
        assert keep_present_early in got_ids
        assert keep_present_late in got_ids
        assert decoy_row not in got_ids
        assert [row["attendance_date"] for row in r["attendance"]] == [d3, d1]
        assert r["attendance"][0]["status"] == "present"
        assert r["attendance"][0]["employee_name"] == "Keep Listed"
        assert r["summary"]["total_records"] == 2
        assert r["summary"]["present"] == 2
        assert r["summary"]["absent"] == 0
        assert r["summary"]["half_day"] == 0
        assert r["summary"]["on_leave"] == 0
        assert r["summary"]["work_from_home"] == 0

        assert _snapshot(conn, _HR_TABLES + _LEDGERS) == snapshot

    def test_invalid_status_refusal_leaves_db_identical(self, conn, env):
        emp_id = _employee(conn, env, "Keep", "Pinned")
        res = call_action(H.mark_attendance, conn, ns(
            employee_id=emp_id, date="2026-01-05", status="present",
            shift=None, check_in_time=None, check_out_time=None,
            working_hours=None, late_entry=None, early_exit=None, source=None))
        assert is_ok(res), res
        snapshot = _snapshot(conn, _HR_TABLES + _LEDGERS)

        r = call_action(H.list_attendance, conn, ns(
            employee_id=None, from_date=None, to_date=None,
            status="sleeping", limit=None, offset=None))
        assert is_error(r)
        assert _msg(r) == (
            "Invalid attendance status 'sleeping'. Valid: %s" % ATT_STATUSES_SUFFIX)

        assert _snapshot(conn, _HR_TABLES + _LEDGERS) == snapshot


class TestListShiftAssignmentsStrong:
    def test_filter_by_employee_orders_and_joins(self, conn, env):
        other = _second_company(conn)
        emp1 = _employee(conn, env, "First", "Assignee")
        emp2 = _employee(conn, env, "Second", "Assignee")
        emp_other = _employee(conn, env, "Other", "Assignee", company_id=other)
        day = call_action(H.add_shift_type, conn, ns(
            name="Strong Day", start_time="08:00", end_time="16:00",
            company_id=env["company_id"], status=None, limit=None, offset=None))
        night = call_action(H.add_shift_type, conn, ns(
            name="Strong Night", start_time="22:00", end_time="06:00",
            company_id=env["company_id"], status=None, limit=None, offset=None))
        assert is_ok(day) and is_ok(night), (day, night)
        other_shift = call_action(H.add_shift_type, conn, ns(
            name="OtherCo Night", start_time="22:00", end_time="06:00",
            company_id=other, status=None, limit=None, offset=None))
        assert is_ok(other_shift), other_shift

        def _assign(emp, shift_id, start):
            res = call_action(H.assign_shift, conn, ns(
                employee_id=emp, shift_type_id=shift_id,
                start_date=start, end_date=None,
                status=None, limit=None, offset=None))
            assert is_ok(res), res
            return res["shift_assignment_id"]

        early_id = _assign(emp1, day["shift_type_id"], "2026-01-01")
        late_id = _assign(emp1, night["shift_type_id"], "2026-06-01")
        other_emp_id = _assign(emp2, day["shift_type_id"], "2026-03-01")
        other_co_id = _assign(emp_other, other_shift["shift_type_id"], "2026-02-01")
        snapshot = _snapshot(conn, _HR_TABLES + _LEDGERS)

        r = call_action(H.list_shift_assignments, conn, ns(
            employee_id=emp1, shift_type_id=None, company_id=None,
            status=None, limit="20", offset="0"))
        assert is_ok(r), r
        assert r["count"] == 2
        assert len(r["shift_assignments"]) == 2
        assert [a["id"] for a in r["shift_assignments"]] == [late_id, early_id]
        assert r["shift_assignments"][0]["shift_name"] == "Strong Night"
        assert r["shift_assignments"][1]["shift_name"] == "Strong Day"
        assert r["shift_assignments"][0]["employee_name"] == "First Assignee"
        got_ids = [a["id"] for a in r["shift_assignments"]]
        assert other_emp_id not in got_ids
        assert other_co_id not in got_ids

        co = call_action(H.list_shift_assignments, conn, ns(
            employee_id=None, shift_type_id=None, company_id=other,
            status=None, limit="20", offset="0"))
        assert is_ok(co), co
        assert [a["id"] for a in co["shift_assignments"]] == [other_co_id]
        assert early_id not in [a["id"] for a in co["shift_assignments"]]

        assert _snapshot(conn, _HR_TABLES + _LEDGERS) == snapshot

    def test_unknown_employee_returns_empty_and_changes_nothing(self, conn, env):
        emp_id = _employee(conn, env, "First", "Kept")
        shift = call_action(H.add_shift_type, conn, ns(
            name="Kept Day", start_time="08:00", end_time="16:00",
            company_id=env["company_id"], status=None, limit=None, offset=None))
        assert is_ok(shift), shift
        kept = call_action(H.assign_shift, conn, ns(
            employee_id=emp_id, shift_type_id=shift["shift_type_id"],
            start_date="2026-01-01", end_date=None,
            status=None, limit=None, offset=None))
        assert is_ok(kept), kept
        snapshot = _snapshot(conn, _HR_TABLES + _LEDGERS)

        r = call_action(H.list_shift_assignments, conn, ns(
            employee_id="no-such-employee", shift_type_id=None, company_id=None,
            status=None, limit="20", offset="0"))
        assert is_ok(r), r
        assert r["count"] == 0
        assert r["shift_assignments"] == []

        assert _snapshot(conn, _HR_TABLES + _LEDGERS) == snapshot


class TestListShiftTypesStrong:
    def test_filter_by_company_and_status_orders(self, conn, env):
        other = _second_company(conn)
        alpha = call_action(H.add_shift_type, conn, ns(
            name="Alpha Cover", start_time="06:00", end_time="14:00",
            company_id=env["company_id"], status="active", limit=None, offset=None))
        beta = call_action(H.add_shift_type, conn, ns(
            name="Beta Cover", start_time="14:00", end_time="22:00",
            company_id=env["company_id"], status="inactive", limit=None, offset=None))
        zebra = call_action(H.add_shift_type, conn, ns(
            name="Zebra Cover", start_time="08:00", end_time="16:00",
            company_id=env["company_id"], status="active", limit=None, offset=None))
        gamma = call_action(H.add_shift_type, conn, ns(
            name="Gamma Cover", start_time="08:00", end_time="16:00",
            company_id=other, status="active", limit=None, offset=None))
        assert is_ok(alpha) and is_ok(beta) and is_ok(zebra) and is_ok(gamma)
        snapshot = _snapshot(conn, _HR_TABLES + _LEDGERS)

        r = call_action(H.list_shift_types, conn, ns(
            company_id=env["company_id"], status="active",
            limit="20", offset="0"))
        assert is_ok(r), r
        assert r["count"] == 2
        assert [s["name"] for s in r["shift_types"]] == ["Alpha Cover", "Zebra Cover"]
        assert r["shift_types"][0]["start_time"] == "06:00"
        assert r["shift_types"][1]["start_time"] == "08:00"
        got_ids = [s["id"] for s in r["shift_types"]]
        assert beta["shift_type_id"] not in got_ids
        assert gamma["shift_type_id"] not in got_ids

        assert _snapshot(conn, _HR_TABLES + _LEDGERS) == snapshot

    def test_unknown_company_refuses_and_changes_nothing(self, conn, env):
        created = call_action(H.add_shift_type, conn, ns(
            name="Kept Cover", start_time="08:00", end_time="16:00",
            company_id=env["company_id"], status=None, limit=None, offset=None))
        assert is_ok(created), created
        snapshot = _snapshot(conn, _HR_TABLES + _LEDGERS)

        r = call_action(H.list_shift_types, conn, ns(
            company_id="no-such-company", status=None,
            limit="20", offset="0"))
        assert r == {"status": "error", "error": "Company not found: no-such-company", "message": "Company not found: no-such-company"}, r

        assert _snapshot(conn, _HR_TABLES + _LEDGERS) == snapshot
