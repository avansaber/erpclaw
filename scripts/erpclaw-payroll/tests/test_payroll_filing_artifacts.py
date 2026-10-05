"""Draft employer returns use existing submitted payroll and posted tax amounts."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from decimal import Decimal

import pytest

from payroll_helpers import (
    build_payroll_env, call_action, load_db_query, ns, seed_account,
)
from erpclaw_lib.query import Q, Table

mod = load_db_query()


def _ok(result):
    assert result["status"] == "ok", result
    return result


def _ready(conn, env, name):
    seed_account(conn, env["company_id"], "FUTA Payable", root_type="liability")
    component = _ok(call_action(mod.add_salary_component, conn, ns(
        name=name, component_type="earning", is_tax_applicable=None,
        is_statutory=None, is_pre_tax=None, variable_based_on_taxable_salary=None,
        depends_on_payment_days=None, gl_account_id=None, description=None,
    )))["salary_component_id"]
    structure = _ok(call_action(mod.add_salary_structure, conn, ns(
        name=name, company_id=env["company_id"], payroll_frequency=None,
        components=json.dumps([{"salary_component_id": component, "amount": "0"}]),
    )))["salary_structure_id"]
    _ok(call_action(mod.add_salary_assignment, conn, ns(
        employee_id=env["employee_id"], salary_structure_id=structure,
        base_amount="1000.00", effective_from="2026-01-01", effective_to=None,
    )))


def _run(conn, env, start, end, submit=True, cancel=False):
    run = _ok(call_action(mod.create_payroll_run, conn, ns(
        company_id=env["company_id"], period_start=start, period_end=end,
        department_id=None, payroll_frequency="monthly",
    )))["payroll_run_id"]
    _ok(call_action(mod.generate_salary_slips, conn, ns(payroll_run_id=run)))
    if submit:
        _ok(call_action(mod.submit_payroll_run, conn, ns(
            payroll_run_id=run, cost_center_id=env["cost_center_id"],
        )))
    if cancel:
        _ok(call_action(mod.cancel_payroll_run, conn, ns(payroll_run_id=run)))
    return run


@pytest.fixture
def book(conn):
    a, b = build_payroll_env(conn), build_payroll_env(conn)
    _ok(call_action(mod.update_fica_config, conn, ns(
        tax_year="2026", ss_wage_base="168600", ss_employee_rate="6.2",
        ss_employer_rate="6.2", medicare_employee_rate="1.45",
        medicare_employer_rate="1.45", additional_medicare_threshold="200000",
        additional_medicare_rate="0.9",
    )))
    _ok(call_action(mod.update_futa_suta_config, conn, ns(
        tax_year="2026", wage_base="7000", rate="0.6", state_code=None,
        employer_rate_override=None,
    )))
    _ok(call_action(mod.add_income_tax_slab, conn, ns(
        name="Fixture federal", tax_jurisdiction="federal",
        effective_from="2026-01-01", filing_status=None, state_code=None,
        standard_deduction=None,
        rates=json.dumps([{"from_amount": "0", "to_amount": None, "rate": "10"}]),
    )))
    _ready(conn, a, "Filing fixture A")
    _ready(conn, b, "Filing fixture B")
    january = _run(conn, a, "2026-01-01", "2026-01-31")
    _run(conn, a, "2026-02-01", "2026-02-28", cancel=True)
    _run(conn, a, "2026-03-01", "2026-03-31", submit=False)
    april = _run(conn, a, "2026-04-01", "2026-04-30")
    _run(conn, b, "2026-01-01", "2026-01-31")
    return {"a": a, "b": b, "january": january, "april": april}


def _snapshot(conn):
    result = {}
    for name in ("payroll_run", "salary_slip", "salary_slip_detail", "gl_entry", "audit_log"):
        table = Table(name)
        rows = conn.execute(Q.from_(table).select(table.star).orderby(table.id).get_sql()).fetchall()
        result[name] = [dict(row) for row in rows]
    return result


def _artifact(conn, book, form="941", year="2026", quarter="1"):
    action = mod.generate_form941_data if form == "941" else mod.generate_form940_data
    return call_action(action, conn, ns(
        company_id=book["a"]["company_id"], tax_year=year, quarter=quarter,
    ))


def test_form941_exact_posted_taxes_and_submitted_quarter_scope(conn, book):
    before = _snapshot(conn)
    result = _ok(_artifact(conn, book))
    assert result["artifact_status"] == "draft" and result["filed"] is False
    assert result["payroll_run_ids"] == [book["january"]]
    assert (result["employee_count"], result["slip_count"]) == (1, 1)
    assert (result["period_start"], result["period_end"]) == ("2026-01-01", "2026-03-31")
    assert result["totals"] == {
        "gross_wages": "1000.00", "federal_income_tax_withheld": "100.00",
        "employee_social_security": "62.00", "employee_medicare": "14.50",
        "combined_social_security": "124.00", "combined_medicare": "29.00",
        "employer_social_security": "62.00", "employer_medicare": "14.50",
        "recorded_941_taxes": "253.00",
    }
    assert Decimal(result["totals"]["recorded_941_taxes"]) == Decimal("253.00")
    assert _snapshot(conn) == before


def test_form940_annual_futa_excludes_other_company_cancelled_and_draft(conn, book):
    before = _snapshot(conn)
    result = _ok(_artifact(conn, book, form="940", quarter=None))
    assert result["form"] == "940" and result["quarter"] is None
    assert result["payroll_run_ids"] == sorted([book["january"], book["april"]])
    assert result["totals"]["gross_wages"] == "2000.00"
    assert Decimal(result["totals"]["futa_tax_posted"]) == Decimal("12.00")
    assert result["totals"]["employer_social_security"] == "124.00"
    assert result["totals"]["employer_medicare"] == "29.00"
    assert result["filed"] is False and result["review_required"]
    assert _snapshot(conn) == before


def test_artifacts_use_posted_values_after_tax_rates_change(conn, book):
    _ok(call_action(mod.update_fica_config, conn, ns(
        tax_year="2026", ss_wage_base="168600", ss_employee_rate="9",
        ss_employer_rate="9", medicare_employee_rate="9", medicare_employer_rate="9",
        additional_medicare_threshold="200000", additional_medicare_rate="0.9",
    )))
    result = _ok(_artifact(conn, book))
    assert result["totals"]["employer_social_security"] == "62.00"
    assert result["totals"]["employer_medicare"] == "14.50"
    april = _ok(_artifact(conn, book, quarter="2"))
    assert april["payroll_run_ids"] == [book["april"]]
    assert april["totals"]["recorded_941_taxes"] == "253.00"


def test_no_submitted_slips_returns_an_empty_draft(conn, book):
    result = _ok(_artifact(conn, book, year="2025"))
    assert result["slip_count"] == 0 and result["payroll_run_ids"] == []
    assert all(Decimal(amount) == Decimal("0.00") for amount in result["totals"].values())


@pytest.mark.parametrize("form, year, quarter, message", [
    ("941", None, "1", "tax-year"),
    ("941", "2026", "5", "quarter"),
    ("940", "2026", "1", "annual"),
])
def test_invalid_period_refuses_without_writes(conn, book, form, year, quarter, message):
    before = _snapshot(conn)
    result = _artifact(conn, book, form=form, year=year, quarter=quarter)
    assert result["status"] == "error" and message in result["message"]
    assert _snapshot(conn) == before


def test_ambiguous_account_mapping_refuses_without_writes(conn, book):
    seed_account(conn, book["a"]["company_id"], "Second Social Security Payable",
                 root_type="liability")
    before = _snapshot(conn)
    result = _artifact(conn, book)
    assert result["status"] == "error" and "found 2" in result["message"]
    assert _snapshot(conn) == before


@pytest.mark.parametrize("form", ["941", "940"])
def test_artifact_routes_as_a_read_only_action(conn, db_path, book, tmp_path,
                                              monkeypatch, form):
    scripts = Path(__file__).resolve().parents[2]
    conn.commit()
    install = tmp_path / "readonly-install"
    install.mkdir()
    target = install / "data.sqlite"
    # Use the sweep's rollback-journal snapshot format, so opening the
    # disposable read-only copy does not create WAL coordination files.
    conn.execute("VACUUM INTO ?", (str(target),))
    conn.close()
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    env = {**os.environ, "ERPCLAW_HOME": str(install),
           "ERPCLAW_DB_PATH": str(target), "ERPCLAW_DB_READONLY": "1",
           "PYTHONPATH": str(scripts / "erpclaw-setup" / "lib")}
    env.pop("ERPCLAW_ACTOR_CONTEXT", None)
    env.pop("ERPCLAW_TEST_SESSION", None)
    action = f"generate-form{form}-data"
    args = {"company_id": book["a"]["company_id"], "tax_year": "2026"}
    if form == "941":
        args["quarter"] = "1"
    argv = [sys.executable, str(scripts / "db_query.py"), "--action", action]
    for key, value in args.items():
        argv.extend(["--" + key.replace("_", "-"), value])
    result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["totals"]["recorded_941_taxes"] == ("253.00" if form == "941" else "506.00")
    assert hashlib.sha256(target.read_bytes()).hexdigest() == before
    monkeypatch.syspath_prepend(str(scripts.parents[2] / "testing"))
    import readonly_sweep
    swept = readonly_sweep._dispatch_once(
        readonly_sweep._child_argv(action, args), readonly_sweep._child_env, str(target))
    assert swept["status"] == "ok", swept
    assert swept["findings"] == [], swept
