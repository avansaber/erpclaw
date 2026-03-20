"""Tests for NACHA/ACH file generation (Feature #22e, Sprint 7).

Actions tested:
  - add-employee-bank-account
  - list-employee-bank-accounts
  - generate-nacha-file
"""
import json
import pytest
from decimal import Decimal
from payroll_helpers import (
    call_action, ns, is_error, is_ok,
    seed_company, seed_employee, build_payroll_env, load_db_query,
)

mod = load_db_query()


# ──────────────────────────────────────────────────────────────────────────────
# add-employee-bank-account
# ──────────────────────────────────────────────────────────────────────────────

class TestAddEmployeeBankAccount:
    def test_basic_add(self, conn):
        """Add a checking account for an employee."""
        cid = seed_company(conn)
        emp_id = seed_employee(conn, cid)

        result = call_action(mod.add_employee_bank_account, conn, ns(
            employee_id=emp_id,
            bank_name="First National Bank",
            routing_number="021000021",
            account_number="123456789",
            account_type="checking",
        ))
        assert is_ok(result)
        assert result["bank_name"] == "First National Bank"
        assert result["account_type"] == "checking"
        assert "employee_bank_account_id" in result

        # Verify in DB
        row = conn.execute(
            "SELECT * FROM employee_bank_account WHERE id = ?",
            (result["employee_bank_account_id"],)
        ).fetchone()
        assert row is not None
        assert row["routing_number"] == "021000021"
        assert row["account_number"] == "123456789"

    def test_savings_account(self, conn):
        """Add a savings account."""
        cid = seed_company(conn)
        emp_id = seed_employee(conn, cid)

        result = call_action(mod.add_employee_bank_account, conn, ns(
            employee_id=emp_id,
            bank_name="Chase Bank",
            routing_number="322271627",
            account_number="9876543210",
            account_type="savings",
        ))
        assert is_ok(result)
        assert result["account_type"] == "savings"

    def test_invalid_routing_number(self, conn):
        """Reject routing number that is not 9 digits."""
        cid = seed_company(conn)
        emp_id = seed_employee(conn, cid)

        result = call_action(mod.add_employee_bank_account, conn, ns(
            employee_id=emp_id,
            bank_name="Test Bank",
            routing_number="12345",
            account_number="123456789",
            account_type="checking",
        ))
        assert is_error(result)
        assert "9 digits" in result["message"]

    def test_invalid_account_type(self, conn):
        """Reject invalid account type."""
        cid = seed_company(conn)
        emp_id = seed_employee(conn, cid)

        result = call_action(mod.add_employee_bank_account, conn, ns(
            employee_id=emp_id,
            bank_name="Test Bank",
            routing_number="021000021",
            account_number="123456789",
            account_type="credit",
        ))
        assert is_error(result)
        assert "checking, savings" in result["message"]


# ──────────────────────────────────────────────────────────────────────────────
# list-employee-bank-accounts
# ──────────────────────────────────────────────────────────────────────────────

class TestListEmployeeBankAccounts:
    def test_list_accounts(self, conn):
        """List accounts and verify masking."""
        cid = seed_company(conn)
        emp_id = seed_employee(conn, cid)

        # Add account
        call_action(mod.add_employee_bank_account, conn, ns(
            employee_id=emp_id,
            bank_name="Test Bank",
            routing_number="021000021",
            account_number="123456789",
            account_type="checking",
        ))

        result = call_action(mod.list_employee_bank_accounts, conn, ns(
            employee_id=emp_id,
        ))
        assert is_ok(result)
        assert result["count"] == 1
        acct = result["accounts"][0]
        assert acct["account_number_masked"] == "****6789"


# ──────────────────────────────────────────────────────────────────────────────
# generate-nacha-file
# ──────────────────────────────────────────────────────────────────────────────

class TestGenerateNachaFile:
    def test_basic_nacha_generation(self, conn):
        """Generate NACHA file from a submitted payroll run with bank accounts."""
        env = build_payroll_env(conn)
        emp_id = env["employee_id"]
        company_id = env["company_id"]

        # Add bank account for employee
        call_action(mod.add_employee_bank_account, conn, ns(
            employee_id=emp_id,
            bank_name="First National Bank",
            routing_number="021000021",
            account_number="123456789",
            account_type="checking",
        ))

        # Create a submitted payroll run with salary slips
        import uuid
        run_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO payroll_run (id, period_start, period_end,
               payroll_frequency, total_gross, total_deductions, total_net,
               employee_count, status, company_id)
               VALUES (?, '2026-01-01', '2026-01-31', 'monthly',
                       '5000.00', '1000.00', '4000.00', 1, 'submitted', ?)""",
            (run_id, company_id)
        )
        slip_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO salary_slip (id, payroll_run_id, employee_id,
               period_start, period_end, total_working_days, payment_days,
               gross_pay, total_deductions, net_pay, status, company_id)
               VALUES (?, ?, ?, '2026-01-01', '2026-01-31', '22', '22',
                       '5000.00', '1000.00', '4000.00', 'submitted', ?)""",
            (slip_id, run_id, emp_id, company_id)
        )
        conn.commit()

        result = call_action(mod.generate_nacha_file, conn, ns(
            payroll_run_id=run_id,
            company_id=company_id,
        ))
        assert is_ok(result)
        assert result["entry_count"] == 1
        assert Decimal(result["total_amount"]) == Decimal("4000.00")
        assert "file_content" in result

        # Verify NACHA file structure
        lines = result["file_content"].split("\n")
        assert lines[0][0] == "1"   # File Header
        assert lines[1][0] == "5"   # Batch Header
        assert lines[2][0] == "6"   # Entry Detail
        assert lines[3][0] == "8"   # Batch Control
        assert lines[4][0] == "9"   # File Control

    def test_nacha_rejects_draft_run(self, conn):
        """Cannot generate NACHA from a draft payroll run."""
        env = build_payroll_env(conn)
        import uuid
        run_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO payroll_run (id, period_start, period_end,
               payroll_frequency, status, company_id)
               VALUES (?, '2026-01-01', '2026-01-31', 'monthly', 'draft', ?)""",
            (run_id, env["company_id"])
        )
        conn.commit()

        result = call_action(mod.generate_nacha_file, conn, ns(
            payroll_run_id=run_id,
            company_id=env["company_id"],
        ))
        assert is_error(result)
        assert "submitted" in result["message"]
