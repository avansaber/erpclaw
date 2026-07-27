"""QA bounce regressions (2026-07-25) — wavef-s11-s13 round 2.

Pins the four branch-attributable defects + the cross-dialect aggregation fix:
- D1: garbage money in TOU/hybrid validators is a CAUGHT validation error
  with a named message, never "An unexpected error occurred".
- D2: prepaid preview (rate-consumption) never claims money moved —
  deducted/remaining_credit report DB truth; projection has its own keys.
- D3: garbage money is rejected at write time on every entry action, and
  legacy garbage already in the DB is a contained per-meter error in
  run-billing (one bad meter neither hides nor aborts the run).
- D4: mutation-killers for the three QA-surviving mutants — lexical peak
  max (M2), float TOU math (M5), removed expired-credit filter (M7).
- F1: usage aggregation is exact Decimal on both dialects
  (SQL SUM over TEXT is float drift on SQLite, an error on PostgreSQL).

Round 2 (2026-07-26, final bounce) — classes prefixed TestR2:
- DEFECT-1: non-finite Decimals ('NaN'/'Infinity', any case) are rejected
  by every write gate and by _dec on the read side — non-finite money can
  neither enter the DB nor bill through as a successful charge.
- DEFECT-2: aggregation is Python-side via _dec, so one garbage legacy
  usage_event.quantity / meter_reading.consumption row is a contained
  per-meter error naming the row — never a whole-run abort (S1.2a).
"""
import json
import uuid
from decimal import Decimal

import pytest

from billing_helpers import call_action, ns, is_error, is_ok
from test_rating_engine import (
    mod, _plan_args, _create_plan, _create_meter, _add_event,
    _run_billing, _get_period, TOU_TIERS, DEMAND_TIERS,
)

UNEXPECTED = "An unexpected error occurred"


def _garbage_tou_tiers(rate="abc"):
    tiers = json.loads(json.dumps(TOU_TIERS))
    tiers[0]["rate"] = rate
    return tiers


# ── D1: validator garbage -> caught validation error, not a crash ──────────

class TestD1ValidatorGarbageIsCaught:
    def test_tou_tier_rate_garbage_names_the_field(self, conn, env):
        result = call_action(mod.add_rate_plan, conn, _plan_args(
            billing_model="time_of_use",
            tiers=json.dumps(_garbage_tou_tiers())))
        assert is_error(result), result
        assert result["message"] != UNEXPECTED
        assert "not a valid number" in result["message"]
        assert "'abc'" in result["message"]

    def test_hybrid_base_charge_garbage_names_the_field(self, conn, env):
        bad = {"components": [{"type": "flat", "tiers": [{"rate": "0"}],
                               "base_charge": "ten dollars"}]}
        result = call_action(mod.add_rate_plan, conn, _plan_args(
            billing_model="hybrid", tier_strategy=json.dumps(bad)))
        assert is_error(result), result
        assert result["message"] != UNEXPECTED
        assert "base_charge" in result["message"]
        assert "not a valid number" in result["message"]

    def test_hybrid_minimum_charge_list_names_the_field(self, conn, env):
        bad = {"components": [{"type": "flat", "tiers": [{"rate": "0"}],
                               "minimum_charge": []}]}
        result = call_action(mod.add_rate_plan, conn, _plan_args(
            billing_model="hybrid", tier_strategy=json.dumps(bad)))
        assert is_error(result), result
        assert result["message"] != UNEXPECTED
        assert "minimum_charge" in result["message"]

    def test_hybrid_component_tier_rate_garbage_is_caught(self, conn, env):
        bad = {"components": [{"type": "flat", "tiers": [{"rate": "abc"}]}]}
        result = call_action(mod.add_rate_plan, conn, _plan_args(
            billing_model="hybrid", tier_strategy=json.dumps(bad)))
        assert is_error(result), result
        assert result["message"] != UNEXPECTED
        assert "not a valid number" in result["message"]


# ── D3 (write gate): garbage money never enters the DB ─────────────────────

