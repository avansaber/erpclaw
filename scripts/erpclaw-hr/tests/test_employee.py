"""Tests for erpclaw-hr employee management actions.

Actions tested:
  - add-employee
  - update-employee
  - get-employee
  - list-employees
"""
import json
import pytest
from hr_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
    seed_company,
)
from erpclaw_lib.govid_shape import CAUTION_MESSAGE, mask_text

mod = load_db_query()


def _shaped_id():
    """A nine-digit-dashed government-ID-shaped value, assembled at runtime so no
    literal appears in source."""
    return "-".join(("123", "45", "6789"))


def _shaped_letter_id():
    """A letter-prefixed government-ID-shaped value, assembled at runtime."""
    return "A" + "23456781"


def _audit_blob(conn):
    """Concatenate every free-text audit_log field so a test can assert a shaped
    value never landed there."""
    rows = conn.execute(
        "SELECT old_values, new_values, description FROM audit_log"
    ).fetchall()
    return " ".join(str(v) for r in rows for v in dict(r).values() if v)


def _update_employee_ns(**overrides):
    """Full update-employee namespace with every field defaulting to None."""
    defaults = dict(
        employee_id=None, first_name=None, last_name=None, date_of_birth=None,
        gender=None, date_of_joining=None, date_of_exit=None, employment_type=None,
        status=None, department_id=None, designation_id=None, employee_grade_id=None,
        branch=None, reporting_to=None, company_email=None, personal_email=None,
        cell_phone=None, emergency_contact=None, bank_details=None, ssn=None,
        federal_filing_status=None, w4_allowances=None, w4_additional_withholding=None,
        state_filing_status=None, state_withholding_allowances=None,
        employee_401k_rate=None, hsa_contribution=None, is_exempt_from_fica=None,
        salary_structure_id=None, leave_policy_id=None, shift_id=None,
        attendance_device_id=None, holiday_list_id=None, payroll_cost_center_id=None,
    )
    defaults.update(overrides)
    return ns(**defaults)


# ── All args accessed by add_employee ──
# Required: first_name, date_of_joining, company_id
# Optional: last_name, date_of_birth, gender, employment_type,
#           department_id, designation_id, employee_grade_id,
#           branch, reporting_to, company_email, personal_email,
#           cell_phone, emergency_contact, bank_details,
#           federal_filing_status, w4_allowances,
#           holiday_list_id, payroll_cost_center_id

def _add_employee_ns(**overrides):
    """Build a full namespace for add-employee with sensible defaults."""
    defaults = dict(
        first_name="John",
        last_name=None,
        date_of_birth=None,
        gender=None,
        date_of_joining="2026-01-15",
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
        ssn=None,
    )
    defaults.update(overrides)
    return ns(**defaults)


class TestAddEmployee:
    def test_basic_create(self, conn, env):
        result = call_action(mod.add_employee, conn, _add_employee_ns(
            company_id=env["company_id"],
        ))
        assert is_ok(result)
        assert result["full_name"] == "John"
        assert "employee_id" in result
        assert "naming_series" in result

    def test_with_department(self, conn, env):
        # Create a department first
        dept_result = call_action(mod.add_department, conn, ns(
            name="Engineering",
            company_id=env["company_id"],
            parent_id=None,
            cost_center_id=None,
        ))
        assert is_ok(dept_result)

        result = call_action(mod.add_employee, conn, _add_employee_ns(
            first_name="Jane",
            last_name="Doe",
            company_id=env["company_id"],
            department_id=dept_result["department_id"],
        ))
        assert is_ok(result)
        assert result["full_name"] == "Jane Doe"

    def test_missing_name_fails(self, conn, env):
        result = call_action(mod.add_employee, conn, _add_employee_ns(
            first_name=None,
            company_id=env["company_id"],
        ))
        assert is_error(result)

    def test_missing_company_fails(self, conn, env):
        result = call_action(mod.add_employee, conn, _add_employee_ns(
            company_id=None,
        ))
        assert is_error(result)


