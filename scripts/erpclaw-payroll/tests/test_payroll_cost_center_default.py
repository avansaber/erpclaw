"""Payroll runs without --cost-center-id post on the company default."""
import json
from decimal import Decimal

from payroll_helpers import (
    call_action,
    is_ok,
    load_db_query,
    ns,
    seed_cost_center,
)
from erpclaw_lib.query import P, Q, Table

mod = load_db_query()


def _ok(result):
    assert is_ok(result), result
    return result


def _setup_payroll_ready(conn, env):
    comp = _ok(call_action(mod.add_salary_component, conn, ns(
        name="Default CC Base", component_type="earning",
        is_tax_applicable=None, is_statutory=None, is_pre_tax=None,
        variable_based_on_taxable_salary=None, depends_on_payment_days=None,
        gl_account_id=None, description=None,
    )))
    comp_id = comp["salary_component_id"]
    ss = _ok(call_action(mod.add_salary_structure, conn, ns(
        name="Default CC Structure",
        company_id=env["company_id"],
        components=json.dumps([
            {"salary_component_id": comp_id, "amount": "5000"},
        ]),
        payroll_frequency=None,
    )))
    sa = _ok(call_action(mod.add_salary_assignment, conn, ns(
        employee_id=env["employee_id"],
        salary_structure_id=ss["salary_structure_id"],
        base_amount="5000.00",
        effective_from="2026-01-01",
        effective_to=None,
    )))
    fica = _ok(call_action(mod.update_fica_config, conn, ns(
        tax_year="2026",
        ss_wage_base="168600",
        ss_employee_rate="6.2",
        ss_employer_rate="6.2",
        medicare_employee_rate="1.45",
        medicare_employer_rate="1.45",
        additional_medicare_threshold="200000",
        additional_medicare_rate="0.9",
    )))
    return {"component_id": comp_id, "assignment_id": sa["salary_assignment_id"]}


def test_payroll_run_default_is_the_company_default(conn, env):
    zulu = seed_cost_center(conn, env["company_id"], "Zulu CC")
    seed_cost_center(conn, env["company_id"], "Alpha CC")
    co = Table("company")
    q = Q.update(co).set(co.default_cost_center_id, P()).where(co.id == P())
    conn.execute(q.get_sql(), (zulu, env["company_id"]))
    conn.commit()

    _setup_payroll_ready(conn, env)
    run_id = _ok(call_action(mod.create_payroll_run, conn, ns(
        company_id=env["company_id"],
        period_start="2026-01-01",
        period_end="2026-01-31",
        department_id=None,
        payroll_frequency="monthly",
    )))["payroll_run_id"]
    _ok(call_action(mod.generate_salary_slips, conn, ns(
        payroll_run_id=run_id,
    )))
    result = call_action(mod.submit_payroll_run, conn, ns(
        payroll_run_id=run_id,
        cost_center_id=None,
    ))
    assert is_ok(result), result

    gl = Table("gl_entry")
    q = (Q.from_(gl)
         .select(gl.debit, gl.cost_center_id)
         .where(gl.voucher_id == P()))
    legs = [dict(r) for r in conn.execute(q.get_sql(), (run_id,)).fetchall()]
    expense_legs = [leg for leg in legs if Decimal(leg["debit"]) > 0]
    assert expense_legs
    assert all(leg["cost_center_id"] == zulu for leg in expense_legs)