class TestD3WriteTimeGate:
    def test_flat_tier_rate_garbage_rejected_at_add(self, conn, env):
        result = call_action(mod.add_rate_plan, conn, _plan_args(
            name="Bad Flat", billing_model="flat",
            tiers=json.dumps([{"rate": "abc"}])))
        assert is_error(result), result
        assert "not a valid number" in result["message"]
        # nothing stored
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM rate_plan WHERE name = 'Bad Flat'"
        ).fetchone()
        assert row["cnt"] == 0

    def test_update_rate_plan_rejects_garbage_tier_rate(self, conn, env):
        plan = _create_plan(conn, "flat", tiers=[{"rate": "0.10"}])
        result = call_action(mod.update_rate_plan, conn, ns(
            rate_plan_id=plan["id"], name=None, base_charge=None,
            effective_to=None, minimum_charge=None, overage_rate=None,
            tiers=json.dumps([{"rate": "abc"}]), tier_strategy=None))
        assert is_error(result), result
        assert "not a valid number" in result["message"]
        row = conn.execute(
            "SELECT rate FROM rate_tier WHERE rate_plan_id = ?",
            (plan["id"],)).fetchone()
        assert row["rate"] == "0.10"  # old tier untouched

    def test_plan_level_money_flags_rejected(self, conn, env):
        result = call_action(mod.add_rate_plan, conn, _plan_args(
            billing_model="flat", tiers=json.dumps([{"rate": "0.10"}]),
            base_charge="free"))
        assert is_error(result), result
        assert "--base-charge" in result["message"]

    def test_update_rate_plan_rejects_garbage_base_charge(self, conn, env):
        plan = _create_plan(conn, "flat", tiers=[{"rate": "0.10"}])
        result = call_action(mod.update_rate_plan, conn, ns(
            rate_plan_id=plan["id"], name=None, base_charge="lots",
            effective_to=None, minimum_charge=None, overage_rate=None,
            tiers=None, tier_strategy=None))
        assert is_error(result), result
        assert "--base-charge" in result["message"]

    def test_add_prepaid_credit_rejects_garbage_and_negative(self, conn, env):
        for amount in ("abc", "-5", "0"):
            result = call_action(mod.add_prepaid_credit, conn, ns(
                customer_id=env["customer"], amount=amount,
                valid_until="2027-12-31", rate_plan_id=None))
            assert is_error(result), (amount, result)
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM prepaid_credit_balance"
        ).fetchone()
        assert row["cnt"] == 0

    def test_add_usage_event_rejects_garbage_quantity(self, conn, env):
        plan = _create_plan(conn, "flat", tiers=[{"rate": "0.10"}])
        meter = _create_meter(conn, env, plan["id"])
        result = call_action(mod.add_usage_event, conn, ns(
            meter_id=meter["id"], event_date="2026-06-10 10:00:00",
            quantity="abc", event_type="usage", properties=None,
            idempotency_key=None))
        assert is_error(result), result
        assert "--quantity" in result["message"]

    def test_batch_reports_garbage_quantity_per_event(self, conn, env):
        plan = _create_plan(conn, "flat", tiers=[{"rate": "0.10"}])
        meter = _create_meter(conn, env, plan["id"])
        events = [
            {"meter_id": meter["id"], "event_date": "2026-06-10 10:00:00",
             "quantity": "100"},
            {"meter_id": meter["id"], "event_date": "2026-06-10 11:00:00",
             "quantity": "abc"},
        ]
        result = call_action(mod.add_usage_events_batch, conn, ns(
            events=json.dumps(events)))
        assert is_ok(result), result
        assert result["inserted"] == 1
        assert len(result["errors"]) == 1
        assert result["errors"][0]["index"] == 1
        assert "quantity must be a number" in result["errors"][0]["error"]

    def test_add_meter_reading_rejects_garbage_value(self, conn, env):
        plan = _create_plan(conn, "flat", tiers=[{"rate": "0.10"}])
        meter = _create_meter(conn, env, plan["id"])
        result = call_action(mod.add_meter_reading, conn, ns(
            meter_id=meter["id"], reading_date="2026-06-10",
            reading_value="abc", reading_type=None, source=None, uom=None))
        assert is_error(result), result
        assert result["message"] != UNEXPECTED
        assert "--reading-value" in result["message"]

    def test_add_billing_adjustment_rejects_garbage_amount(self, conn, env):
        result = call_action(mod.add_billing_adjustment, conn, ns(
            billing_period_id="whatever", amount="abc",
            adjustment_type="credit", reason=None, approved_by=None))
        assert is_error(result), result
        assert "--amount" in result["message"]


# ── D3 (containment): legacy garbage = per-meter error, run continues ──────

class TestD3LegacyGarbageContainment:
    def test_garbage_tier_rate_neither_hides_nor_aborts(self, conn, env):
        good = _create_plan(conn, "flat", tiers=[{"rate": "0.10"}])
        good_meter = _create_meter(conn, env, good["id"])
        _add_event(conn, good_meter["id"], "2026-06-10 10:00:00", "500")

        bad = _create_plan(conn, "flat", tiers=[{"rate": "0.20"}])
        bad_meter = _create_meter(conn, env, bad["id"])
        _add_event(conn, bad_meter["id"], "2026-06-11 10:00:00", "100")
        conn.execute("UPDATE rate_tier SET rate = 'abc' "
                     "WHERE rate_plan_id = ?", (bad["id"],))
        conn.commit()

        result = _run_billing(conn, env, "2026-06-01", "2026-06-30")
        assert is_ok(result), result
        assert result["periods_created"] == 1  # healthy meter billed
        assert Decimal(result["total_billed"]) == Decimal("50.00")
        assert result["error_count"] == 1
        entry = result["errors"][0]
        assert entry["meter_id"] == bad_meter["id"]
        assert "not a valid number" in entry["error"]
        # bad meter got no period; healthy one did
        rows = conn.execute(
            "SELECT meter_id FROM billing_period").fetchall()
        assert [r["meter_id"] for r in rows] == [good_meter["id"]]

    def test_garbage_prepaid_credit_row_is_contained(self, conn, env):
        good = _create_plan(conn, "flat", tiers=[{"rate": "0.10"}])
        good_meter = _create_meter(conn, env, good["id"])
        _add_event(conn, good_meter["id"], "2026-06-10 10:00:00", "500")

        prepaid = _create_plan(conn, "prepaid_credit",
                               tiers=[{"rate": "0.10"}])
        prepaid_meter = _create_meter(conn, env, prepaid["id"])
        _add_event(conn, prepaid_meter["id"], "2026-06-11 10:00:00", "300")
        conn.execute(
            """INSERT INTO prepaid_credit_balance
               (id, customer_id, rate_plan_id, original_amount,
                remaining_amount, period_start, period_end,
                overage_amount, status)
               VALUES (?, ?, ?, 'lots', 'lots', '2026-01-01',
                       '2027-12-31', '0', 'active')""",
            (str(uuid.uuid4()), env["customer"], prepaid["id"]))
        conn.commit()

        result = _run_billing(conn, env, "2026-06-01", "2026-06-30")
        assert is_ok(result), result
        assert result["periods_created"] == 1
        assert result["error_count"] == 1
        entry = result["errors"][0]
        assert entry["meter_id"] == prepaid_meter["id"]
        assert "remaining_amount" in entry["error"]
        # no partial writes for the failed meter
        rows = conn.execute(
            "SELECT meter_id FROM billing_period").fetchall()
        assert [r["meter_id"] for r in rows] == [good_meter["id"]]
        credit = conn.execute(
            "SELECT remaining_amount, status FROM prepaid_credit_balance"
        ).fetchone()
        assert credit["remaining_amount"] == "lots"  # untouched
        assert credit["status"] == "active"

    def test_garbage_stored_peak_demand_is_contained(self, conn, env):
        plan = _create_plan(conn, "demand", tiers=DEMAND_TIERS)
        meter = _create_meter(conn, env, plan["id"])
        created = call_action(mod.create_billing_period, conn, ns(
            customer_id=env["customer"], meter_id=meter["id"],
            from_date="2026-06-01", to_date="2026-06-30",
            rate_plan_id=plan["id"]))
        assert is_ok(created), created
        conn.execute(
            "UPDATE billing_period SET peak_demand = 'high' WHERE id = ?",
            (created["billing_period"]["id"],))
        conn.commit()

        result = _run_billing(conn, env, "2026-06-01", "2026-06-30")
        assert is_ok(result), result
        assert result["periods_created"] == 0
        assert result["error_count"] == 1
        assert "peak_demand" in result["errors"][0]["error"]
        # the open period was not touched
        bp = _get_period(conn, created["billing_period"]["id"])
        assert bp["status"] == "open"


