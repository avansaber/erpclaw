"""Wave F S1.1/S1.2 — rating engine for all 7 plan types + loud failures.

Covers (wave file §F-debt.2 scenarios):
- L0-style: every plan type rates to balanced exact Decimal (TEXT, no float)
- time_of_use: noon -> peak rate, 3 AM -> off_peak rate; 24h validator
- demand: peak_demand 50 kW x 12/kW = 600.00 demand charge
- prepaid_credit: balance 100 -> charge 30 -> 70; charge 80 -> over_limit
- hybrid: $100 base + $0.10/kWh after 1000 kWh; 800 -> 100.00; 1500 -> 150.00
- S1.2a: run-billing reports per-meter data errors (no silent skip)
- S1.2b: generate-invoices never marks a period invoiced without an invoice
"""
import json
import os
import stat
import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest
from billing_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
)

mod = load_db_query()


# ── plan factories ──────────────────────────────────────────────────────────

def _plan_args(**overrides):
    base = dict(
        name="Plan", billing_model="flat", service_type="electricity",
        base_charge=None, base_charge_period=None, effective_from=None,
        effective_to=None, minimum_charge=None, minimum_commitment=None,
        overage_rate=None, tiers=None, tier_strategy=None,
    )
    base.update(overrides)
    return ns(**base)


TOU_TIERS = [
    {"time_of_use_period": "peak", "time_of_use_hours": ["09:00-17:00"],
     "rate": "0.20"},
    {"time_of_use_period": "off_peak",
     "time_of_use_hours": ["00:00-06:00", "22:00-24:00"], "rate": "0.05"},
    {"time_of_use_period": "shoulder",
     "time_of_use_hours": ["06:00-09:00", "17:00-22:00"], "rate": "0.10"},
]

DEMAND_TIERS = [
    {"demand_type": "demand", "rate": "12"},
    {"demand_type": "energy", "rate": "0.05"},
]

HYBRID_STRATEGY = {
    "components": [
        {"type": "flat", "tiers": [{"rate": "0"}], "base_charge": "100.00"},
        {"type": "tiered", "tiers": [
            {"tier_start": "0", "tier_end": "1000", "rate": "0"},
            {"tier_start": "1000", "rate": "0.10"},
        ]},
    ],
}


def _create_plan(conn, model, tiers=None, tier_strategy=None, **overrides):
    result = call_action(mod.add_rate_plan, conn, _plan_args(
        name=f"{model} plan", billing_model=model,
        tiers=json.dumps(tiers) if tiers is not None else None,
        tier_strategy=json.dumps(tier_strategy) if tier_strategy else None,
        **overrides,
    ))
    assert is_ok(result), result
    return result["rate_plan"]


def _create_meter(conn, env, plan_id):
    result = call_action(mod.add_meter, conn, ns(
        customer_id=env["customer"], meter_type="electricity",
        name="M", address=None, rate_plan_id=plan_id,
        install_date=None, unit="kWh",
    ))
    assert is_ok(result), result
    return result["meter"]


def _add_event(conn, meter_id, timestamp, quantity):
    result = call_action(mod.add_usage_event, conn, ns(
        meter_id=meter_id, event_date=timestamp, quantity=quantity,
        event_type="usage", properties=None, idempotency_key=None,
    ))
    assert is_ok(result), result


def _run_billing(conn, env, from_date, to_date):
    return call_action(mod.run_billing, conn, ns(
        company_id=env["company_id"], billing_date=to_date,
        from_date=from_date, to_date=to_date,
    ))


def _get_period(conn, period_id):
    row = conn.execute(
        "SELECT * FROM billing_period WHERE id = ?", (period_id,)).fetchone()
    assert row is not None
    return dict(row)


# ── L0-style: all 7 plan types rate to balanced exact Decimal ───────────────

ALL_TYPE_CASES = [
    ("flat", [{"rate": "0.10"}], "500", {}, "50.00"),
    ("tiered", [{"tier_start": "0", "tier_end": "100", "rate": "0.05"},
                {"tier_start": "100", "rate": "0.10"}], "600", {}, "55.00"),
    ("volume_discount", [{"tier_start": "0", "tier_end": "1000",
                          "rate": "0.10"},
                         {"tier_start": "1000", "rate": "0.08"}],
     "1500", {}, "120.00"),
    ("time_of_use", TOU_TIERS, "200",
     {"usage_by_period": {"peak": "100", "off_peak": "100"}}, "25.00"),
    ("demand", DEMAND_TIERS, "100", {"peak_demand": "50"}, "605.00"),
    ("prepaid_credit", [{"rate": "0.10"}], "300", {}, "30.00"),
    ("hybrid", [], "1500", {"tier_strategy": HYBRID_STRATEGY}, "150.00"),
]