class TestUpdateEmployee:
    def _create_employee(self, conn, env):
        result = call_action(mod.add_employee, conn, _add_employee_ns(
            company_id=env["company_id"],
        ))
        assert is_ok(result)
        return result["employee_id"]

    def test_update_name(self, conn, env):
        eid = self._create_employee(conn, env)
        result = call_action(mod.update_employee, conn, ns(
            employee_id=eid,
            first_name="Updated",
            last_name="Name",
            date_of_birth=None,
            gender=None,
            date_of_joining=None,
            date_of_exit=None,
            employment_type=None,
            status=None,
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
            w4_additional_withholding=None,
            state_filing_status=None,
            state_withholding_allowances=None,
            employee_401k_rate=None,
            hsa_contribution=None,
            is_exempt_from_fica=None,
            salary_structure_id=None,
            leave_policy_id=None,
            shift_id=None,
            attendance_device_id=None,
            holiday_list_id=None,
            payroll_cost_center_id=None,
        ))
        assert is_ok(result)
        assert "first_name" in result["updated_fields"]
        assert "last_name" in result["updated_fields"]

        row = conn.execute(
            "SELECT full_name FROM employee WHERE id=?", (eid,)
        ).fetchone()
        assert row["full_name"] == "Updated Name"

    def test_no_fields_fails(self, conn, env):
        eid = self._create_employee(conn, env)
        result = call_action(mod.update_employee, conn, ns(
            employee_id=eid,
            first_name=None,
            last_name=None,
            date_of_birth=None,
            gender=None,
            date_of_joining=None,
            date_of_exit=None,
            employment_type=None,
            status=None,
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
            w4_additional_withholding=None,
            state_filing_status=None,
            state_withholding_allowances=None,
            employee_401k_rate=None,
            hsa_contribution=None,
            is_exempt_from_fica=None,
            salary_structure_id=None,
            leave_policy_id=None,
            shift_id=None,
            attendance_device_id=None,
            holiday_list_id=None,
            payroll_cost_center_id=None,
        ))
        assert is_error(result)


class TestGetEmployee:
    def test_get(self, conn, env):
        create = call_action(mod.add_employee, conn, _add_employee_ns(
            first_name="Alice",
            last_name="Smith",
            company_id=env["company_id"],
        ))
        assert is_ok(create)

        result = call_action(mod.get_employee, conn, ns(
            employee_id=create["employee_id"],
        ))
        assert is_ok(result)
        assert result["employee"]["full_name"] == "Alice Smith"
        assert "leave_balances" in result["employee"]
        assert "attendance_summary" in result["employee"]

    def test_get_nonexistent_fails(self, conn, env):
        result = call_action(mod.get_employee, conn, ns(
            employee_id="fake-id-does-not-exist",
        ))
        assert is_error(result)


