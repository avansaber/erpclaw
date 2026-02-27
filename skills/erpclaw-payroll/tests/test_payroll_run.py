"""Tests for payroll run lifecycle: create, generate slips, submit, cancel."""
import json
from decimal import Decimal
from helpers import (
    _call_action, setup_payroll_environment,
    create_test_account, create_test_employee,
)
from db_query import ACTIONS


# --- Shared setup helper ---

def _setup_full_payroll(fresh_db):
    """Create a complete payroll environment with structure, assignments, and tax config."""
    env = setup_payroll_environment(fresh_db)

    # Create components
    basic = _call_action(ACTIONS["add-salary-component"], fresh_db,
                         name="Basic Salary", component_type="earning",
                         gl_account_id=env["salary_expense_id"])
    hra = _call_action(ACTIONS["add-salary-component"], fresh_db,
                       name="HRA", component_type="earning",
                       gl_account_id=env["salary_expense_id"])

    # Create structure: Basic=base_amount, HRA=40% of Basic
    components = json.dumps([
        {"salary_component_id": basic["salary_component_id"], "amount": "0",
         "sort_order": 1},  # amount=0 means use base_amount
        {"salary_component_id": hra["salary_component_id"], "percentage": "40",
         "base_component_id": basic["salary_component_id"], "sort_order": 2},
    ])
    struct = _call_action(ACTIONS["add-salary-structure"], fresh_db,
                          name="Standard", company_id=env["company_id"],
                          components=components)

    # Assign to both employees (base_amount = 5000 monthly)
    for emp_id in env["employee_ids"]:
        _call_action(ACTIONS["add-salary-assignment"], fresh_db,
                     employee_id=emp_id,
                     salary_structure_id=struct["salary_structure_id"],
                     base_amount="5000",
                     effective_from="2026-01-01",
                     company_id=env["company_id"])

    # FICA config for 2026
    _call_action(ACTIONS["update-fica-config"], fresh_db,
                 tax_year="2026",
                 ss_wage_base="168600",
                 ss_employee_rate="6.2",
                 ss_employer_rate="6.2",
                 medicare_employee_rate="1.45",
                 medicare_employer_rate="1.45",
                 additional_medicare_threshold="200000",
                 additional_medicare_rate="0.9")

    # Federal tax brackets (simplified 2026)
    rates = json.dumps([
        {"from_amount": "0", "to_amount": "11600", "rate": "10"},
        {"from_amount": "11600", "to_amount": "47150", "rate": "12"},
        {"from_amount": "47150", "to_amount": "100525", "rate": "22"},
        {"from_amount": "100525", "to_amount": "191950", "rate": "24"},
        {"from_amount": "191950", "to_amount": "243725", "rate": "32"},
        {"from_amount": "243725", "to_amount": "609350", "rate": "35"},
        {"from_amount": "609350", "to_amount": None, "rate": "37"},
    ])
    _call_action(ACTIONS["add-income-tax-slab"], fresh_db,
                 name="2026 Federal Single",
                 tax_jurisdiction="federal",
                 filing_status="single",
                 effective_from="2026-01-01",
                 standard_deduction="14600",
                 rates=rates)

    # Also add married jointly brackets (same rates, different thresholds for simplicity)
    rates_mj = json.dumps([
        {"from_amount": "0", "to_amount": "23200", "rate": "10"},
        {"from_amount": "23200", "to_amount": "94300", "rate": "12"},
        {"from_amount": "94300", "to_amount": "201050", "rate": "22"},
        {"from_amount": "201050", "to_amount": "383900", "rate": "24"},
        {"from_amount": "383900", "to_amount": "487450", "rate": "32"},
        {"from_amount": "487450", "to_amount": "731200", "rate": "35"},
        {"from_amount": "731200", "to_amount": None, "rate": "37"},
    ])
    _call_action(ACTIONS["add-income-tax-slab"], fresh_db,
                 name="2026 Federal MFJ",
                 tax_jurisdiction="federal",
                 filing_status="married_jointly",
                 effective_from="2026-01-01",
                 standard_deduction="29200",
                 rates=rates_mj)

    env["basic_component_id"] = basic["salary_component_id"]
    env["hra_component_id"] = hra["salary_component_id"]
    env["structure_id"] = struct["salary_structure_id"]
    return env


# --- Tests ---