class TestAllPlanTypesExactDecimal:
    @pytest.mark.parametrize(
        "plan_type,tiers,consumption,extra,expected_usage",
        ALL_TYPE_CASES, ids=[c[0] for c in ALL_TYPE_CASES])
    def test_rates_to_balanced_exact_decimal(
            self, plan_type, tiers, consumption, extra, expected_usage):
        result = mod._calculate_charge(
            plan_type, tiers, consumption, base_charge="10.00", **extra)
        for key in ("usage_charge", "base_charge", "total_charge"):
            assert isinstance(result[key], str), (
                f"{plan_type}.{key} must be TEXT, got {type(result[key])}")
            Decimal(result[key])  # exact-parseable, raises otherwise
        assert Decimal(result["usage_charge"]) == Decimal(expected_usage)
        # Balanced: total == base + usage, exactly (no minimum in play)
        assert (Decimal(result["total_charge"])
                == Decimal(result["base_charge"])
                + Decimal(result["usage_charge"]))

    def test_unknown_plan_type_raises_loud(self):
        with pytest.raises(mod.RatingError, match="data error"):
            mod._calculate_charge("bogus", [], "100")

    def test_supported_covers_all_seven(self):
        assert tuple(mod.VALID_SUPPORTED_PLAN_TYPES) == tuple(mod.VALID_PLAN_TYPES)
        assert len(mod.VALID_PLAN_TYPES) == 7


# ── time_of_use ─────────────────────────────────────────────────────────────

class TestTimeOfUse:
    def test_noon_is_peak_3am_is_off_peak(self, conn, env):
        plan = _create_plan(conn, "time_of_use", tiers=TOU_TIERS)
        noon = call_action(mod.rate_consumption, conn, ns(
            rate_plan_id=plan["id"], consumption="100",
            usage_by_period=json.dumps({"peak": "100"}),
        ))
        assert is_ok(noon), noon
        assert Decimal(noon["calculation"]["usage_charge"]) == Decimal("20.00")
        three_am = call_action(mod.rate_consumption, conn, ns(
            rate_plan_id=plan["id"], consumption="100",
            usage_by_period=json.dumps({"off_peak": "100"}),
        ))
        assert Decimal(three_am["calculation"]["usage_charge"]) == Decimal("5.00")

    def test_run_billing_partitions_events_by_timestamp(self, conn, env):
        plan = _create_plan(conn, "time_of_use", tiers=TOU_TIERS)
        meter = _create_meter(conn, env, plan["id"])
        _add_event(conn, meter["id"], "2026-06-10 12:00:00", "100")  # peak
        _add_event(conn, meter["id"], "2026-06-11 03:00:00", "100")  # off_peak
        result = _run_billing(conn, env, "2026-06-01", "2026-06-30")
        assert is_ok(result), result
        assert result["periods_created"] == 1
        assert result["errors"] == []
        # 100*0.20 + 100*0.05 = 25.00
        assert Decimal(result["total_billed"]) == Decimal("25.00")
        bp = _get_period(conn, result["period_ids"][0])
        assert Decimal(bp["grand_total"]) == Decimal("25.00")
        assert bp["status"] == "rated"

    def test_rate_consumption_requires_partitioned_usage(self, conn, env):
        plan = _create_plan(conn, "time_of_use", tiers=TOU_TIERS)
        result = call_action(mod.rate_consumption, conn, ns(
            rate_plan_id=plan["id"], consumption="100",
        ))
        assert is_error(result)

    def test_date_only_reading_is_loud_error_in_run_billing(self, conn, env):
        plan = _create_plan(conn, "time_of_use", tiers=TOU_TIERS)
        meter = _create_meter(conn, env, plan["id"])
        conn.execute(
            """INSERT INTO meter_reading (id, meter_id, reading_date,
               reading_value, consumption, reading_type, source, validated)
               VALUES (?, ?, '2026-06-10', '500', '500', 'actual',
                       'manual', 0)""",
            (str(uuid.uuid4()), meter["id"]))
        conn.commit()
        result = _run_billing(conn, env, "2026-06-01", "2026-06-30")
        assert is_ok(result), result
        assert result["periods_created"] == 0
        assert result["error_count"] == 1
        assert "time-stamped usage" in result["errors"][0]["error"]
        assert result["errors"][0]["meter_id"] == meter["id"]

    def test_validator_rejects_gap(self, conn, env):
        tiers = [t for t in TOU_TIERS if t["time_of_use_period"] != "shoulder"]
        result = call_action(mod.add_rate_plan, conn, _plan_args(
            billing_model="time_of_use", tiers=json.dumps(tiers)))
        assert is_error(result)
        assert "gap" in result["message"]

    def test_validator_rejects_overlap(self, conn, env):
        tiers = json.loads(json.dumps(TOU_TIERS))
        tiers[0]["time_of_use_hours"] = ["08:00-17:00"]  # overlaps shoulder
        result = call_action(mod.add_rate_plan, conn, _plan_args(
            billing_model="time_of_use", tiers=json.dumps(tiers)))
        assert is_error(result)
        assert "overlap" in result["message"]

    def test_validator_rejects_duplicate_period(self, conn, env):
        tiers = TOU_TIERS + [{"time_of_use_period": "peak",
                              "time_of_use_hours": ["17:00-18:00"],
                              "rate": "0.30"}]
        result = call_action(mod.add_rate_plan, conn, _plan_args(
            billing_model="time_of_use", tiers=json.dumps(tiers)))
        assert is_error(result)
        assert "more than one tier" in result["message"]

    def test_validator_rejects_cross_midnight_range(self, conn, env):
        tiers = json.loads(json.dumps(TOU_TIERS))
        tiers[1]["time_of_use_hours"] = ["22:00-06:00"]
        result = call_action(mod.add_rate_plan, conn, _plan_args(
            billing_model="time_of_use", tiers=json.dumps(tiers)))
        assert is_error(result)

    def test_validator_accepts_wave_file_dict_shape(self, conn, env):
        hours = {"peak": ["09:00-17:00"],
                 "off_peak": ["00:00-06:00", "22:00-24:00"],
                 "shoulder": ["06:00-09:00", "17:00-22:00"]}
        tiers = [
            {"time_of_use_period": p, "time_of_use_hours": hours, "rate": r}
            for p, r in (("peak", "0.20"), ("off_peak", "0.05"),
                         ("shoulder", "0.10"))
        ]
        result = call_action(mod.add_rate_plan, conn, _plan_args(
            billing_model="time_of_use", tiers=json.dumps(tiers)))
        assert is_ok(result), result

    def test_update_rate_plan_validates_tou_tiers(self, conn, env):
        plan = _create_plan(conn, "time_of_use", tiers=TOU_TIERS)
        bad = [t for t in TOU_TIERS if t["time_of_use_period"] != "peak"]
        result = call_action(mod.update_rate_plan, conn, ns(
            rate_plan_id=plan["id"], name=None, base_charge=None,
            effective_to=None, minimum_charge=None, overage_rate=None,
            tiers=json.dumps(bad), tier_strategy=None,
        ))
        assert is_error(result)


