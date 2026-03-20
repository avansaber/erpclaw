"""Tests for erpclaw-payroll payroll run processing actions.

Actions tested:
  - create-payroll-run
  - generate-salary-slips
  - get-salary-slip
  - list-salary-slips
  - submit-payroll-run
  - cancel-payroll-run
  - status
"""
import json
import pytest
from decimal import Decimal
from payroll_helpers import (
    call_action, ns, is_error, is_ok,
    seed_company, seed_employee, seed_naming_series,
    seed_account, seed_fiscal_year, seed_cost_center,
    build_payroll_env, load_db_query,
)

mod = load_db_query()


# ──────────────────────────────────────────────────────────────────────────────
# Helpers: set up a fully-ready payroll environment with component, structure,
# assignment, and FICA config so that salary slips can be generated.
# ──────────────────────────────────────────────────────────────────────────────

def _setup_payroll_ready(conn, env):
    """Create component, structure, assignment, and FICA config.

    Returns dict with additional IDs (component, structure, assignment).
    """
    # Salary component
    comp = call_action(mod.add_salary_component, conn, ns(
        name="Proc Test Base", component_type="earning",
        is_tax_applicable=None, is_statutory=None, is_pre_tax=None,
        variable_based_on_taxable_salary=None, depends_on_payment_days=None,
        gl_account_id=None, description=None,
    ))
    assert is_ok(comp)
    comp_id = comp["salary_component_id"]

    # Salary structure
    ss = call_action(mod.add_salary_structure, conn, ns(
        name="Proc Test Structure",
        company_id=env["company_id"],
        components=json.dumps([
            {"salary_component_id": comp_id, "amount": "5000"},
        ]),
        payroll_frequency=None,
    ))
    assert is_ok(ss)
    ss_id = ss["salary_structure_id"]

    # Salary assignment
    sa = call_action(mod.add_salary_assignment, conn, ns(
        employee_id=env["employee_id"],
        salary_structure_id=ss_id,
        base_amount="5000.00",
        effective_from="2026-01-01",
        effective_to=None,
    ))
    assert is_ok(sa)

    # FICA config for 2026
    fica = call_action(mod.update_fica_config, conn, ns(
        tax_year="2026",
        ss_wage_base="168600",
        ss_employee_rate="6.2",
        ss_employer_rate="6.2",
        medicare_employee_rate="1.45",
        medicare_employer_rate="1.45",
        additional_medicare_threshold="200000",
        additional_medicare_rate="0.9",
    ))
    assert is_ok(fica)

    return {
        "component_id": comp_id,
        "structure_id": ss_id,
        "assignment_id": sa["salary_assignment_id"],
    }


def _create_and_generate(conn, env):
    """Full setup: payroll env + config + create run + generate slips.

    Returns (env, setup_ids, payroll_run_id).
    """
    setup = _setup_payroll_ready(conn, env)

    # Create payroll run
    run = call_action(mod.create_payroll_run, conn, ns(
        company_id=env["company_id"],
        period_start="2026-01-01",
        period_end="2026-01-31",
        department_id=None,
        payroll_frequency="monthly",
    ))
    assert is_ok(run)
    run_id = run["payroll_run_id"]

    # Generate salary slips
    gen = call_action(mod.generate_salary_slips, conn, ns(
        payroll_run_id=run_id,
    ))
    assert is_ok(gen)

    return setup, run_id


# ──────────────────────────────────────────────────────────────────────────────
# create-payroll-run
# ──────────────────────────────────────────────────────────────────────────────

class TestCreatePayrollRun:
    def test_basic_create(self, conn, env):
        """Create a draft payroll run."""
        _setup_payroll_ready(conn, env)

        result = call_action(mod.create_payroll_run, conn, ns(
            company_id=env["company_id"],
            period_start="2026-02-01",
            period_end="2026-02-28",
            department_id=None,
            payroll_frequency="monthly",
        ))
        assert is_ok(result)
        assert "payroll_run_id" in result
        assert result["period_start"] == "2026-02-01"
        assert result["period_end"] == "2026-02-28"
        assert result["payroll_frequency"] == "monthly"
        assert "naming_series" in result

        # Verify in DB
        row = conn.execute(
            "SELECT * FROM payroll_run WHERE id = ?",
            (result["payroll_run_id"],),
        ).fetchone()
        assert row is not None
        assert row["status"] == "draft"
        assert row["company_id"] == env["company_id"]