class TestListEmployees:
    def test_list(self, conn, env):
        # Create two employees
        call_action(mod.add_employee, conn, _add_employee_ns(
            first_name="Bob", company_id=env["company_id"],
        ))
        call_action(mod.add_employee, conn, _add_employee_ns(
            first_name="Carol", company_id=env["company_id"],
        ))

        result = call_action(mod.list_employees, conn, ns(
            company_id=env["company_id"],
            department_id=None,
            designation_id=None,
            status=None,
            employment_type=None,
            search=None,
            limit=None,
            offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 2
        assert len(result["employees"]) >= 2

    def test_list_search(self, conn, env):
        call_action(mod.add_employee, conn, _add_employee_ns(
            first_name="Searchable", last_name="Person",
            company_id=env["company_id"],
        ))

        result = call_action(mod.list_employees, conn, ns(
            company_id=env["company_id"],
            department_id=None,
            designation_id=None,
            status=None,
            employment_type=None,
            search="Searchable",
            limit=None,
            offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1
        names = [e["full_name"] for e in result["employees"]]
        assert any("Searchable" in n for n in names)


# ── M30: SSN wiring (T1) + PII warn (layer A) + output masking (layer C) ──

class TestSsnWiring:
    def test_ssn_encrypt_read_last4_roundtrip(self, conn, env):
        ssn = _shaped_id()
        res = call_action(mod.add_employee, conn, _add_employee_ns(
            first_name="Enc", last_name="Emp",
            company_id=env["company_id"], ssn=ssn,
        ))
        assert is_ok(res)
        eid = res["employee_id"]

        # Stored ciphertext on disk, never plaintext (mirrors NACHA precedent).
        row = conn.execute("SELECT ssn FROM employee WHERE id = ?", (eid,)).fetchone()
        assert row["ssn"].startswith("enc:v2:"), row["ssn"]
        assert ssn not in row["ssn"]

        # Round-trips back to the original.
        from erpclaw_lib.encrypted_columns import decrypt_for_column
        assert decrypt_for_column(row["ssn"], "employee", "ssn") == ssn

        # get-employee exposes last-4 only, never the full value.
        got = call_action(mod.get_employee, conn, ns(employee_id=eid))
        assert is_ok(got)
        emp = got["employee"]
        assert emp["ssn_last_four"] == ssn[-4:]
        assert emp.get("ssn") is None
        assert ssn not in json.dumps(emp)

    def test_ssn_undashed_accepted(self, conn, env):
        ssn = "".join(("123", "456", "789"))  # 9 digits, no dashes
        res = call_action(mod.add_employee, conn, _add_employee_ns(
            first_name="Nine", company_id=env["company_id"], ssn=ssn,
        ))
        assert is_ok(res)
        got = call_action(mod.get_employee, conn, ns(employee_id=res["employee_id"]))
        assert got["employee"]["ssn_last_four"] == ssn[-4:]

    def test_ssn_invalid_shape_rejected(self, conn, env):
        res = call_action(mod.add_employee, conn, _add_employee_ns(
            first_name="Bad", company_id=env["company_id"], ssn="12",
        ))
        assert is_error(res)

    def test_update_sets_and_clears_ssn(self, conn, env):
        ssn = _shaped_id()
        res = call_action(mod.add_employee, conn, _add_employee_ns(
            first_name="Upd", company_id=env["company_id"], ssn=ssn,
        ))
        eid = res["employee_id"]

        # Change it.
        new_ssn = "".join(("987", "65", "4321"))
        upd = call_action(mod.update_employee, conn, _update_employee_ns(
            employee_id=eid, ssn=new_ssn,
        ))
        assert is_ok(upd)
        got = call_action(mod.get_employee, conn, ns(employee_id=eid))
        assert got["employee"]["ssn_last_four"] == new_ssn[-4:]

        # Clear it with an empty value.
        call_action(mod.update_employee, conn, _update_employee_ns(
            employee_id=eid, ssn="",
        ))
        got = call_action(mod.get_employee, conn, ns(employee_id=eid))
        assert got["employee"]["ssn_last_four"] is None


class TestPiiWarnAndMask:
    def test_warn_on_shaped_emergency_contact_no_audit_leak(self, conn, env):
        shaped = _shaped_id()
        ec = json.dumps({"name": "Kin", "note": shaped})
        res = call_action(mod.add_employee, conn, _add_employee_ns(
            first_name="Warn", company_id=env["company_id"], emergency_contact=ec,
        ))
        # Warn never blocks the write.
        assert is_ok(res)
        assert res.get("caution") == CAUTION_MESSAGE

        # The matched value is never written to audit_log.
        assert shaped not in _audit_blob(conn)

        # DB stores the value as-is: masking is display-only, warn does not mutate.
        row = conn.execute(
            "SELECT emergency_contact FROM employee WHERE id = ?",
            (res["employee_id"],),
        ).fetchone()
        assert shaped in row["emergency_contact"]

    def test_clean_input_produces_no_caution(self, conn, env):
        ec = json.dumps({"name": "Kin", "phone": "555-0100", "note": "call anytime"})
        res = call_action(mod.add_employee, conn, _add_employee_ns(
            first_name="Clean", company_id=env["company_id"], emergency_contact=ec,
        ))
        assert is_ok(res)
        assert "caution" not in res

    def test_get_employee_masks_emergency_contact(self, conn, env):
        shaped = _shaped_id()
        ec = json.dumps({"name": "Kin", "note": shaped})
        res = call_action(mod.add_employee, conn, _add_employee_ns(
            first_name="Mask", company_id=env["company_id"], emergency_contact=ec,
        ))
        got = call_action(mod.get_employee, conn, ns(employee_id=res["employee_id"]))
        emp = got["employee"]
        assert emp["emergency_contact"]["note"] == mask_text(shaped)
        blob = json.dumps(emp["emergency_contact"])
        assert shaped not in blob
        assert shaped[-4:] in blob  # last-4 retained

    def test_update_warns_on_shaped_bank_details(self, conn, env):
        res = call_action(mod.add_employee, conn, _add_employee_ns(
            first_name="UpdWarn", company_id=env["company_id"],
        ))
        bd = json.dumps({"bank": "First", "memo": _shaped_letter_id()})
        upd = call_action(mod.update_employee, conn, _update_employee_ns(
            employee_id=res["employee_id"], bank_details=bd,
        ))
        assert is_ok(upd)
        assert upd.get("caution") == CAUTION_MESSAGE