# ── D2: preview never claims money moved ───────────────────────────────────

class TestD2TruthfulPreview:
    def test_preview_reports_db_truth_plus_projection(self, conn, env):
        plan = _create_plan(conn, "prepaid_credit", tiers=[{"rate": "0.10"}])
        _create_meter(conn, env, plan["id"])
        added = call_action(mod.add_prepaid_credit, conn, ns(
            customer_id=env["customer"], amount="100.00",
            valid_until="2027-12-31", rate_plan_id=plan["id"]))
        assert is_ok(added), added

        result = call_action(mod.rate_consumption, conn, ns(
            rate_plan_id=plan["id"], consumption="300",
            customer_id=env["customer"]))
        assert is_ok(result), result
        prepaid = result["calculation"]["prepaid"]
        assert prepaid["preview"] is True
        assert prepaid["over_limit"] is False
        # DB truth: nothing was deducted, so nothing claims it was
        assert Decimal(prepaid["deducted"]) == Decimal("0.00")
        assert Decimal(prepaid["remaining_credit"]) == Decimal("100.00")
        # the projection lives in its own, honestly named keys
        assert Decimal(prepaid["would_deduct"]) == Decimal("30.00")
        assert (Decimal(prepaid["projected_remaining_credit"])
                == Decimal("70.00"))
        row = conn.execute(
            "SELECT remaining_amount, status FROM prepaid_credit_balance"
        ).fetchone()
        assert row["remaining_amount"] == "100.00"
        assert row["status"] == "active"

    def test_real_deduction_still_reports_deducted(self, conn, env):
        plan = _create_plan(conn, "prepaid_credit", tiers=[{"rate": "0.10"}])
        meter = _create_meter(conn, env, plan["id"])
        call_action(mod.add_prepaid_credit, conn, ns(
            customer_id=env["customer"], amount="100.00",
            valid_until="2027-12-31", rate_plan_id=plan["id"]))
        _add_event(conn, meter["id"], "2026-06-10 10:00:00", "300")
        run = _run_billing(conn, env, "2026-06-01", "2026-06-30")
        assert is_ok(run), run
        outcome = run["prepaid"][0]
        assert Decimal(outcome["deducted"]) == Decimal("30.00")
        assert Decimal(outcome["remaining_credit"]) == Decimal("70.00")
        assert "would_deduct" not in outcome
        row = conn.execute(
            "SELECT remaining_amount FROM prepaid_credit_balance").fetchone()
        assert row["remaining_amount"] == "70.00"


# ── D4: killers for the three QA-surviving mutants ─────────────────────────

class TestD4MutationKillers:
    def test_demand_peak_is_numeric_max_not_lexical(self, conn, env):
        """M2: peaks 9/10/100 — lexical (string) max picks '9'."""
        plan = _create_plan(conn, "demand", tiers=DEMAND_TIERS)
        meter = _create_meter(conn, env, plan["id"])
        for ts, qty in (("2026-06-05 10:00:00", "9"),
                        ("2026-06-10 18:00:00", "10"),
                        ("2026-06-20 02:00:00", "100")):
            _add_event(conn, meter["id"], ts, qty)
        result = _run_billing(conn, env, "2026-06-01", "2026-06-30")
        assert is_ok(result), result
        assert result["errors"] == []
        bp = _get_period(conn, result["period_ids"][0])
        assert Decimal(bp["peak_demand"]) == Decimal("100")
        assert Decimal(bp["demand_charge"]) == Decimal("1200.00")
        # energy (9+10+100)*0.05 = 5.95; grand = 1205.95
        assert Decimal(bp["grand_total"]) == Decimal("1205.95")

    def test_tou_charge_is_decimal_rounding_not_float(self):
        """M5: 3 x 0.145 = 0.435 -> 0.44 HALF_UP; float rounds to 0.43."""
        tiers = json.loads(json.dumps(TOU_TIERS))
        tiers[0]["rate"] = "0.145"  # peak
        result = mod._calculate_charge(
            "time_of_use", tiers, "3",
            usage_by_period={"peak": "3"})
        assert result["usage_charge"] == "0.44"
        assert Decimal(result["total_charge"]) == Decimal("0.44")

    def test_expired_but_active_credit_never_consumed(self, conn, env):
        """M7: an expired-but-unmarked credit must not fund the charge."""
        plan = _create_plan(conn, "prepaid_credit", tiers=[{"rate": "0.10"}])
        meter = _create_meter(conn, env, plan["id"])
        added = call_action(mod.add_prepaid_credit, conn, ns(
            customer_id=env["customer"], amount="100.00",
            valid_until="2026-01-31", rate_plan_id=plan["id"]))  # expired
        assert is_ok(added), added
        _add_event(conn, meter["id"], "2026-06-10 10:00:00", "300")
        run = _run_billing(conn, env, "2026-06-01", "2026-06-30")
        assert is_ok(run), run
        outcome = run["prepaid"][0]
        assert outcome["over_limit"] is True
        assert Decimal(outcome["available_credit"]) == Decimal("0.00")
        assert Decimal(outcome["credit_shortfall"]) == Decimal("30.00")
        assert Decimal(outcome["deducted"]) == Decimal("0.00")
        row = conn.execute(
            "SELECT remaining_amount, status FROM prepaid_credit_balance"
        ).fetchone()
        assert row["remaining_amount"] == "100.00"  # never burned
        assert row["status"] == "active"
        # the period still rates: the amount is owed, collection is separate
        bp = _get_period(conn, outcome["billing_period_id"])
        assert bp["status"] == "rated"


