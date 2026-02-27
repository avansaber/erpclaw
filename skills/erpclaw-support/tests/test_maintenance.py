"""Tests for maintenance schedule and visit actions."""
import pytest
from helpers import (
    _call_action,
    create_test_company,
    create_test_customer,
    create_test_item,
    create_test_maintenance_schedule,
)
from db_query import ACTIONS


# ── 1. Add schedule — calculates next_due_date from start_date ──────────────

def test_add_schedule_calculates_next_due(fresh_db):
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id=company_id)

    result = _call_action(
        ACTIONS["add-maintenance-schedule"],
        fresh_db,
        customer_id=customer_id,
        schedule_frequency="quarterly",
        start_date="2026-01-01",
        end_date="2026-12-31",
    )

    assert result["status"] == "ok"
    sched = result["maintenance_schedule"]
    assert sched["naming_series"].startswith("MS-")
    assert sched["status"] == "active"
    # 2026-01-01 + 90 days = 2026-04-01
    assert sched["next_due_date"] == "2026-04-01"


# ── 2. List schedules — filter by customer ──────────────────────────────────

def test_list_schedules_by_customer(fresh_db):
    company_id = create_test_company(fresh_db)
    customer1_id = create_test_customer(fresh_db, company_id=company_id,
                                        customer_name="Customer One")
    customer2_id = create_test_customer(fresh_db, company_id=company_id,
                                        customer_name="Customer Two")

    # 2 schedules for customer 1
    create_test_maintenance_schedule(fresh_db, customer_id=customer1_id)
    create_test_maintenance_schedule(fresh_db, customer_id=customer1_id,
                                     start_date="2026-02-01")
    # 1 schedule for customer 2
    create_test_maintenance_schedule(fresh_db, customer_id=customer2_id)

    result = _call_action(
        ACTIONS["list-maintenance-schedules"],
        fresh_db,
        customer_id=customer1_id,
    )

    assert result["status"] == "ok"
    assert result["total"] == 2


# ── 3. Record visit — updates schedule's last_completed_date & next_due ─────

def test_record_visit_updates_schedule(fresh_db):
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id=company_id)
    schedule_id = create_test_maintenance_schedule(
        fresh_db, customer_id=customer_id,
        schedule_frequency="quarterly",
        start_date="2026-01-01",
        end_date="2026-12-31",
    )

    result = _call_action(
        ACTIONS["record-maintenance-visit"],
        fresh_db,
        schedule_id=schedule_id,
        visit_date="2026-04-01",
        status="completed",
        work_done="Serviced equipment",
    )

    assert result["status"] == "ok"
    assert result["schedule_updated"] is True

    visit = result["visit"]
    assert visit["naming_series"].startswith("MV-")
    assert visit["work_done"] == "Serviced equipment"

    sched = result["schedule"]
    assert sched["last_completed_date"] == "2026-04-01"
    # 2026-04-01 + 90 days = 2026-06-30
    assert sched["next_due_date"] == "2026-06-30"


# ── 4. Completed visit — recalculates next_due_date (monthly) ───────────────

def test_completed_visit_recalculates_next_due(fresh_db):
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id=company_id)
    schedule_id = create_test_maintenance_schedule(
        fresh_db, customer_id=customer_id,
        schedule_frequency="monthly",
        start_date="2026-01-01",
        end_date="2026-06-30",
    )

    result = _call_action(
        ACTIONS["record-maintenance-visit"],
        fresh_db,
        schedule_id=schedule_id,
        visit_date="2026-03-01",
        status="completed",
    )

    assert result["status"] == "ok"
    sched = result["schedule"]
    # 2026-03-01 + 30 days = 2026-03-31
    assert sched["next_due_date"] == "2026-03-31"


# ── 5. Visit past end_date — expires the schedule ───────────────────────────

def test_visit_past_end_date_expires_schedule(fresh_db):
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id=company_id)
    schedule_id = create_test_maintenance_schedule(
        fresh_db, customer_id=customer_id,
        schedule_frequency="quarterly",
        start_date="2026-01-01",
        end_date="2026-06-30",
    )

    result = _call_action(
        ACTIONS["record-maintenance-visit"],
        fresh_db,
        schedule_id=schedule_id,
        visit_date="2026-05-01",
        status="completed",
    )

    assert result["status"] == "ok"
    sched = result["schedule"]
    # 2026-05-01 + 90 days = 2026-07-30 > end_date 2026-06-30
    assert sched["status"] == "expired"
