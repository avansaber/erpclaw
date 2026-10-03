"""Strong-depth pins for seven usage-billing read actions.

Each test deepens the existing behavioural test for its action with a
fresh-seam read-back of the exact stored rows, a hand-written money
literal where money exists, an exact-message refusal (or a pinned empty
result where the action has no refusal branch), an unchanged-table set,
a read-computed derived value (computed by the read action's own code), and decoys that differ on the filtered
column (plus a second-company decoy wherever the action takes a company).

Existing tests are left in place; the assertion named in CHANGES.md
carries the weight for each action.
"""
import json
from decimal import Decimal

import pytest
from billing_helpers import (
    call_action, ns, is_error, is_ok, load_db_query, seed_customer,
    seed_company, seed_naming_series,
)
from erpclaw_lib.db import get_connection, integrity_error_types
from erpclaw_lib.query import Q, Table, P, fn, Field
from erpclaw_lib import billing_run as billing_run_lib

mod = load_db_query()

BILLING_READ_TABLES = (
    "meter",
    "meter_reading",
    "rate_plan",
    "rate_tier",
    "billing_run",
    "billing_run_target",
    "billing_period",
    "audit_log",
)


def _fresh_count(table_name, column=None, value=None):
    conn2 = get_connection()
    try:
        t = Table(table_name)
        q = Q.from_(t).select(fn.Count("*").as_("cnt"))
        params = ()
        if column is not None:
            q = q.where(Field(column) == P())
            params = (value,)
        return conn2.execute(q.get_sql(), params).fetchone()["cnt"]
    finally:
        conn2.close()


def _fresh_row(table_name, row_id):
    conn2 = get_connection()
    try:
        t = Table(table_name)
        q = Q.from_(t).select(t.star).where(t.id == P())
        row = conn2.execute(q.get_sql(), (row_id,)).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn2.close()


def _fresh_filtered(table_name, column, value):
    conn2 = get_connection()
    try:
        t = Table(table_name)
        q = Q.from_(t).select(t.star).where(Field(column) == P())
        return [dict(r) for r in conn2.execute(q.get_sql(), (value,)).fetchall()]
    finally:
        conn2.close()


def _all_rows(table_name):
    conn2 = get_connection()
    try:
        blk = Table(table_name)
        q = Q.from_(blk).select(blk.star).orderby(blk.id)
        return [dict(r) for r in conn2.execute(q.get_sql(), ()).fetchall()]
    finally:
        conn2.close()


def _snapshot(tables):
    return {name: _all_rows(name) for name in tables}


def _set_created_at(conn, table_name, row_id, created_at):
    blk = Table(table_name)
    q = Q.update(blk).set(Field("created_at"), P()).where(Field("id") == P())
    conn.execute(q.get_sql(), (created_at, row_id))
    conn.commit()


def _add_meter(conn, customer_id, meter_type="electricity", name="M",
               rate_plan_id=None, unit="kWh"):
    result = call_action(mod.add_meter, conn, ns(
        customer_id=customer_id, meter_type=meter_type, name=name,
        address=None, rate_plan_id=rate_plan_id, install_date=None,
        unit=unit))
    assert is_ok(result), result
    return result["meter"]


def _add_reading(conn, meter_id, reading_date, value, reading_type=None):
    result = call_action(mod.add_meter_reading, conn, ns(
        meter_id=meter_id, reading_date=reading_date,
        reading_value=value, reading_type=reading_type, source=None,
        uom=None))
    assert is_ok(result), result
    return result["reading"]


def _add_flat_plan(conn, name, rate="0.10", base_charge="25.00",
                   service_type="electricity"):
    result = call_action(mod.add_rate_plan, conn, ns(
        name=name, billing_model="flat", service_type=service_type,
        base_charge=base_charge, base_charge_period=None,
        effective_from=None, effective_to=None, minimum_charge=None,
        minimum_commitment=None, overage_rate=None,
        tiers=json.dumps([{"rate": rate}])))
    assert is_ok(result), result
    return result["rate_plan"]


def _add_event(conn, meter_id, timestamp, quantity):
    result = call_action(mod.add_usage_event, conn, ns(
        meter_id=meter_id, event_date=timestamp, quantity=quantity,
        event_type="usage", properties=None, idempotency_key=None))
    assert is_ok(result), result
    return result


