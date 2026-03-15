"""Tests for erpclaw-billing rate plans, billing periods, and billing actions.

Actions tested: add-rate-plan, update-rate-plan, get-rate-plan, list-rate-plans,
                rate-consumption, create-billing-period, list-billing-periods,
                get-billing-period, add-billing-adjustment,
                add-prepaid-credit, get-prepaid-balance, status
"""
import json
import pytest
from decimal import Decimal
from billing_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
)

mod = load_db_query()


def _create_flat_plan(conn, rate="0.10", base_charge="25.00"):
    """Create a flat rate plan and return it."""
    tiers = json.dumps([{"rate": rate}])
    return call_action(mod.add_rate_plan, conn, ns(
        name="Flat Plan", billing_model="flat",
        service_type="electricity", base_charge=base_charge,
        base_charge_period=None, effective_from=None,
        effective_to=None, minimum_charge=None,
        minimum_commitment=None, overage_rate=None, tiers=tiers,
    ))


def _create_tiered_plan(conn):
    """Create a tiered rate plan."""
    tiers = json.dumps([
        {"tier_start": "0", "tier_end": "100", "rate": "0.05"},
        {"tier_start": "100", "tier_end": "500", "rate": "0.10"},
        {"tier_start": "500", "rate": "0.15"},
    ])
    return call_action(mod.add_rate_plan, conn, ns(
        name="Tiered Plan", billing_model="tiered",
        service_type="electricity", base_charge="10.00",
        base_charge_period=None, effective_from=None,
        effective_to=None, minimum_charge=None,
        minimum_commitment=None, overage_rate=None, tiers=tiers,
    ))


def _create_meter_with_plan(conn, env, plan_id):
    """Create a meter assigned to a rate plan."""
    return call_action(mod.add_meter, conn, ns(
        customer_id=env["customer"], meter_type="electricity",
        name="Billed Meter", address=None,
        rate_plan_id=plan_id, install_date=None, unit="kWh",
    ))


# ──────────────────────────────────────────────────────────────────────────────
# Rate Plans
# ──────────────────────────────────────────────────────────────────────────────

class TestAddRatePlan:
    def test_flat_plan(self, conn, env):
        result = _create_flat_plan(conn)
        assert is_ok(result)
        assert "rate_plan" in result
        assert result["rate_plan"]["plan_type"] == "flat"

    def test_tiered_plan(self, conn, env):
        result = _create_tiered_plan(conn)
        assert is_ok(result)
        assert len(result["rate_plan"]["tiers"]) == 3

    def test_missing_name_fails(self, conn, env):
        result = call_action(mod.add_rate_plan, conn, ns(
            name=None, billing_model="flat",
            service_type=None, base_charge=None,
            base_charge_period=None, effective_from=None,
            effective_to=None, minimum_charge=None,
            minimum_commitment=None, overage_rate=None, tiers=None,
        ))
        assert is_error(result)

    def test_missing_model_fails(self, conn, env):
        result = call_action(mod.add_rate_plan, conn, ns(
            name="No Model", billing_model=None,
            service_type=None, base_charge=None,
            base_charge_period=None, effective_from=None,
            effective_to=None, minimum_charge=None,
            minimum_commitment=None, overage_rate=None, tiers=None,
        ))
        assert is_error(result)


class TestUpdateRatePlan:
    def test_update_name(self, conn, env):
        plan = _create_flat_plan(conn)
        result = call_action(mod.update_rate_plan, conn, ns(
            rate_plan_id=plan["rate_plan"]["id"],
            name="Updated Flat Plan", base_charge=None,
            effective_to=None, minimum_charge=None,
            overage_rate=None, tiers=None,
        ))
        assert is_ok(result)
        assert result["rate_plan"]["name"] == "Updated Flat Plan"

    def test_no_fields_fails(self, conn, env):
        plan = _create_flat_plan(conn)
        result = call_action(mod.update_rate_plan, conn, ns(
            rate_plan_id=plan["rate_plan"]["id"],
            name=None, base_charge=None,
            effective_to=None, minimum_charge=None,
            overage_rate=None, tiers=None,
        ))
        assert is_error(result)