# ── demand ──────────────────────────────────────────────────────────────────

class TestDemand:
    def test_peak_50_times_12_is_600(self):
        result = mod._calculate_charge(
            "demand", [{"demand_type": "demand", "rate": "12"}], "0",
            peak_demand="50")
        assert Decimal(result["usage_charge"]) == Decimal("600.00")
        assert Decimal(result["demand_charge"]) == Decimal("600.00")

    def test_energy_component_separate(self):
        result = mod._calculate_charge(
            "demand", DEMAND_TIERS, "100", peak_demand="50")
        # demand 50*12=600, energy 100*0.05=5
        assert Decimal(result["demand_charge"]) == Decimal("600.00")
        assert Decimal(result["usage_charge"]) == Decimal("605.00")

    def test_run_billing_derives_peak_from_events(self, conn, env):
        plan = _create_plan(conn, "demand", tiers=DEMAND_TIERS)
        meter = _create_meter(conn, env, plan["id"])
        for ts, qty in (("2026-06-05 10:00:00", "30"),
                        ("2026-06-10 18:00:00", "50"),
                        ("2026-06-20 02:00:00", "20")):
            _add_event(conn, meter["id"], ts, qty)
        result = _run_billing(conn, env, "2026-06-01", "2026-06-30")
        assert is_ok(result), result
        assert result["errors"] == []
        assert result["periods_created"] == 1
        bp = _get_period(conn, result["period_ids"][0])
        assert Decimal(bp["peak_demand"]) == Decimal("50")
        assert Decimal(bp["demand_charge"]) == Decimal("600.00")
        # usage = 600 demand + (30+50+20)*0.05 = 605; grand = 605
        assert Decimal(bp["usage_charge"]) == Decimal("605.00")
        assert Decimal(bp["grand_total"]) == Decimal("605.00")

    def test_rate_consumption_requires_peak_demand(self, conn, env):
        plan = _create_plan(conn, "demand", tiers=DEMAND_TIERS)
        result = call_action(mod.rate_consumption, conn, ns(
            rate_plan_id=plan["id"], consumption="100",
        ))
        assert is_error(result)

    def test_plan_requires_demand_tier(self, conn, env):
        result = call_action(mod.add_rate_plan, conn, _plan_args(
            billing_model="demand",
            tiers=json.dumps([{"demand_type": "energy", "rate": "0.05"}])))
        assert is_error(result)
        assert "demand_type='demand'" in result["message"]