def _run_billing(conn, company_id, billing_date, from_date, to_date):
    result = call_action(mod.run_billing, conn, ns(
        company_id=company_id, billing_date=billing_date,
        from_date=from_date, to_date=to_date))
    assert is_ok(result), result
    return result


class TestGetMeterStrong:
    def test_latest_reading_back_and_refusal(self, conn, env):
        other_customer = seed_customer(conn, env["company_id"],
                                       "Decoy Customer")
        decoy = _add_meter(conn, other_customer, name="Decoy Panel")
        _add_reading(conn, decoy["id"], "2026-07-15", "999")
        meter = _add_meter(conn, env["customer"], name="Strong Panel")
        _add_reading(conn, meter["id"], "2026-06-01", "100")
        _add_reading(conn, meter["id"], "2026-07-01", "250")
        assert decoy["id"] != meter["id"]

        before = _snapshot(BILLING_READ_TABLES)
        result = call_action(mod.get_meter, conn, ns(
            meter_id=meter["id"]))
        assert is_ok(result), result
        got = result["meter"]
        assert got["id"] == meter["id"]
        assert got["service_type"] == "electricity"
        assert got["customer_id"] == env["customer"]
        assert got["meter_number"] == meter["meter_number"]
        assert got["reading_count"] == 2
        assert got["latest_reading"]["reading_value"] == "250"
        assert got["latest_reading"]["consumption"] == "150"
        assert got["latest_reading"]["reading_date"] == "2026-07-01"

        stored = _fresh_row("meter", meter["id"])
        assert stored["meter_number"] == got["meter_number"]
        assert stored["last_reading_value"] == "250"
        readings = _fresh_filtered("meter_reading", "meter_id",
                                   meter["id"])
        assert len(readings) == 2
        by_date = {r["reading_date"]: r for r in readings}
        assert by_date["2026-06-01"]["reading_value"] == "100"
        assert by_date["2026-07-01"]["consumption"] == "150"
        assert _fresh_count("meter_reading",
                            "meter_id", decoy["id"]) == 1
        assert _snapshot(BILLING_READ_TABLES) == before

        refused = call_action(mod.get_meter, conn, ns(
            meter_id="no-such-meter"))
        assert is_error(refused)
        assert refused["message"] == "Meter not found: no-such-meter"
        assert _snapshot(BILLING_READ_TABLES) == before


