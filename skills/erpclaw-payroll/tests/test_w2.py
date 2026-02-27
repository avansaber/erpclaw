"""Tests for W-2 data generation."""
import json
from decimal import Decimal
from helpers import _call_action, setup_payroll_environment, create_test_employee
from db_query import ACTIONS


def _run_payroll(fresh_db, env, period_start, period_end):
    """Helper: create run, generate slips, submit."""
    run = _call_action(ACTIONS["create-payroll-run"], fresh_db,
                       company_id=env["company_id"],
                       period_start=period_start, period_end=period_end)
    _call_action(ACTIONS["generate-salary-slips"], fresh_db,
                 payroll_run_id=run["payroll_run_id"])
    _call_action(ACTIONS["submit-payroll-run"], fresh_db,
                 payroll_run_id=run["payroll_run_id"])
    return run


def _setup_w2_env(fresh_db):
    """Full setup for W-2 tests (reuses _setup_full_payroll logic)."""
    env = setup_payroll_environment(fresh_db)

    basic = _call_action(ACTIONS["add-salary-component"], fresh_db,
                         name="Basic Salary", component_type="earning",
                         gl_account_id=env["salary_expense_id"])
    components = json.dumps([
        {"salary_component_id": basic["salary_component_id"], "amount": "5000"},
    ])
    struct = _call_action(ACTIONS["add-salary-structure"], fresh_db,
                          name="Standard", company_id=env["company_id"],
                          components=components)

    for emp_id in env["employee_ids"]:
        _call_action(ACTIONS["add-salary-assignment"], fresh_db,
                     employee_id=emp_id,
                     salary_structure_id=struct["salary_structure_id"],
                     base_amount="5000",
                     effective_from="2026-01-01",
                     company_id=env["company_id"])

    _call_action(ACTIONS["update-fica-config"], fresh_db,
                 tax_year="2026", ss_wage_base="168600",
                 ss_employee_rate="6.2", ss_employer_rate="6.2",
                 medicare_employee_rate="1.45", medicare_employer_rate="1.45",
                 additional_medicare_threshold="200000", additional_medicare_rate="0.9")

    rates = json.dumps([
        {"from_amount": "0", "to_amount": "11600", "rate": "10"},
        {"from_amount": "11600", "to_amount": "47150", "rate": "12"},
        {"from_amount": "47150", "to_amount": "100525", "rate": "22"},
    ])
    _call_action(ACTIONS["add-income-tax-slab"], fresh_db,
                 name="2026 Federal Single", tax_jurisdiction="federal",
                 filing_status="single", effective_from="2026-01-01",
                 standard_deduction="14600", rates=rates)
    _call_action(ACTIONS["add-income-tax-slab"], fresh_db,
                 name="2026 Federal MFJ", tax_jurisdiction="federal",
                 filing_status="married_jointly", effective_from="2026-01-01",
                 standard_deduction="29200", rates=rates)

    env["structure_id"] = struct["salary_structure_id"]
    return env


def test_generate_w2_basic(fresh_db):
    """Generate W-2 data for a single month."""
    env = _setup_w2_env(fresh_db)
    _run_payroll(fresh_db, env, "2026-01-01", "2026-01-31")

    result = _call_action(ACTIONS["generate-w2-data"], fresh_db,
                          tax_year="2026", company_id=env["company_id"])
    assert result["status"] == "ok"
    assert result["employee_count"] == 2

    # Each employee has W-2 data
    for w2 in result["w2_data"]:
        assert "employee_id" in w2
        assert "boxes" in w2
        # Box 5 (Medicare wages) = gross = 5000
        assert Decimal(w2["boxes"]["5"]) == Decimal("5000.00")
        # Box 3 (SS wages) = min(gross, ss_wage_base) = 5000
        assert Decimal(w2["boxes"]["3"]) == Decimal("5000.00")
        # Box 4 (SS tax) = 5000 * 6.2% = 310
        assert Decimal(w2["boxes"]["4"]) == Decimal("310.00")
        # Box 6 (Medicare tax) = 5000 * 1.45% = 72.50
        assert Decimal(w2["boxes"]["6"]) == Decimal("72.50")