# ──────────────────────────────────────────────────────────────────────────────
# generate-salary-slips
# ──────────────────────────────────────────────────────────────────────────────

class TestGenerateSalarySlips:
    def test_generate(self, conn, env):
        """Generate salary slips for a payroll run."""
        _setup_payroll_ready(conn, env)

        run = call_action(mod.create_payroll_run, conn, ns(
            company_id=env["company_id"],
            period_start="2026-03-01",
            period_end="2026-03-31",
            department_id=None,
            payroll_frequency="monthly",
        ))
        assert is_ok(run)
        run_id = run["payroll_run_id"]

        result = call_action(mod.generate_salary_slips, conn, ns(
            payroll_run_id=run_id,
        ))
        assert is_ok(result)
        assert result["slips_generated"] >= 1
        assert Decimal(result["total_gross"]) > 0
        assert Decimal(result["total_net"]) > 0

        # Verify salary slip exists in DB
        slips = conn.execute(
            "SELECT * FROM salary_slip WHERE payroll_run_id = ?",
            (run_id,),
        ).fetchall()
        assert len(slips) >= 1
        assert slips[0]["status"] == "draft"
        assert Decimal(slips[0]["gross_pay"]) > 0


# ──────────────────────────────────────────────────────────────────────────────
# get-salary-slip
# ──────────────────────────────────────────────────────────────────────────────

class TestGetSalarySlip:
    def test_get(self, conn, env):
        """Retrieve a salary slip with earnings and deductions."""
        _, run_id = _create_and_generate(conn, env)

        # Get the first salary slip
        slip_row = conn.execute(
            "SELECT id FROM salary_slip WHERE payroll_run_id = ? LIMIT 1",
            (run_id,),
        ).fetchone()
        assert slip_row is not None
        slip_id = slip_row["id"]

        result = call_action(mod.get_salary_slip, conn, ns(
            salary_slip_id=slip_id,
        ))
        assert is_ok(result)
        assert result["id"] == slip_id
        assert result["employee_id"] == env["employee_id"]
        assert "earnings" in result
        assert "deductions" in result
        assert len(result["earnings"]) >= 1
        assert Decimal(result["gross_pay"]) > 0


# ──────────────────────────────────────────────────────────────────────────────
# list-salary-slips
# ──────────────────────────────────────────────────────────────────────────────

class TestListSalarySlips:
    def test_list(self, conn, env):
        """List salary slips for a payroll run."""
        _, run_id = _create_and_generate(conn, env)

        result = call_action(mod.list_salary_slips, conn, ns(
            payroll_run_id=run_id,
            employee_id=None,
            status=None,
            limit="20",
            offset="0",
        ))
        assert is_ok(result)
        assert result["count"] >= 1
        assert len(result["slips"]) >= 1
        assert result["slips"][0]["payroll_run_id"] == run_id


# ──────────────────────────────────────────────────────────────────────────────
# submit-payroll-run
# ──────────────────────────────────────────────────────────────────────────────