# ── prepaid_credit ──────────────────────────────────────────────────────────

class TestPrepaidCredit:
    def _setup(self, conn, env, credit="100.00"):
        plan = _create_plan(conn, "prepaid_credit", tiers=[{"rate": "0.10"}])
        meter = _create_meter(conn, env, plan["id"])
        result = call_action(mod.add_prepaid_credit, conn, ns(
            customer_id=env["customer"], amount=credit,
            valid_until="2027-12-31", rate_plan_id=plan["id"],
        ))
        assert is_ok(result), result
        return plan, meter

    def _remaining(self, conn, env):
        result = call_action(mod.get_prepaid_balance, conn, ns(
            customer_id=env["customer"]))
        assert is_ok(result), result
        return Decimal(result["total_remaining"])

    def test_deduct_then_insufficient(self, conn, env):
        plan, meter = self._setup(conn, env, credit="100.00")
        # Charge 1: 300 kWh * 0.10 = 30.00 -> balance 100 -> 70
        _add_event(conn, meter["id"], "2026-06-10 10:00:00", "300")
        run1 = _run_billing(conn, env, "2026-06-01", "2026-06-30")
        assert is_ok(run1), run1
        assert run1["errors"] == []
        assert Decimal(run1["total_billed"]) == Decimal("30.00")
        assert run1["prepaid"][0]["over_limit"] is False
        assert Decimal(run1["prepaid"][0]["deducted"]) == Decimal("30.00")
        assert self._remaining(conn, env) == Decimal("70.00")

        # Charge 2: 800 kWh * 0.10 = 80.00 > 70 -> over_limit, NO deduction
        _add_event(conn, meter["id"], "2026-07-10 10:00:00", "800")
        run2 = _run_billing(conn, env, "2026-07-01", "2026-07-31")
        assert is_ok(run2), run2
        outcome = run2["prepaid"][0]
        assert outcome["over_limit"] is True
        assert Decimal(outcome["credit_shortfall"]) == Decimal("10.00")
        assert Decimal(outcome["deducted"]) == Decimal("0.00")
        assert self._remaining(conn, env) == Decimal("70.00")
        # The period still rates (the amount is owed) — explicitly, not silently
        bp = _get_period(conn, outcome["billing_period_id"])
        assert bp["status"] == "rated"
        assert Decimal(bp["grand_total"]) == Decimal("80.00")

    def test_exhausted_status_set_when_fully_consumed(self, conn, env):
        plan, meter = self._setup(conn, env, credit="30.00")
        _add_event(conn, meter["id"], "2026-06-10 10:00:00", "300")
        run = _run_billing(conn, env, "2026-06-01", "2026-06-30")
        assert is_ok(run), run
        assert self._remaining(conn, env) == Decimal("0.00")
        row = conn.execute(
            "SELECT status FROM prepaid_credit_balance WHERE customer_id = ?",
            (env["customer"],)).fetchone()
        assert row["status"] == "exhausted"

    def test_rate_consumption_previews_without_deducting(self, conn, env):
        plan, meter = self._setup(conn, env, credit="100.00")
        result = call_action(mod.rate_consumption, conn, ns(
            rate_plan_id=plan["id"], consumption="300",
            customer_id=env["customer"],
        ))
        assert is_ok(result), result
        prepaid = result["calculation"]["prepaid"]
        assert prepaid["preview"] is True
        assert prepaid["over_limit"] is False
        assert self._remaining(conn, env) == Decimal("100.00")  # untouched

    def test_rate_consumption_requires_customer(self, conn, env):
        plan, _ = self._setup(conn, env)
        result = call_action(mod.rate_consumption, conn, ns(
            rate_plan_id=plan["id"], consumption="300",
        ))
        assert is_error(result)


# ── hybrid ──────────────────────────────────────────────────────────────────