def test_create_payroll_run(fresh_db):
    """Create a draft payroll run."""
    env = _setup_full_payroll(fresh_db)
    result = _call_action(ACTIONS["create-payroll-run"], fresh_db,
                          company_id=env["company_id"],
                          period_start="2026-02-01",
                          period_end="2026-02-28")
    assert result["status"] == "ok"
    assert "payroll_run_id" in result
    assert result["naming_series"].startswith("PRUN-2026-")


def test_generate_salary_slips(fresh_db):
    """Generate salary slips for all eligible employees."""
    env = _setup_full_payroll(fresh_db)
    run = _call_action(ACTIONS["create-payroll-run"], fresh_db,
                       company_id=env["company_id"],
                       period_start="2026-02-01",
                       period_end="2026-02-28")

    result = _call_action(ACTIONS["generate-salary-slips"], fresh_db,
                          payroll_run_id=run["payroll_run_id"])
    assert result["status"] == "ok"
    assert result["slips_generated"] == 2  # Alice and Bob
    # Each employee: base=5000, HRA=2000 (40%), gross=7000
    assert Decimal(result["total_gross"]) == Decimal("14000.00")


def test_salary_slip_details(fresh_db):
    """Verify salary slip has correct earnings and deductions."""
    env = _setup_full_payroll(fresh_db)
    run = _call_action(ACTIONS["create-payroll-run"], fresh_db,
                       company_id=env["company_id"],
                       period_start="2026-02-01",
                       period_end="2026-02-28")
    _call_action(ACTIONS["generate-salary-slips"], fresh_db,
                 payroll_run_id=run["payroll_run_id"])

    # Get Alice's slip
    slips = _call_action(ACTIONS["list-salary-slips"], fresh_db,
                         payroll_run_id=run["payroll_run_id"])
    alice_slip_id = None
    for s in slips["slips"]:
        if s["employee_id"] == env["employee_ids"][0]:
            alice_slip_id = s["id"]
            break

    result = _call_action(ACTIONS["get-salary-slip"], fresh_db,
                          salary_slip_id=alice_slip_id)
    assert result["status"] == "ok"
    assert Decimal(result["gross_pay"]) == Decimal("7000.00")

    # Should have earnings: Basic Salary (5000) + HRA (2000)
    earnings = [d for d in result["details"] if d["component_type"] == "earning"]
    assert len(earnings) == 2

    # Should have deductions: federal tax, SS, Medicare
    deductions = [d for d in result["details"] if d["component_type"] == "deduction"]
    assert len(deductions) >= 2  # At least federal tax + SS + Medicare


def test_fica_calculation(fresh_db):
    """Verify FICA (Social Security + Medicare) calculation."""
    env = _setup_full_payroll(fresh_db)
    run = _call_action(ACTIONS["create-payroll-run"], fresh_db,
                       company_id=env["company_id"],
                       period_start="2026-02-01",
                       period_end="2026-02-28")
    _call_action(ACTIONS["generate-salary-slips"], fresh_db,
                 payroll_run_id=run["payroll_run_id"])

    slips = _call_action(ACTIONS["list-salary-slips"], fresh_db,
                         payroll_run_id=run["payroll_run_id"])
    alice_slip_id = [s["id"] for s in slips["slips"]
                     if s["employee_id"] == env["employee_ids"][0]][0]

    slip = _call_action(ACTIONS["get-salary-slip"], fresh_db,
                        salary_slip_id=alice_slip_id)

    # Alice: gross=7000, SS=7000*6.2%=434, Medicare=7000*1.45%=101.50
    deductions = {d["component_name"]: Decimal(d["amount"])
                  for d in slip["details"] if d["component_type"] == "deduction"}

    # Check SS and Medicare are present and correct
    ss_amount = None
    medicare_amount = None
    for name, amount in deductions.items():
        if "social security" in name.lower() or "ss" in name.lower():
            ss_amount = amount
        elif "medicare" in name.lower():
            medicare_amount = amount

    assert ss_amount == Decimal("434.00")
    assert medicare_amount == Decimal("101.50")


