"""Tests for Sprint 6 payroll features (#22a-d).

Actions tested:
  - add-state-tax-slab (Feature #22a: Multi-State Payroll)
  - update-employee-state-config (Feature #22a)
  - add-overtime-policy (Feature #22b: Overtime Calculation)
  - calculate-overtime (Feature #22b)
  - calculate-retro-pay (Feature #22c: Retroactive Pay)
  - generate-salary-slips with state tax + supplemental wages (#22a, #22d)
"""
import json
import pytest
import uuid
from datetime import date, timedelta
from decimal import Decimal
from payroll_helpers import (
    call_action, ns, is_error, is_ok,
    seed_company, seed_employee, seed_naming_series,
    seed_account, seed_fiscal_year, seed_cost_center,
    build_payroll_env, load_db_query,
)

mod = load_db_query()


# ==============================================================================
# Helpers
# ==============================================================================


def _setup_payroll_ready(conn, env, base_amount="5000.00"):
    """Create component, structure, assignment, and FICA config."""
    comp = call_action(mod.add_salary_component, conn, ns(
        name=f"S6 Base {uuid.uuid4().hex[:6]}",
        component_type="earning",
        is_tax_applicable=None, is_statutory=None, is_pre_tax=None,
        variable_based_on_taxable_salary=None, depends_on_payment_days=None,
        gl_account_id=None, description=None, is_supplemental=None,
    ))
    assert is_ok(comp)
    comp_id = comp["salary_component_id"]

    ss = call_action(mod.add_salary_structure, conn, ns(
        name=f"S6 Structure {uuid.uuid4().hex[:6]}",
        company_id=env["company_id"],
        components=json.dumps([
            {"salary_component_id": comp_id, "amount": base_amount},
        ]),
        payroll_frequency=None,
    ))
    assert is_ok(ss)
    ss_id = ss["salary_structure_id"]

    sa = call_action(mod.add_salary_assignment, conn, ns(
        employee_id=env["employee_id"],
        salary_structure_id=ss_id,
        base_amount=base_amount,
        effective_from="2026-01-01",
        effective_to=None,
    ))
    assert is_ok(sa)

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


# ==============================================================================
# Feature #22a: Multi-State Payroll
# ==============================================================================


class TestAddStateTaxSlab:
    def test_add_state_slab_basic(self, conn, env):
        """Add a California state tax bracket."""
        result = call_action(mod.add_state_tax_slab, conn, ns(
            state_code="CA",
            bracket_start="0",
            bracket_end="50000",
            rate="4.0",
            filing_status="single",
        ))
        assert is_ok(result)
        assert result["state_code"] == "CA"
        assert result["rate"] == "4.00"
        assert result["bracket_start"] == "0.00"
        assert result["bracket_end"] == "50000.00"

    def test_add_multiple_brackets(self, conn, env):
        """Add multiple state tax brackets for progressive taxation."""
        r1 = call_action(mod.add_state_tax_slab, conn, ns(
            state_code="NY",
            bracket_start="0",
            bracket_end="8500",
            rate="4.0",
            filing_status="single",
        ))
        assert is_ok(r1)

        r2 = call_action(mod.add_state_tax_slab, conn, ns(
            state_code="NY",
            bracket_start="8500",
            bracket_end="11700",
            rate="4.5",
            filing_status="single",
        ))
        assert is_ok(r2)

        r3 = call_action(mod.add_state_tax_slab, conn, ns(
            state_code="NY",
            bracket_start="11700",
            bracket_end=None,
            rate="5.25",
            filing_status="single",
        ))
        assert is_ok(r3)

    def test_add_slab_negative_rate(self, conn, env):
        """Negative rate should be rejected."""
        result = call_action(mod.add_state_tax_slab, conn, ns(
            state_code="TX",
            bracket_start="0",
            bracket_end="50000",
            rate="-1",
            filing_status="single",
        ))
        assert is_error(result)

    def test_add_slab_bracket_end_less_than_start(self, conn, env):
        """bracket_end less than bracket_start should fail."""
        result = call_action(mod.add_state_tax_slab, conn, ns(
            state_code="FL",
            bracket_start="50000",
            bracket_end="10000",
            rate="5.0",
            filing_status="single",
        ))
        assert is_error(result)

    def test_add_slab_missing_state_code(self, conn, env):
        """Missing state-code should fail."""
        result = call_action(mod.add_state_tax_slab, conn, ns(
            state_code=None,
            bracket_start="0",
            bracket_end="50000",
            rate="4.0",
            filing_status="single",
        ))
        assert is_error(result)


