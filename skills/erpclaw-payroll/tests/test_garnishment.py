"""Tests for wage garnishment CRUD and payroll integration.

Tests: add/list/get, single garnishment deduction, multiple priority,
25% cap, child support 50% cap, cumulative tracking, auto-pause.
"""
import json
import uuid
from decimal import Decimal

import db_query
from db_query import ACTIONS
from helpers import (
    _call_action, setup_payroll_environment,
    create_test_employee,
)


# ---------------------------------------------------------------------------
# Reuse payroll setup from test_payroll_run
# ---------------------------------------------------------------------------

def _setup_full_payroll(fresh_db):
    """Create a complete payroll environment (same as test_payroll_run)."""
    env = setup_payroll_environment(fresh_db)

    basic = _call_action(ACTIONS["add-salary-component"], fresh_db,
                         name="Basic Salary", component_type="earning",
                         gl_account_id=env["salary_expense_id"])
    hra = _call_action(ACTIONS["add-salary-component"], fresh_db,
                       name="HRA", component_type="earning",
                       gl_account_id=env["salary_expense_id"])

    components = json.dumps([
        {"salary_component_id": basic["salary_component_id"], "amount": "0",
         "sort_order": 1},
        {"salary_component_id": hra["salary_component_id"], "percentage": "40",
         "base_component_id": basic["salary_component_id"], "sort_order": 2},
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
                 tax_year="2026",
                 ss_wage_base="168600", ss_employee_rate="6.2",
                 ss_employer_rate="6.2", medicare_employee_rate="1.45",
                 medicare_employer_rate="1.45",
                 additional_medicare_threshold="200000",
                 additional_medicare_rate="0.9")

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
                 name="2026 Federal Single", tax_jurisdiction="federal",
                 filing_status="single", effective_from="2026-01-01",
                 standard_deduction="14600", rates=rates)

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
                 name="2026 Federal MFJ", tax_jurisdiction="federal",
                 filing_status="married_jointly", effective_from="2026-01-01",
                 standard_deduction="29200", rates=rates_mj)

    env["basic_component_id"] = basic["salary_component_id"]
    env["hra_component_id"] = hra["salary_component_id"]
    env["structure_id"] = struct["salary_structure_id"]
    return env


# ---------------------------------------------------------------------------
# CRUD tests
# ---------------------------------------------------------------------------

def test_add_garnishment(fresh_db):
    """Add a garnishment and verify it's created with correct priority."""
    env = _setup_full_payroll(fresh_db)
    emp_id = env["employee_ids"][0]

    result = _call_action(db_query.add_garnishment, fresh_db,
                          employee_id=emp_id,
                          order_number="CO-2026-001",
                          creditor_name="State Tax Authority",
                          garnishment_type="tax_levy",
                          amount_or_percentage="500",
                          start_date="2026-01-01")

    assert result["status"] == "ok"
    assert result["priority"] == 2  # tax_levy = priority 2


def test_list_garnishments(fresh_db):
    """List garnishments for an employee, ordered by priority."""
    env = _setup_full_payroll(fresh_db)
    emp_id = env["employee_ids"][0]

    # Add two garnishments with different priorities
    _call_action(db_query.add_garnishment, fresh_db,
                 employee_id=emp_id, order_number="CO-001",
                 creditor_name="Credit Corp", garnishment_type="creditor",
                 amount_or_percentage="200", start_date="2026-01-01")
    _call_action(db_query.add_garnishment, fresh_db,
                 employee_id=emp_id, order_number="CO-002",
                 creditor_name="State Tax", garnishment_type="tax_levy",
                 amount_or_percentage="300", start_date="2026-01-01")

    result = _call_action(db_query.list_garnishments, fresh_db,
                          employee_id=emp_id)
    assert result["status"] == "ok"
    assert result["count"] == 2
    # tax_levy (priority 2) before creditor (priority 4)
    assert result["garnishments"][0]["garnishment_type"] == "tax_levy"
    assert result["garnishments"][1]["garnishment_type"] == "creditor"