class TestHybrid:
    def test_wave_scenario_800_and_1500(self, conn, env):
        plan = _create_plan(conn, "hybrid", tier_strategy=HYBRID_STRATEGY)
        r800 = call_action(mod.rate_consumption, conn, ns(
            rate_plan_id=plan["id"], consumption="800"))
        assert is_ok(r800), r800
        assert Decimal(r800["calculation"]["total_charge"]) == Decimal("100.00")
        r1500 = call_action(mod.rate_consumption, conn, ns(
            rate_plan_id=plan["id"], consumption="1500"))
        assert Decimal(r1500["calculation"]["total_charge"]) == Decimal("150.00")

    def test_run_billing_hybrid_end_to_end(self, conn, env):
        plan = _create_plan(conn, "hybrid", tier_strategy=HYBRID_STRATEGY)
        meter = _create_meter(conn, env, plan["id"])
        _add_event(conn, meter["id"], "2026-06-10 10:00:00", "1500")
        result = _run_billing(conn, env, "2026-06-01", "2026-06-30")
        assert is_ok(result), result
        assert result["errors"] == []
        assert Decimal(result["total_billed"]) == Decimal("150.00")

    def test_requires_tier_strategy(self, conn, env):
        result = call_action(mod.add_rate_plan, conn, _plan_args(
            billing_model="hybrid"))
        assert is_error(result)
        assert "tier-strategy" in result["message"]

    def test_rejects_nested_hybrid(self, conn, env):
        bad = {"components": [{"type": "hybrid", "tiers": [{"rate": "1"}]}]}
        result = call_action(mod.add_rate_plan, conn, _plan_args(
            billing_model="hybrid", tier_strategy=json.dumps(bad)))
        assert is_error(result)
        assert "nested" in result["message"]

    def test_rejects_prepaid_component(self, conn, env):
        bad = {"components": [{"type": "prepaid_credit",
                               "tiers": [{"rate": "1"}]}]}
        result = call_action(mod.add_rate_plan, conn, _plan_args(
            billing_model="hybrid", tier_strategy=json.dumps(bad)))
        assert is_error(result)
        assert "settlement mechanism" in result["message"]

    def test_rejects_strategy_on_non_hybrid(self, conn, env):
        result = call_action(mod.add_rate_plan, conn, _plan_args(
            billing_model="flat", tiers=json.dumps([{"rate": "0.10"}]),
            tier_strategy=json.dumps(HYBRID_STRATEGY)))
        assert is_error(result)

    def test_rejects_empty_components(self, conn, env):
        result = call_action(mod.add_rate_plan, conn, _plan_args(
            billing_model="hybrid", tier_strategy=json.dumps({"components": []})))
        assert is_error(result)


# ── S1.2a: run-billing loud per-meter errors ────────────────────────────────

class TestRunBillingLoudErrors:
    def test_dangling_rate_plan_is_reported_not_skipped(self, conn, env):
        plan = _create_plan(conn, "flat", tiers=[{"rate": "0.10"}])
        meter = _create_meter(conn, env, plan["id"])
        conn.execute("UPDATE meter SET rate_plan_id = 'dangling-ref' "
                     "WHERE id = ?", (meter["id"],))
        conn.commit()
        result = _run_billing(conn, env, "2026-06-01", "2026-06-30")
        assert is_ok(result), result
        assert result["periods_created"] == 0
        assert result["error_count"] == 1
        entry = result["errors"][0]
        assert entry["meter_id"] == meter["id"]
        assert "Rate plan not found" in entry["error"]

    def test_healthy_flat_meter_still_bills_with_no_errors(self, conn, env):
        plan = _create_plan(conn, "flat", tiers=[{"rate": "0.10"}])
        meter = _create_meter(conn, env, plan["id"])
        _add_event(conn, meter["id"], "2026-06-10 10:00:00", "500")
        result = _run_billing(conn, env, "2026-06-01", "2026-06-30")
        assert is_ok(result), result
        assert result["periods_created"] == 1
        assert result["errors"] == []
        assert Decimal(result["total_billed"]) == Decimal("50.00")

    def test_one_bad_meter_does_not_abort_the_run(self, conn, env):
        good_plan = _create_plan(conn, "flat", tiers=[{"rate": "0.10"}])
        good_meter = _create_meter(conn, env, good_plan["id"])
        _add_event(conn, good_meter["id"], "2026-06-10 10:00:00", "100")
        bad_plan = _create_plan(conn, "flat", tiers=[{"rate": "0.10"}])
        bad_meter = _create_meter(conn, env, bad_plan["id"])
        conn.execute("UPDATE meter SET rate_plan_id = 'gone' WHERE id = ?",
                     (bad_meter["id"],))
        conn.commit()
        result = _run_billing(conn, env, "2026-06-01", "2026-06-30")
        assert is_ok(result), result
        assert result["periods_created"] == 1
        assert result["error_count"] == 1


