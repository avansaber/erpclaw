"""Tests for rate plan actions and rate consumption calculation."""
import json
import sys
import os

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

TESTS_DIR = os.path.dirname(__file__)
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

from db_query import ACTIONS
from helpers import _call_action, create_test_rate_plan


def test_add_rate_plan_flat(fresh_db):
    """Create a flat rate plan with one tier."""
    tiers = json.dumps([{"rate": "0.15"}])
    result = _call_action(ACTIONS["add-rate-plan"], fresh_db,
                          name="Flat Electric", billing_model="flat",
                          tiers=tiers, effective_from="2026-01-01")
    assert result["status"] == "ok"
    plan = result["rate_plan"]
    assert plan["name"] == "Flat Electric"
    assert plan["plan_type"] == "flat"
    assert len(plan["tiers"]) == 1
    assert plan["tiers"][0]["rate"] == "0.15"


def test_add_rate_plan_tiered(fresh_db):
    """Create a tiered rate plan with 3 tiers."""
    tiers = json.dumps([
        {"tier_start": "0", "tier_end": "100", "rate": "0.10"},
        {"tier_start": "100", "tier_end": "500", "rate": "0.08"},
        {"tier_start": "500", "rate": "0.06"},
    ])
    result = _call_action(ACTIONS["add-rate-plan"], fresh_db,
                          name="Tiered Electric", billing_model="tiered",
                          tiers=tiers, effective_from="2026-01-01")
    assert result["status"] == "ok"
    assert len(result["rate_plan"]["tiers"]) == 3


def test_update_rate_plan_tiers(fresh_db):
    """Updating tiers replaces old ones."""
    plan_id = create_test_rate_plan(fresh_db, name="Original Plan")
    new_tiers = json.dumps([
        {"tier_start": "0", "tier_end": "200", "rate": "0.12"},
        {"tier_start": "200", "rate": "0.09"},
    ])
    result = _call_action(ACTIONS["update-rate-plan"], fresh_db,
                          rate_plan_id=plan_id, tiers=new_tiers)
    assert result["status"] == "ok"
    assert len(result["rate_plan"]["tiers"]) == 2
    assert result["rate_plan"]["tiers"][0]["rate"] == "0.12"


def test_get_rate_plan_with_tiers(fresh_db):
    """Get should join tiers."""
    plan_id = create_test_rate_plan(fresh_db)
    result = _call_action(ACTIONS["get-rate-plan"], fresh_db,
                          rate_plan_id=plan_id)
    assert result["status"] == "ok"
    assert "tiers" in result["rate_plan"]
    assert len(result["rate_plan"]["tiers"]) == 3


def test_list_rate_plans(fresh_db):
    """List rate plans returns all created plans."""
    create_test_rate_plan(fresh_db, name="Plan A")
    create_test_rate_plan(fresh_db, name="Plan B")
    result = _call_action(ACTIONS["list-rate-plans"], fresh_db)
    assert result["status"] == "ok"
    assert result["total_count"] == 2


def test_rate_consumption_flat(fresh_db):
    """Flat: 100 kWh at $0.15 = $15.00."""
    tiers = json.dumps([{"rate": "0.15"}])
    r = _call_action(ACTIONS["add-rate-plan"], fresh_db,
                     name="Flat", billing_model="flat",
                     tiers=tiers, effective_from="2026-01-01")
    plan_id = r["rate_plan"]["id"]
    result = _call_action(ACTIONS["rate-consumption"], fresh_db,
                          rate_plan_id=plan_id, consumption="100")
    assert result["status"] == "ok"
    calc = result["calculation"]
    assert calc["usage_charge"] == "15.00"
    assert calc["total_charge"] == "15.00"


def test_rate_consumption_tiered(fresh_db):
    """Tiered: 150 kWh across 2 tiers.
    First 100 at $0.10 = $10.00, next 50 at $0.08 = $4.00, total = $14.00."""
    plan_id = create_test_rate_plan(fresh_db, billing_model="tiered")
    result = _call_action(ACTIONS["rate-consumption"], fresh_db,
                          rate_plan_id=plan_id, consumption="150")
    assert result["status"] == "ok"
    calc = result["calculation"]
    assert calc["usage_charge"] == "14.00"
    assert calc["total_charge"] == "14.00"
    assert len(calc["breakdown"]) == 2


def test_rate_consumption_volume_discount(fresh_db):
    """Volume discount: 250 units falls in tier 2 ($0.08/unit) = $20.00."""
    tiers = json.dumps([
        {"tier_start": "0", "tier_end": "100", "rate": "0.10"},
        {"tier_start": "100", "tier_end": "500", "rate": "0.08"},
        {"tier_start": "500", "rate": "0.06"},
    ])
    r = _call_action(ACTIONS["add-rate-plan"], fresh_db,
                     name="Volume", billing_model="volume_discount",
                     tiers=tiers, effective_from="2026-01-01")
    plan_id = r["rate_plan"]["id"]
    result = _call_action(ACTIONS["rate-consumption"], fresh_db,
                          rate_plan_id=plan_id, consumption="250")
    assert result["status"] == "ok"
    calc = result["calculation"]
    assert calc["usage_charge"] == "20.00"
    assert calc["total_charge"] == "20.00"
