"""Tests for billing period, run-billing, and adjustment actions."""
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
from helpers import (
    _call_action, create_test_company, create_test_customer,
    create_test_meter, create_test_rate_plan, create_test_meter_reading,
    create_test_usage_event, setup_billing_environment,
)


def test_create_billing_period(fresh_db):
    """Create a billing period in open status."""
    env = setup_billing_environment(fresh_db)
    result = _call_action(ACTIONS["create-billing-period"], fresh_db,
                          customer_id=env["customer_id"],
                          meter_id=env["meter_id"],
                          from_date="2026-01-01", to_date="2026-01-31")
    assert result["status"] == "ok"
    bp = result["billing_period"]
    assert bp["status"] == "open"
    assert bp["period_start"] == "2026-01-01"
    assert bp["grand_total"] == "0"


def test_run_billing_basic(fresh_db):
    """Run billing: meter + readings -> rated period."""
    env = setup_billing_environment(fresh_db)
    # Add readings with consumption
    create_test_meter_reading(fresh_db, env["meter_id"], "2026-01-15", "1000")
    create_test_meter_reading(fresh_db, env["meter_id"], "2026-01-31", "1150")
    # 1150 - 1000 = 150 kWh consumption from readings
    result = _call_action(ACTIONS["run-billing"], fresh_db,
                          company_id=env["company_id"],
                          billing_date="2026-01-31",
                          from_date="2026-01-01",
                          to_date="2026-01-31")
    assert result["status"] == "ok"
    assert result["periods_created"] == 1
    # Check the billing period was rated
    bp_id = result["period_ids"][0]
    bp = fresh_db.execute("SELECT * FROM billing_period WHERE id = ?",
                          (bp_id,)).fetchone()
    assert bp["status"] == "rated"
    assert bp["total_consumption"] == "150"


def test_run_billing_marks_events_processed(fresh_db):
    """Run billing should mark usage events as processed."""
    env = setup_billing_environment(fresh_db)
    create_test_usage_event(fresh_db, env["meter_id"], "2026-01-10", "50")
    create_test_usage_event(fresh_db, env["meter_id"], "2026-01-20", "30")
    # Check events are unprocessed
    cnt = fresh_db.execute(
        "SELECT COUNT(*) as c FROM usage_event WHERE processed = 0"
    ).fetchone()["c"]
    assert cnt == 2
    _call_action(ACTIONS["run-billing"], fresh_db,
                 company_id=env["company_id"],
                 billing_date="2026-01-31",
                 from_date="2026-01-01", to_date="2026-01-31")
    # Now they should be processed
    cnt = fresh_db.execute(
        "SELECT COUNT(*) as c FROM usage_event WHERE processed = 0"
    ).fetchone()["c"]
    assert cnt == 0


def test_run_billing_with_base_charge(fresh_db):
    """Run billing with base_charge included in total."""
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id)
    rate_plan_id = create_test_rate_plan(
        fresh_db, name="Plan with Base",
        billing_model="flat",
        tiers=[{"rate": "0.10"}],
        base_charge="25.00")
    meter_id = create_test_meter(fresh_db, customer_id,
                                 rate_plan_id=rate_plan_id)
    create_test_meter_reading(fresh_db, meter_id, "2026-01-15", "100")
    create_test_meter_reading(fresh_db, meter_id, "2026-01-31", "200")
    # 100 kWh at $0.10 = $10.00 + $25.00 base = $35.00
    result = _call_action(ACTIONS["run-billing"], fresh_db,
                          company_id=company_id, billing_date="2026-01-31",
                          from_date="2026-01-01", to_date="2026-01-31")
    assert result["status"] == "ok"
    bp_id = result["period_ids"][0]
    bp = fresh_db.execute("SELECT * FROM billing_period WHERE id = ?",
                          (bp_id,)).fetchone()
    assert bp["base_charge"] == "25.00"
    assert bp["usage_charge"] == "10.00"
    assert bp["grand_total"] == "35.00"


def test_run_billing_minimum_charge(fresh_db):
    """Minimum charge should be enforced."""
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id)
    rate_plan_id = create_test_rate_plan(
        fresh_db, name="Plan with Min",
        billing_model="flat",
        tiers=[{"rate": "0.10"}],
        minimum_charge="50.00")
    meter_id = create_test_meter(fresh_db, customer_id,
                                 rate_plan_id=rate_plan_id)
    create_test_meter_reading(fresh_db, meter_id, "2026-01-15", "100")
    create_test_meter_reading(fresh_db, meter_id, "2026-01-31", "110")
    # 10 kWh at $0.10 = $1.00, but minimum is $50.00
    result = _call_action(ACTIONS["run-billing"], fresh_db,
                          company_id=company_id, billing_date="2026-01-31",
                          from_date="2026-01-01", to_date="2026-01-31")
    assert result["status"] == "ok"
    bp_id = result["period_ids"][0]
    bp = fresh_db.execute("SELECT * FROM billing_period WHERE id = ?",
                          (bp_id,)).fetchone()
    assert bp["grand_total"] == "50.00"


