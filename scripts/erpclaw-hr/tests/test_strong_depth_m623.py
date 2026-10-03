"""Strong behavioural depth for nine read-only erpclaw-hr actions (task m623).

Each action below already had a behavioural test that only asserted on output
content. The tests in this file deepen that coverage without deleting or
weakening anything: fixtures are seeded through the owning module's own write
actions, the read action runs, and then the stored rows are read back with
PyPika-built queries through ``erpclaw_lib.query`` on a fresh connection from
``erpclaw_lib.db.get_connection`` and compared as exact values.

Per-action weight (the assertion that now carries the weight):

- check-expiring-documents: days_until_expiry literals [-3, 10], expiry
  ordering, and the three decoy-exclusion assertions.
- get-employee-document: exact stored-row equality plus the document_status
  envelope decision.
- list-employee-documents: count == 1 under the type+status filter.
- list-departments: employee_count plus alphabetical ordering.
- list-designations: employee_count plus ordering plus has_more pagination.
- list-employees: total_count == 2 under the company+status filter.
- list-expense-claims: item_count == 2 plus the hand-computed "650.00" total.
- list-leave-applications: hand-computed total_days "3"/"1" plus the
  approved-filter count.
- list-leave-types: ordering plus total_count plus has_more pagination.

Every test snapshots the HR tables together with the audit trail and the
ledgers before the call and asserts the snapshot is identical afterwards (a
read must change nothing). Refusals assert the exact message and the same
unchanged snapshot. Actions with no refusal branch pin the empty result
instead; see CHANGES.md.

No test here performs raw catalog reads; all reads are PyPika-built and run
on connections from ``erpclaw_lib.db.get_connection``. Money and quantities
are compared as exact TEXT strings, never float.
"""
import json
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from hr_helpers import (  # noqa: E402
    build_hr_env, call_action, is_error, is_ok, load_db_query, ns,
)
from erpclaw_lib.db import get_connection  # noqa: E402
from erpclaw_lib.query import Field, P, Q, Table, insert_row  # noqa: E402

H = load_db_query()


@pytest.fixture
def conn(db_path):
    connection = get_connection(db_path)
    yield connection
    connection.close()


@pytest.fixture
def env(conn):
    return build_hr_env(conn)


def _row(conn, table, row_id):
    t = Table(table)
    q = Q.from_(t).select(t.star).where(t.id == P())
    found = conn.execute(q.get_sql(), (row_id,)).fetchone()
    assert found is not None, f"{table} row {row_id} missing on read-back"
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


_SNAPSHOT_TABLES = (
    "employee", "department", "designation",
    "leave_type", "leave_allocation", "leave_application",
    "expense_claim", "expense_claim_item", "employee_document",
    "attendance", "holiday", "holiday_list",
    "audit_log", "gl_entry", "payment_ledger_entry", "stock_ledger_entry",
)


def _snapshot(conn, tables=_SNAPSHOT_TABLES):
    return {name: _all(conn, name) for name in tables}


def _shaped_id():
    """A nine-digit-dashed government-ID-shaped value, assembled at runtime so no
    literal appears in source."""
    return "-".join(("123", "45", "6789"))


def _add_employee(conn, company_id, first, last="X", **kw):
    result = call_action(H.add_employee, conn, ns(
        first_name=first, last_name=last, date_of_birth=None, gender=None,
        date_of_joining=kw.get("date_of_joining", "2025-01-01"),
        employment_type=kw.get("employment_type"),
        company_id=company_id, department_id=kw.get("department_id"),
        designation_id=kw.get("designation_id"), employee_grade_id=None,
        branch=None, reporting_to=None, company_email=None, personal_email=None,
        cell_phone=None, emergency_contact=None, bank_details=None,
        federal_filing_status=None, w4_allowances=None, holiday_list_id=None,
        payroll_cost_center_id=None, ssn=None,
    ))
    assert is_ok(result), result
    return result["employee_id"]


def _update_employee_ns(**overrides):
    defaults = dict(
        employee_id=None, first_name=None, last_name=None, date_of_birth=None,
        gender=None, date_of_joining=None, date_of_exit=None,
        employment_type=None, status=None, department_id=None,
        designation_id=None, employee_grade_id=None, branch=None,
        reporting_to=None, company_email=None, personal_email=None,
        cell_phone=None, emergency_contact=None, bank_details=None, ssn=None,
        federal_filing_status=None, w4_allowances=None,
        w4_additional_withholding=None, state_filing_status=None,
        state_withholding_allowances=None, employee_401k_rate=None,
        hsa_contribution=None, is_exempt_from_fica=None,
        salary_structure_id=None, leave_policy_id=None, shift_id=None,
        attendance_device_id=None, holiday_list_id=None,
        payroll_cost_center_id=None,
    )
    defaults.update(overrides)
    return ns(**defaults)