# ── F1: exact cross-dialect aggregation (decimal_sum, not SQL SUM) ─────────

class TestF1ExactAggregation:
    def test_reading_consumption_sum_is_exact_decimal(self, conn, env):
        """SQLite SUM(TEXT) is float: 0.1+0.2 -> 0.30000000000000004."""
        plan = _create_plan(conn, "flat", tiers=[{"rate": "1"}])
        meter = _create_meter(conn, env, plan["id"])
        for date, value in (("2026-06-01", "100"),
                            ("2026-06-05", "100.1"),   # consumption 0.1
                            ("2026-06-10", "100.3")):  # consumption 0.2
            result = call_action(mod.add_meter_reading, conn, ns(
                meter_id=meter["id"], reading_date=date,
                reading_value=value, reading_type=None, source=None,
                uom=None))
            assert is_ok(result), result
        run = _run_billing(conn, env, "2026-06-01", "2026-06-30")
        assert is_ok(run), run
        bp = _get_period(conn, run["period_ids"][0])
        assert bp["total_consumption"] == "0.3"  # exact, no float artifact
        assert Decimal(run["total_billed"]) == Decimal("0.30")

    def test_event_quantity_sum_is_exact_decimal(self, conn, env):
        plan = _create_plan(conn, "flat", tiers=[{"rate": "1"}])
        meter = _create_meter(conn, env, plan["id"])
        _add_event(conn, meter["id"], "2026-06-10 10:00:00", "0.1")
        _add_event(conn, meter["id"], "2026-06-11 10:00:00", "0.2")
        run = _run_billing(conn, env, "2026-06-01", "2026-06-30")
        assert is_ok(run), run
        bp = _get_period(conn, run["period_ids"][0])
        assert bp["total_consumption"] == "0.3"
        assert Decimal(run["total_billed"]) == Decimal("0.30")


# ── Round 2 — DEFECT-1: non-finite money rejected at every write gate ──────

NON_FINITE = ("NaN", "nan", "sNaN", "Infinity", "-Infinity", "inf", "-inf")

MONEY_COLUMNS = ("total_consumption", "base_charge", "usage_charge",
                 "adjustments_total", "subtotal", "tax_amount",
                 "grand_total")


def _seed_legacy_event(conn, env, meter_id, timestamp, quantity):
    """Simulate a legacy pre-gate row: direct INSERT, bypassing the action."""
    event_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO usage_event
           (id, customer_id, meter_id, event_type, quantity, timestamp,
            processed, created_at)
           VALUES (?, ?, ?, 'usage', ?, ?, 0, ?)""",
        (event_id, env["customer"], meter_id, quantity, timestamp,
         "2026-06-01 00:00:00"))
    conn.commit()
    return event_id


def _seed_legacy_reading(conn, meter_id, reading_date, consumption):
    row_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO meter_reading
           (id, meter_id, reading_date, reading_value, consumption,
            reading_type, source, validated, created_at)
           VALUES (?, ?, ?, '0', ?, 'actual', 'manual', 0,
                   '2026-06-01 00:00:00')""",
        (row_id, meter_id, reading_date, consumption))
    conn.commit()
    return row_id


class TestR2NonFiniteRejectedAtWrite:
    def test_usage_event_rejects_every_non_finite_literal(self, conn, env):
        """The exact QA reproduction: quantity NaN stored as 'NaN', billed ok."""
        plan = _create_plan(conn, "flat", tiers=[{"rate": "0.10"}])
        meter = _create_meter(conn, env, plan["id"])
        for literal in NON_FINITE:
            result = call_action(mod.add_usage_event, conn, ns(
                meter_id=meter["id"], event_date="2026-06-10 10:00:00",
                quantity=literal, event_type="usage", properties=None,
                idempotency_key=None))
            assert is_error(result), (literal, result)
            assert result["message"] != UNEXPECTED
            assert "--quantity" in result["message"]
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM usage_event").fetchone()
        assert row["cnt"] == 0  # nothing stored

    def test_tier_rate_nan_rejected_at_add(self, conn, env):
        result = call_action(mod.add_rate_plan, conn, _plan_args(
            name="NaN Flat", billing_model="flat",
            tiers=json.dumps([{"rate": "NaN"}])))
        assert is_error(result), result
        assert result["message"] != UNEXPECTED
        assert "not a valid number" in result["message"]
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM rate_plan WHERE name = 'NaN Flat'"
        ).fetchone()
        assert row["cnt"] == 0

    def test_infinity_base_charge_rejected_at_add_and_update(self, conn, env):
        result = call_action(mod.add_rate_plan, conn, _plan_args(
            billing_model="flat", tiers=json.dumps([{"rate": "0.10"}]),
            base_charge="Infinity"))
        assert is_error(result), result
        assert result["message"] != UNEXPECTED
        assert "--base-charge" in result["message"]

        plan = _create_plan(conn, "flat", tiers=[{"rate": "0.10"}])
        result = call_action(mod.update_rate_plan, conn, ns(
            rate_plan_id=plan["id"], name=None, base_charge="Infinity",
            effective_to=None, minimum_charge=None, overage_rate=None,
            tiers=None, tier_strategy=None))
        assert is_error(result), result
        assert "--base-charge" in result["message"]

    def test_prepaid_amount_nan_gets_named_error(self, conn, env):
        """QA round 2: NaN <= 0 raised InvalidOperation -> generic message."""
        result = call_action(mod.add_prepaid_credit, conn, ns(
            customer_id=env["customer"], amount="NaN",
            valid_until="2027-12-31", rate_plan_id=None))
        assert is_error(result), result
        assert result["message"] != UNEXPECTED
        assert "--amount" in result["message"]
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM prepaid_credit_balance").fetchone()
        assert row["cnt"] == 0

    def test_adjustment_amount_and_reading_value_reject_non_finite(
            self, conn, env):
        result = call_action(mod.add_billing_adjustment, conn, ns(
            billing_period_id="whatever", amount="Infinity",
            adjustment_type="credit", reason=None, approved_by=None))
        assert is_error(result), result
        assert "--amount" in result["message"]

        plan = _create_plan(conn, "flat", tiers=[{"rate": "0.10"}])
        meter = _create_meter(conn, env, plan["id"])
        result = call_action(mod.add_meter_reading, conn, ns(
            meter_id=meter["id"], reading_date="2026-06-10",
            reading_value="NaN", reading_type=None, source=None, uom=None))
        assert is_error(result), result
        assert "--reading-value" in result["message"]

    def test_batch_reports_non_finite_quantity_per_event(self, conn, env):
        plan = _create_plan(conn, "flat", tiers=[{"rate": "0.10"}])
        meter = _create_meter(conn, env, plan["id"])
        events = [
            {"meter_id": meter["id"], "event_date": "2026-06-10 10:00:00",
             "quantity": "100"},
            {"meter_id": meter["id"], "event_date": "2026-06-10 11:00:00",
             "quantity": "NaN"},
        ]
        result = call_action(mod.add_usage_events_batch, conn, ns(
            events=json.dumps(events)))
        assert is_ok(result), result
        assert result["inserted"] == 1
        assert len(result["errors"]) == 1
        assert result["errors"][0]["index"] == 1
        assert "quantity must be a number" in result["errors"][0]["error"]

    def test_legacy_nan_last_reading_value_cannot_mint_nan_consumption(
            self, conn, env):
        """FE2: NaN - x = NaN and diff<0 is False -> 'NaN' consumption row."""
        plan = _create_plan(conn, "flat", tiers=[{"rate": "0.10"}])
        meter = _create_meter(conn, env, plan["id"])
        conn.execute(
            "UPDATE meter SET last_reading_value = 'NaN' WHERE id = ?",
            (meter["id"],))
        conn.commit()
        result = call_action(mod.add_meter_reading, conn, ns(
            meter_id=meter["id"], reading_date="2026-06-10",
            reading_value="150", reading_type=None, source=None, uom=None))
        assert is_error(result), result
        assert result["message"] != UNEXPECTED
        assert "last_reading_value" in result["message"]
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM meter_reading").fetchone()
        assert row["cnt"] == 0  # no reading row written