class TestUpdateEmployeeStateConfig:
    def test_set_employee_state_config(self, conn, env):
        """Set work and residence state for an employee."""
        result = call_action(mod.update_employee_state_config, conn, ns(
            employee_id=env["employee_id"],
            work_state="CA",
            residence_state="CA",
        ))
        assert is_ok(result)
        assert result["work_state"] == "CA"
        assert result["residence_state"] == "CA"

    def test_update_employee_state_config(self, conn, env):
        """Upsert: update existing state config."""
        call_action(mod.update_employee_state_config, conn, ns(
            employee_id=env["employee_id"],
            work_state="CA",
            residence_state="CA",
        ))

        result = call_action(mod.update_employee_state_config, conn, ns(
            employee_id=env["employee_id"],
            work_state="NY",
            residence_state="NJ",
        ))
        assert is_ok(result)
        assert result["work_state"] == "NY"
        assert result["residence_state"] == "NJ"

    def test_nonexistent_employee(self, conn, env):
        """Setting config for nonexistent employee should fail."""
        result = call_action(mod.update_employee_state_config, conn, ns(
            employee_id="nonexistent-emp",
            work_state="CA",
            residence_state="CA",
        ))
        assert is_error(result)

    def test_state_tax_integration_in_payroll(self, conn, env):
        """Generate salary slips with state tax slabs configured.

        Verifies that state income tax is actually deducted when
        an employee has state config and state tax slabs exist.
        """
        setup = _setup_payroll_ready(conn, env, base_amount="5000.00")

        # Add CA state tax slabs
        call_action(mod.add_state_tax_slab, conn, ns(
            state_code="CA", bracket_start="0", bracket_end="10099",
            rate="1.0", filing_status="single",
        ))
        call_action(mod.add_state_tax_slab, conn, ns(
            state_code="CA", bracket_start="10099", bracket_end="23942",
            rate="2.0", filing_status="single",
        ))
        call_action(mod.add_state_tax_slab, conn, ns(
            state_code="CA", bracket_start="23942", bracket_end=None,
            rate="4.0", filing_status="single",
        ))

        # Set employee state config
        call_action(mod.update_employee_state_config, conn, ns(
            employee_id=env["employee_id"],
            work_state="CA",
            residence_state="CA",
        ))

        # Create payroll run and generate slips
        run = call_action(mod.create_payroll_run, conn, ns(
            company_id=env["company_id"],
            period_start="2026-03-01",
            period_end="2026-03-31",
            department_id=None,
            payroll_frequency="monthly",
        ))
        assert is_ok(run)

        gen = call_action(mod.generate_salary_slips, conn, ns(
            payroll_run_id=run["payroll_run_id"],
        ))
        assert is_ok(gen)

        # The total deductions should include state tax (>0)
        total_ded = Decimal(gen["total_deductions"])
        assert total_ded > Decimal("0"), "Should have deductions including state tax"

        # Verify state tax component exists in salary slip details
        slip = call_action(mod.list_salary_slips, conn, ns(
            payroll_run_id=run["payroll_run_id"],
            employee_id=None, status=None,
            limit="20", offset="0", search=None,
        ))
        assert is_ok(slip)
        assert slip["count"] == 1

        # Get the slip details
        slip_id = slip["slips"][0]["id"]
        detail = call_action(mod.get_salary_slip, conn, ns(
            salary_slip_id=slip_id,
        ))
        assert is_ok(detail)

        # Check that State Income Tax deduction exists and is > 0
        state_tax_found = False
        for d in detail.get("details", []):
            if d.get("component_name") == "State Income Tax":
                state_tax_found = True
                assert Decimal(d["amount"]) > Decimal("0"), \
                    "State income tax should be > 0"
                break
        assert state_tax_found, "State Income Tax component should be in salary slip"


