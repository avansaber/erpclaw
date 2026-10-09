"""Operator progress estimates produce exact read-only recognition proposals."""

from decimal import Decimal
from pathlib import Path

import pytest

from advacct_helpers import (
    _ConnWrapper, build_advacct_env, call_action, load_db_query, ns,
)
from erpclaw_lib.db import get_connection
from erpclaw_lib.query import Q, Table

mod = load_db_query()


@pytest.fixture
def progress_book(db_path):
    conn = _ConnWrapper(get_connection(db_path))
    env = build_advacct_env(conn)
    yield conn, env
    conn.close()


def _obligation(conn, env, basis="input", method="over_time", price="10000.00"):
    contract = call_action(mod.add_revenue_contract, conn, ns(
        company_id=env["company_id"], customer_name="Progress fixture",
        total_value=price, start_date="2026-01-01", end_date="2026-12-31"))
    assert contract["status"] == "ok", contract
    obligation = call_action(mod.add_performance_obligation, conn, ns(
        contract_id=contract["id"], company_id=env["company_id"], name="Building work",
        standalone_price=price, recognition_method=method, recognition_basis=basis))
    assert obligation["status"] == "ok", obligation
    return obligation["id"]


def _calculate(conn, env, obligation, **kw):
    args = {"company_id": env["company_id"], "obligation_id": obligation,
            "recognized_to_date": "0.00"}
    args.update(kw)
    return call_action(mod.calculate_revenue_progress, conn, ns(**args))


def _snapshot(conn):
    result = {}
    for name in ("advacct_revenue_contract", "advacct_performance_obligation",
                 "advacct_revenue_schedule", "gl_entry", "audit_log"):
        table = Table(name)
        result[name] = [dict(row) for row in conn.execute(
            Q.from_(table).select(table.star).orderby(table.id).get_sql()).fetchall()]
    return result


def test_cost_to_cost_uses_measurement_not_contract_months(progress_book):
    conn, env = progress_book
    obligation = _obligation(conn, env)
    before = _snapshot(conn)
    result = _calculate(conn, env, obligation, costs_incurred="2500.00",
                        estimated_total_costs="8000.00", recognized_to_date="2000.00")
    assert result["status"] == "ok", result
    assert result["recognition_basis"] == "input"
    assert result["progress_percent"] == "31.250000"
    assert Decimal(result["cumulative_revenue_target"]) == Decimal("3125.00")
    assert result["current_period_catch_up"] == "1125.00"
    assert result["remaining_allocated_revenue"] == "6875.00"
    assert result["posted"] is False and result["result_kind"] == "calculation_only"
    assert _snapshot(conn) == before


def test_revised_estimate_produces_negative_catch_up_without_restatement(progress_book):
    conn, env = progress_book
    obligation = _obligation(conn, env)
    before = _snapshot(conn)
    original = _calculate(conn, env, obligation, costs_incurred="4000.00",
                          estimated_total_costs="8000.00")
    revised = _calculate(conn, env, obligation, costs_incurred="4000.00",
                         estimated_total_costs="10000.00", recognized_to_date="5000.00")
    assert original["cumulative_revenue_target"] == "5000.00"
    assert revised["cumulative_revenue_target"] == "4000.00"
    assert revised["current_period_catch_up"] == "-1000.00"
    assert "reviewed reversal" in revised["review_required"]
    assert _snapshot(conn) == before


def test_output_measure_and_currency_rounding(progress_book):
    conn, env = progress_book
    obligation = _obligation(conn, env, basis="output", price="100.00")
    before = _snapshot(conn)
    result = _calculate(conn, env, obligation, completed_units="1.000000", total_units="3")
    assert result["status"] == "ok", result
    assert result["progress_percent"] == "33.333333"
    assert result["cumulative_revenue_target"] == "33.33"
    assert result["current_period_catch_up"] == "33.33"
    assert result["remaining_allocated_revenue"] == "66.67"
    assert _snapshot(conn) == before


def test_cost_overrun_caps_progress_and_does_not_book_a_loss(progress_book):
    conn, env = progress_book
    obligation = _obligation(conn, env)
    before = _snapshot(conn)
    result = _calculate(conn, env, obligation, costs_incurred="12000.00",
                        estimated_total_costs="10000.00", recognized_to_date="9500.00")
    assert result["progress_percent"] == "100.000000"
    assert result["cumulative_revenue_target"] == "10000.00"
    assert result["current_period_catch_up"] == "500.00"
    assert result["input_cost_overrun"] is True and result["posted"] is False
    assert _snapshot(conn) == before


@pytest.mark.parametrize("inputs", [
    {"costs_incurred": "NaN", "estimated_total_costs": "8000.00"},
    {"costs_incurred": "100.001", "estimated_total_costs": "8000.00"},
    {"costs_incurred": "-1", "estimated_total_costs": "8000.00"},
    {"costs_incurred": "100", "estimated_total_costs": "0"},
    {"costs_incurred": "100", "estimated_total_costs": "8000", "recognized_to_date": None},
    {"costs_incurred": "100", "estimated_total_costs": "8000", "recognized_to_date": "10001"},
    {"costs_incurred": "100", "estimated_total_costs": "8000", "completed_units": "1"},
])
def test_invalid_input_refuses_without_writes(progress_book, inputs):
    conn, env = progress_book
    obligation = _obligation(conn, env)
    before = _snapshot(conn)
    result = _calculate(conn, env, obligation, **inputs)
    assert result["status"] == "error", result
    assert _snapshot(conn) == before


@pytest.mark.parametrize("basis, method", [("time", "over_time"), ("input", "point_in_time")])
def test_unsupported_basis_or_method_is_not_reinterpreted(progress_book, basis, method):
    conn, env = progress_book
    obligation = _obligation(conn, env, basis=basis, method=method)
    before = _snapshot(conn)
    result = _calculate(conn, env, obligation, costs_incurred="100", estimated_total_costs="8000")
    assert result["status"] == "error", result
    assert _snapshot(conn) == before


def test_company_scope_and_invalid_output_are_refused(progress_book):
    conn, env = progress_book
    obligation = _obligation(conn, env, basis="output")
    before = _snapshot(conn)
    wrong_company = _calculate(conn, env, obligation, company_id=env["company2_id"],
                               completed_units="1", total_units="4")
    assert wrong_company["status"] == "error"
    for completed in ("5", "Infinity", "1.0000001"):
        result = _calculate(conn, env, obligation, completed_units=completed, total_units="4")
        assert result["status"] == "error", result
    assert _snapshot(conn) == before


def test_actual_read_only_sweep(progress_book, tmp_path, monkeypatch):
    conn, env = progress_book
    obligation = _obligation(conn, env)
    snapshot = tmp_path / "progress-snapshot.sqlite"
    conn.commit()
    conn.execute("VACUUM INTO ?", (str(snapshot),))
    conn.close()
    root = Path(__file__).resolve().parents[5]
    monkeypatch.syspath_prepend(str(root / "testing"))
    import readonly_sweep
    argv = readonly_sweep._child_argv("calculate-revenue-progress", {
        "company_id": env["company_id"], "obligation_id": obligation,
        "recognized_to_date": "0.00", "costs_incurred": "2500.00",
        "estimated_total_costs": "8000.00"})
    result = readonly_sweep._dispatch_once(argv, readonly_sweep._child_env, str(snapshot))
    assert result["status"] == "ok" and result["findings"] == [], result
