"""Tests for Attendance Regularization (#22f) and Employee Documents (#21), Sprint 7.

Actions tested:
  - add-regularization-rule
  - apply-attendance-regularization
  - add-employee-document
  - list-employee-documents
  - get-employee-document
  - check-expiring-documents
"""
import json
import pytest
import uuid
from datetime import date, timedelta
from hr_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
    seed_company, build_hr_env,
)

mod = load_db_query()


def _seed_employee(conn, company_id, first_name="Jane", last_name="Smith"):
    """Insert a test employee."""
    eid = str(uuid.uuid4())
    full_name = f"{first_name} {last_name}"
    conn.execute(
        """INSERT INTO employee (id, first_name, last_name, full_name,
           date_of_joining, employment_type, status, company_id,
           federal_filing_status, employee_401k_rate, hsa_contribution)
           VALUES (?, ?, ?, ?, '2025-01-15', 'full_time', 'active', ?,
                   'single', '0', '0')""",
        (eid, first_name, last_name, full_name, company_id)
    )
    conn.commit()
    return eid


# ──────────────────────────────────────────────────────────────────────────────
# add-regularization-rule
# ──────────────────────────────────────────────────────────────────────────────

class TestAddRegularizationRule:
    def test_basic_add(self, conn):
        """Add a half_day regularization rule with 15 min threshold."""
        env = build_hr_env(conn)
        cid = env["company_id"]

        result = call_action(mod.add_regularization_rule, conn, ns(
            company_id=cid,
            late_threshold_minutes="15",
            regularization_action="half_day",
        ))
        assert is_ok(result)
        assert result["late_threshold_minutes"] == 15
        assert result["action"] == "half_day"
        assert "rule_id" in result

        # Verify in DB
        row = conn.execute(
            "SELECT * FROM attendance_regularization_rule WHERE id = ?",
            (result["rule_id"],)
        ).fetchone()
        assert row is not None
        assert row["late_threshold_minutes"] == 15

    def test_invalid_action(self, conn):
        """Reject invalid regularization action."""
        env = build_hr_env(conn)

        result = call_action(mod.add_regularization_rule, conn, ns(
            company_id=env["company_id"],
            late_threshold_minutes="10",
            regularization_action="fire",
        ))
        assert is_error(result)
        assert "half_day" in result["message"]

    def test_negative_threshold(self, conn):
        """Reject non-positive threshold."""
        env = build_hr_env(conn)

        result = call_action(mod.add_regularization_rule, conn, ns(
            company_id=env["company_id"],
            late_threshold_minutes="-5",
            regularization_action="warn",
        ))
        assert is_error(result)

    def test_warn_rule(self, conn):
        """Add a warn rule."""
        env = build_hr_env(conn)

        result = call_action(mod.add_regularization_rule, conn, ns(
            company_id=env["company_id"],
            late_threshold_minutes="30",
            regularization_action="warn",
        ))
        assert is_ok(result)
        assert result["action"] == "warn"


# ──────────────────────────────────────────────────────────────────────────────
# apply-attendance-regularization
# ──────────────────────────────────────────────────────────────────────────────