class TestSubmitPayrollRun:
    def test_submit(self, conn, env):
        """Submit a payroll run and verify GL entries are created."""
        _, run_id = _create_and_generate(conn, env)

        result = call_action(mod.submit_payroll_run, conn, ns(
            payroll_run_id=run_id,
            cost_center_id=env["cost_center_id"],
        ))
        assert is_ok(result)
        assert result["payroll_run_id"] == run_id
        assert result["gl_entries"] > 0

        # Verify payroll run status in DB
        row = conn.execute(
            "SELECT status FROM payroll_run WHERE id = ?", (run_id,),
        ).fetchone()
        assert row["status"] == "submitted"

        # Verify salary slips are now submitted
        draft_count = conn.execute(
            "SELECT COUNT(*) FROM salary_slip WHERE payroll_run_id = ? AND status = 'draft'",
            (run_id,),
        ).fetchone()[0]
        assert draft_count == 0

        submitted_count = conn.execute(
            "SELECT COUNT(*) FROM salary_slip WHERE payroll_run_id = ? AND status = 'submitted'",
            (run_id,),
        ).fetchone()[0]
        assert submitted_count >= 1

        # Verify GL entries exist
        gl_count = conn.execute(
            "SELECT COUNT(*) FROM gl_entry WHERE voucher_type = 'payroll_entry' AND voucher_id = ?",
            (run_id,),
        ).fetchone()[0]
        assert gl_count > 0

        # Verify GL entries balance (debits == credits)
        gl_totals = conn.execute(
            """SELECT
                SUM(CAST(debit AS REAL)) as total_debit,
                SUM(CAST(credit AS REAL)) as total_credit
               FROM gl_entry
               WHERE voucher_type = 'payroll_entry' AND voucher_id = ?""",
            (run_id,),
        ).fetchone()
        assert abs(gl_totals["total_debit"] - gl_totals["total_credit"]) < 0.02


# ──────────────────────────────────────────────────────────────────────────────
# cancel-payroll-run
# ──────────────────────────────────────────────────────────────────────────────

class TestCancelPayrollRun:
    def test_cancel(self, conn, env):
        """Cancel a submitted payroll run and verify GL reversal."""
        _, run_id = _create_and_generate(conn, env)

        # First submit
        submit = call_action(mod.submit_payroll_run, conn, ns(
            payroll_run_id=run_id,
            cost_center_id=env["cost_center_id"],
        ))
        assert is_ok(submit)

        # Count GL entries before cancel
        gl_before = conn.execute(
            "SELECT COUNT(*) FROM gl_entry WHERE voucher_type = 'payroll_entry' AND voucher_id = ?",
            (run_id,),
        ).fetchone()[0]
        assert gl_before > 0

        # Now cancel
        result = call_action(mod.cancel_payroll_run, conn, ns(
            payroll_run_id=run_id,
        ))
        assert is_ok(result)
        assert result["payroll_run_id"] == run_id
        assert result["reversed_entries"] > 0

        # Verify payroll run status in DB
        row = conn.execute(
            "SELECT status FROM payroll_run WHERE id = ?", (run_id,),
        ).fetchone()
        assert row["status"] == "cancelled"

        # Verify salary slips are cancelled
        submitted_count = conn.execute(
            "SELECT COUNT(*) FROM salary_slip WHERE payroll_run_id = ? AND status = 'submitted'",
            (run_id,),
        ).fetchone()[0]
        assert submitted_count == 0

        cancelled_count = conn.execute(
            "SELECT COUNT(*) FROM salary_slip WHERE payroll_run_id = ? AND status = 'cancelled'",
            (run_id,),
        ).fetchone()[0]
        assert cancelled_count >= 1

        # GL entries should now have reversal entries (is_cancelled = 1)
        gl_after = conn.execute(
            "SELECT COUNT(*) FROM gl_entry WHERE voucher_type = 'payroll_entry' AND voucher_id = ?",
            (run_id,),
        ).fetchone()[0]
        assert gl_after > gl_before  # reversal entries added


# ──────────────────────────────────────────────────────────────────────────────
# status
# ──────────────────────────────────────────────────────────────────────────────

class TestStatus:
    def test_status(self, conn, env):
        """Retrieve payroll status summary after setting up data."""
        _setup_payroll_ready(conn, env)

        result = call_action(mod.status_action, conn, ns(
            company_id=env["company_id"],
        ))
        assert is_ok(result)
        assert "total_salary_components" in result
        assert result["total_salary_components"] >= 1
        assert "total_salary_structures" in result
        assert "total_salary_assignments" in result
        assert "total_payroll_runs" in result
        assert "fica_configs" in result
        assert result["fica_configs"] >= 1