# ── S1.2b: generate-invoices never lies ─────────────────────────────────────

def _rated_period(conn, env):
    plan = call_action(mod.add_rate_plan, conn, _plan_args(
        billing_model="flat", tiers=json.dumps([{"rate": "0.10"}])))
    meter = _create_meter(conn, env, plan["rate_plan"]["id"])
    _add_event(conn, meter["id"], "2026-06-10 10:00:00", "500")
    run = _run_billing(conn, env, "2026-06-01", "2026-06-30")
    assert run["periods_created"] == 1
    return run["period_ids"][0]


def _fake_script(tmp_path, name, body):
    path = tmp_path / name
    path.write_text("#!/usr/bin/env python3\n" + body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return str(path)


class TestGenerateInvoicesHonesty:
    def test_selling_unavailable_leaves_period_rated(self, conn, env,
                                                     tmp_path):
        bp_id = _rated_period(conn, env)
        with patch("erpclaw_lib.dependencies.resolve_skill_script",
                   return_value=None):
            result = call_action(mod.generate_invoices, conn, ns(
                billing_period_ids=json.dumps([bp_id])))
        assert is_ok(result), result
        assert result["generated"] == 0
        assert result["failed"] == 1
        assert "unavailable" in result["results"][0]["error"]
        bp = _get_period(conn, bp_id)
        assert bp["status"] == "rated"
        assert bp["invoice_id"] is None

    def test_failing_subprocess_leaves_period_rated(self, conn, env,
                                                    tmp_path):
        bp_id = _rated_period(conn, env)
        fail = _fake_script(tmp_path, "fail.py",
                            "import sys; print('boom'); sys.exit(1)\n")
        with patch("erpclaw_lib.dependencies.resolve_skill_script",
                   return_value=fail):
            result = call_action(mod.generate_invoices, conn, ns(
                billing_period_ids=json.dumps([bp_id])))
        assert is_ok(result), result
        assert result["generated"] == 0
        assert result["failed"] == 1
        assert "exited 1" in result["results"][0]["error"]
        bp = _get_period(conn, bp_id)
        assert bp["status"] == "rated"
        assert bp["invoice_id"] is None

    def test_error_status_output_leaves_period_rated(self, conn, env,
                                                     tmp_path):
        bp_id = _rated_period(conn, env)
        errscript = _fake_script(
            tmp_path, "err.py",
            "import json; print(json.dumps("
            "{'status': 'error', 'error': 'no such customer'}))\n")
        with patch("erpclaw_lib.dependencies.resolve_skill_script",
                   return_value=errscript):
            result = call_action(mod.generate_invoices, conn, ns(
                billing_period_ids=json.dumps([bp_id])))
        assert result["generated"] == 0
        assert "no such customer" in result["results"][0]["error"]
        assert _get_period(conn, bp_id)["status"] == "rated"

    def test_real_invoice_id_records_link_leaves_rated(self, conn, env, tmp_path):
        bp_id = _rated_period(conn, env)
        # One fake dispatcher stands in for both subprocess legs generate-
        # invoices now drives (ADR-0033): add-item -> item_id, then
        # add-sales-invoice -> sales_invoice_id (the real top-level key). The
        # honesty claim is unchanged, but the SEMANTICS are N8 (F22): the draft
        # posts no GL, so the period stays 'rated' with the invoice_id link (the
        # double-generation guard) and NO invoiced_at. It flips to 'invoiced'
        # only when the invoice is submitted (see test_billing_period_invoice_state).
        okscript = _fake_script(
            tmp_path, "ok.py",
            "import json; print(json.dumps("
            "{'status': 'ok', 'item_id': 'svc-item-1', "
            "'sales_invoice_id': 'inv-real-1'}))\n")
        with patch("erpclaw_lib.dependencies.resolve_skill_script",
                   return_value=okscript):
            result = call_action(mod.generate_invoices, conn, ns(
                billing_period_ids=json.dumps([bp_id])))
        assert result["generated"] == 1
        assert result["failed"] == 0
        bp = _get_period(conn, bp_id)
        assert bp["status"] == "rated"
        assert bp["invoice_id"] == "inv-real-1"
        assert bp["invoiced_at"] is None


# ── F3 (M47): un-mocked end-to-end — a real invoice is produced ─────────────
#
# The four TestGenerateInvoicesHonesty tests above pin the failure contract
# against fake scripts. This one drives the REAL erpclaw dispatcher
# (erpclaw-selling create-sales-invoice + erpclaw-inventory add-item) with NO
# resolve_skill_script patch, all the way to a real sales_invoice row. Per
# ADR-0033, generate-invoices resolves-or-creates one generic company-scoped
# billing service item and posts a standalone draft invoice for the period.

import billing_helpers as _bh

# <root>/erpclaw/scripts/db_query.py must resolve; MODULE_DIR is
# <root>/erpclaw/scripts/erpclaw-billing, so OPENCLAW_SKILLS_DIR = <root>.
_SKILLS_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_bh.MODULE_DIR)))