class TestListMetersStrong:
    def test_customer_scoped_counts_and_empty_pin(self, conn, env):
        meter_a = _add_meter(conn, env["customer"], meter_type="electricity",
                             name="Strong Panel A")
        meter_b = _add_meter(conn, env["customer"], meter_type="gas",
                             name="Strong Gas B", unit=None)
        customer_two = seed_customer(conn, env["company_id"],
                                     "Second Customer")
        decoy_meter = _add_meter(conn, customer_two, meter_type="electricity",
                                 name="Decoy Panel")
        _set_created_at(conn, "meter", meter_a["id"], "2026-01-01 00:00:01")
        _set_created_at(conn, "meter", decoy_meter["id"], "2026-01-01 00:00:02")
        _set_created_at(conn, "meter", meter_b["id"], "2026-01-01 00:00:03")

        before = _snapshot(BILLING_READ_TABLES)
        result = call_action(mod.list_meters, conn, ns(
            customer_id=env["customer"], meter_type=None, status=None,
            limit=20, offset=0))
        assert is_ok(result), result
        assert result["total_count"] == 2
        assert result["has_more"] is False
        assert {m["customer_id"] for m in result["meters"]} == {
            env["customer"]}
        assert {m["customer_name"] for m in result["meters"]} == {
            "Utility Customer"}
        assert [m["id"] for m in result["meters"]] == [
            meter_b["id"], meter_a["id"]]
        assert [m["created_at"] for m in result["meters"]] == [
            "2026-01-01 00:00:03", "2026-01-01 00:00:01"]

        by_type = call_action(mod.list_meters, conn, ns(
            customer_id=None, meter_type="electricity", status=None,
            limit=20, offset=0))
        assert is_ok(by_type), by_type
        assert by_type["total_count"] == 2
        assert {m["service_type"] for m in by_type["meters"]} == {
            "electricity"}
        by_both = call_action(mod.list_meters, conn, ns(
            customer_id=env["customer"], meter_type="gas", status=None,
            limit=20, offset=0))
        assert is_ok(by_both), by_both
        assert by_both["total_count"] == 1
        assert by_both["meters"][0]["service_point_id"] == "Strong Gas B"

        assert _fresh_count("meter", "customer_id", env["customer"]) == 2
        assert _fresh_count("meter") == 3
        assert _snapshot(BILLING_READ_TABLES) == before

        empty = call_action(mod.list_meters, conn, ns(
            customer_id="no-such-customer", meter_type=None, status=None,
            limit=20, offset=0))
        assert is_ok(empty)
        assert empty["total_count"] == 0
        assert empty["meters"] == []
        assert _snapshot(BILLING_READ_TABLES) == before

    def test_invalid_meter_type_refusal(self, conn, env):
        _add_meter(conn, env["customer"], meter_type="electricity",
                   name="Type Guard A")
        before = _snapshot(BILLING_READ_TABLES)
        refused = call_action(mod.list_meters, conn, ns(
            customer_id=None, meter_type="bogus-type", status=None,
            limit=20, offset=0))
        assert is_error(refused)
        assert refused["message"] == (
            "Invalid meter-type: bogus-type. Must be one of: electricity, "
            "water, gas, telecom, saas, parking, rental, waste, custom")
        assert _snapshot(BILLING_READ_TABLES) == before

    def test_invalid_status_refusal(self, conn, env):
        _add_meter(conn, env["customer"], meter_type="electricity",
                   name="Status Guard A")
        before = _snapshot(BILLING_READ_TABLES)
        refused = call_action(mod.list_meters, conn, ns(
            customer_id=None, meter_type=None, status="bogus",
            limit=20, offset=0))
        assert is_error(refused)
        assert refused["message"] == (
            "Invalid status: bogus. Must be one of: active, disconnected, "
            "removed, suspended")
        assert _snapshot(BILLING_READ_TABLES) == before

    def test_empty_string_filters_mean_no_filter(self, conn, env):
        meter_a = _add_meter(conn, env["customer"], meter_type="electricity",
                             name="Strong Panel A")
        meter_b = _add_meter(conn, env["customer"], meter_type="gas",
                             name="Strong Gas B", unit=None)
        _set_created_at(conn, "meter", meter_a["id"], "2026-01-01 00:00:01")
        _set_created_at(conn, "meter", meter_b["id"], "2026-01-01 00:00:02")

        before = _snapshot(BILLING_READ_TABLES)
        unfiltered = call_action(mod.list_meters, conn, ns(
            customer_id=env["customer"], meter_type=None, status=None,
            limit=20, offset=0))
        assert is_ok(unfiltered), unfiltered
        assert unfiltered["total_count"] == 2

        by_empty_type = call_action(mod.list_meters, conn, ns(
            customer_id=env["customer"], meter_type="", status=None,
            limit=20, offset=0))
        assert is_ok(by_empty_type), by_empty_type
        assert by_empty_type["meters"] == unfiltered["meters"]
        assert by_empty_type["total_count"] == unfiltered["total_count"]
        assert [m["id"] for m in by_empty_type["meters"]] == [
            m["id"] for m in unfiltered["meters"]]
        assert _snapshot(BILLING_READ_TABLES) == before

        by_empty_status = call_action(mod.list_meters, conn, ns(
            customer_id=env["customer"], meter_type=None, status="",
            limit=20, offset=0))
        assert is_ok(by_empty_status), by_empty_status
        assert by_empty_status["meters"] == unfiltered["meters"]
        assert by_empty_status["total_count"] == unfiltered["total_count"]
        assert [m["id"] for m in by_empty_status["meters"]] == [
            m["id"] for m in unfiltered["meters"]]
        assert _snapshot(BILLING_READ_TABLES) == before