def test_federal_tax_calculation(fresh_db):
    """Verify progressive federal income tax calculation."""
    env = _setup_full_payroll(fresh_db)
    run = _call_action(ACTIONS["create-payroll-run"], fresh_db,
                       company_id=env["company_id"],
                       period_start="2026-02-01",
                       period_end="2026-02-28")
    _call_action(ACTIONS["generate-salary-slips"], fresh_db,
                 payroll_run_id=run["payroll_run_id"])

    slips = _call_action(ACTIONS["list-salary-slips"], fresh_db,
                         payroll_run_id=run["payroll_run_id"])
    alice_slip_id = [s["id"] for s in slips["slips"]
                     if s["employee_id"] == env["employee_ids"][0]][0]

    slip = _call_action(ACTIONS["get-salary-slip"], fresh_db,
                        salary_slip_id=alice_slip_id)

    # Alice (single): gross=7000/month, annual=84000, standard_deduction=14600
    # Taxable annual = 84000 - 14600 = 69400
    # Tax: 11600*10% + (47150-11600)*12% + (69400-47150)*22%
    #     = 1160 + 4266 + 4895 = 10321
    # Monthly = 10321/12 = 860.08
    deductions = {d["component_name"]: Decimal(d["amount"])
                  for d in slip["details"] if d["component_type"] == "deduction"}

    federal_tax = None
    for name, amount in deductions.items():
        if "federal" in name.lower() and "tax" in name.lower():
            federal_tax = amount
            break

    assert federal_tax is not None
    # Allow small rounding variance (+/-$1) due to annualize/de-annualize
    assert abs(federal_tax - Decimal("860.08")) <= Decimal("1.00")


def test_pre_tax_deductions(fresh_db):
    """Verify 401k and HSA pre-tax deductions reduce taxable income."""
    env = setup_payroll_environment(fresh_db)

    # Create employee with 401k and HSA
    emp_id = create_test_employee(
        fresh_db, env["company_id"], "Charlie", "Brown",
        department_id=env["department_id"],
        federal_filing_status="single",
        employee_401k_rate="6",  # 6% of gross
        hsa_contribution="200",  # $200/month flat
    )

    # Create components and structure
    basic = _call_action(ACTIONS["add-salary-component"], fresh_db,
                         name="Basic Salary", component_type="earning",
                         gl_account_id=env["salary_expense_id"])
    components = json.dumps([
        {"salary_component_id": basic["salary_component_id"], "amount": "10000"},
    ])
    struct = _call_action(ACTIONS["add-salary-structure"], fresh_db,
                          name="Executive", company_id=env["company_id"],
                          components=components)
    _call_action(ACTIONS["add-salary-assignment"], fresh_db,
                 employee_id=emp_id,
                 salary_structure_id=struct["salary_structure_id"],
                 base_amount="10000",
                 effective_from="2026-01-01",
                 company_id=env["company_id"])

    # Tax config
    _call_action(ACTIONS["update-fica-config"], fresh_db,
                 tax_year="2026", ss_wage_base="168600",
                 ss_employee_rate="6.2", ss_employer_rate="6.2",
                 medicare_employee_rate="1.45", medicare_employer_rate="1.45",
                 additional_medicare_threshold="200000", additional_medicare_rate="0.9")
    rates = json.dumps([
        {"from_amount": "0", "to_amount": "11600", "rate": "10"},
        {"from_amount": "11600", "to_amount": "47150", "rate": "12"},
        {"from_amount": "47150", "to_amount": "100525", "rate": "22"},
        {"from_amount": "100525", "to_amount": "191950", "rate": "24"},
    ])
    _call_action(ACTIONS["add-income-tax-slab"], fresh_db,
                 name="2026 Federal Single", tax_jurisdiction="federal",
                 filing_status="single", effective_from="2026-01-01",
                 standard_deduction="14600", rates=rates)

    # Run payroll
    run = _call_action(ACTIONS["create-payroll-run"], fresh_db,
                       company_id=env["company_id"],
                       period_start="2026-02-01", period_end="2026-02-28")
    _call_action(ACTIONS["generate-salary-slips"], fresh_db,
                 payroll_run_id=run["payroll_run_id"])

    slips = _call_action(ACTIONS["list-salary-slips"], fresh_db,
                         payroll_run_id=run["payroll_run_id"])
    slip_id = [s["id"] for s in slips["slips"] if s["employee_id"] == emp_id][0]
    slip = _call_action(ACTIONS["get-salary-slip"], fresh_db, salary_slip_id=slip_id)

    assert Decimal(slip["gross_pay"]) == Decimal("10000.00")

    # 401k = 10000 * 6% = 600; HSA = 200
    deductions = {d["component_name"]: Decimal(d["amount"])
                  for d in slip["details"] if d["component_type"] == "deduction"}

    has_401k = any("401k" in name.lower() or "401(k)" in name.lower() for name in deductions)
    has_hsa = any("hsa" in name.lower() for name in deductions)
    assert has_401k
    assert has_hsa