def test_get_garnishment(fresh_db):
    """Get a specific garnishment by ID."""
    env = _setup_full_payroll(fresh_db)
    emp_id = env["employee_ids"][0]

    added = _call_action(db_query.add_garnishment, fresh_db,
                         employee_id=emp_id, order_number="CO-003",
                         creditor_name="IRS", garnishment_type="tax_levy",
                         amount_or_percentage="400", start_date="2026-02-01",
                         total_owed="5000")

    result = _call_action(db_query.get_garnishment, fresh_db,
                          garnishment_id=added["garnishment_id"])
    assert result["status"] == "ok"
    assert result["creditor_name"] == "IRS"
    assert result["total_owed"] == "5000"
    assert result["cumulative_paid"] == "0"


# ---------------------------------------------------------------------------
# Payroll integration tests
# ---------------------------------------------------------------------------

def _run_payroll_with_garnishment(fresh_db, env, emp_id, garnishments):
    """Helper: add garnishments, run one payroll cycle, return slip details."""
    for g in garnishments:
        _call_action(db_query.add_garnishment, fresh_db,
                     employee_id=emp_id, **g)

    # Create and generate payroll run
    run = _call_action(ACTIONS["create-payroll-run"], fresh_db,
                       company_id=env["company_id"],
                       period_start="2026-01-01", period_end="2026-01-31")
    assert run["status"] == "ok"

    gen = _call_action(ACTIONS["generate-salary-slips"], fresh_db,
                       payroll_run_id=run["payroll_run_id"])
    assert gen["status"] == "ok"

    # Get the employee's salary slip
    slip = fresh_db.execute(
        "SELECT * FROM salary_slip WHERE payroll_run_id = ? AND employee_id = ?",
        (run["payroll_run_id"], emp_id),
    ).fetchone()

    details = fresh_db.execute(
        "SELECT * FROM salary_slip_detail WHERE salary_slip_id = ?",
        (slip["id"],),
    ).fetchall()

    return slip, details


def test_single_garnishment_deducted(fresh_db):
    """A single creditor garnishment should appear as a deduction on the salary slip."""
    env = _setup_full_payroll(fresh_db)
    emp_id = env["employee_ids"][0]

    slip, details = _run_payroll_with_garnishment(fresh_db, env, emp_id, [
        {"order_number": "CO-100", "creditor_name": "Debt Collector",
         "garnishment_type": "creditor", "amount_or_percentage": "200",
         "start_date": "2026-01-01"},
    ])

    # Find garnishment detail
    garn_details = [d for d in details
                    if "Garnishment" in (fresh_db.execute(
                        "SELECT name FROM salary_component WHERE id = ?",
                        (d["salary_component_id"],)).fetchone()["name"])]
    assert len(garn_details) == 1
    assert Decimal(garn_details[0]["amount"]) == Decimal("200.00")

    # Net pay should be reduced by garnishment amount
    net_pay = Decimal(slip["net_pay"])
    gross = Decimal(slip["gross_pay"])
    total_ded = Decimal(slip["total_deductions"])
    assert net_pay == gross - total_ded


def test_garnishment_25pct_cap(fresh_db):
    """Creditor garnishment should be capped at 25% of disposable income."""
    env = _setup_full_payroll(fresh_db)
    emp_id = env["employee_ids"][0]

    # Request a huge garnishment that exceeds 25%
    slip, details = _run_payroll_with_garnishment(fresh_db, env, emp_id, [
        {"order_number": "CO-200", "creditor_name": "Big Creditor",
         "garnishment_type": "creditor", "amount_or_percentage": "99999",
         "start_date": "2026-01-01"},
    ])

    garn_details = [d for d in details
                    if "Garnishment" in (fresh_db.execute(
                        "SELECT name FROM salary_component WHERE id = ?",
                        (d["salary_component_id"],)).fetchone()["name"])]

    if garn_details:
        garn_amt = Decimal(garn_details[0]["amount"])
        # The garnishment should not exceed 25% of the net-before-garnishment
        # (net pay + garnishment amount = disposable income before garnishment)
        disposable = Decimal(slip["net_pay"]) + garn_amt
        max_25 = disposable * Decimal("25") / Decimal("100")
        assert garn_amt <= max_25 + Decimal("0.01")  # Rounding tolerance


