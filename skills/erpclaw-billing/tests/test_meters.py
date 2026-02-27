"""Tests for meter and meter reading actions."""
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
    create_test_meter, create_test_meter_reading,
)


def test_add_meter(fresh_db):
    """Add a meter with valid fields."""
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id)
    result = _call_action(ACTIONS["add-meter"], fresh_db,
                          customer_id=customer_id, meter_type="electricity",
                          name="Main Panel")
    assert result["status"] == "ok"
    meter = result["meter"]
    assert meter["meter_number"].startswith("MTR-")
    assert meter["service_type"] == "electricity"
    assert meter["service_point_id"] == "Main Panel"
    assert meter["status"] == "active"


def test_add_meter_invalid_type(fresh_db):
    """Reject invalid meter type."""
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id)
    result = _call_action(ACTIONS["add-meter"], fresh_db,
                          customer_id=customer_id, meter_type="nuclear")
    assert result["status"] == "error"
    assert "Invalid meter-type" in result["message"]


def test_update_meter_status(fresh_db):
    """Update meter status to disconnected."""
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id)
    meter_id = create_test_meter(fresh_db, customer_id)
    result = _call_action(ACTIONS["update-meter"], fresh_db,
                          meter_id=meter_id, status="disconnected")
    assert result["status"] == "ok"
    assert result["meter"]["status"] == "disconnected"


def test_get_meter_with_latest_reading(fresh_db):
    """Get meter should include the latest reading."""
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id)
    meter_id = create_test_meter(fresh_db, customer_id)
    create_test_meter_reading(fresh_db, meter_id, "2026-01-15", "500")
    create_test_meter_reading(fresh_db, meter_id, "2026-02-15", "850")
    result = _call_action(ACTIONS["get-meter"], fresh_db, meter_id=meter_id)
    assert result["status"] == "ok"
    assert result["meter"]["latest_reading"] is not None
    assert result["meter"]["latest_reading"]["reading_value"] == "850"
    assert result["meter"]["reading_count"] == 2


def test_list_meters_by_customer(fresh_db):
    """List meters filtered by customer."""
    company_id = create_test_company(fresh_db)
    cust1 = create_test_customer(fresh_db, company_id, "Customer A")
    cust2 = create_test_customer(fresh_db, company_id, "Customer B")
    create_test_meter(fresh_db, cust1)
    create_test_meter(fresh_db, cust1)
    create_test_meter(fresh_db, cust2)
    result = _call_action(ACTIONS["list-meters"], fresh_db, customer_id=cust1)
    assert result["status"] == "ok"
    assert result["total_count"] == 2


def test_add_meter_reading_auto_consumption(fresh_db):
    """Reading should auto-calculate consumption from previous."""
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id)
    meter_id = create_test_meter(fresh_db, customer_id)
    # First reading — no previous, no consumption
    r1 = _call_action(ACTIONS["add-meter-reading"], fresh_db,
                      meter_id=meter_id, reading_date="2026-01-15",
                      reading_value="1000")
    assert r1["status"] == "ok"
    assert r1["reading"]["consumption"] is None
    # Second reading — consumption = 1350 - 1000 = 350
    r2 = _call_action(ACTIONS["add-meter-reading"], fresh_db,
                      meter_id=meter_id, reading_date="2026-02-15",
                      reading_value="1350")
    assert r2["status"] == "ok"
    assert r2["reading"]["consumption"] == "350"
    assert r2["reading"]["previous_reading_value"] == "1000"


def test_add_meter_reading_updates_meter(fresh_db):
    """Reading should update meter's last_reading fields."""
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id)
    meter_id = create_test_meter(fresh_db, customer_id)
    _call_action(ACTIONS["add-meter-reading"], fresh_db,
                 meter_id=meter_id, reading_date="2026-01-15",
                 reading_value="500")
    meter = fresh_db.execute("SELECT * FROM meter WHERE id = ?",
                             (meter_id,)).fetchone()
    assert meter["last_reading_date"] == "2026-01-15"
    assert meter["last_reading_value"] == "500"


def test_list_meter_readings_date_filter(fresh_db):
    """List readings with date range filter."""
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id)
    meter_id = create_test_meter(fresh_db, customer_id)
    create_test_meter_reading(fresh_db, meter_id, "2026-01-15", "100")
    create_test_meter_reading(fresh_db, meter_id, "2026-02-15", "200")
    create_test_meter_reading(fresh_db, meter_id, "2026-03-15", "300")
    result = _call_action(ACTIONS["list-meter-readings"], fresh_db,
                          meter_id=meter_id, from_date="2026-02-01",
                          to_date="2026-02-28")
    assert result["status"] == "ok"
    assert result["total_count"] == 1
    assert result["readings"][0]["reading_value"] == "200"
