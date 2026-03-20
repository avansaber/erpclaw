"""Tests for erpclaw-billing meters and meter readings.

Actions tested: add-meter, update-meter, get-meter, list-meters,
                add-meter-reading, list-meter-readings
"""
import json
import pytest
from decimal import Decimal
from billing_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
)

mod = load_db_query()


class TestAddMeter:
    def test_basic_create(self, conn, env):
        result = call_action(mod.add_meter, conn, ns(
            customer_id=env["customer"], meter_type="electricity",
            name="Main Panel", address="123 Main St",
            rate_plan_id=None, install_date="2026-01-15",
            unit="kWh",
        ))
        assert is_ok(result)
        assert "meter" in result
        assert result["meter"]["service_type"] == "electricity"

    def test_missing_customer_fails(self, conn, env):
        result = call_action(mod.add_meter, conn, ns(
            customer_id=None, meter_type="water",
            name=None, address=None,
            rate_plan_id=None, install_date=None,
            unit=None,
        ))
        assert is_error(result)

    def test_missing_type_fails(self, conn, env):
        result = call_action(mod.add_meter, conn, ns(
            customer_id=env["customer"], meter_type=None,
            name=None, address=None,
            rate_plan_id=None, install_date=None,
            unit=None,
        ))
        assert is_error(result)

    def test_invalid_type_fails(self, conn, env):
        result = call_action(mod.add_meter, conn, ns(
            customer_id=env["customer"], meter_type="invalid",
            name=None, address=None,
            rate_plan_id=None, install_date=None,
            unit=None,
        ))
        assert is_error(result)


class TestUpdateMeter:
    def _create_meter(self, conn, env):
        result = call_action(mod.add_meter, conn, ns(
            customer_id=env["customer"], meter_type="electricity",
            name="Test Meter", address=None,
            rate_plan_id=None, install_date=None, unit=None,
        ))
        return result["meter"]["id"]

    def test_update_name(self, conn, env):
        meter_id = self._create_meter(conn, env)
        result = call_action(mod.update_meter, conn, ns(
            meter_id=meter_id, name="Updated Meter",
            status=None, rate_plan_id=None,
        ))
        assert is_ok(result)

    def test_update_status(self, conn, env):
        meter_id = self._create_meter(conn, env)
        result = call_action(mod.update_meter, conn, ns(
            meter_id=meter_id, name=None,
            status="disconnected", rate_plan_id=None,
        ))
        assert is_ok(result)
        assert result["meter"]["status"] == "disconnected"

    def test_no_fields_fails(self, conn, env):
        meter_id = self._create_meter(conn, env)
        result = call_action(mod.update_meter, conn, ns(
            meter_id=meter_id, name=None,
            status=None, rate_plan_id=None,
        ))
        assert is_error(result)


class TestGetMeter:
    def test_get(self, conn, env):
        create = call_action(mod.add_meter, conn, ns(
            customer_id=env["customer"], meter_type="water",
            name="Water Meter", address=None,
            rate_plan_id=None, install_date=None, unit="gallons",
        ))
        result = call_action(mod.get_meter, conn, ns(
            meter_id=create["meter"]["id"],
        ))
        assert is_ok(result)
        assert result["meter"]["id"] == create["meter"]["id"]
        assert "latest_reading" in result["meter"]

    def test_get_nonexistent_fails(self, conn, env):
        result = call_action(mod.get_meter, conn, ns(
            meter_id="fake-id",
        ))
        assert is_error(result)


class TestListMeters:
    def test_list(self, conn, env):
        call_action(mod.add_meter, conn, ns(
            customer_id=env["customer"], meter_type="gas",
            name="Gas Meter", address=None,
            rate_plan_id=None, install_date=None, unit=None,
        ))
        result = call_action(mod.list_meters, conn, ns(
            customer_id=None, meter_type=None, status=None,
            limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1

    def test_list_by_customer(self, conn, env):
        call_action(mod.add_meter, conn, ns(
            customer_id=env["customer"], meter_type="electricity",
            name="Elec Meter", address=None,
            rate_plan_id=None, install_date=None, unit=None,
        ))
        result = call_action(mod.list_meters, conn, ns(
            customer_id=env["customer"], meter_type=None, status=None,
            limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1


class TestAddMeterReading:
    def _create_meter(self, conn, env):
        result = call_action(mod.add_meter, conn, ns(
            customer_id=env["customer"], meter_type="electricity",
            name="Reading Test Meter", address=None,
            rate_plan_id=None, install_date=None, unit="kWh",
        ))
        return result["meter"]["id"]

    def test_first_reading(self, conn, env):
        meter_id = self._create_meter(conn, env)
        result = call_action(mod.add_meter_reading, conn, ns(
            meter_id=meter_id, reading_date="2026-06-01",
            reading_value="100", reading_type=None, source=None,
            uom=None,
        ))
        assert is_ok(result)
        assert "reading" in result
        assert result["reading"]["reading_value"] == "100"

    def test_second_reading_calculates_consumption(self, conn, env):
        meter_id = self._create_meter(conn, env)
        call_action(mod.add_meter_reading, conn, ns(
            meter_id=meter_id, reading_date="2026-06-01",
            reading_value="100", reading_type=None, source=None,
            uom=None,
        ))
        result = call_action(mod.add_meter_reading, conn, ns(
            meter_id=meter_id, reading_date="2026-07-01",
            reading_value="250", reading_type=None, source=None,
            uom=None,
        ))
        assert is_ok(result)
        assert result["reading"]["consumption"] == "150"

    def test_missing_meter_fails(self, conn, env):
        result = call_action(mod.add_meter_reading, conn, ns(
            meter_id=None, reading_date="2026-06-01",
            reading_value="100", reading_type=None, source=None,
            uom=None,
        ))
        assert is_error(result)


class TestListMeterReadings:
    def test_list(self, conn, env):
        meter = call_action(mod.add_meter, conn, ns(
            customer_id=env["customer"], meter_type="electricity",
            name="List Reading Meter", address=None,
            rate_plan_id=None, install_date=None, unit=None,
        ))
        meter_id = meter["meter"]["id"]
        call_action(mod.add_meter_reading, conn, ns(
            meter_id=meter_id, reading_date="2026-06-01",
            reading_value="100", reading_type=None, source=None,
            uom=None,
        ))
        result = call_action(mod.list_meter_readings, conn, ns(
            meter_id=meter_id, from_date=None, to_date=None,
            limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1