def _add_department(conn, company_id, name, parent_id=None):
    result = call_action(H.add_department, conn, ns(
        name=name, company_id=company_id, parent_id=parent_id,
        cost_center_id=None,
    ))
    assert is_ok(result), result
    return result["department_id"]


def _add_designation(conn, name, description=None):
    result = call_action(H.add_designation, conn, ns(
        name=name, description=description,
    ))
    assert is_ok(result), result
    return result["designation_id"]


def _add_leave_type(conn, name, max_days, **kw):
    result = call_action(H.add_leave_type, conn, ns(
        name=name, max_days_allowed=max_days,
        is_paid_leave=kw.get("is_paid_leave"),
        is_carry_forward=kw.get("is_carry_forward"),
        max_carry_forward_days=kw.get("max_carry_forward_days"),
        is_compensatory=kw.get("is_compensatory"),
        applicable_after_days=kw.get("applicable_after_days"),
    ))
    assert is_ok(result), result
    return result["leave_type_id"]


def _add_allocation(conn, employee_id, leave_type_id, total, fiscal_year):
    result = call_action(H.add_leave_allocation, conn, ns(
        employee_id=employee_id, leave_type_id=leave_type_id,
        total_leaves=total, fiscal_year=fiscal_year,
    ))
    assert is_ok(result), result
    return result["allocation_id"]


def _add_leave(conn, employee_id, leave_type_id, from_date, to_date,
               reason="test leave"):
    result = call_action(H.add_leave_application, conn, ns(
        employee_id=employee_id, leave_type_id=leave_type_id,
        from_date=from_date, to_date=to_date,
        half_day=None, half_day_date=None, reason=reason,
    ))
    assert is_ok(result), result
    return result["leave_application_id"]


def _add_claim(conn, employee_id, company_id, expense_date, items):
    result = call_action(H.add_expense_claim, conn, ns(
        employee_id=employee_id, expense_date=expense_date,
        company_id=company_id, items=json.dumps(items),
    ))
    assert is_ok(result), result
    return result["expense_claim_id"]


def _claim_item(expense_type, description, amount, account_id):
    return {"expense_type": expense_type, "description": description,
            "amount": amount, "account_id": account_id}


def _add_document(conn, employee_id, doc_type, name, expiry, notes=None):
    result = call_action(H.add_employee_document, conn, ns(
        employee_id=employee_id, document_type=doc_type,
        document_name=name, expiry_date=expiry, notes=notes, status=None,
    ))
    assert is_ok(result), result
    return result["employee_document_id"]


# ──────────────────────────────────────────────────────────────────────────────
# check-expiring-documents (no money fields; no refusal branch in the action)
# ──────────────────────────────────────────────────────────────────────────────

class TestCheckExpiringDocumentsStrong:
    def test_expiring_with_decoys_and_readback(self, conn, db_path, env):
        cid = env["company_id"]
        env2 = build_hr_env(conn)
        emp_a = _add_employee(conn, cid, "Alice", "A")
        emp_b = _add_employee(conn, env2["company_id"], "Zed", "Z")
        today = date.today()
        soon = (today + timedelta(days=10)).isoformat()
        far = (today + timedelta(days=365)).isoformat()
        past = (today - timedelta(days=3)).isoformat()
        archived_on = (today + timedelta(days=5)).isoformat()
        other_co_on = (today + timedelta(days=7)).isoformat()

        d_visa = _add_document(conn, emp_a, "visa", "H1B Visa", soon)
        _add_document(conn, emp_a, "passport", "US Passport", far)
        _add_document(conn, emp_a, "contract", "Old Contract", past)
        arch_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        sql, _cols = insert_row("employee_document", {
            "id": P(), "employee_id": P(), "document_type": P(),
            "document_name": P(), "expiry_date": P(), "notes": P(),
            "status": P(), "created_at": P(), "updated_at": P(),
        })
        conn.execute(sql, (arch_id, emp_a, "visa", "Archived Visa",
                           archived_on, None, "archived", now, now))
        conn.commit()
        _add_document(conn, emp_b, "visa", "OtherCo Visa", other_co_on)

        before = _snapshot(conn)
        result = call_action(H.check_expiring_documents, conn, ns(
            company_id=cid, days="30",
        ))
        assert is_ok(result), result
        assert result["days_window"] == 30
        assert result["count"] == 2
        docs = result["expiring_documents"]
        assert [d["document_name"] for d in docs] == [
            "Old Contract", "H1B Visa"]
        assert [d["days_until_expiry"] for d in docs] == [-3, 10]
        assert [d["is_expired"] for d in docs] == [True, False]
        by_name = {d["document_name"]: d for d in docs}
        assert by_name["H1B Visa"]["document_type"] == "visa"
        assert by_name["H1B Visa"]["expiry_date"] == soon
        assert by_name["H1B Visa"]["employee_name"] == "Alice A"
        assert by_name["H1B Visa"]["company_id"] == cid
        assert by_name["Old Contract"]["expiry_date"] == past
        names = [d["document_name"] for d in docs]
        assert "US Passport" not in names
        assert "Archived Visa" not in names
        assert "OtherCo Visa" not in names

        fresh = get_connection(db_path)
        try:
            stored = _row(fresh, "employee_document", d_visa)
        finally:
            fresh.close()
        assert stored["document_name"] == "H1B Visa"
        assert stored["document_type"] == "visa"
        assert stored["expiry_date"] == soon
        assert stored["status"] == "active"
        assert stored["employee_id"] == emp_a
        assert by_name["H1B Visa"]["expiry_date"] == stored["expiry_date"]
        assert by_name["H1B Visa"]["document_name"] == stored["document_name"]
        assert _snapshot(conn) == before

    def test_unknown_company_returns_empty_and_changes_nothing(
            self, conn, env):
        emp_a = _add_employee(conn, env["company_id"], "Alice", "A")
        soon = (date.today() + timedelta(days=10)).isoformat()
        _add_document(conn, emp_a, "visa", "H1B Visa", soon)
        before = _snapshot(conn)
        result = call_action(H.check_expiring_documents, conn, ns(
            company_id="no-such-company", days="30",
        ))
        assert is_ok(result), result
        assert result["count"] == 0
        assert result["expiring_documents"] == []
        assert result["days_window"] == 30
        assert _snapshot(conn) == before

    def test_bad_days_refused_and_changes_nothing(self, conn, env):
        emp_a = _add_employee(conn, env["company_id"], "Alice", "A")
        soon = (date.today() + timedelta(days=10)).isoformat()
        _add_document(conn, emp_a, "visa", "H1B Visa", soon)
        before = _snapshot(conn)

        result = call_action(H.check_expiring_documents, conn, ns(
            company_id=None, days="soon",
        ))
        assert is_error(result)
        assert result["message"] == (
            "Invalid --days 'soon'. Must be a non-negative integer")

        result = call_action(H.check_expiring_documents, conn, ns(
            company_id=None, days="-3",
        ))
        assert is_error(result)
        assert result["message"] == (
            "Invalid --days '-3'. Must be a non-negative integer")

        assert _snapshot(conn) == before