def test_generate_invoices_marks_invoiced(fresh_db):
    """Generate invoices should mark billing periods as invoiced."""
    env = setup_billing_environment(fresh_db)
    create_test_meter_reading(fresh_db, env["meter_id"], "2026-01-15", "1000")
    create_test_meter_reading(fresh_db, env["meter_id"], "2026-01-31", "1100")
    bill = _call_action(ACTIONS["run-billing"], fresh_db,
                        company_id=env["company_id"],
                        billing_date="2026-01-31",
                        from_date="2026-01-01", to_date="2026-01-31")
    bp_id = bill["period_ids"][0]
    result = _call_action(ACTIONS["generate-invoices"], fresh_db,
                          billing_period_ids=json.dumps([bp_id]))
    assert result["status"] == "ok"
    assert result["invoiced"] == 1
    bp = fresh_db.execute("SELECT status FROM billing_period WHERE id = ?",
                          (bp_id,)).fetchone()
    assert bp["status"] == "invoiced"


def test_add_billing_adjustment_credit(fresh_db):
    """Credit adjustment recalculates totals."""
    env = setup_billing_environment(fresh_db)
    create_test_meter_reading(fresh_db, env["meter_id"], "2026-01-15", "1000")
    create_test_meter_reading(fresh_db, env["meter_id"], "2026-01-31", "1100")
    bill = _call_action(ACTIONS["run-billing"], fresh_db,
                        company_id=env["company_id"],
                        billing_date="2026-01-31",
                        from_date="2026-01-01", to_date="2026-01-31")
    bp_id = bill["period_ids"][0]
    result = _call_action(ACTIONS["add-billing-adjustment"], fresh_db,
                          billing_period_id=bp_id, amount="-5.00",
                          adjustment_type="credit", reason="Loyalty discount")
    assert result["status"] == "ok"
    bp = fresh_db.execute("SELECT * FROM billing_period WHERE id = ?",
                          (bp_id,)).fetchone()
    # Original total was for 100 kWh tiered. adjustments_total = -5.00
    assert bp["adjustments_total"] == "-5.00"


def test_add_billing_adjustment_late_fee(fresh_db):
    """Late fee adjustment increases grand total."""
    env = setup_billing_environment(fresh_db)
    # Create an open period to add adjustment
    bp_result = _call_action(ACTIONS["create-billing-period"], fresh_db,
                             customer_id=env["customer_id"],
                             meter_id=env["meter_id"],
                             from_date="2026-01-01", to_date="2026-01-31")
    bp_id = bp_result["billing_period"]["id"]
    result = _call_action(ACTIONS["add-billing-adjustment"], fresh_db,
                          billing_period_id=bp_id, amount="25.00",
                          adjustment_type="late_fee")
    assert result["status"] == "ok"
    bp = fresh_db.execute("SELECT * FROM billing_period WHERE id = ?",
                          (bp_id,)).fetchone()
    assert bp["adjustments_total"] == "25.00"
    assert bp["grand_total"] == "25.00"


def test_list_billing_periods_filters(fresh_db):
    """List billing periods with status filter."""
    env = setup_billing_environment(fresh_db)
    _call_action(ACTIONS["create-billing-period"], fresh_db,
                 customer_id=env["customer_id"], meter_id=env["meter_id"],
                 from_date="2026-01-01", to_date="2026-01-31")
    result = _call_action(ACTIONS["list-billing-periods"], fresh_db,
                          status="open")
    assert result["status"] == "ok"
    assert result["total_count"] == 1
    result2 = _call_action(ACTIONS["list-billing-periods"], fresh_db,
                           status="rated")
    assert result2["total_count"] == 0


def test_get_billing_period_with_adjustments_and_status(fresh_db):
    """Get billing period with embedded adjustments + status action."""
    env = setup_billing_environment(fresh_db)
    bp_result = _call_action(ACTIONS["create-billing-period"], fresh_db,
                             customer_id=env["customer_id"],
                             meter_id=env["meter_id"],
                             from_date="2026-01-01", to_date="2026-01-31")
    bp_id = bp_result["billing_period"]["id"]
    _call_action(ACTIONS["add-billing-adjustment"], fresh_db,
                 billing_period_id=bp_id, amount="10.00",
                 adjustment_type="late_fee")
    result = _call_action(ACTIONS["get-billing-period"], fresh_db,
                          billing_period_id=bp_id)
    assert result["status"] == "ok"
    assert len(result["billing_period"]["adjustments"]) == 1
    # Status action
    stat = _call_action(ACTIONS["status"], fresh_db)
    assert stat["status"] == "ok"
    assert stat["billing_periods_total"] >= 1