class TestListMeterReadingsStrong:
    def test_date_window_counts_and_refusal(self, conn, env):
        meter = _add_meter(conn, env["customer"], name="Reading Meter")
        _add_reading(conn, meter["id"], "2026-06-01", "100")
        _add_reading(conn, meter["id"], "2026-07-01", "250")
        _add_reading(conn, meter["id"], "2026-08-01", "350")
        other_customer = seed_customer(conn, env["company_id"],
                                       "Decoy Customer")
        decoy = _add_meter(conn, other_customer, name="Decoy Meter")
        _add_reading(conn, decoy["id"], "2026-07-15", "999")

        before = _snapshot(BILLING_READ_TABLES)
        result = call_action(mod.list_meter_readings, conn, ns(
            meter_id=meter["id"], from_date="2026-07-01",
            to_date="2026-08-01", limit=20, offset=0))
        assert is_ok(result), result
        assert result["total_count"] == 2
        assert [r["reading_date"] for r in result["readings"]] == [
            "2026-08-01", "2026-07-01"]
        assert [r["reading_value"] for r in result["readings"]] == [
            "350", "250"]
        assert [r["consumption"] for r in result["readings"]] == [
            "100", "150"]
        # Sum computed by the test, not by the product.
        total = sum((Decimal(r["consumption"])
                     for r in result["readings"]), Decimal("0"))
        assert str(total) == "250"

        capped = call_action(mod.list_meter_readings, conn, ns(
            meter_id=meter["id"], from_date=None, to_date="2026-07-15",
            limit=20, offset=0))
        assert is_ok(capped), capped
        assert capped["total_count"] == 2
        assert [r["reading_date"] for r in capped["readings"]] == [
            "2026-07-01", "2026-06-01"]

        stored = _fresh_filtered("meter_reading", "meter_id", meter["id"])
        assert len(stored) == 3
        by_date = {r["reading_date"]: r for r in stored}
        assert by_date["2026-07-01"]["consumption"] == "150"
        assert by_date["2026-08-01"]["previous_reading_value"] == "250"
        assert _fresh_count("meter_reading",
                            "meter_id", decoy["id"]) == 1
        assert _snapshot(BILLING_READ_TABLES) == before

        refused = call_action(mod.list_meter_readings, conn, ns(
            meter_id=None, from_date=None, to_date=None, limit=20,
            offset=0))
        assert is_error(refused)
        assert refused["message"] == "--meter-id is required"
        assert _snapshot(BILLING_READ_TABLES) == before


class TestGetRatePlanStrong:
    def test_tiers_back_and_refusal(self, conn, env):
        decoy_plan = _add_flat_plan(conn, "Decoy Flat", rate="0.99",
                                    base_charge="99.00")
        result = call_action(mod.add_rate_plan, conn, ns(
            name="Strong Tiered", billing_model="tiered",
            service_type="electricity", base_charge="10.00",
            base_charge_period=None, effective_from=None, effective_to=None,
            minimum_charge=None, minimum_commitment=None, overage_rate=None,
            tiers=json.dumps([
                {"tier_start": "0", "tier_end": "100", "rate": "0.05"},
                {"tier_start": "100", "tier_end": "500", "rate": "0.10"},
                {"tier_start": "500", "rate": "0.15"},
            ])))
        assert is_ok(result), result
        plan_id = result["rate_plan"]["id"]
        assert decoy_plan["id"] != plan_id

        before = _snapshot(BILLING_READ_TABLES)
        got = call_action(mod.get_rate_plan, conn, ns(
            rate_plan_id=plan_id))
        assert is_ok(got), got
        plan = got["rate_plan"]
        assert plan["id"] == plan_id
        assert plan["name"] == "Strong Tiered"
        assert plan["plan_type"] == "tiered"
        assert plan["base_charge"] == "10.00"
        assert plan["currency"] == "USD"
        assert [t["rate"] for t in plan["tiers"]] == [
            "0.05", "0.10", "0.15"]
        assert [t["sort_order"] for t in plan["tiers"]] == [0, 1, 2]
        # Sum computed by the test, not by the product.
        total = sum((Decimal(t["rate"]) for t in plan["tiers"]),
                    Decimal("0"))
        assert str(total) == "0.30"

        assert _fresh_row("rate_plan", plan_id)["base_charge"] == "10.00"
        tiers = _fresh_filtered("rate_tier", "rate_plan_id", plan_id)
        assert len(tiers) == 3
        assert sorted(t["rate"] for t in tiers) == [
            "0.05", "0.10", "0.15"]
        assert _snapshot(BILLING_READ_TABLES) == before

        refused = call_action(mod.get_rate_plan, conn, ns(
            rate_plan_id="no-such-plan"))
        assert is_error(refused)
        assert refused["message"] == "Rate plan not found: no-such-plan"
        assert _snapshot(BILLING_READ_TABLES) == before