class TestApplyAttendanceRegularization:
    def test_apply_half_day(self, conn):
        """Apply regularization to convert late attendance to half_day."""
        env = build_hr_env(conn)
        cid = env["company_id"]
        emp_id = _seed_employee(conn, cid)

        # Add rule: 15 min late = half_day
        call_action(mod.add_regularization_rule, conn, ns(
            company_id=cid,
            late_threshold_minutes="15",
            regularization_action="half_day",
        ))

        # Mark attendance with late entry and check_in at 09:30
        att_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO attendance (id, employee_id, attendance_date,
               status, late_entry, check_in_time, source, created_at)
               VALUES (?, ?, '2026-03-15', 'present', 1, '09:30', 'manual', datetime('now'))""",
            (att_id, emp_id)
        )
        conn.commit()

        result = call_action(mod.apply_attendance_regularization, conn, ns(
            company_id=cid,
            from_date="2026-03-01",
            to_date="2026-03-31",
        ))
        assert is_ok(result)
        assert result["records_processed"] >= 1
        assert result["records_updated"] >= 1

        # Verify status was changed to half_day
        row = conn.execute(
            "SELECT status FROM attendance WHERE id = ?", (att_id,)
        ).fetchone()
        assert row["status"] == "half_day"


# ──────────────────────────────────────────────────────────────────────────────
# add-employee-document
# ──────────────────────────────────────────────────────────────────────────────

class TestAddEmployeeDocument:
    def test_basic_add(self, conn):
        """Add a passport document for an employee."""
        env = build_hr_env(conn)
        emp_id = _seed_employee(conn, env["company_id"])

        result = call_action(mod.add_employee_document, conn, ns(
            employee_id=emp_id,
            document_type="passport",
            document_name="US Passport",
            expiry_date="2030-06-15",
            notes="Issued in 2020",
            status=None,
        ))
        assert is_ok(result)
        assert result["document_type"] == "passport"
        assert result["document_name"] == "US Passport"
        assert result["expiry_date"] == "2030-06-15"
        assert "employee_document_id" in result

    def test_i9_document(self, conn):
        """Add an I-9 document without expiry."""
        env = build_hr_env(conn)
        emp_id = _seed_employee(conn, env["company_id"])

        result = call_action(mod.add_employee_document, conn, ns(
            employee_id=emp_id,
            document_type="i9",
            document_name="I-9 Employment Eligibility",
            expiry_date=None,
            notes=None,
            status=None,
        ))
        assert is_ok(result)
        assert result["document_type"] == "i9"
        assert result["expiry_date"] is None

    def test_invalid_type(self, conn):
        """Reject invalid document type."""
        env = build_hr_env(conn)
        emp_id = _seed_employee(conn, env["company_id"])

        result = call_action(mod.add_employee_document, conn, ns(
            employee_id=emp_id,
            document_type="invalid_type",
            document_name="Some Doc",
            expiry_date=None,
            notes=None,
            status=None,
        ))
        assert is_error(result)
        assert "passport" in result["message"]

    def test_invalid_expiry_date(self, conn):
        """Reject invalid expiry date format."""
        env = build_hr_env(conn)
        emp_id = _seed_employee(conn, env["company_id"])

        result = call_action(mod.add_employee_document, conn, ns(
            employee_id=emp_id,
            document_type="visa",
            document_name="Work Visa",
            expiry_date="not-a-date",
            notes=None,
            status=None,
        ))
        assert is_error(result)


# ──────────────────────────────────────────────────────────────────────────────
# list-employee-documents / get-employee-document
# ──────────────────────────────────────────────────────────────────────────────

class TestListGetEmployeeDocuments:
    def test_list_documents(self, conn):
        """List documents for an employee."""
        env = build_hr_env(conn)
        emp_id = _seed_employee(conn, env["company_id"])

        call_action(mod.add_employee_document, conn, ns(
            employee_id=emp_id, document_type="passport",
            document_name="US Passport", expiry_date="2030-01-01",
            notes=None, status=None,
        ))
        call_action(mod.add_employee_document, conn, ns(
            employee_id=emp_id, document_type="w4",
            document_name="W-4 Form", expiry_date=None,
            notes=None, status=None,
        ))

        result = call_action(mod.list_employee_documents, conn, ns(
            employee_id=emp_id,
            document_type=None,
            status=None,
        ))
        assert is_ok(result)
        assert result["count"] == 2

    def test_get_document(self, conn):
        """Get a specific document by ID."""
        env = build_hr_env(conn)
        emp_id = _seed_employee(conn, env["company_id"])

        add_result = call_action(mod.add_employee_document, conn, ns(
            employee_id=emp_id, document_type="contract",
            document_name="Employment Contract", expiry_date=None,
            notes="Signed 2025-01", status=None,
        ))
        doc_id = add_result["employee_document_id"]

        result = call_action(mod.get_employee_document, conn, ns(
            document_id=doc_id,
        ))
        assert is_ok(result)
        assert result["document_type"] == "contract"
        assert result["document_name"] == "Employment Contract"


# ──────────────────────────────────────────────────────────────────────────────
# check-expiring-documents
# ──────────────────────────────────────────────────────────────────────────────

class TestCheckExpiringDocuments:
    def test_find_expiring_documents(self, conn):
        """Find documents expiring within 30 days."""
        env = build_hr_env(conn)
        emp_id = _seed_employee(conn, env["company_id"])

        # Add a document expiring soon
        soon = (date.today() + timedelta(days=10)).isoformat()
        call_action(mod.add_employee_document, conn, ns(
            employee_id=emp_id, document_type="visa",
            document_name="H1B Visa", expiry_date=soon,
            notes=None, status=None,
        ))

        # Add a document NOT expiring soon
        far = (date.today() + timedelta(days=365)).isoformat()
        call_action(mod.add_employee_document, conn, ns(
            employee_id=emp_id, document_type="passport",
            document_name="US Passport", expiry_date=far,
            notes=None, status=None,
        ))

        result = call_action(mod.check_expiring_documents, conn, ns(
            company_id=env["company_id"],
            days="30",
        ))
        assert is_ok(result)
        assert result["count"] == 1
        assert result["expiring_documents"][0]["document_type"] == "visa"
        assert result["expiring_documents"][0]["days_until_expiry"] == 10