# ── Round 2 — DEFECT-1 read side + DEFECT-2: containment, never NaN bills ──

class TestR2LegacyGarbageContainedPerMeter:
    def _healthy_meter(self, conn, env):
        good = _create_plan(conn, "flat", tiers=[{"rate": "0.10"}])
        good_meter = _create_meter(conn, env, good["id"])
        _add_event(conn, good_meter["id"], "2026-06-10 10:00:00", "500")
        return good_meter

    def _assert_contained(self, conn, result, good_meter, bad_meter,
                          expect_in_error):
        """Healthy meter bills exactly 50.00; bad meter is one loud error."""
        assert is_ok(result), result
        assert result["periods_created"] == 1
        assert Decimal(result["total_billed"]) == Decimal("50.00")
        assert result["error_count"] == 1
        entry = result["errors"][0]
        assert entry["meter_id"] == bad_meter["id"]
        assert expect_in_error in entry["error"]
        rows = conn.execute(
            "SELECT * FROM billing_period").fetchall()
        assert [r["meter_id"] for r in rows] == [good_meter["id"]]
        # No stored money column anywhere is non-finite or unparseable
        for row in rows:
            for col in MONEY_COLUMNS:
                value = Decimal(str(row[col]))
                assert value.is_finite(), (col, row[col])
        assert Decimal(rows[0]["grand_total"]) == Decimal("50.00")

    def test_legacy_nan_quantity_errors_loud_healthy_meter_bills(
            self, conn, env):
        """The QA DEFECT-1 reproduction: 'NaN' billed as total 'NaN', ok."""
        good_meter = self._healthy_meter(conn, env)
        bad = _create_plan(conn, "flat", tiers=[{"rate": "0.10"}])
        bad_meter = _create_meter(conn, env, bad["id"])
        event_id = _seed_legacy_event(
            conn, env, bad_meter["id"], "2026-06-10 10:00:00", "NaN")
        result = _run_billing(conn, env, "2026-06-01", "2026-06-30")
        self._assert_contained(conn, result, good_meter, bad_meter,
                               f"usage event {event_id} quantity")
        assert "not a valid number" in result["errors"][0]["error"]

    def test_legacy_unparseable_quantity_no_longer_aborts_the_run(
            self, conn, env):
        """The QA DEFECT-2 reproduction: aggregate step raised -> run abort."""
        good_meter = self._healthy_meter(conn, env)
        bad = _create_plan(conn, "flat", tiers=[{"rate": "0.10"}])
        bad_meter = _create_meter(conn, env, bad["id"])
        event_id = _seed_legacy_event(
            conn, env, bad_meter["id"], "2026-06-10 10:00:00", "abc")
        result = _run_billing(conn, env, "2026-06-01", "2026-06-30")
        self._assert_contained(conn, result, good_meter, bad_meter,
                               f"usage event {event_id} quantity")

    def test_legacy_garbage_reading_consumption_is_contained(self, conn, env):
        good_meter = self._healthy_meter(conn, env)
        bad = _create_plan(conn, "flat", tiers=[{"rate": "0.10"}])
        bad_meter = _create_meter(conn, env, bad["id"])
        row_id = _seed_legacy_reading(
            conn, bad_meter["id"], "2026-06-10", "garbage")
        result = _run_billing(conn, env, "2026-06-01", "2026-06-30")
        self._assert_contained(conn, result, good_meter, bad_meter,
                               f"meter reading {row_id} consumption")

    def test_legacy_nan_reading_consumption_is_contained(self, conn, env):
        good_meter = self._healthy_meter(conn, env)
        bad = _create_plan(conn, "flat", tiers=[{"rate": "0.10"}])
        bad_meter = _create_meter(conn, env, bad["id"])
        row_id = _seed_legacy_reading(
            conn, bad_meter["id"], "2026-06-10", "NaN")
        result = _run_billing(conn, env, "2026-06-01", "2026-06-30")
        self._assert_contained(conn, result, good_meter, bad_meter,
                               f"meter reading {row_id} consumption")

    def test_legacy_nan_tier_rate_is_contained(self, conn, env):
        good_meter = self._healthy_meter(conn, env)
        bad = _create_plan(conn, "flat", tiers=[{"rate": "0.20"}])
        bad_meter = _create_meter(conn, env, bad["id"])
        _add_event(conn, bad_meter["id"], "2026-06-11 10:00:00", "100")
        conn.execute("UPDATE rate_tier SET rate = 'NaN' "
                     "WHERE rate_plan_id = ?", (bad["id"],))
        conn.commit()
        result = _run_billing(conn, env, "2026-06-01", "2026-06-30")
        self._assert_contained(conn, result, good_meter, bad_meter,
                               "not a valid number")

    def test_legacy_infinity_base_charge_is_contained(self, conn, env):
        """QA: stored Infinity base_charge crashed run-billing generically."""
        good_meter = self._healthy_meter(conn, env)
        bad = _create_plan(conn, "flat", tiers=[{"rate": "0.10"}])
        bad_meter = _create_meter(conn, env, bad["id"])
        _add_event(conn, bad_meter["id"], "2026-06-11 10:00:00", "100")
        conn.execute(
            "UPDATE rate_plan SET base_charge = 'Infinity' WHERE id = ?",
            (bad["id"],))
        conn.commit()
        result = _run_billing(conn, env, "2026-06-01", "2026-06-30")
        self._assert_contained(conn, result, good_meter, bad_meter,
                               "base_charge")

    def test_prepaid_meter_still_deducts_while_garbage_meter_errors(
            self, conn, env):
        """Isolation upgrade: healthy prepaid billing proceeds in-run."""
        prepaid = _create_plan(conn, "prepaid_credit",
                               tiers=[{"rate": "0.10"}])
        prepaid_meter = _create_meter(conn, env, prepaid["id"])
        call_action(mod.add_prepaid_credit, conn, ns(
            customer_id=env["customer"], amount="100.00",
            valid_until="2027-12-31", rate_plan_id=prepaid["id"]))
        _add_event(conn, prepaid_meter["id"], "2026-06-10 10:00:00", "300")

        bad = _create_plan(conn, "flat", tiers=[{"rate": "0.10"}])
        bad_meter = _create_meter(conn, env, bad["id"])
        _seed_legacy_event(
            conn, env, bad_meter["id"], "2026-06-11 10:00:00", "abc")

        result = _run_billing(conn, env, "2026-06-01", "2026-06-30")
        assert is_ok(result), result
        assert result["periods_created"] == 1
        assert result["error_count"] == 1
        assert result["errors"][0]["meter_id"] == bad_meter["id"]
        outcome = result["prepaid"][0]
        assert Decimal(outcome["deducted"]) == Decimal("30.00")
        assert Decimal(outcome["remaining_credit"]) == Decimal("70.00")
        row = conn.execute(
            "SELECT remaining_amount FROM prepaid_credit_balance").fetchone()
        assert row["remaining_amount"] == "70.00"

    def test_rate_consumption_never_reports_non_finite_money(self, conn, env):
        """QA: rate-consumption returned usage_charge 'NaN' as status ok."""
        plan = _create_plan(conn, "flat", tiers=[{"rate": "0.10"}])
        conn.execute("UPDATE rate_tier SET rate = 'NaN' "
                     "WHERE rate_plan_id = ?", (plan["id"],))
        conn.commit()
        result = call_action(mod.rate_consumption, conn, ns(
            rate_plan_id=plan["id"], consumption="500",
            customer_id=None))
        assert is_error(result), result
        assert result["message"] != UNEXPECTED
        assert "not a valid number" in result["message"]

        good = _create_plan(conn, "flat", tiers=[{"rate": "0.10"}])
        result = call_action(mod.rate_consumption, conn, ns(
            rate_plan_id=good["id"], consumption="NaN",
            customer_id=None))
        assert is_error(result), result
        assert result["message"] != UNEXPECTED
        assert "not a valid number" in result["message"]


