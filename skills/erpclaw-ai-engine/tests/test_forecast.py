"""Tests for cash flow forecasting and correlation discovery actions.

8 tests covering:
  - forecast_cash_flow (empty, with AR/AP, horizon, confidence interval)
  - get_forecast
  - discover_correlations
  - list_correlations (basic, filtered)
"""
import json

from helpers import (
    _call_action,
    setup_ai_environment,
    create_test_sales_invoice,
    create_test_purchase_invoice,
    create_test_gl_entry,
    create_test_payment_entry,
)
from db_query import (
    forecast_cash_flow,
    get_forecast,
    discover_correlations,
    list_correlations,
)


# ── Test 1: Forecast with empty company (no invoices, no GL) ─────────────

def test_forecast_empty_company(fresh_db):
    """Forecast with no invoices or GL entries should give zero balances."""
    env = setup_ai_environment(fresh_db)

    result = _call_action(forecast_cash_flow, fresh_db,
                          company_id=env["company_id"])

    assert result["status"] == "ok"
    assert len(result["forecast_ids"]) == 3

    # Starting balance should be 0 (no bank/cash GL entries)
    starting = float(result["starting_balance"])
    assert starting == 0.0

    # No AR or AP
    assert result["total_ar"] in ("0", "0.00", "0.0")
    assert result["total_ap"] in ("0", "0.00", "0.0")

    # All three scenarios should equal the starting balance (0)
    for scenario_name in ("pessimistic", "expected", "optimistic"):
        assert scenario_name in result["scenarios"]
        bal = float(result["scenarios"][scenario_name])
        assert bal == 0.0, f"{scenario_name} should be 0 when no AR/AP"

    assert result["horizon_days"] == 30


# ── Test 2: Forecast with AR, AP, and starting bank balance ──────────────

def test_forecast_with_ar_ap(fresh_db):
    """Verify scenario math: starting_balance + adjusted inflows - adjusted outflows."""
    env = setup_ai_environment(fresh_db)

    # Create a bank GL entry for starting balance = 10000
    create_test_gl_entry(fresh_db, env["bank_account_id"],
                         debit="10000", credit="0",
                         posting_date="2026-01-01")

    # Open AR = 2000
    create_test_sales_invoice(fresh_db, env["company_id"], env["customer_id"],
                              grand_total="2000", outstanding="2000",
                              posting_date="2026-01-01", due_date="2026-01-31",
                              status="submitted")

    # Open AP = 500
    create_test_purchase_invoice(fresh_db, env["company_id"], env["supplier_id"],
                                 grand_total="500", outstanding="500",
                                 posting_date="2026-01-01", due_date="2026-01-31",
                                 status="submitted")

    result = _call_action(forecast_cash_flow, fresh_db,
                          company_id=env["company_id"])

    assert result["status"] == "ok"

    # Starting balance
    starting = float(result["starting_balance"])
    assert starting == 10000.0

    # AR and AP totals
    assert result["total_ar"] == "2000.00"
    assert result["total_ap"] == "500.00"

    # Pessimistic: 10000 + 2000*0.7 - 500*1.2 = 10000 + 1400 - 600 = 10800
    pess = float(result["scenarios"]["pessimistic"])
    assert abs(pess - 10800.0) < 0.01, f"Pessimistic expected 10800, got {pess}"

    # Expected: 10000 + 2000*0.9 - 500*1.0 = 10000 + 1800 - 500 = 11300
    exp = float(result["scenarios"]["expected"])
    assert abs(exp - 11300.0) < 0.01, f"Expected 11300, got {exp}"

    # Optimistic: 10000 + 2000*1.0 - 500*0.8 = 10000 + 2000 - 400 = 11600
    opt = float(result["scenarios"]["optimistic"])
    assert abs(opt - 11600.0) < 0.01, f"Optimistic expected 11600, got {opt}"


# ── Test 3: Forecast with custom horizon_days ────────────────────────────

def test_forecast_horizon_days(fresh_db):
    """Verify that horizon_days=60 is reflected in the result."""
    env = setup_ai_environment(fresh_db)

    result = _call_action(forecast_cash_flow, fresh_db,
                          company_id=env["company_id"],
                          horizon_days="60")

    assert result["status"] == "ok"
    assert result["horizon_days"] == 60


# ── Test 4: Forecast confidence interval keys ────────────────────────────