# ──────────────────────────────────────────────────────────────────────────────
# get-employee-document (no money fields)
# ──────────────────────────────────────────────────────────────────────────────

class TestGetEmployeeDocumentStrong:
    def test_get_returns_exact_stored_row(self, conn, db_path, env):
        emp_a = _add_employee(conn, env["company_id"], "Alice", "A")
        soon = (date.today() + timedelta(days=10)).isoformat()
        far = (date.today() + timedelta(days=365)).isoformat()
        _add_document(conn, emp_a, "passport", "US Passport", far,
                      notes="Other file")
        shaped = _shaped_id()
        notes = "Ref " + shaped + " on file"
        doc_id = _add_document(
            conn, emp_a, "visa", "H1B Visa", soon, notes=notes)

        before = _snapshot(conn)
        result = call_action(H.get_employee_document, conn, ns(
            document_id=doc_id,
        ))
        assert is_ok(result), result
        assert result["id"] == doc_id
        assert result["document_name"] == "H1B Visa"
        assert result["document_type"] == "visa"
        assert result["expiry_date"] == soon
        assert result["notes"] == "Ref ***-**-6789 on file"
        assert result["employee_id"] == emp_a
        assert result["document_status"] == "active"

        fresh = get_connection(db_path)
        try:
            stored = _row(fresh, "employee_document", doc_id)
        finally:
            fresh.close()
        assert stored["document_name"] == "H1B Visa"
        assert stored["document_type"] == "visa"
        assert stored["expiry_date"] == soon
        assert stored["notes"] == notes
        assert stored["status"] == "active"
        assert stored["employee_id"] == emp_a
        assert result["document_name"] == stored["document_name"]
        assert result["expiry_date"] == stored["expiry_date"]
        assert _snapshot(conn) == before

    def test_refusals_leave_database_unchanged(self, conn, db_path, env):
        emp_a = _add_employee(conn, env["company_id"], "Alice", "A")
        soon = (date.today() + timedelta(days=10)).isoformat()
        doc_id = _add_document(conn, emp_a, "visa", "H1B Visa", soon)
        before = _snapshot(conn)

        result = call_action(H.get_employee_document, conn, ns(
            document_id=None,
        ))
        assert is_error(result)
        assert result["message"] == "--document-id is required"

        result = call_action(H.get_employee_document, conn, ns(
            document_id="no-such-doc-1",
        ))
        assert is_error(result)
        assert result["message"] == "Employee document no-such-doc-1 not found"

        assert _snapshot(conn) == before
        fresh = get_connection(db_path)
        try:
            assert _row(fresh, "employee_document", doc_id)["document_name"] \
                == "H1B Visa"
        finally:
            fresh.close()