# ==============================================================================
# Feature #22b: Overtime Calculation
# ==============================================================================


class TestAddOvertimePolicy:
    def test_add_basic_overtime_policy(self, conn, env):
        """Add a standard overtime policy."""
        result = call_action(mod.add_overtime_policy, conn, ns(
            company_id=env["company_id"],
            weekly_threshold="40",
            daily_threshold=None,
            ot_multiplier="1.5",
            double_ot_multiplier="2.0",
        ))
        assert is_ok(result)
        assert result["weekly_threshold"] == "40"
        assert result["ot_multiplier"] == "1.5"

    def test_upsert_overtime_policy(self, conn, env):
        """Upserting should update the existing policy."""
        call_action(mod.add_overtime_policy, conn, ns(
            company_id=env["company_id"],
            weekly_threshold="40",
            daily_threshold=None,
            ot_multiplier="1.5",
            double_ot_multiplier="2.0",
        ))
        result = call_action(mod.add_overtime_policy, conn, ns(
            company_id=env["company_id"],
            weekly_threshold="35",
            daily_threshold="8",
            ot_multiplier="1.75",
            double_ot_multiplier="2.5",
        ))
        assert is_ok(result)
        assert result["weekly_threshold"] == "35"
        assert result["ot_multiplier"] == "1.75"

    def test_overtime_policy_invalid_company(self, conn, env):
        """Policy for nonexistent company should fail."""
        result = call_action(mod.add_overtime_policy, conn, ns(
            company_id="nonexistent-company",
            weekly_threshold="40",
            daily_threshold=None,
            ot_multiplier="1.5",
            double_ot_multiplier="2.0",
        ))
        assert is_error(result)


class TestCalculateOvertime:
    def test_calculate_overtime_basic(self, conn, env):
        """Calculate overtime for an employee with attendance records."""
        # Set up overtime policy
        call_action(mod.add_overtime_policy, conn, ns(
            company_id=env["company_id"],
            weekly_threshold="40",
            daily_threshold=None,
            ot_multiplier="1.5",
            double_ot_multiplier="2.0",
        ))

        # Set up salary assignment (needed for hourly rate derivation)
        _setup_payroll_ready(conn, env, base_amount="5000.00")

        # Mark attendance with overtime hours (50 hours in one week = 10 OT)
        employee_id = env["employee_id"]
        for i in range(5):
            day = (date(2026, 3, 2) + timedelta(days=i)).isoformat()
            conn.execute(
                """INSERT INTO attendance
                   (id, employee_id, attendance_date, status, working_hours)
                   VALUES (?, ?, ?, 'present', '10')""",
                (str(uuid.uuid4()), employee_id, day),
            )
        conn.commit()

        result = call_action(mod.calculate_overtime, conn, ns(
            employee_id=employee_id,
            period_start="2026-03-01",
            period_end="2026-03-07",
            hourly_rate=None,
        ))
        assert is_ok(result)
        assert Decimal(result["total_hours"]) == Decimal("50")
        assert Decimal(result["ot_hours"]) == Decimal("10")
        assert Decimal(result["ot_amount"]) > Decimal("0")

    def test_no_overtime_policy(self, conn, env):
        """Calculate overtime without policy should fail."""
        result = call_action(mod.calculate_overtime, conn, ns(
            employee_id=env["employee_id"],
            period_start="2026-03-01",
            period_end="2026-03-31",
            hourly_rate="25.00",
        ))
        assert is_error(result)

    def test_calculate_overtime_with_explicit_hourly_rate(self, conn, env):
        """Calculate overtime with explicit hourly rate."""
        call_action(mod.add_overtime_policy, conn, ns(
            company_id=env["company_id"],
            weekly_threshold="40",
            daily_threshold=None,
            ot_multiplier="1.5",
            double_ot_multiplier="2.0",
        ))

        employee_id = env["employee_id"]
        # 45 hours in one week
        for i in range(5):
            day = (date(2026, 4, 6) + timedelta(days=i)).isoformat()
            conn.execute(
                """INSERT INTO attendance
                   (id, employee_id, attendance_date, status, working_hours)
                   VALUES (?, ?, ?, 'present', '9')""",
                (str(uuid.uuid4()), employee_id, day),
            )
        conn.commit()

        result = call_action(mod.calculate_overtime, conn, ns(
            employee_id=employee_id,
            period_start="2026-04-06",
            period_end="2026-04-12",
            hourly_rate="30.00",
        ))
        assert is_ok(result)
        assert Decimal(result["total_hours"]) == Decimal("45")
        assert Decimal(result["ot_hours"]) == Decimal("5")
        # OT amount = 5 hours * $30.00 * 1.5 = $225.00
        assert Decimal(result["ot_amount"]) == Decimal("225.00")