def test_forecast_confidence_interval(fresh_db):
    """Confidence interval should have low, mid, high keys."""
    env = setup_ai_environment(fresh_db)

    # Add some data so the values are non-trivial
    create_test_gl_entry(fresh_db, env["bank_account_id"],
                         debit="5000", credit="0")
    create_test_sales_invoice(fresh_db, env["company_id"], env["customer_id"],
                              grand_total="1000", outstanding="1000")

    result = _call_action(forecast_cash_flow, fresh_db,
                          company_id=env["company_id"])

    assert result["status"] == "ok"
    ci = result["confidence_interval"]
    assert "low" in ci, "confidence_interval must have 'low'"
    assert "mid" in ci, "confidence_interval must have 'mid'"
    assert "high" in ci, "confidence_interval must have 'high'"

    # low <= mid <= high
    assert float(ci["low"]) <= float(ci["mid"]) <= float(ci["high"])


# ── Test 5: get_forecast after forecasting ───────────────────────────────

def test_get_forecast(fresh_db):
    """After running forecast_cash_flow, get_forecast should return 3 records."""
    env = setup_ai_environment(fresh_db)

    # Generate a forecast first
    _call_action(forecast_cash_flow, fresh_db,
                 company_id=env["company_id"])

    result = _call_action(get_forecast, fresh_db,
                          company_id=env["company_id"])

    assert result["status"] == "ok"
    assert len(result["forecasts"]) == 3
    assert result["count"] == 3

    # Each forecast should have a scenario field
    scenarios_found = {f["scenario"] for f in result["forecasts"]}
    assert scenarios_found == {"pessimistic", "expected", "optimistic"}


# ── Test 6: discover_correlations basic ──────────────────────────────────

def test_discover_correlations_basic(fresh_db):
    """Create sales and purchase invoices, then discover correlations."""
    env = setup_ai_environment(fresh_db)

    # Create some sales invoices
    for i in range(3):
        create_test_sales_invoice(fresh_db, env["company_id"], env["customer_id"],
                                  grand_total="1000", outstanding="500",
                                  posting_date=f"2026-01-{10 + i:02d}")

    # Create some purchase invoices
    for i in range(2):
        create_test_purchase_invoice(fresh_db, env["company_id"], env["supplier_id"],
                                     grand_total="600", outstanding="300",
                                     posting_date=f"2026-01-{10 + i:02d}")

    result = _call_action(discover_correlations, fresh_db,
                          company_id=env["company_id"])

    assert result["status"] == "ok"
    assert result["correlations_discovered"] >= 1
    assert len(result["correlation_ids"]) >= 1


# ── Test 7: list_correlations after discovery ────────────────────────────

def test_list_correlations(fresh_db):
    """After discovering correlations, list_correlations should return them."""
    env = setup_ai_environment(fresh_db)

    # Create invoices for correlation discovery
    create_test_sales_invoice(fresh_db, env["company_id"], env["customer_id"],
                              grand_total="2000", outstanding="1000")
    create_test_purchase_invoice(fresh_db, env["company_id"], env["supplier_id"],
                                 grand_total="800", outstanding="400")

    # Discover first
    discover_result = _call_action(discover_correlations, fresh_db,
                                   company_id=env["company_id"])
    assert discover_result["correlations_discovered"] >= 1

    # Now list
    list_result = _call_action(list_correlations, fresh_db,
                               company_id=env["company_id"])

    assert list_result["status"] == "ok"
    assert list_result["total_count"] >= 1
    assert len(list_result["correlations"]) >= 1

    # Each correlation should have required fields
    corr = list_result["correlations"][0]
    assert "id" in corr
    assert "strength" in corr
    assert "description" in corr


# ── Test 8: list_correlations with min_strength filter ───────────────────

def test_list_correlations_min_strength(fresh_db):
    """Filtering by min_strength='strong' should return only strong correlations."""
    env = setup_ai_environment(fresh_db)

    # Create balanced sales/purchase data for a strong correlation
    create_test_sales_invoice(fresh_db, env["company_id"], env["customer_id"],
                              grand_total="1000", outstanding="500")
    create_test_purchase_invoice(fresh_db, env["company_id"], env["supplier_id"],
                                 grand_total="800", outstanding="400")

    # Discover
    _call_action(discover_correlations, fresh_db,
                 company_id=env["company_id"])

    # List all correlations
    all_result = _call_action(list_correlations, fresh_db,
                              company_id=env["company_id"])
    all_count = all_result["total_count"]

    # List only strong
    strong_result = _call_action(list_correlations, fresh_db,
                                 company_id=env["company_id"],
                                 min_strength="strong")

    assert strong_result["status"] == "ok"
    # Strong filter should return <= total correlations
    assert strong_result["total_count"] <= all_count

    # Every returned correlation must be 'strong'
    for corr in strong_result["correlations"]:
        assert corr["strength"] == "strong"