# ──────────────────────────────────────────────────────────────────────────────
# list-employee-documents (no money fields)
# ──────────────────────────────────────────────────────────────────────────────

class TestListEmployeeDocumentsStrong:
    def test_filtered_list_with_decoys_and_readback(
            self, conn, db_path, env):
        emp_a = _add_employee(conn, env["company_id"], "Alice", "A")
        emp_b = _add_employee(conn, env["company_id"], "Bob", "B")
        soon = (date.today() + timedelta(days=10)).isoformat()
        far = (date.today() + timedelta(days=365)).isoformat()
        _add_document(conn, emp_a, "visa", "E1 Visa", soon)
        _add_document(conn, emp_a, "passport", "E1 Passport", far)
        old_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        sql, _cols = insert_row("employee_document", {
            "id": P(), "employee_id": P(), "document_type": P(),
            "document_name": P(), "expiry_date": P(), "notes": P(),
            "status": P(), "created_at": P(), "updated_at": P(),
        })
        conn.execute(sql, (old_id, emp_a, "visa", "E1 Old Visa",
                           soon, None, "archived", now, now))
        conn.commit()
        _add_document(conn, emp_b, "visa", "E2 Visa", soon)

        before = _snapshot(conn)
        result = call_action(H.list_employee_documents, conn, ns(
            employee_id=emp_a, document_type="visa", status="active",
        ))
        assert is_ok(result), result
        assert result["count"] == 1
        assert [d["document_name"] for d in result["documents"]] == ["E1 Visa"]
        only = result["documents"][0]
        assert only["document_type"] == "visa"
        assert only["status"] == "active"
        assert only["expiry_date"] == soon
        assert only["employee_id"] == emp_a

        unfiltered = call_action(H.list_employee_documents, conn, ns(
            employee_id=emp_a, document_type=None, status=None,
        ))
        assert is_ok(unfiltered), unfiltered
        assert unfiltered["count"] == 3

        fresh = get_connection(db_path)
        try:
            stored = _where(fresh, "employee_document", employee_id=emp_a)
        finally:
            fresh.close()
        assert len(stored) == 3
        by_name = {r["document_name"]: r for r in stored}
        assert by_name["E1 Visa"]["document_type"] == "visa"
        assert by_name["E1 Visa"]["expiry_date"] == soon
        assert only["expiry_date"] == by_name["E1 Visa"]["expiry_date"]
        assert only["document_name"] == by_name["E1 Visa"]["document_name"]
        assert _snapshot(conn) == before

    def test_missing_employee_refusal_leaves_database_unchanged(
            self, conn, env):
        emp_a = _add_employee(conn, env["company_id"], "Alice", "A")
        soon = (date.today() + timedelta(days=10)).isoformat()
        _add_document(conn, emp_a, "visa", "E1 Visa", soon)
        before = _snapshot(conn)
        result = call_action(H.list_employee_documents, conn, ns(
            employee_id=None, document_type=None, status=None,
        ))
        assert is_error(result)
        assert result["message"] == "--employee-id is required"
        assert _snapshot(conn) == before


# ──────────────────────────────────────────────────────────────────────────────
# list-departments (no money fields; no refusal branch in the action)
# ──────────────────────────────────────────────────────────────────────────────

class TestListDepartmentsStrong:
    def test_company_filter_with_decoys_and_readback(
            self, conn, db_path, env):
        cid = env["company_id"]
        env2 = build_hr_env(conn)
        alpha = _add_department(conn, cid, "Alpha")
        _add_department(conn, cid, "Beta")
        child = _add_department(conn, cid, "Alpha-Child", parent_id=alpha)
        _add_department(conn, env2["company_id"], "Alpha")
        emp_alice = _add_employee(conn, cid, "Alice", "A",
                                  department_id=alpha)
        beta_id = _where(conn, "department", name="Beta")[0]["id"]
        emp_bob = _add_employee(conn, cid, "Bob", "B",
                                department_id=beta_id)
        moved = call_action(H.update_employee, conn, _update_employee_ns(
            employee_id=emp_bob, status="inactive"))
        assert is_ok(moved), moved

        before = _snapshot(conn)
        result = call_action(H.list_departments, conn, ns(
            company_id=cid, parent_id=None, limit=None, offset=None,
        ))
        assert is_ok(result), result
        assert result["total_count"] == 3
        assert [d["name"] for d in result["departments"]] == [
            "Alpha", "Alpha-Child", "Beta"]
        assert all(d["company_id"] == cid
                   for d in result["departments"])
        by_name = {d["name"]: d for d in result["departments"]}
        assert by_name["Alpha"]["employee_count"] == 1
        assert by_name["Beta"]["employee_count"] == 0
        assert by_name["Alpha-Child"]["parent_name"] == "Alpha"
        assert by_name["Alpha"]["parent_name"] is None

        kids = call_action(H.list_departments, conn, ns(
            company_id=cid, parent_id=alpha, limit=None, offset=None,
        ))
        assert is_ok(kids), kids
        assert kids["total_count"] == 1
        assert [d["name"] for d in kids["departments"]] == ["Alpha-Child"]

        fresh = get_connection(db_path)
        try:
            stored_alpha = _row(fresh, "department", alpha)
            stored_child = _row(fresh, "department", child)
            company = _row(fresh, "company", cid)
        finally:
            fresh.close()
        assert stored_alpha["name"] == "Alpha"
        assert stored_alpha["company_id"] == cid
        assert stored_child["parent_id"] == alpha
        assert by_name["Alpha"]["id"] == stored_alpha["id"]
        assert by_name["Alpha"]["company_id"] == stored_alpha["company_id"]
        assert by_name["Alpha"]["company_name"] == company["name"]
        assert _snapshot(conn) == before

    def test_unknown_company_returns_empty_and_changes_nothing(
            self, conn, env):
        _add_department(conn, env["company_id"], "Alpha")
        before = _snapshot(conn)
        result = call_action(H.list_departments, conn, ns(
            company_id="no-such-company", parent_id=None,
            limit=None, offset=None,
        ))
        assert is_ok(result), result
        assert result["total_count"] == 0
        assert result["departments"] == []
        assert _snapshot(conn) == before