def test_generate_w2_multiple_months(fresh_db):
    """W-2 aggregates across multiple payroll runs."""
    env = _setup_w2_env(fresh_db)
    _run_payroll(fresh_db, env, "2026-01-01", "2026-01-31")
    _run_payroll(fresh_db, env, "2026-02-01", "2026-02-28")

    result = _call_action(ACTIONS["generate-w2-data"], fresh_db,
                          tax_year="2026", company_id=env["company_id"])
    assert result["status"] == "ok"

    for w2 in result["w2_data"]:
        # 2 months * 5000 = 10000 gross
        assert Decimal(w2["boxes"]["5"]) == Decimal("10000.00")
        assert Decimal(w2["boxes"]["3"]) == Decimal("10000.00")


def test_generate_w2_with_pretax(fresh_db):
    """W-2 Box 12 shows pre-tax deductions (401k, HSA)."""
    env = setup_payroll_environment(fresh_db)

    # Employee with 401k and HSA
    emp_id = create_test_employee(
        fresh_db, env["company_id"], "Charlie", "Brown",
        department_id=env["department_id"],
        federal_filing_status="single",
        employee_401k_rate="5",
        hsa_contribution="100",
    )

    basic = _call_action(ACTIONS["add-salary-component"], fresh_db,
                         name="Basic Salary", component_type="earning",
                         gl_account_id=env["salary_expense_id"])
    components = json.dumps([
        {"salary_component_id": basic["salary_component_id"], "amount": "8000"},
    ])
    struct = _call_action(ACTIONS["add-salary-structure"], fresh_db,
                          name="Executive", company_id=env["company_id"],
                          components=components)
    _call_action(ACTIONS["add-salary-assignment"], fresh_db,
                 employee_id=emp_id,
                 salary_structure_id=struct["salary_structure_id"],
                 base_amount="8000",
                 effective_from="2026-01-01",
                 company_id=env["company_id"])

    _call_action(ACTIONS["update-fica-config"], fresh_db,
                 tax_year="2026", ss_wage_base="168600",
                 ss_employee_rate="6.2", ss_employer_rate="6.2",
                 medicare_employee_rate="1.45", medicare_employer_rate="1.45",
                 additional_medicare_threshold="200000", additional_medicare_rate="0.9")
    rates = json.dumps([
        {"from_amount": "0", "to_amount": "11600", "rate": "10"},
        {"from_amount": "11600", "to_amount": "47150", "rate": "12"},
    ])
    _call_action(ACTIONS["add-income-tax-slab"], fresh_db,
                 name="2026 Federal Single", tax_jurisdiction="federal",
                 filing_status="single", effective_from="2026-01-01",
                 standard_deduction="14600", rates=rates)

    run = _call_action(ACTIONS["create-payroll-run"], fresh_db,
                       company_id=env["company_id"],
                       period_start="2026-01-01", period_end="2026-01-31")
    _call_action(ACTIONS["generate-salary-slips"], fresh_db,
                 payroll_run_id=run["payroll_run_id"])
    _call_action(ACTIONS["submit-payroll-run"], fresh_db,
                 payroll_run_id=run["payroll_run_id"])

    result = _call_action(ACTIONS["generate-w2-data"], fresh_db,
                          tax_year="2026", company_id=env["company_id"])
    assert result["status"] == "ok"

    charlie_w2 = [w for w in result["w2_data"] if w["employee_id"] == emp_id][0]

    # Box 1 (wages) = gross - pretax = 8000 - 400 (5% 401k) - 100 (HSA) = 7500
    assert Decimal(charlie_w2["boxes"]["1"]) == Decimal("7500.00")

    # Box 12 should have 401k (code D) = 400 and HSA (code W) = 100
    box12 = charlie_w2["boxes"].get("12", {})
    assert Decimal(box12.get("D", "0")) == Decimal("400.00")
    assert Decimal(box12.get("W", "0")) == Decimal("100.00")