def test_child_support_50pct_cap(fresh_db):
    """Child support can take up to 50% of disposable income."""
    env = _setup_full_payroll(fresh_db)
    emp_id = env["employee_ids"][0]

    slip, details = _run_payroll_with_garnishment(fresh_db, env, emp_id, [
        {"order_number": "CS-100", "creditor_name": "Family Court",
         "garnishment_type": "child_support", "amount_or_percentage": "99999",
         "start_date": "2026-01-01"},
    ])

    garn_details = [d for d in details
                    if "Garnishment" in (fresh_db.execute(
                        "SELECT name FROM salary_component WHERE id = ?",
                        (d["salary_component_id"],)).fetchone()["name"])]

    if garn_details:
        garn_amt = Decimal(garn_details[0]["amount"])
        disposable = Decimal(slip["net_pay"]) + garn_amt
        max_50 = disposable * Decimal("50") / Decimal("100")
        assert garn_amt <= max_50 + Decimal("0.01")
        # Child support 50% cap should allow more than 25%
        max_25 = disposable * Decimal("25") / Decimal("100")
        assert garn_amt > max_25  # Proves the 50% cap is actually higher


def test_cumulative_tracking(fresh_db):
    """After payroll, cumulative_paid on the garnishment should be updated."""
    env = _setup_full_payroll(fresh_db)
    emp_id = env["employee_ids"][0]

    # Add garnishment
    added = _call_action(db_query.add_garnishment, fresh_db,
                         employee_id=emp_id, order_number="CO-300",
                         creditor_name="Tracker Corp", garnishment_type="creditor",
                         amount_or_percentage="150", start_date="2026-01-01",
                         total_owed="1000")

    # Run payroll
    run = _call_action(ACTIONS["create-payroll-run"], fresh_db,
                       company_id=env["company_id"],
                       period_start="2026-01-01", period_end="2026-01-31")
    _call_action(ACTIONS["generate-salary-slips"], fresh_db,
                 payroll_run_id=run["payroll_run_id"])

    # Check cumulative was updated
    garn = fresh_db.execute(
        "SELECT * FROM wage_garnishment WHERE id = ?",
        (added["garnishment_id"],),
    ).fetchone()
    assert Decimal(garn["cumulative_paid"]) == Decimal("150.00")
    assert garn["status"] == "active"  # Still active (150 < 1000)


def test_auto_complete_when_fully_paid(fresh_db):
    """Garnishment should auto-complete when total_owed is reached."""
    env = _setup_full_payroll(fresh_db)
    emp_id = env["employee_ids"][0]

    # Add garnishment with small total_owed
    added = _call_action(db_query.add_garnishment, fresh_db,
                         employee_id=emp_id, order_number="CO-400",
                         creditor_name="Small Debt", garnishment_type="creditor",
                         amount_or_percentage="500", start_date="2026-01-01",
                         total_owed="100")  # Only $100 owed but requesting $500/period

    run = _call_action(ACTIONS["create-payroll-run"], fresh_db,
                       company_id=env["company_id"],
                       period_start="2026-01-01", period_end="2026-01-31")
    _call_action(ACTIONS["generate-salary-slips"], fresh_db,
                 payroll_run_id=run["payroll_run_id"])

    garn = fresh_db.execute(
        "SELECT * FROM wage_garnishment WHERE id = ?",
        (added["garnishment_id"],),
    ).fetchone()
    # Should have deducted only $100 (total_owed) and marked completed
    assert Decimal(garn["cumulative_paid"]) == Decimal("100.00")
    assert garn["status"] == "completed"