def test_generate_invoices_real_end_to_end(conn, env, db_path, monkeypatch):
    # Point resolve_skill_script at the real in-tree dispatcher. We do NOT
    # patch resolve_skill_script itself — that is the honesty tests' job; this
    # test must exercise the real selling/inventory scripts end to end.
    monkeypatch.setenv("OPENCLAW_SKILLS_DIR", _SKILLS_ROOT)
    assert os.path.exists(os.path.join(
        _SKILLS_ROOT, "erpclaw", "scripts", "db_query.py")), \
        "real dispatcher not found — check OPENCLAW_SKILLS_DIR derivation"

    bp_id = _rated_period(conn, env)
    assert Decimal(_get_period(conn, bp_id)["grand_total"]) == Decimal("50.00")

    result = call_action(mod.generate_invoices, conn, ns(
        billing_period_ids=json.dumps([bp_id]), db_path=db_path))
    assert is_ok(result), result
    assert result["generated"] == 1, result
    assert result["failed"] == 0, result
    invoice_id = result["results"][0]["invoice_id"]

    # A real sales_invoice row with the right customer, company and total,
    # posting-dated to the period end (ADR-0033 distinguishability).
    inv = conn.execute(
        "SELECT customer_id, company_id, grand_total, status, posting_date "
        "FROM sales_invoice WHERE id = ?", (invoice_id,)).fetchone()
    assert inv is not None, "no real sales_invoice row was created"
    assert inv["customer_id"] == env["customer"]
    assert inv["company_id"] == env["company_id"]
    assert Decimal(inv["grand_total"]) == Decimal("50.00")
    # A freshly generated invoice is a draft — no GL yet (F22).
    assert inv["status"] == "draft"
    assert inv["posting_date"] == _get_period(conn, bp_id)["period_end"]

    # The line carries the auto-created generic billing service item.
    line = conn.execute(
        "SELECT item_id FROM sales_invoice_item WHERE sales_invoice_id = ?",
        (invoice_id,)).fetchone()
    assert line is not None
    item = conn.execute(
        "SELECT item_code, item_name, item_type, is_stock_item "
        "FROM item WHERE id = ?", (line["item_id"],)).fetchone()
    assert item["item_code"] == f"BILLING-SVC-{env['company_id']}"
    assert item["item_name"] == "Billing Service"
    assert item["item_type"] == "service"
    assert item["is_stock_item"] == 0

    # The period now points at the created draft invoice but stays 'rated'
    # (the draft posts no GL) — the link is the double-generation guard (F22).
    bp = _get_period(conn, bp_id)
    assert bp["status"] == "rated"
    assert bp["invoice_id"] == invoice_id
    assert bp["invoiced_at"] is None


def test_generate_invoices_reuses_one_service_item_across_periods(
        conn, env, db_path, monkeypatch):
    # ADR-0033: one billing service item per company — a second period must
    # reuse it, never create a duplicate.
    monkeypatch.setenv("OPENCLAW_SKILLS_DIR", _SKILLS_ROOT)
    bp1 = _rated_period(conn, env)
    r1 = call_action(mod.generate_invoices, conn, ns(
        billing_period_ids=json.dumps([bp1]), db_path=db_path))
    assert r1["generated"] == 1, r1
    bp2 = _rated_period(conn, env)
    r2 = call_action(mod.generate_invoices, conn, ns(
        billing_period_ids=json.dumps([bp2]), db_path=db_path))
    assert r2["generated"] == 1, r2
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM item WHERE item_code = ?",
        (f"BILLING-SVC-{env['company_id']}",)).fetchone()["n"]
    assert n == 1, f"expected exactly one billing service item, found {n}"