class TestListRatePlansStrong:
    def test_service_type_filter_and_empty_pin(self, conn, env):
        plan_a = _add_flat_plan(conn, "Strong Flat A", rate="0.10",
                                base_charge="25.00")
        tiered = call_action(mod.add_rate_plan, conn, ns(
            name="Strong Tiered B", billing_model="tiered",
            service_type="electricity", base_charge="10.00",
            base_charge_period=None, effective_from=None, effective_to=None,
            minimum_charge=None, minimum_commitment=None, overage_rate=None,
            tiers=json.dumps([
                {"tier_start": "0", "tier_end": "100", "rate": "0.05"},
                {"tier_start": "100", "rate": "0.10"},
            ])))
        assert is_ok(tiered), tiered
        plan_b_id = tiered["rate_plan"]["id"]
        plan_c = _add_flat_plan(conn, "Strong Water C", rate="1.50",
                                base_charge="5.00", service_type="water")
        _set_created_at(conn, "rate_plan", plan_a["id"], "2026-01-01 00:00:01")
        _set_created_at(conn, "rate_plan", plan_c["id"], "2026-01-01 00:00:02")
        _set_created_at(conn, "rate_plan", plan_b_id, "2026-01-01 00:00:03")

        before = _snapshot(BILLING_READ_TABLES)
        result = call_action(mod.list_rate_plans, conn, ns(
            service_type="electricity", limit=20, offset=0))
        assert is_ok(result), result
        assert result["total_count"] == 2
        assert {p["name"] for p in result["rate_plans"]} == {
            "Strong Flat A", "Strong Tiered B"}
        assert {p["service_type"] for p in result["rate_plans"]} == {
            "electricity"}
        assert [p["id"] for p in result["rate_plans"]] == [
            plan_b_id, plan_a["id"]]
        assert [p["created_at"] for p in result["rate_plans"]] == [
            "2026-01-01 00:00:03", "2026-01-01 00:00:01"]
        assert result["has_more"] is False

        page = call_action(mod.list_rate_plans, conn, ns(
            service_type=None, limit=2, offset=0))
        assert is_ok(page), page
        assert page["total_count"] == 3
        assert len(page["rate_plans"]) == 2
        assert page["has_more"] is True

        assert _fresh_count("rate_plan", "service_type",
                            "electricity") == 2
        assert _fresh_count("rate_plan") == 3
        stored = _fresh_filtered("rate_tier", "rate_plan_id",
                                 tiered["rate_plan"]["id"])
        assert sorted(t["rate"] for t in stored) == ["0.05", "0.10"]
        assert _snapshot(BILLING_READ_TABLES) == before

        empty = call_action(mod.list_rate_plans, conn, ns(
            service_type="bogus-type", limit=20, offset=0))
        assert is_ok(empty)
        assert empty["total_count"] == 0
        assert empty["rate_plans"] == []
        assert _snapshot(BILLING_READ_TABLES) == before