# ═══════════════════════════════════════════════════════════════════════════
# Round 3 (2026-07-26, directed final round) — finite-but-unrepresentable
# magnitudes. Root cause: a PLAIN 27+-integer-digit value passes is_finite()
# but round_currency's quantize(0.01) raises decimal.InvalidOperation (an
# ArithmeticError, not a RatingError), escaping the per-meter guard and
# hitting main()'s generic handler. Gate invariant pinned here: ANY value
# that passes a write gate survives round_currency without raising.
# ═══════════════════════════════════════════════════════════════════════════

BIG = "1000000000000000000000000000"  # QA round-3 verbatim magnitude


class TestR3UnrepresentableRejectedAtWrite:
    def test_oversized_usage_event_rejected_at_write(self, conn, env):
        """QA HIGH-1 write leg: the oversized quantity must not enter."""
        plan = _create_plan(conn, "flat", tiers=[{"rate": "0.10"}])
        meter = _create_meter(conn, env, plan["id"])
        result = call_action(mod.add_usage_event, conn, ns(
            meter_id=meter["id"], event_date="2026-02-05", quantity=BIG,
            event_type="consumption", properties=None, idempotency_key=None))
        assert is_error(result), result
        assert result["message"] != UNEXPECTED
        assert "--quantity must be a representable amount" in result["message"]
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM usage_event").fetchone()
        assert row["cnt"] == 0  # nothing stored

    def test_batch_oversized_rejected_per_entry_then_clean_run(
            self, conn, env):
        """QA HIGH-2 verbatim array: NaN/Infinity AND the oversized row are
        all per-entry errors; only the healthy row inserts; the subsequent
        run-billing over that feed completes ok."""
        plan = _create_plan(conn, "flat", tiers=[{"rate": "0.10"}])
        m1 = _create_meter(conn, env, plan["id"])
        m2 = _create_meter(conn, env, plan["id"])
        events = [
            {"meter_id": m1["id"], "event_date": "2026-06-10 10:00:00",
             "quantity": "10"},
            {"meter_id": m2["id"], "event_date": "2026-06-10 10:00:00",
             "quantity": "NaN"},
            {"meter_id": m2["id"], "event_date": "2026-06-10 10:00:00",
             "quantity": "Infinity"},
            {"meter_id": m2["id"], "event_date": "2026-06-10 10:00:00",
             "quantity": BIG},
        ]
        result = call_action(mod.add_usage_events_batch, conn, ns(
            events=json.dumps(events)))
        assert is_ok(result), result
        assert result["inserted"] == 1
        assert [e["index"] for e in result["errors"]] == [1, 2, 3]
        assert "must be a number, got: 'NaN'" in result["errors"][0]["error"]
        assert ("must be a number, got: 'Infinity'"
                in result["errors"][1]["error"])
        assert ("must be a representable amount"
                in result["errors"][2]["error"])
        rows = conn.execute(
            "SELECT quantity FROM usage_event").fetchall()
        assert [r["quantity"] for r in rows] == ["10"]
        # The realistic vector's follow-up: run-billing over this feed
        run = _run_billing(conn, env, "2026-06-01", "2026-06-30")
        assert is_ok(run), run
        assert run["error_count"] == 0
        assert run["periods_created"] == 2
        assert Decimal(run["total_billed"]) == Decimal("1.00")

    def test_oversized_tier_rate_rejected_at_add(self, conn, env):
        """QA HIGH-3 write leg: named error at add-rate-plan write time."""
        result = call_action(mod.add_rate_plan, conn, _plan_args(
            name="Huge Rate", billing_model="flat",
            tiers=json.dumps([{"tier_start": "0", "rate": BIG}])))
        assert is_error(result), result
        assert result["message"] != UNEXPECTED
        assert "not a representable amount" in result["message"]
        assert "rate" in result["message"]
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM rate_plan "
            "WHERE name = 'Huge Rate'").fetchone()
        assert row["cnt"] == 0

    def test_oversized_tier_rate_rejected_at_update(self, conn, env):
        plan = _create_plan(conn, "flat", tiers=[{"rate": "0.10"}])
        result = call_action(mod.update_rate_plan, conn, ns(
            rate_plan_id=plan["id"], name=None, base_charge=None,
            effective_to=None, minimum_charge=None, overage_rate=None,
            tiers=json.dumps([{"rate": BIG}]), tier_strategy=None))
        assert is_error(result), result
        assert "not a representable amount" in result["message"]
        row = conn.execute(
            "SELECT rate FROM rate_tier WHERE rate_plan_id = ?",
            (plan["id"],)).fetchone()
        assert row["rate"] == "0.10"  # old tier untouched

    def test_oversized_base_charge_rejected(self, conn, env):
        result = call_action(mod.add_rate_plan, conn, _plan_args(
            billing_model="flat", tiers=json.dumps([{"rate": "0.10"}]),
            base_charge=BIG))
        assert is_error(result), result
        assert "--base-charge" in result["message"]
        assert "not a representable amount" in result["message"]

    def test_oversized_reading_value_rejected(self, conn, env):
        plan = _create_plan(conn, "flat", tiers=[{"rate": "0.10"}])
        meter = _create_meter(conn, env, plan["id"])
        result = call_action(mod.add_meter_reading, conn, ns(
            meter_id=meter["id"], reading_date="2026-06-10",
            reading_value=BIG, reading_type=None, source=None, uom=None))
        assert is_error(result), result
        assert ("--reading-value must be a representable amount"
                in result["message"])
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM meter_reading").fetchone()
        assert row["cnt"] == 0

    def test_oversized_prepaid_amount_rejected_at_write(self, conn, env):
        """QA HIGH-4 write leg: the poison can no longer enter."""
        plan = _create_plan(conn, "prepaid_credit", tiers=[{"rate": "0.10"}])
        result = call_action(mod.add_prepaid_credit, conn, ns(
            customer_id=env["customer"], amount=BIG,
            valid_until="2026-12-31", rate_plan_id=plan["id"]))
        assert is_error(result), result
        assert result["message"] != UNEXPECTED
        assert "--amount must be a representable amount" in result["message"]
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM prepaid_credit_balance").fetchone()
        assert row["cnt"] == 0

    def test_oversized_adjustment_amount_named_error_nothing_written(
            self, conn, env):
        """QA MEDIUM-2 verbatim: named message contract; no write."""
        plan = _create_plan(conn, "flat", tiers=[{"rate": "0.10"}])
        meter = _create_meter(conn, env, plan["id"])
        _add_event(conn, meter["id"], "2026-06-10 10:00:00", "500")
        run = _run_billing(conn, env, "2026-06-01", "2026-06-30")
        assert is_ok(run), run
        period_id = run["period_ids"][0]
        result = call_action(mod.add_billing_adjustment, conn, ns(
            billing_period_id=period_id, amount=BIG,
            adjustment_type="credit", reason="test", approved_by=None))
        assert is_error(result), result
        assert result["message"] != UNEXPECTED
        assert "--amount must be a representable amount" in result["message"]
        rows = conn.execute(
            "SELECT COUNT(*) AS cnt FROM billing_adjustment").fetchone()
        assert rows["cnt"] == 0
        bp = conn.execute(
            "SELECT grand_total FROM billing_period WHERE id = ?",
            (period_id,)).fetchone()
        assert bp["grand_total"] == "50.00"  # untouched

    def test_gate_invariant_pass_implies_round_currency_survives(self):
        """The ruling's gate invariant: ANY value that passes a write gate
        must survive round_currency without raising."""
        for value in ("9" * 26, "-" + "9" * 26, "0.005", "123.456", "1",
                      "0.12345678901234567890123456789012"):
            d = mod._finite_decimal(value)  # gate passes...
            rounded = mod.round_currency(d)  # ...must not raise
            assert rounded.is_finite()
        for value in (BIG, "1" + "0" * 26, "9" * 27):
            with pytest.raises(ValueError):
                mod._finite_decimal(value)


