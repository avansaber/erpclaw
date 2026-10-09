"""Employer benefit calculations from explicit supplied measurements and schedules."""
import json
from pathlib import Path

import pytest

from advacct_helpers import _ConnWrapper, build_advacct_env, call_action, load_db_query, ns
from erpclaw_lib.db import get_connection
from erpclaw_lib.query import Q, Table
from erpclaw_lib.seam import table_names

mod = load_db_query()


@pytest.fixture
def benefits(db_path):
    connection = _ConnWrapper(get_connection(db_path))
    environment = build_advacct_env(connection)
    yield connection, environment
    connection.close()


def inputs(environment, **changes):
    values = dict(company_id=environment["company_id"], currency="USD", benefit_type="pension",
                  measurement_date="2025-12-31", reporting_date="2026-12-31",
                  total_benefit_liability="1200.01", fiduciary_net_position="800.00",
                  employer_share_percent="12.5", review_reference="Reviewed measurement 2026",
                  expense_before_deferrals="10.00", benefit_deferrals=json.dumps([
                      {"id": "experience", "direction": "outflow", "opening_amount": "100.00",
                       "schedule": [{"year": 2026, "amount": "33.34"}, {"year": 2027, "amount": "33.33"},
                                    {"year": 2028, "amount": "33.33"}]},
                      {"id": "investment", "direction": "inflow", "opening_amount": "20.00",
                       "schedule": [{"year": 2026, "amount": "4.00"}, {"year": 2027, "amount": "16.00"}]},
                  ]))
    values.update(changes)
    return ns(**values)


def snapshot(connection):
    return {name: [tuple(row) for row in connection.execute(Q.from_(Table(name)).select("*").get_sql()).fetchall()]
            for name in sorted(table_names())}


@pytest.mark.parametrize("benefit_type", ["pension", "opeb"])
def test_explicit_liability_and_expense_without_writes(benefits, benefit_type):
    connection, environment = benefits
    before = snapshot(connection)
    result = call_action(mod.calculate_benefit_liability, connection, inputs(environment, benefit_type=benefit_type))
    assert result["status"] == "ok"
    assert result["benefit_type"] == benefit_type
    assert result["currency"] == "USD"
    assert result["plan_net_liability"] == "400.01"
    assert result["employer_net_liability"] == "50.00"
    assert result["employer_asset"] == "0.00"
    assert result["deferred_outflow_closing"] == "66.66"
    assert result["deferred_inflow_closing"] == "16.00"
    assert result["deferral_expense_adjustment"] == "29.34"
    assert result["expense_preview"] == "39.34"
    assert result["measurement_lag_days"] == 365
    assert result["result_kind"] == "calculation_only"
    assert result["posted"] is False and result["stored"] is False
    assert result["deferrals"][0]["schedule"][-1] == {"year": 2028, "amount": "33.33"}
    assert snapshot(connection) == before


def test_negative_net_liability_preserves_asset_and_half_cent(benefits):
    connection, environment = benefits
    result = call_action(mod.calculate_benefit_liability, connection, inputs(
        environment, total_benefit_liability="0.00", fiduciary_net_position="0.03",
        employer_share_percent="50", expense_before_deferrals="-2.00", benefit_deferrals="[]"))
    assert result["employer_net_liability"] == "-0.02"
    assert result["employer_liability"] == "0.00"
    assert result["employer_asset"] == "0.02"
    assert result["expense_preview"] == "-2.00"


def test_future_recognition_and_employer_amounts_are_not_scaled_twice(benefits):
    connection, environment = benefits
    result = call_action(mod.calculate_benefit_liability, connection, inputs(environment, benefit_deferrals=json.dumps([
        {"id": "later", "direction": "outflow", "opening_amount": "0.01",
         "schedule": [{"year": 2027, "amount": "0.01"}]},
        {"id": "now", "direction": "inflow", "opening_amount": "0.03",
         "schedule": [{"year": 2026, "amount": "0.03"}]},
    ])))
    assert result["expense_preview"] == "9.97"
    assert result["deferred_outflow_closing"] == "0.01"
    assert result["deferred_inflow_closing"] == "0.00"


@pytest.mark.parametrize("changes", [
    {"company_id": None}, {"company_id": "absent"}, {"benefit_type": "other"},
    {"currency": None}, {"currency": "EUR"},
    {"measurement_date": "20261231"}, {"reporting_date": "2025-01-01"},
    {"measurement_date": "2025-02-30"}, {"review_reference": " "},
    {"total_benefit_liability": "NaN"}, {"fiduciary_net_position": "-1"},
    {"expense_before_deferrals": "1e2"}, {"expense_before_deferrals": "1.001"},
    {"total_benefit_liability": 100}, {"employer_share_percent": "100.000001"},
    {"employer_share_percent": "0.0000001"}, {"benefit_deferrals": None},
    {"benefit_deferrals": "{}"}, {"benefit_deferrals": "bad"},
])
def test_invalid_inputs_refuse_without_writes(benefits, changes):
    connection, environment = benefits
    before = snapshot(connection)
    result = call_action(mod.calculate_benefit_liability, connection, inputs(environment, **changes))
    assert result["status"] == "error"
    assert snapshot(connection) == before


@pytest.mark.parametrize("change", [
    {"direction": "unknown"}, {"id": ""}, {"opening_amount": "0.02"},
    {"schedule": [{"year": 2026, "amount": 0.01}]},
    {"schedule": [{"year": True, "amount": "0.01"}]},
    {"schedule": [{"year": 2025, "amount": "0.01"}]},
    {"schedule": [{"year": 2026, "amount": "0.00"}, {"year": 2026, "amount": "0.01"}]},
    {"schedule": [{"year": 2027, "amount": "0.00"}, {"year": 2026, "amount": "0.01"}]},
    {"schedule": [{"year": 2026, "amount": "0.01", "extra": "bad"}]},
    {"schedule": []}, {"extra": "bad"},
])
def test_schedule_contract_is_exact(benefits, change):
    connection, environment = benefits
    item = {"id": "item", "direction": "outflow", "opening_amount": "0.01",
            "schedule": [{"year": 2026, "amount": "0.01"}]}
    item.update(change)
    result = call_action(mod.calculate_benefit_liability, connection, inputs(environment, benefit_deferrals=json.dumps([item])))
    assert result["status"] == "error"


def test_duplicate_schedule_id_refuses(benefits):
    connection, environment = benefits
    args = inputs(environment)
    rows = json.loads(args.benefit_deferrals)
    rows[1]["id"] = rows[0]["id"]
    args.benefit_deferrals = json.dumps(rows)
    assert call_action(mod.calculate_benefit_liability, connection, args)["status"] == "error"


def test_actual_routed_seeded_readonly_sweep(benefits, tmp_path, monkeypatch):
    connection, environment = benefits
    connection.commit()
    disposable = tmp_path / "sweep.sqlite"
    connection.execute("VACUUM INTO ?", (str(disposable),))
    root = Path(__file__).resolve().parents[5]
    monkeypatch.syspath_prepend(str(root / "testing"))
    import readonly_sweep
    flags = {key.replace("_", "-"): value for key, value in vars(inputs(environment)).items()}
    argv = readonly_sweep._child_argv("calculate-benefit-liability", flags)
    result = readonly_sweep._dispatch_once(argv, readonly_sweep._child_env, str(disposable))
    assert result["status"] == "ok", result
    assert result["findings"] == [], result