# ──────────────────────────────────────────────────────────────────────────────
# list-designations (no money fields; no refusal branch in the action)
# ──────────────────────────────────────────────────────────────────────────────

class TestListDesignationsStrong:
    def test_ordering_counts_pagination_and_readback(
            self, conn, db_path, env):
        _add_designation(conn, "ZZZ-Director", "Owns things")
        _add_designation(conn, "MMM-Manager", "Runs things")
        eng = _add_designation(conn, "AAA-Engineer", "Builds things")
        _add_employee(conn, env["company_id"], "Alice", "A",
                      designation_id=eng)
        quiet = _add_employee(conn, env["company_id"], "Quinn", "Q",
                              designation_id=eng)
        moved = call_action(H.update_employee, conn, _update_employee_ns(
            employee_id=quiet, status="inactive"))
        assert is_ok(moved), moved

        before = _snapshot(conn)
        result = call_action(H.list_designations, conn, ns(
            limit=None, offset=None,
        ))
        assert is_ok(result), result
        assert result["total_count"] == 3
        assert [d["name"] for d in result["designations"]] == [
            "AAA-Engineer", "MMM-Manager", "ZZZ-Director"]
        by_name = {d["name"]: d for d in result["designations"]}
        assert by_name["AAA-Engineer"]["employee_count"] == 1
        assert by_name["MMM-Manager"]["employee_count"] == 0
        assert by_name["ZZZ-Director"]["employee_count"] == 0
        assert by_name["AAA-Engineer"]["description"] == "Builds things"

        page = call_action(H.list_designations, conn, ns(
            limit="1", offset="0",
        ))
        assert is_ok(page), page
        assert page["total_count"] == 3
        assert [d["name"] for d in page["designations"]] == ["AAA-Engineer"]
        assert page["has_more"] is True

        tail = call_action(H.list_designations, conn, ns(
            limit="2", offset="2",
        ))
        assert is_ok(tail), tail
        assert [d["name"] for d in tail["designations"]] == ["ZZZ-Director"]
        assert tail["has_more"] is False

        fresh = get_connection(db_path)
        try:
            stored = _row(fresh, "designation", eng)
        finally:
            fresh.close()
        assert stored["name"] == "AAA-Engineer"
        assert stored["description"] == "Builds things"
        assert by_name["AAA-Engineer"]["id"] == stored["id"]
        assert by_name["AAA-Engineer"]["description"] == stored["description"]
        assert _snapshot(conn) == before

    def test_far_offset_returns_empty_and_changes_nothing(self, conn, env):
        _add_designation(conn, "AAA-Engineer", "Builds things")
        before = _snapshot(conn)
        result = call_action(H.list_designations, conn, ns(
            limit=None, offset="99",
        ))
        assert is_ok(result), result
        assert result["total_count"] == 1
        assert result["designations"] == []
        assert _snapshot(conn) == before

    def test_bad_pagination_refused_and_changes_nothing(self, conn, env):
        _add_designation(conn, "AAA-Engineer", "Builds things")
        before = _snapshot(conn)

        result = call_action(H.list_designations, conn, ns(
            limit="many", offset="0",
        ))
        assert is_error(result)
        assert result["message"] == (
            "Invalid --limit 'many'. Must be a positive integer")

        result = call_action(H.list_designations, conn, ns(
            limit="-1", offset="0",
        ))
        assert is_error(result)
        assert result["message"] == (
            "Invalid --limit '-1'. Must be a positive integer")

        result = call_action(H.list_designations, conn, ns(
            limit="20", offset="-2",
        ))
        assert is_error(result)
        assert result["message"] == (
            "Invalid --offset '-2'. Must be a non-negative integer")

        assert _snapshot(conn) == before