class TestListBillingRunsStrong:
    def _seed_two_runs(self, conn, env):
        plan = _add_flat_plan(conn, "Run Plan", rate="0.10",
                              base_charge=None)
        meter = _add_meter(conn, env["customer"], name="Run Meter",
                           rate_plan_id=plan["id"])
        _add_event(conn, meter["id"], "2026-06-10 12:00:00", "500")
        first = _run_billing(conn, env["company_id"], "2026-06-30",
                             "2026-06-01", "2026-06-30")
        _add_event(conn, meter["id"], "2026-07-10 12:00:00", "200")
        second = _run_billing(conn, env["company_id"], "2026-07-31",
                              "2026-07-01", "2026-07-31")
        decoy_id = billing_run_lib.start(
            conn, "recurring_invoices", "2026-06-30", [],
            company_id=env["company_id"])
        _set_created_at(conn, "billing_run", first["billing_run_id"],
                        "2026-01-01 00:00:01")
        _set_created_at(conn, "billing_run", decoy_id,
                        "2026-01-01 00:00:02")
        _set_created_at(conn, "billing_run", second["billing_run_id"],
                        "2026-01-01 00:00:03")
        return (meter["id"], first["billing_run_id"],
                second["billing_run_id"], decoy_id)

    def test_status_filter_ordering_and_refusal(self, conn, env):
        meter_id, first_id, second_id, decoy_id = self._seed_two_runs(conn, env)

        before = _snapshot(BILLING_READ_TABLES)
        result = call_action(mod.list_billing_runs, conn, ns(
            status="completed", run_type=None, from_date=None,
            to_date=None, limit=20, offset=0))
        assert is_ok(result), result
        assert result["total_count"] == 2
        assert {r["id"] for r in result["billing_runs"]} == {
            first_id, second_id}
        assert [r["run_type"] for r in result["billing_runs"]] == [
            "usage_billing", "usage_billing"]
        assert [r["id"] for r in result["billing_runs"]] == [
            second_id, first_id]
        assert [r["created_at"] for r in result["billing_runs"]] == [
            "2026-01-01 00:00:03", "2026-01-01 00:00:01"]
        assert result["has_more"] is False
        for row in result["billing_runs"]:
            assert row["company_id"] == env["company_id"]
            assert row["total_targets"] == 1
            assert row["targets_succeeded"] == 1

        by_type = call_action(mod.list_billing_runs, conn, ns(
            status=None, run_type="usage_billing", from_date=None,
            to_date=None, limit=20, offset=0))
        assert is_ok(by_type), by_type
        assert by_type["total_count"] == 2
        assert [r["id"] for r in by_type["billing_runs"]] == [
            second_id, first_id]
        by_other_type = call_action(mod.list_billing_runs, conn, ns(
            status=None, run_type="recurring_invoices", from_date=None,
            to_date=None, limit=20, offset=0))
        assert is_ok(by_other_type), by_other_type
        assert by_other_type["total_count"] == 1
        assert [r["id"] for r in by_other_type["billing_runs"]] == [decoy_id]
        assert by_other_type["billing_runs"][0]["run_type"] == "recurring_invoices"
        unfiltered = call_action(mod.list_billing_runs, conn, ns(
            status=None, run_type=None, from_date=None, to_date=None,
            limit=20, offset=0))
        assert is_ok(unfiltered), unfiltered
        assert unfiltered["total_count"] == 3
        assert [r["id"] for r in unfiltered["billing_runs"]] == [
            second_id, decoy_id, first_id]
        missing = call_action(mod.list_billing_runs, conn, ns(
            status="failed", run_type=None, from_date=None, to_date=None,
            limit=20, offset=0))
        assert is_ok(missing), missing
        assert missing["total_count"] == 0
        assert missing["billing_runs"] == []
        beyond = call_action(mod.list_billing_runs, conn, ns(
            status=None, run_type=None, from_date=None, to_date=None,
            limit=20, offset=3))
        assert is_ok(beyond), beyond
        assert beyond["total_count"] == 3
        assert beyond["billing_runs"] == []

        stored = _fresh_filtered("billing_run_target", "target_id",
                                 meter_id)
        assert len(stored) == 2
        assert {s["status"] for s in stored} == {"done"}
        assert _snapshot(BILLING_READ_TABLES) == before

        refused = call_action(mod.list_billing_runs, conn, ns(
            status="bogus", run_type=None, from_date=None, to_date=None,
            limit=20, offset=0))
        assert is_error(refused)
        assert refused["message"] == (
            "Invalid --status: bogus. Must be one of: pending, running, "
            "completed, failed, partially_completed")
        assert _snapshot(BILLING_READ_TABLES) == before