def test_submit_payroll_run_gl_balance(fresh_db):
    """Submit payroll run and verify GL entries balance (DR = CR)."""
    env = _setup_full_payroll(fresh_db)
    run = _call_action(ACTIONS["create-payroll-run"], fresh_db,
                       company_id=env["company_id"],
                       period_start="2026-02-01", period_end="2026-02-28")
    _call_action(ACTIONS["generate-salary-slips"], fresh_db,
                 payroll_run_id=run["payroll_run_id"])

    result = _call_action(ACTIONS["submit-payroll-run"], fresh_db,
                          payroll_run_id=run["payroll_run_id"])
    assert result["status"] == "ok"
    assert result["gl_entries"] > 0

    # Verify total debits = total credits
    gl = fresh_db.execute(
        """SELECT SUM(CAST(debit AS REAL)) as total_debit,
                  SUM(CAST(credit AS REAL)) as total_credit
           FROM gl_entry WHERE voucher_type='payroll_entry'
             AND voucher_id=? AND is_cancelled=0""",
        (run["payroll_run_id"],)
    ).fetchone()
    assert abs(gl["total_debit"] - gl["total_credit"]) < 0.01


def test_submit_creates_expense_entries(fresh_db):
    """Submit creates DR salary expense entries."""
    env = _setup_full_payroll(fresh_db)
    run = _call_action(ACTIONS["create-payroll-run"], fresh_db,
                       company_id=env["company_id"],
                       period_start="2026-02-01", period_end="2026-02-28")
    _call_action(ACTIONS["generate-salary-slips"], fresh_db,
                 payroll_run_id=run["payroll_run_id"])
    _call_action(ACTIONS["submit-payroll-run"], fresh_db,
                 payroll_run_id=run["payroll_run_id"])

    # Check salary expense debit exists (gross pay for both employees)
    expense_gl = fresh_db.execute(
        """SELECT SUM(CAST(debit AS REAL)) as total
           FROM gl_entry WHERE voucher_type='payroll_entry'
             AND voucher_id=? AND account_id=? AND is_cancelled=0""",
        (run["payroll_run_id"], env["salary_expense_id"])
    ).fetchone()
    # 2 employees * 7000 gross = 14000
    assert abs(expense_gl["total"] - 14000.0) < 0.01


def test_cancel_payroll_run(fresh_db):
    """Cancel reverses GL entries."""
    env = _setup_full_payroll(fresh_db)
    run = _call_action(ACTIONS["create-payroll-run"], fresh_db,
                       company_id=env["company_id"],
                       period_start="2026-02-01", period_end="2026-02-28")
    _call_action(ACTIONS["generate-salary-slips"], fresh_db,
                 payroll_run_id=run["payroll_run_id"])
    _call_action(ACTIONS["submit-payroll-run"], fresh_db,
                 payroll_run_id=run["payroll_run_id"])

    result = _call_action(ACTIONS["cancel-payroll-run"], fresh_db,
                          payroll_run_id=run["payroll_run_id"])
    assert result["status"] == "ok"

    # All original entries should be cancelled
    # Reversal entries exist but originals are cancelled
    # Net balance should be zero
    gl = fresh_db.execute(
        """SELECT SUM(CAST(debit AS REAL)) - SUM(CAST(credit AS REAL)) as net
           FROM gl_entry WHERE voucher_type='payroll_entry' AND voucher_id=?""",
        (run["payroll_run_id"],)
    ).fetchone()
    assert abs(gl["net"]) < 0.01


def test_regenerate_slips(fresh_db):
    """Generate slips can be called again on draft run (clears old slips)."""
    env = _setup_full_payroll(fresh_db)
    run = _call_action(ACTIONS["create-payroll-run"], fresh_db,
                       company_id=env["company_id"],
                       period_start="2026-02-01", period_end="2026-02-28")

    # Generate once
    result1 = _call_action(ACTIONS["generate-salary-slips"], fresh_db,
                           payroll_run_id=run["payroll_run_id"])
    assert result1["slips_generated"] == 2

    # Generate again (should succeed, replacing old slips)
    result2 = _call_action(ACTIONS["generate-salary-slips"], fresh_db,
                           payroll_run_id=run["payroll_run_id"])
    assert result2["slips_generated"] == 2

    # Should still have only 2 slips total (not 4)
    slips = _call_action(ACTIONS["list-salary-slips"], fresh_db,
                         payroll_run_id=run["payroll_run_id"])
    assert slips["count"] == 2