class TestR3LegacyOversizedContained:
    """Defense-in-depth over legacy stored poison (seeded via raw SQL —
    the write gates now block every CLI path)."""

    def _healthy_meter(self, conn, env):
        plan = _create_plan(conn, "flat", tiers=[{"rate": "0.10"}])
        good_meter = _create_meter(conn, env, plan["id"])
        _add_event(conn, good_meter["id"], "2026-06-10 10:00:00", "500")
        return good_meter

    def _assert_contained(self, conn, result, good_meter, bad_meter,
                          expect_in_error):
        assert is_ok(result), result
        assert result["periods_created"] == 1
        assert Decimal(result["total_billed"]) == Decimal("50.00")
        assert result["error_count"] == 1
        entry = result["errors"][0]
        assert entry["meter_id"] == bad_meter["id"]
        assert expect_in_error in entry["error"]
        rows = conn.execute("SELECT * FROM billing_period").fetchall()
        assert [r["meter_id"] for r in rows] == [good_meter["id"]]
        assert Decimal(rows[0]["grand_total"]) == Decimal("50.00")

    def test_legacy_oversized_event_contained_healthy_meter_bills(
            self, conn, env):
        """QA HIGH-1 verbatim: the poison row no longer aborts the run —
        healthy meter bills 50.00, error_count 1 naming the row."""
        good_meter = self._healthy_meter(conn, env)
        bad = _create_plan(conn, "flat", tiers=[{"rate": "0.10"}])
        bad_meter = _create_meter(conn, env, bad["id"])
        event_id = _seed_legacy_event(
            conn, env, bad_meter["id"], "2026-06-10 10:00:00", BIG)
        result = _run_billing(conn, env, "2026-06-01", "2026-06-30")
        self._assert_contained(conn, result, good_meter, bad_meter,
                               f"usage event {event_id} quantity")
        assert ("not a representable amount"
                in result["errors"][0]["error"])

    def test_legacy_oversized_reading_contained(self, conn, env):
        good_meter = self._healthy_meter(conn, env)
        bad = _create_plan(conn, "flat", tiers=[{"rate": "0.10"}])
        bad_meter = _create_meter(conn, env, bad["id"])
        row_id = _seed_legacy_reading(
            conn, bad_meter["id"], "2026-06-10", BIG)
        result = _run_billing(conn, env, "2026-06-01", "2026-06-30")
        self._assert_contained(conn, result, good_meter, bad_meter,
                               f"meter reading {row_id} consumption")

    def test_legacy_oversized_tier_rate_contained_and_named(self, conn, env):
        """QA HIGH-3: an ORDINARY 500-unit meter on the poisoned plan is a
        contained per-meter error; rate-consumption on it is a named error;
        the healthy meter still bills in the same run."""
        good_meter = self._healthy_meter(conn, env)
        bad = _create_plan(conn, "flat", tiers=[{"rate": "0.20"}])
        bad_meter = _create_meter(conn, env, bad["id"])
        _add_event(conn, bad_meter["id"], "2026-06-11 10:00:00", "500")
        conn.execute("UPDATE rate_tier SET rate = ? WHERE rate_plan_id = ?",
                     (BIG, bad["id"]))
        conn.commit()
        result = _run_billing(conn, env, "2026-06-01", "2026-06-30")
        self._assert_contained(conn, result, good_meter, bad_meter,
                               "not a representable amount")
        rc = call_action(mod.rate_consumption, conn, ns(
            rate_plan_id=bad["id"], consumption="500", customer_id=None))
        assert is_error(rc), rc
        assert rc["message"] != UNEXPECTED
        assert "not a representable amount" in rc["message"]

    def test_legacy_oversized_prepaid_row_named_error_read_path(
            self, conn, env):
        """QA HIGH-4 read leg: get-prepaid-balance and rate-consumption
        over the poisoned row return a NAMED error identifying the row —
        never the generic message. (Recovery is manual row repair; no
        delete-prepaid-credit action, per scope ruling.)"""
        plan = _create_plan(conn, "prepaid_credit", tiers=[{"rate": "0.10"}])
        good = call_action(mod.add_prepaid_credit, conn, ns(
            customer_id=env["customer"], amount="100.00",
            valid_until="2026-12-31", rate_plan_id=plan["id"]))
        assert is_ok(good), good
        poison_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO prepaid_credit_balance
               (id, customer_id, rate_plan_id, original_amount,
                remaining_amount, period_start, period_end, overage_amount,
                status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, '2026-01-01', '2026-12-31', '0',
                       'active', '2026-01-01', '2026-01-01')""",
            (poison_id, env["customer"], plan["id"], BIG, BIG))
        conn.commit()

        bal = call_action(mod.get_prepaid_balance, conn, ns(
            customer_id=env["customer"]))
        assert is_error(bal), bal
        assert bal["message"] != UNEXPECTED
        assert poison_id in bal["message"]

        rc = call_action(mod.rate_consumption, conn, ns(
            rate_plan_id=plan["id"], consumption="300",
            customer_id=env["customer"]))
        assert is_error(rc), rc
        assert rc["message"] != UNEXPECTED
        assert poison_id in rc["message"]
        assert "not a representable amount" in rc["message"]

    def test_rate_consumption_oversized_consumption_named_error(
            self, conn, env):
        """QA MEDIUM-1 verbatim: named error, never the generic message."""
        plan = _create_plan(conn, "flat", tiers=[{"rate": "0.10"}])
        result = call_action(mod.rate_consumption, conn, ns(
            rate_plan_id=plan["id"], consumption=BIG, customer_id=None))
        assert is_error(result), result
        assert result["message"] != UNEXPECTED
        assert "consumption" in result["message"]
        assert "not a representable amount" in result["message"]