# ──────────────────────────────────────────────────────────────────────────────
# list-employees (no money fields)
# ──────────────────────────────────────────────────────────────────────────────

class TestListEmployeesStrong:
    def test_company_status_filter_with_decoys_and_readback(
            self, conn, db_path, env):
        cid = env["company_id"]
        env2 = build_hr_env(conn)
        dept = _add_department(conn, cid, "Eng")
        desig = _add_designation(conn, "SE-Depth", "Writes code")
        _add_employee(conn, cid, "Cara", "Part",
                      employment_type="part_time")
        alice = _add_employee(conn, cid, "Alice", "Active",
                              department_id=dept, designation_id=desig)
        bob = _add_employee(conn, cid, "Bob", "Inactive")
        moved = call_action(H.update_employee, conn, _update_employee_ns(
            employee_id=bob, status="inactive"))
        assert is_ok(moved), moved
        _add_employee(conn, env2["company_id"], "Alice", "Active")

        before = _snapshot(conn)
        result = call_action(H.list_employees, conn, ns(
            company_id=cid, department_id=None, designation_id=None,
            status="active", employment_type=None, search=None,
            limit=None, offset=None,
        ))
        assert is_ok(result), result
        assert result["total_count"] == 2
        assert [e["full_name"] for e in result["employees"]] == [
            "Alice Active", "Cara Part"]
        assert all(e["company_id"] == cid for e in result["employees"])
        by_name = {e["full_name"]: e for e in result["employees"]}
        assert by_name["Alice Active"]["department_name"] == "Eng"
        assert by_name["Alice Active"]["designation_name"] == "SE-Depth"

        part = call_action(H.list_employees, conn, ns(
            company_id=cid, department_id=None, designation_id=None,
            status=None, employment_type="part_time", search=None,
            limit=None, offset=None,
        ))
        assert is_ok(part), part
        assert part["total_count"] == 1
        assert [e["full_name"] for e in part["employees"]] == ["Cara Part"]

        fresh = get_connection(db_path)
        try:
            stored = _row(fresh, "employee", alice)
        finally:
            fresh.close()
        assert stored["full_name"] == "Alice Active"
        assert stored["company_id"] == cid
        assert stored["status"] == "active"
        assert stored["employment_type"] == "full_time"
        assert by_name["Alice Active"]["id"] == stored["id"]
        assert by_name["Alice Active"]["naming_series"] == \
            stored["naming_series"]
        assert by_name["Alice Active"]["company_id"] == stored["company_id"]
        assert _snapshot(conn) == before

    def test_invalid_filters_refuse_and_change_nothing(self, conn, env):
        _add_employee(conn, env["company_id"], "Alice", "A")
        before = _snapshot(conn)

        result = call_action(H.list_employees, conn, ns(
            company_id=None, department_id=None, designation_id=None,
            status="bogus", employment_type=None, search=None,
            limit=None, offset=None,
        ))
        assert is_error(result)
        assert result["message"] == (
            "Invalid status 'bogus'. Valid: "
            "('active', 'inactive', 'suspended', 'left')")

        result = call_action(H.list_employees, conn, ns(
            company_id=None, department_id=None, designation_id=None,
            status=None, employment_type="bogus", search=None,
            limit=None, offset=None,
        ))
        assert is_error(result)
        assert result["message"] == (
            "Invalid employment type 'bogus'. Valid: "
            "('full_time', 'part_time', 'contract', 'intern')")

        assert _snapshot(conn) == before


# ──────────────────────────────────────────────────────────────────────────────
# list-expense-claims (money: total_amount is TEXT)
# ──────────────────────────────────────────────────────────────────────────────

