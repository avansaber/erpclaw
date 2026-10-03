"""Submit names missing payroll tax accounts instead of posting unbalanced (m801)."""
import json
from decimal import Decimal

from payroll_helpers import (
    build_payroll_env,
    call_action,
    is_error,
    is_ok,
    load_db_query,
    ns,
    seed_account,
    seed_company,
    seed_cost_center,
    seed_employee,
    seed_fiscal_year,
    seed_naming_series,
)

mod = load_db_query()


def _build_env(conn, with_federal=True, with_ss=True, with_medicare=True):
    cid = seed_company(conn)
    seed_fiscal_year(conn, cid)
    ccid = seed_cost_center(conn, cid)
    seed_account(conn, cid, "Cash", root_type="asset", account_type="cash")
    seed_account(conn, cid, "Salary Expense", root_type="expense")
    seed_account(conn, cid, "Payroll Payable", root_type="liability",
                 account_type="payable")
    seed_account(conn, cid, "Employer Tax Expense", root_type="expense")
    if with_federal:
        seed_account(conn, cid, "Federal Income Tax Withheld",
                     root_type="liability")
    if with_ss:
        seed_account(conn, cid, "Social Security Payable",
                     root_type="liability")
    if with_medicare:
        seed_account(conn, cid, "Medicare Payable", root_type="liability")
    emp_id = seed_employee(conn, cid)
    seed_naming_series(conn, cid)
    return {"company_id": cid, "cost_center_id": ccid, "employee_id": emp_id}


def _setup_payroll(conn, env, base_amount):
    comp = call_action(mod.add_salary_component, conn, ns(
        name="Missing Acct Base", component_type="earning",
        is_tax_applicable=None, is_statutory=None, is_pre_tax=None,
        variable_based_on_taxable_salary=None, depends_on_payment_days=None,
        gl_account_id=None, description=None,
    ))
    assert is_ok(comp)
    struct = call_action(mod.add_salary_structure, conn, ns(
        name="Missing Acct Structure",
        company_id=env["company_id"],
        components=json.dumps([
            {"salary_component_id": comp["salary_component_id"],
             "amount": base_amount},
        ]),
        payroll_frequency=None,
    ))
    assert is_ok(struct)
    assign = call_action(mod.add_salary_assignment, conn, ns(
        employee_id=env["employee_id"],
        salary_structure_id=struct["salary_structure_id"],
        base_amount=base_amount,
        effective_from="2026-01-01",
        effective_to=None,
    ))
    assert is_ok(assign)
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


def _create_and_generate(conn, env):
    run = call_action(mod.create_payroll_run, conn, ns(
        company_id=env["company_id"],
        period_start="2026-01-01",
        period_end="2026-01-31",
        department_id=None,
        payroll_frequency="monthly",
    ))
    assert is_ok(run)
    run_id = run["payroll_run_id"]
    gen = call_action(mod.generate_salary_slips, conn, ns(
        payroll_run_id=run_id,
    ))
    assert is_ok(gen)
    return run_id


def _run_status(conn, run_id):
    return conn.execute(
        "SELECT status FROM payroll_run WHERE id = ?", (run_id,),
    ).fetchone()["status"]


def _slip_statuses(conn, run_id):
    return [r["status"] for r in conn.execute(
        "SELECT status FROM salary_slip WHERE payroll_run_id = ?", (run_id,),
    ).fetchall()]


def _ledger_count(conn, run_id):
    return conn.execute(
        "SELECT COUNT(*) FROM gl_entry WHERE voucher_id = ?", (run_id,),
    ).fetchone()[0]


def test_missing_ss_and_medicare_named(conn):
    env = _build_env(conn, with_ss=False, with_medicare=False)
    _setup_payroll(conn, env, "4000.00")
    run_id = _create_and_generate(conn, env)
    result = call_action(mod.submit_payroll_run, conn, ns(
        payroll_run_id=run_id,
        cost_center_id=env["cost_center_id"],
    ))
    assert is_error(result)
    message = result["message"]
    assert message.startswith("Cannot post payroll: ")
    assert "Social Security" in message
    assert "496.00" in message
    assert "Medicare" in message
    assert "116.00" in message
    assert "Nothing was posted" in message
    assert _run_status(conn, run_id) == "draft"
    assert _slip_statuses(conn, run_id) == ["draft"]
    assert _ledger_count(conn, run_id) == 0


def test_small_run_missing_account_not_folded(conn):
    env = _build_env(conn, with_medicare=False)
    _setup_payroll(conn, env, "10.00")
    run_id = _create_and_generate(conn, env)
    result = call_action(mod.submit_payroll_run, conn, ns(
        payroll_run_id=run_id,
        cost_center_id=env["cost_center_id"],
    ))
    assert is_error(result)
    message = result["message"]
    assert "Medicare" in message
    assert "0.30" in message
    assert "Nothing was posted" in message
    assert _run_status(conn, run_id) == "draft"
    assert _slip_statuses(conn, run_id) == ["draft"]
    assert _ledger_count(conn, run_id) == 0


def test_all_accounts_present_unchanged(conn):
    env = build_payroll_env(conn)
    _setup_payroll(conn, env, "4000.00")
    run_id = _create_and_generate(conn, env)
    result = call_action(mod.submit_payroll_run, conn, ns(
        payroll_run_id=run_id,
        cost_center_id=env["cost_center_id"],
    ))
    assert is_ok(result)
    rows = conn.execute(
        """SELECT a.name AS name, e.debit AS debit, e.credit AS credit
           FROM gl_entry e JOIN account a ON a.id = e.account_id
           WHERE e.voucher_id = ?""",
        (run_id,),
    ).fetchall()
    legs = {(r["name"], r["debit"], r["credit"]) for r in rows}
    assert legs == {
        ("Salary Expense", "4000.00", "0.00"),
        ("Employer Tax Expense", "306.00", "0.00"),
        ("Payroll Payable", "0.00", "3694.00"),
        ("Social Security Payable", "0.00", "496.00"),
        ("Medicare Payable", "0.00", "116.00"),
    }


def test_zero_total_needs_no_account(conn):
    env = _build_env(conn, with_federal=False)
    _setup_payroll(conn, env, "4000.00")
    run_id = _create_and_generate(conn, env)
    result = call_action(mod.submit_payroll_run, conn, ns(
        payroll_run_id=run_id,
        cost_center_id=env["cost_center_id"],
    ))
    assert is_ok(result), result
    assert _run_status(conn, run_id) == "submitted"


def test_refused_run_can_submit_after_accounts_added(conn):
    env = _build_env(conn, with_ss=False, with_medicare=False)
    _setup_payroll(conn, env, "4000.00")
    run_id = _create_and_generate(conn, env)
    refused = call_action(mod.submit_payroll_run, conn, ns(
        payroll_run_id=run_id,
        cost_center_id=env["cost_center_id"],
    ))
    assert is_error(refused)
    assert "Nothing was posted" in refused["message"]
    seed_account(conn, env["company_id"], "Social Security Payable",
                 root_type="liability")
    seed_account(conn, env["company_id"], "Medicare Payable",
                 root_type="liability")
    result = call_action(mod.submit_payroll_run, conn, ns(
        payroll_run_id=run_id,
        cost_center_id=env["cost_center_id"],
    ))
    assert is_ok(result), result
    assert _run_status(conn, run_id) == "submitted"
    assert _ledger_count(conn, run_id) > 0
