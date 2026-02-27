"""Tests for usage event actions."""
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
    create_test_meter, create_test_usage_event,
)


def test_add_usage_event(fresh_db):
    """Add a usage event."""
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id)
    meter_id = create_test_meter(fresh_db, customer_id)
    result = _call_action(ACTIONS["add-usage-event"], fresh_db,
                          meter_id=meter_id, event_date="2026-02-10",
                          quantity="50", event_type="api_call")
    assert result["status"] == "ok"
    evt = result["usage_event"]
    assert evt["quantity"] == "50"
    assert evt["event_type"] == "api_call"
    assert evt["processed"] == 0


def test_add_usage_event_idempotency(fresh_db):
    """Duplicate idempotency key returns existing event."""
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id)
    meter_id = create_test_meter(fresh_db, customer_id)
    r1 = _call_action(ACTIONS["add-usage-event"], fresh_db,
                      meter_id=meter_id, event_date="2026-02-10",
                      quantity="50", idempotency_key="key-001")
    assert r1["status"] == "ok"
    r2 = _call_action(ACTIONS["add-usage-event"], fresh_db,
                      meter_id=meter_id, event_date="2026-02-10",
                      quantity="50", idempotency_key="key-001")
    assert r2["status"] == "ok"
    assert r2.get("deduplicated") is True
    assert r2["usage_event"]["id"] == r1["usage_event"]["id"]


def test_add_usage_events_batch(fresh_db):
    """Batch insert multiple events."""
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id)
    meter_id = create_test_meter(fresh_db, customer_id)
    events = json.dumps([
        {"meter_id": meter_id, "event_date": "2026-02-01", "quantity": "10"},
        {"meter_id": meter_id, "event_date": "2026-02-02", "quantity": "20"},
        {"meter_id": meter_id, "event_date": "2026-02-03", "quantity": "30"},
    ])
    result = _call_action(ACTIONS["add-usage-events-batch"], fresh_db,
                          events=events)
    assert result["status"] == "ok"
    assert result["inserted"] == 3
    assert result["duplicates"] == 0
    assert result["total_processed"] == 3


def test_add_usage_events_batch_with_duplicates(fresh_db):
    """Batch with duplicate idempotency keys counts them separately."""
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id)
    meter_id = create_test_meter(fresh_db, customer_id)
    # Insert one first
    _call_action(ACTIONS["add-usage-event"], fresh_db,
                 meter_id=meter_id, event_date="2026-02-01",
                 quantity="10", idempotency_key="dup-key")
    events = json.dumps([
        {"meter_id": meter_id, "event_date": "2026-02-01", "quantity": "10",
         "idempotency_key": "dup-key"},
        {"meter_id": meter_id, "event_date": "2026-02-02", "quantity": "20",
         "idempotency_key": "new-key"},
    ])
    result = _call_action(ACTIONS["add-usage-events-batch"], fresh_db,
                          events=events)
    assert result["status"] == "ok"
    assert result["inserted"] == 1
    assert result["duplicates"] == 1


def test_usage_event_processed_flag(fresh_db):
    """Events start with processed=0."""
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id)
    meter_id = create_test_meter(fresh_db, customer_id)
    create_test_usage_event(fresh_db, meter_id, "2026-02-10", "50")
    row = fresh_db.execute(
        "SELECT processed FROM usage_event WHERE meter_id = ?",
        (meter_id,)).fetchone()
    assert row["processed"] == 0


def test_usage_event_gets_customer_from_meter(fresh_db):
    """Event should inherit customer_id from meter."""
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id)
    meter_id = create_test_meter(fresh_db, customer_id)
    result = _call_action(ACTIONS["add-usage-event"], fresh_db,
                          meter_id=meter_id, event_date="2026-02-10",
                          quantity="50")
    assert result["status"] == "ok"
    assert result["usage_event"]["customer_id"] == customer_id