# ==============================================================================
# Feature #22c: Retroactive Pay
# ==============================================================================


class TestCalculateRetroPay:
    def test_retro_pay_with_salary_slips(self, conn, env):
        """Calculate retro pay when salary slips exist for past periods."""
        setup = _setup_payroll_ready(conn, env, base_amount="5000.00")

        # Create and generate salary slip for Jan 2026
        run1 = call_action(mod.create_payroll_run, conn, ns(
            company_id=env["company_id"],
            period_start="2026-01-01",
            period_end="2026-01-31",
            department_id=None,
            payroll_frequency="monthly",
        ))
        assert is_ok(run1)
        gen1 = call_action(mod.generate_salary_slips, conn, ns(
            payroll_run_id=run1["payroll_run_id"],
        ))
        assert is_ok(gen1)

        # Now add a second salary assignment with a raise (effective Feb)
        sa2 = call_action(mod.add_salary_assignment, conn, ns(
            employee_id=env["employee_id"],
            salary_structure_id=setup["structure_id"],
            base_amount="6000.00",
            effective_from="2026-02-01",
            effective_to=None,
        ))
        assert is_ok(sa2)

        # Calculate retro pay
        result = call_action(mod.calculate_retro_pay, conn, ns(
            employee_id=env["employee_id"],
            from_date=None, to_date=None,
        ))
        assert is_ok(result)
        assert result["periods_affected"] == 1
        # $6000 - $5000 = $1000 per period
        assert Decimal(result["total_retro_amount"]) == Decimal("1000.00")
        assert len(result["details"]) == 1
        assert result["details"][0]["old_rate"] == "5000.00"
        assert result["details"][0]["new_rate"] == "6000.00"

    def test_retro_pay_no_raise(self, conn, env):
        """Retro pay calculation when rate didn't increase should return 0."""
        setup = _setup_payroll_ready(conn, env, base_amount="5000.00")

        # Add same-rate assignment
        sa2 = call_action(mod.add_salary_assignment, conn, ns(
            employee_id=env["employee_id"],
            salary_structure_id=setup["structure_id"],
            base_amount="5000.00",
            effective_from="2026-02-01",
            effective_to=None,
        ))
        assert is_ok(sa2)

        result = call_action(mod.calculate_retro_pay, conn, ns(
            employee_id=env["employee_id"],
            from_date=None, to_date=None,
        ))
        assert is_ok(result)
        assert result["periods_affected"] == 0
        assert result["total_retro_amount"] == "0.00"

    def test_retro_pay_single_assignment(self, conn, env):
        """Retro pay with only one assignment should fail."""
        _setup_payroll_ready(conn, env, base_amount="5000.00")

        result = call_action(mod.calculate_retro_pay, conn, ns(
            employee_id=env["employee_id"],
            from_date=None, to_date=None,
        ))
        assert is_error(result)

    def test_retro_pay_nonexistent_employee(self, conn, env):
        """Retro pay for nonexistent employee should fail."""
        result = call_action(mod.calculate_retro_pay, conn, ns(
            employee_id="nonexistent-emp",
            from_date=None, to_date=None,
        ))
        assert is_error(result)

    def test_retro_pay_records_adjustments(self, conn, env):
        """Retro pay should create retro_pay_adjustment records."""
        setup = _setup_payroll_ready(conn, env, base_amount="4000.00")

        # Create Jan payroll
        run1 = call_action(mod.create_payroll_run, conn, ns(
            company_id=env["company_id"],
            period_start="2026-01-01",
            period_end="2026-01-31",
            department_id=None,
            payroll_frequency="monthly",
        ))
        assert is_ok(run1)
        gen1 = call_action(mod.generate_salary_slips, conn, ns(
            payroll_run_id=run1["payroll_run_id"],
        ))
        assert is_ok(gen1)

        # Add raise to $5000 effective Feb
        sa2 = call_action(mod.add_salary_assignment, conn, ns(
            employee_id=env["employee_id"],
            salary_structure_id=setup["structure_id"],
            base_amount="5000.00",
            effective_from="2026-02-01",
            effective_to=None,
        ))
        assert is_ok(sa2)

        result = call_action(mod.calculate_retro_pay, conn, ns(
            employee_id=env["employee_id"],
            from_date=None, to_date=None,
        ))
        assert is_ok(result)

        # Verify retro_pay_adjustment record in DB
        rows = conn.execute(
            "SELECT * FROM retro_pay_adjustment WHERE employee_id = ?",
            (env["employee_id"],),
        ).fetchall()
        assert len(rows) >= 1
        adj = dict(rows[0])
        assert adj["status"] == "pending"
        assert adj["old_rate"] == "4000.00"
        assert adj["new_rate"] == "5000.00"
        assert adj["adjustment_amount"] == "1000.00"