class TestGetRatePlan:
    def test_get(self, conn, env):
        plan = _create_flat_plan(conn)
        result = call_action(mod.get_rate_plan, conn, ns(
            rate_plan_id=plan["rate_plan"]["id"],
        ))
        assert is_ok(result)
        assert "tiers" in result["rate_plan"]

    def test_get_nonexistent_fails(self, conn, env):
        result = call_action(mod.get_rate_plan, conn, ns(
            rate_plan_id="fake-id",
        ))
        assert is_error(result)


class TestListRatePlans:
    def test_list(self, conn, env):
        _create_flat_plan(conn)
        result = call_action(mod.list_rate_plans, conn, ns(
            service_type=None, limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1


# ──────────────────────────────────────────────────────────────────────────────
# Rate Consumption (calculation engine)
# ──────────────────────────────────────────────────────────────────────────────

class TestRateConsumption:
    def test_flat_rate(self, conn, env):
        plan = _create_flat_plan(conn, rate="0.10", base_charge="25.00")
        result = call_action(mod.rate_consumption, conn, ns(
            rate_plan_id=plan["rate_plan"]["id"],
            consumption="500",
        ))
        assert is_ok(result)
        calc = result["calculation"]
        assert Decimal(calc["usage_charge"]) == Decimal("50.00")
        assert Decimal(calc["base_charge"]) == Decimal("25.00")
        assert Decimal(calc["total_charge"]) == Decimal("75.00")

    def test_tiered_rate(self, conn, env):
        plan = _create_tiered_plan(conn)
        result = call_action(mod.rate_consumption, conn, ns(
            rate_plan_id=plan["rate_plan"]["id"],
            consumption="600",
        ))
        assert is_ok(result)
        calc = result["calculation"]
        # 100*0.05=5 + 400*0.10=40 + 100*0.15=15 = 60
        assert Decimal(calc["usage_charge"]) == Decimal("60.00")
        # base_charge=10 + usage=60 = 70
        assert Decimal(calc["total_charge"]) == Decimal("70.00")

    def test_missing_plan_fails(self, conn, env):
        result = call_action(mod.rate_consumption, conn, ns(
            rate_plan_id=None, consumption="100",
        ))
        assert is_error(result)


# ──────────────────────────────────────────────────────────────────────────────
# Billing Periods
# ──────────────────────────────────────────────────────────────────────────────

class TestCreateBillingPeriod:
    def test_basic_create(self, conn, env):
        plan = _create_flat_plan(conn)
        meter = _create_meter_with_plan(conn, env, plan["rate_plan"]["id"])
        result = call_action(mod.create_billing_period, conn, ns(
            customer_id=env["customer"], meter_id=meter["meter"]["id"],
            from_date="2026-06-01", to_date="2026-06-30",
            rate_plan_id=None,
        ))
        assert is_ok(result)
        assert "billing_period" in result
        assert result["billing_period"]["status"] == "open"

    def test_missing_customer_fails(self, conn, env):
        result = call_action(mod.create_billing_period, conn, ns(
            customer_id=None, meter_id="fake",
            from_date="2026-06-01", to_date="2026-06-30",
            rate_plan_id=None,
        ))
        assert is_error(result)

    def test_overlapping_period_fails(self, conn, env):
        plan = _create_flat_plan(conn)
        meter = _create_meter_with_plan(conn, env, plan["rate_plan"]["id"])
        call_action(mod.create_billing_period, conn, ns(
            customer_id=env["customer"], meter_id=meter["meter"]["id"],
            from_date="2026-06-01", to_date="2026-06-30",
            rate_plan_id=None,
        ))
        result = call_action(mod.create_billing_period, conn, ns(
            customer_id=env["customer"], meter_id=meter["meter"]["id"],
            from_date="2026-06-15", to_date="2026-07-15",
            rate_plan_id=None,
        ))
        assert is_error(result)


class TestListBillingPeriods:
    def test_list(self, conn, env):
        plan = _create_flat_plan(conn)
        meter = _create_meter_with_plan(conn, env, plan["rate_plan"]["id"])
        call_action(mod.create_billing_period, conn, ns(
            customer_id=env["customer"], meter_id=meter["meter"]["id"],
            from_date="2026-06-01", to_date="2026-06-30",
            rate_plan_id=None,
        ))
        result = call_action(mod.list_billing_periods, conn, ns(
            customer_id=env["customer"], meter_id=None,
            status=None, from_date=None, to_date=None,
            limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1


class TestGetBillingPeriod:
    def test_get(self, conn, env):
        plan = _create_flat_plan(conn)
        meter = _create_meter_with_plan(conn, env, plan["rate_plan"]["id"])
        bp = call_action(mod.create_billing_period, conn, ns(
            customer_id=env["customer"], meter_id=meter["meter"]["id"],
            from_date="2026-06-01", to_date="2026-06-30",
            rate_plan_id=None,
        ))
        result = call_action(mod.get_billing_period, conn, ns(
            billing_period_id=bp["billing_period"]["id"],
        ))
        assert is_ok(result)


# ──────────────────────────────────────────────────────────────────────────────
# Billing Adjustments
# ──────────────────────────────────────────────────────────────────────────────

class TestAddBillingAdjustment:
    def test_add_credit(self, conn, env):
        plan = _create_flat_plan(conn)
        meter = _create_meter_with_plan(conn, env, plan["rate_plan"]["id"])
        bp = call_action(mod.create_billing_period, conn, ns(
            customer_id=env["customer"], meter_id=meter["meter"]["id"],
            from_date="2026-06-01", to_date="2026-06-30",
            rate_plan_id=None,
        ))
        result = call_action(mod.add_billing_adjustment, conn, ns(
            billing_period_id=bp["billing_period"]["id"],
            amount="50.00", adjustment_type="credit",
            reason="Customer goodwill", approved_by=None,
        ))
        assert is_ok(result)

    def test_invalid_type_fails(self, conn, env):
        plan = _create_flat_plan(conn)
        meter = _create_meter_with_plan(conn, env, plan["rate_plan"]["id"])
        bp = call_action(mod.create_billing_period, conn, ns(
            customer_id=env["customer"], meter_id=meter["meter"]["id"],
            from_date="2026-06-01", to_date="2026-06-30",
            rate_plan_id=None,
        ))
        result = call_action(mod.add_billing_adjustment, conn, ns(
            billing_period_id=bp["billing_period"]["id"],
            amount="10.00", adjustment_type="invalid",
            reason=None, approved_by=None,
        ))
        assert is_error(result)


# ──────────────────────────────────────────────────────────────────────────────
# Prepaid Credits
# ──────────────────────────────────────────────────────────────────────────────

class TestAddPrepaidCredit:
    def test_add_credit(self, conn, env):
        plan = _create_flat_plan(conn)
        result = call_action(mod.add_prepaid_credit, conn, ns(
            customer_id=env["customer"],
            amount="500.00", reason="Initial deposit",
            valid_until="2027-12-31",
            rate_plan_id=plan["rate_plan"]["id"],
        ))
        assert is_ok(result)

    def test_missing_customer_fails(self, conn, env):
        result = call_action(mod.add_prepaid_credit, conn, ns(
            customer_id=None,
            amount="100.00", reason=None,
            valid_until=None,
            rate_plan_id=None,
        ))
        assert is_error(result)


class TestGetPrepaidBalance:
    def test_get_balance(self, conn, env):
        plan = _create_flat_plan(conn)
        call_action(mod.add_prepaid_credit, conn, ns(
            customer_id=env["customer"],
            amount="200.00", reason=None,
            valid_until="2027-12-31",
            rate_plan_id=plan["rate_plan"]["id"],
        ))
        result = call_action(mod.get_prepaid_balance, conn, ns(
            customer_id=env["customer"],
        ))
        assert is_ok(result)
        assert Decimal(result["total_remaining"]) >= Decimal("200.00")


# ──────────────────────────────────────────────────────────────────────────────
# Status
# ──────────────────────────────────────────────────────────────────────────────

class TestStatus:
    @pytest.mark.xfail(reason="BUG-007: usage_event table missing from init_schema.py")
    def test_status(self, conn, env):
        result = call_action(mod.status_action, conn, ns(
            company_id=env["company_id"],
        ))
        assert is_ok(result)