class TestGetBillingRunStrong:
    def test_header_targets_back_and_refusal(self, conn, env):
        plan = _add_flat_plan(conn, "Run Plan", rate="0.10",
                              base_charge=None)
        meter = _add_meter(conn, env["customer"], name="Run Meter",
                           rate_plan_id=plan["id"])
        _add_event(conn, meter["id"], "2026-06-10 12:00:00", "500")
        decoy_run_id = _run_billing(conn, env["company_id"], "2026-06-30",
                                    "2026-06-01", "2026-06-30")["billing_run_id"]
        _add_event(conn, meter["id"], "2026-07-10 12:00:00", "200")
        run_id = _run_billing(conn, env["company_id"], "2026-07-31",
                              "2026-07-01", "2026-07-31")["billing_run_id"]
        assert decoy_run_id != run_id

        before = _snapshot(BILLING_READ_TABLES)
        result = call_action(mod.get_billing_run, conn, ns(run_id=run_id))
        assert is_ok(result), result
        header = result["billing_run"]
        assert header["id"] == run_id
        assert header["run_type"] == "usage_billing"
        assert header["status"] == "completed"
        assert header["as_of_date"] == "2026-07-31"
        assert header["company_id"] == env["company_id"]
        assert result["target_count"] == 1
        assert len(result["targets"]) == 1
        target = result["targets"][0]
        assert target["target_type"] == "meter"
        assert target["target_id"] == meter["id"]
        assert target["status"] == "done"

        stored = _fresh_row("billing_run", run_id)
        assert stored["status"] == "completed"
        assert stored["run_type"] == "usage_billing"
        targets = _fresh_filtered("billing_run_target", "billing_run_id",
                                  run_id)
        assert len(targets) == 1
        assert targets[0]["status"] == "done"
        assert targets[0]["target_id"] == meter["id"]
        decoy_stored = _fresh_row("billing_run", decoy_run_id)
        assert decoy_stored is not None
        # Precondition: the decoy run exists (row fetched by its own id).
        assert decoy_stored["id"] == decoy_run_id
        assert decoy_stored["id"] != run_id
        assert _snapshot(BILLING_READ_TABLES) == before

        refused = call_action(mod.get_billing_run, conn, ns(
            run_id="no-such-run"))
        assert is_error(refused)
        assert refused["message"] == "Billing run not found: no-such-run"
        assert _snapshot(BILLING_READ_TABLES) == before

        missing = call_action(mod.get_billing_run, conn, ns(run_id=None))
        assert is_error(missing)
        assert missing["message"] == "--run-id is required"
        assert _snapshot(BILLING_READ_TABLES) == before


@pytest.mark.xfail(strict=True, raises=integrity_error_types(),
                   reason="meter_number is globally unique while the numbering series is per company, so two companies first meters collide")
def test_meter_numbers_collide_across_companies(conn):
    from billing_helpers import seed_customer as _seed_customer
    company_a = seed_company(conn)
    seed_naming_series(conn, company_a)
    customer_a = _seed_customer(conn, company_a, "First Co Customer")
    company_b = seed_company(conn)
    seed_naming_series(conn, company_b)
    customer_b = _seed_customer(conn, company_b, "Second Co Customer")
    first = call_action(mod.add_meter, conn, ns(
        customer_id=customer_a, meter_type="electricity", name="First Meter",
        address=None, rate_plan_id=None, install_date=None, unit="kWh"))
    assert is_ok(first), first
    series_t = Table("naming_series")
    series_q = (Q.from_(series_t).select(series_t.star)
                .where(series_t.company_id == P())
                .where(series_t.entity_type == P()))
    company_rows = [dict(r) for r in conn.execute(
        series_q.get_sql(), (company_a, "meter")).fetchall()]
    year_rows = [r for r in company_rows if r["prefix"] != "MTR-"]
    assert len(year_rows) == 1
    assert year_rows[0]["current_value"] == 1
    expected_number = "%s%05d" % (
        year_rows[0]["prefix"], year_rows[0]["current_value"])
    stored_first = _fresh_row("meter", first["meter"]["id"])
    assert stored_first["meter_number"] == expected_number
    other_rows = [dict(r) for r in conn.execute(
        series_q.get_sql(), (company_b, "meter")).fetchall()]
    assert all(r["prefix"] != year_rows[0]["prefix"] for r in other_rows)
    second = call_action(mod.add_meter, conn, ns(
        customer_id=customer_b, meter_type="electricity", name="Second Meter",
        address=None, rate_plan_id=None, install_date=None, unit="kWh"))
    assert is_ok(second), second