# ==============================================================================
# Feature #22d: Supplemental Wages
# ==============================================================================


class TestSupplementalWages:
    def test_add_supplemental_component(self, conn, env):
        """Create a salary component marked as supplemental."""
        result = call_action(mod.add_salary_component, conn, ns(
            name="Signing Bonus",
            component_type="earning",
            is_tax_applicable="1",
            is_statutory="0",
            is_pre_tax="0",
            variable_based_on_taxable_salary="0",
            depends_on_payment_days="0",
            gl_account_id=None,
            description="One-time signing bonus",
            is_supplemental="1",
        ))
        assert is_ok(result)

        # Verify in DB
        row = conn.execute(
            "SELECT is_supplemental FROM salary_component WHERE id = ?",
            (result["salary_component_id"],),
        ).fetchone()
        assert row["is_supplemental"] == 1

    def test_supplemental_flat_tax_in_payroll(self, conn, env):
        """Supplemental wages should be taxed at flat 22% federal rate.

        Creates a salary structure with a regular base component and a
        supplemental bonus component, then verifies the federal tax includes
        the flat supplemental withholding.
        """
        # Create regular component
        reg_comp = call_action(mod.add_salary_component, conn, ns(
            name=f"S6 Regular Base {uuid.uuid4().hex[:6]}",
            component_type="earning",
            is_tax_applicable=None, is_statutory=None, is_pre_tax=None,
            variable_based_on_taxable_salary=None, depends_on_payment_days=None,
            gl_account_id=None, description=None, is_supplemental="0",
        ))
        assert is_ok(reg_comp)

        # Create supplemental component (bonus)
        supp_comp = call_action(mod.add_salary_component, conn, ns(
            name=f"S6 Bonus {uuid.uuid4().hex[:6]}",
            component_type="earning",
            is_tax_applicable="1", is_statutory="0", is_pre_tax="0",
            variable_based_on_taxable_salary="0", depends_on_payment_days="0",
            gl_account_id=None, description="Supplemental bonus",
            is_supplemental="1",
        ))
        assert is_ok(supp_comp)

        # Create salary structure with both components
        ss = call_action(mod.add_salary_structure, conn, ns(
            name=f"S6 SupStruct {uuid.uuid4().hex[:6]}",
            company_id=env["company_id"],
            components=json.dumps([
                {"salary_component_id": reg_comp["salary_component_id"],
                 "amount": "5000"},
                {"salary_component_id": supp_comp["salary_component_id"],
                 "amount": "2000"},
            ]),
            payroll_frequency=None,
        ))
        assert is_ok(ss)

        # Assignment
        sa = call_action(mod.add_salary_assignment, conn, ns(
            employee_id=env["employee_id"],
            salary_structure_id=ss["salary_structure_id"],
            base_amount="5000.00",
            effective_from="2026-01-01",
            effective_to=None,
        ))
        assert is_ok(sa)

        # FICA
        call_action(mod.update_fica_config, conn, ns(
            tax_year="2026",
            ss_wage_base="168600", ss_employee_rate="6.2",
            ss_employer_rate="6.2", medicare_employee_rate="1.45",
            medicare_employer_rate="1.45",
            additional_medicare_threshold="200000",
            additional_medicare_rate="0.9",
        ))

        # Federal tax slab (needed for regular wages)
        call_action(mod.add_income_tax_slab, conn, ns(
            name="Fed 2026 S6 Test",
            tax_jurisdiction="federal",
            effective_from="2026-01-01",
            filing_status="single",
            state_code=None,
            standard_deduction="14600",
            rates=json.dumps([
                {"from_amount": "0", "to_amount": "11600", "rate": "10"},
                {"from_amount": "11600", "to_amount": "47150", "rate": "12"},
                {"from_amount": "47150", "to_amount": "100525", "rate": "22"},
            ]),
        ))

        # Create payroll run
        run = call_action(mod.create_payroll_run, conn, ns(
            company_id=env["company_id"],
            period_start="2026-05-01",
            period_end="2026-05-31",
            department_id=None,
            payroll_frequency="monthly",
        ))
        assert is_ok(run)

        gen = call_action(mod.generate_salary_slips, conn, ns(
            payroll_run_id=run["payroll_run_id"],
        ))
        assert is_ok(gen)

        # Gross should be $7000 (5000 regular + 2000 supplemental)
        assert Decimal(gen["total_gross"]) == Decimal("7000.00")

        # The federal tax should include flat 22% on the $2000 supplemental = $440
        # plus progressive tax on the $5000 regular wages
        # So federal tax should be > $440
        slip = call_action(mod.list_salary_slips, conn, ns(
            payroll_run_id=run["payroll_run_id"],
            employee_id=None, status=None,
            limit="20", offset="0", search=None,
        ))
        assert is_ok(slip)
        slip_id = slip["slips"][0]["id"]

        detail = call_action(mod.get_salary_slip, conn, ns(
            salary_slip_id=slip_id,
        ))
        assert is_ok(detail)

        # Find federal income tax
        fed_tax_amt = Decimal("0")
        for d in detail.get("details", []):
            if d.get("component_name") == "Federal Income Tax":
                fed_tax_amt = Decimal(d["amount"])
                break

        # Federal tax should include at least the $440 supplemental flat tax
        assert fed_tax_amt >= Decimal("440.00"), \
            f"Federal tax {fed_tax_amt} should be >= $440 (22% of $2000 supplemental)"