class TestListExpenseClaimsStrong:
    def test_filters_decoys_money_and_readback(self, conn, db_path, env):
        cid = env["company_id"]
        env2 = build_hr_env(conn)
        alice = _add_employee(conn, cid, "Alice", "A")
        bob = _add_employee(conn, cid, "Bob", "B")
        zed = _add_employee(conn, env2["company_id"], "Zed", "Z")
        hand_total = str(Decimal("500.00") + Decimal("150.00"))
        assert hand_total == "650.00"
        claim_draft = _add_claim(conn, alice, cid, "2026-03-01", [
            _claim_item("travel", "Flight", "500.00", env["expense_account"]),
            _claim_item("meals", "Lunch", "150.00", env["expense_account"]),
        ])
        claim_sub = _add_claim(conn, alice, cid, "2026-04-01", [
            _claim_item("meals", "Dinner", "100.00", env["expense_account"]),
        ])
        submitted = call_action(H.submit_expense_claim, conn, ns(
            expense_claim_id=claim_sub))
        assert is_ok(submitted), submitted
        _add_claim(conn, bob, cid, "2026-03-15", [
            _claim_item("supplies", "Paper", "200.00", env["expense_account"]),
        ])
        _add_claim(conn, zed, env2["company_id"], "2026-03-02", [
            _claim_item("travel", "Taxi", "999.99", env2["expense_account"]),
        ])

        before = _snapshot(conn)
        result = call_action(H.list_expense_claims, conn, ns(
            employee_id=alice, status="draft", company_id=None,
            from_date=None, to_date=None, limit=None, offset=None,
        ))
        assert is_ok(result), result
        assert result["total_count"] == 1
        only = result["expense_claims"][0]
        assert only["id"] == claim_draft
        assert only["total_amount"] == "650.00"
        assert only["item_count"] == 2
        assert only["expense_date"] == "2026-03-01"
        assert only["employee_name"] == "Alice A"
        assert only["company_id"] == cid
        assert only["status"] == "draft"

        march = call_action(H.list_expense_claims, conn, ns(
            employee_id=None, status=None, company_id=cid,
            from_date="2026-03-01", to_date="2026-03-31",
            limit=None, offset=None,
        ))
        assert is_ok(march), march
        assert march["total_count"] == 2
        assert sorted(c["total_amount"] for c in march["expense_claims"]) == [
            "200.00", "650.00"]
        assert all(c["company_id"] == cid for c in march["expense_claims"])

        fresh = get_connection(db_path)
        try:
            stored = _row(fresh, "expense_claim", claim_draft)
            items = _where(fresh, "expense_claim_item",
                           expense_claim_id=claim_draft)
        finally:
            fresh.close()
        assert stored["total_amount"] == "650.00"
        assert stored["status"] == "draft"
        assert stored["employee_id"] == alice
        assert stored["company_id"] == cid
        assert sorted(i["amount"] for i in items) == ["150.00", "500.00"]
        assert only["total_amount"] == stored["total_amount"]
        assert only["item_count"] == len(items)
        assert _snapshot(conn) == before

    def test_invalid_status_refusal_leaves_database_unchanged(
            self, conn, db_path, env):
        alice = _add_employee(conn, env["company_id"], "Alice", "A")
        claim = _add_claim(conn, alice, env["company_id"], "2026-03-01", [
            _claim_item("travel", "Flight", "500.00",
                        env["expense_account"]),
        ])
        before = _snapshot(conn)
        result = call_action(H.list_expense_claims, conn, ns(
            employee_id=None, status="bogus", company_id=None,
            from_date=None, to_date=None, limit=None, offset=None,
        ))
        assert is_error(result)
        assert result["message"] == (
            "Invalid expense claim status 'bogus'. Valid: "
            "('draft', 'submitted', 'approved', 'rejected', 'paid', "
            "'cancelled')")
        assert _snapshot(conn) == before
        fresh = get_connection(db_path)
        try:
            assert _row(fresh, "expense_claim", claim)["status"] == "draft"
        finally:
            fresh.close()


# ──────────────────────────────────────────────────────────────────────────────
# list-leave-applications (quantity: total_days is TEXT)
# ──────────────────────────────────────────────────────────────────────────────