def _two_leg_script(tmp_path, name, add_item_line, add_sales_invoice_line):
    """A fake dispatcher that branches on --action, so generate-invoices'
    two subprocess legs (add-item, then add-sales-invoice) can be stubbed
    independently. Each *_line is one Python statement run for that action."""
    return _fake_script(
        tmp_path, name,
        "import sys, json\n"
        "_act = None\n"
        "for _i, _a in enumerate(sys.argv):\n"
        "    if _a == '--action' and _i + 1 < len(sys.argv):\n"
        "        _act = sys.argv[_i + 1]\n"
        "if _act == 'add-item':\n"
        f"    {add_item_line}\n"
        "elif _act == 'add-sales-invoice':\n"
        f"    {add_sales_invoice_line}\n"
        "else:\n"
        "    print(json.dumps({'status': 'error', 'error': 'unexpected action'})); sys.exit(2)\n")


def test_generate_invoices_add_sales_invoice_failure_leg_stays_rated(
        conn, env, tmp_path):
    # ADR-0033 made generate-invoices drive TWO subprocess legs (add-item,
    # then add-sales-invoice). The single-script honesty fixtures now fail at
    # the add-item leg, so they no longer exercise the add-sales-invoice
    # failure path. This restores that coverage with an action-aware fake:
    # add-item succeeds, add-sales-invoice fails -> the period must stay
    # 'rated' with no invoice_id, and the error must name the invoice leg.
    bp_id = _rated_period(conn, env)
    two_leg = _two_leg_script(
        tmp_path, "invoice_leg_fails.py",
        "print(json.dumps({'status': 'ok', 'item_id': 'svc-item-1'}))",
        "print('kaboom'); sys.exit(1)")
    with patch("erpclaw_lib.dependencies.resolve_skill_script",
               return_value=two_leg):
        result = call_action(mod.generate_invoices, conn, ns(
            billing_period_ids=json.dumps([bp_id])))
    assert is_ok(result), result
    assert result["generated"] == 0
    assert result["failed"] == 1
    assert "add-sales-invoice exited 1" in result["results"][0]["error"]
    bp = _get_period(conn, bp_id)
    assert bp["status"] == "rated"
    assert bp["invoice_id"] is None


def test_generate_invoices_add_sales_invoice_error_status_stays_rated(
        conn, env, tmp_path):
    # Companion to the above: add-item ok, add-sales-invoice returns a
    # status:error JSON (exit 0) -> period stays 'rated', error surfaced.
    bp_id = _rated_period(conn, env)
    two_leg = _two_leg_script(
        tmp_path, "invoice_leg_error.py",
        "print(json.dumps({'status': 'ok', 'item_id': 'svc-item-1'}))",
        "print(json.dumps({'status': 'error', 'error': 'no such customer'}))")
    with patch("erpclaw_lib.dependencies.resolve_skill_script",
               return_value=two_leg):
        result = call_action(mod.generate_invoices, conn, ns(
            billing_period_ids=json.dumps([bp_id])))
    assert result["generated"] == 0
    assert "no such customer" in result["results"][0]["error"]
    assert _get_period(conn, bp_id)["status"] == "rated"


# ── regression: existing 3 types keep their exact numbers ───────────────────

class TestExistingTypesRegression:
    def test_flat_unchanged(self):
        result = mod._calculate_charge(
            "flat", [{"rate": "0.10"}], "500", base_charge="25.00")
        assert Decimal(result["usage_charge"]) == Decimal("50.00")
        assert Decimal(result["total_charge"]) == Decimal("75.00")

    def test_tiered_unchanged(self):
        tiers = [
            {"tier_start": "0", "tier_end": "100", "rate": "0.05"},
            {"tier_start": "100", "tier_end": "500", "rate": "0.10"},
            {"tier_start": "500", "rate": "0.15"},
        ]
        result = mod._calculate_charge(
            "tiered", tiers, "600", base_charge="10.00")
        assert Decimal(result["usage_charge"]) == Decimal("60.00")
        assert Decimal(result["total_charge"]) == Decimal("70.00")

    def test_volume_discount_unchanged(self):
        tiers = [
            {"tier_start": "0", "tier_end": "1000", "rate": "0.10"},
            {"tier_start": "1000", "rate": "0.08"},
        ]
        result = mod._calculate_charge("volume_discount", tiers, "1500")
        assert Decimal(result["usage_charge"]) == Decimal("120.00")

    def test_minimum_charge_still_applies(self):
        result = mod._calculate_charge(
            "flat", [{"rate": "0.10"}], "10", minimum_charge="50.00")
        assert Decimal(result["total_charge"]) == Decimal("50.00")