class TestListLeaveApplicationsStrong:
    def test_filters_decoys_quantities_and_readback(
            self, conn, db_path, env):
        cid = env["company_id"]
        alice = _add_employee(conn, cid, "Alice", "A")
        bob = _add_employee(conn, cid, "Bob", "B")
        boss = _add_employee(conn, cid, "Boss", "C")
        leave_type = _add_leave_type(conn, "Annual-Strong", "20")
        _add_allocation(conn, alice, leave_type, "20",
                        env["fiscal_year_name"])
        _add_allocation(conn, bob, leave_type, "20",
                        env["fiscal_year_name"])
        app_long = _add_leave(conn, alice, leave_type,
                              "2026-06-02", "2026-06-04", reason="holiday")
        app_single = _add_leave(conn, alice, leave_type,
                                "2026-07-01", "2026-07-01", reason="sick")
        _add_leave(conn, bob, leave_type,
                   "2026-06-10", "2026-06-11", reason="away")
        approved = call_action(H.approve_leave, conn, ns(
            leave_application_id=app_long, approved_by=boss))
        assert is_ok(approved), approved

        before = _snapshot(conn)
        result = call_action(H.list_leave_applications, conn, ns(
            employee_id=alice, status=None, from_date=None, to_date=None,
            leave_type_id=None, limit=None, offset=None,
        ))
        assert is_ok(result), result
        assert result["total_count"] == 2
        by_id = {a["id"]: a for a in result["leave_applications"]}
        assert by_id[app_long]["total_days"] == "3"
        assert by_id[app_single]["total_days"] == "1"
        assert by_id[app_long]["status"] == "approved"
        assert by_id[app_single]["status"] == "draft"
        assert by_id[app_long]["leave_type_name"] == "Annual-Strong"
        assert by_id[app_long]["employee_name"] == "Alice A"

        just_approved = call_action(H.list_leave_applications, conn, ns(
            employee_id=alice, status="approved", from_date=None,
            to_date=None, leave_type_id=None, limit=None, offset=None,
        ))
        assert is_ok(just_approved), just_approved
        assert just_approved["total_count"] == 1
        assert [a["id"] for a in just_approved["leave_applications"]] == [
            app_long]

        fresh = get_connection(db_path)
        try:
            stored_long = _row(fresh, "leave_application", app_long)
            stored_single = _row(fresh, "leave_application", app_single)
        finally:
            fresh.close()
        assert stored_long["total_days"] == "3"
        assert stored_single["total_days"] == "1"
        assert stored_long["status"] == "approved"
        assert stored_long["employee_id"] == alice
        assert by_id[app_long]["total_days"] == stored_long["total_days"]
        assert by_id[app_single]["total_days"] == stored_single["total_days"]
        assert _snapshot(conn) == before

    def test_invalid_status_refusal_leaves_database_unchanged(
            self, conn, db_path, env):
        alice = _add_employee(conn, env["company_id"], "Alice", "A")
        leave_type = _add_leave_type(conn, "Annual-Strong", "20")
        _add_allocation(conn, alice, leave_type, "20",
                        env["fiscal_year_name"])
        app = _add_leave(conn, alice, leave_type,
                         "2026-06-02", "2026-06-04", reason="holiday")
        before = _snapshot(conn)
        result = call_action(H.list_leave_applications, conn, ns(
            employee_id=None, status="bogus", from_date=None, to_date=None,
            leave_type_id=None, limit=None, offset=None,
        ))
        assert is_error(result)
        assert result["message"] == (
            "Invalid leave status 'bogus'. Valid: "
            "('draft', 'approved', 'rejected', 'cancelled')")
        assert _snapshot(conn) == before
        fresh = get_connection(db_path)
        try:
            assert _row(fresh, "leave_application", app)["status"] == "draft"
        finally:
            fresh.close()


# ──────────────────────────────────────────────────────────────────────────────
# list-leave-types (quantity: max_days_allowed is TEXT; no refusal branch)
# ──────────────────────────────────────────────────────────────────────────────

class TestListLeaveTypesStrong:
    def test_ordering_quantities_pagination_and_readback(
            self, conn, db_path, env):
        _add_leave_type(conn, "Unpaid-ZZZ", "365", is_paid_leave="0")
        _add_leave_type(conn, "Sick-MMM", "10")
        annual = _add_leave_type(conn, "Annual-AAA", "20")

        before = _snapshot(conn)
        result = call_action(H.list_leave_types, conn, ns(
            limit=None, offset=None,
        ))
        assert is_ok(result), result
        assert result["total_count"] == 3
        assert [t["name"] for t in result["leave_types"]] == [
            "Annual-AAA", "Sick-MMM", "Unpaid-ZZZ"]
        by_name = {t["name"]: t for t in result["leave_types"]}
        assert by_name["Annual-AAA"]["max_days_allowed"] == "20"
        assert by_name["Sick-MMM"]["max_days_allowed"] == "10"
        assert by_name["Unpaid-ZZZ"]["max_days_allowed"] == "365"
        assert by_name["Annual-AAA"]["is_paid_leave"] == 1
        assert by_name["Unpaid-ZZZ"]["is_paid_leave"] == 0

        page = call_action(H.list_leave_types, conn, ns(
            limit="2", offset="0",
        ))
        assert is_ok(page), page
        assert page["total_count"] == 3
        assert [t["name"] for t in page["leave_types"]] == [
            "Annual-AAA", "Sick-MMM"]
        assert page["has_more"] is True

        whole = call_action(H.list_leave_types, conn, ns(
            limit="3", offset="0",
        ))
        assert is_ok(whole), whole
        assert whole["total_count"] == 3
        assert [t["name"] for t in whole["leave_types"]] == [
            "Annual-AAA", "Sick-MMM", "Unpaid-ZZZ"]
        assert whole["has_more"] is False

        fresh = get_connection(db_path)
        try:
            stored = _row(fresh, "leave_type", annual)
        finally:
            fresh.close()
        assert stored["name"] == "Annual-AAA"
        assert stored["max_days_allowed"] == "20"
        assert by_name["Annual-AAA"]["id"] == stored["id"]
        assert by_name["Annual-AAA"]["max_days_allowed"] == \
            stored["max_days_allowed"]
        assert _snapshot(conn) == before

    def test_far_offset_returns_empty_and_changes_nothing(self, conn, env):
        _add_leave_type(conn, "Annual-AAA", "20")
        before = _snapshot(conn)
        result = call_action(H.list_leave_types, conn, ns(
            limit=None, offset="99",
        ))
        assert is_ok(result), result
        assert result["total_count"] == 1
        assert result["leave_types"] == []
        assert _snapshot(conn) == before
