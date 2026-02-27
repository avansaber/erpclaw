"""Tests for SLA management actions (add, list, auto-apply, due date calculation)."""
import json
import pytest
from datetime import datetime, timedelta
from helpers import (
    _call_action,
    create_test_company,
    create_test_customer,
    create_test_sla,
    create_test_issue,
)
from db_query import ACTIONS


# ---------------------------------------------------------------------------
# 1. add SLA with JSON priorities
# ---------------------------------------------------------------------------

def test_add_sla_with_json_priorities(fresh_db):
    create_test_company(fresh_db)

    priorities = json.dumps({
        "response_times": {"low": "48", "medium": "24", "high": "8", "critical": "4"},
        "resolution_times": {"low": "120", "medium": "72", "high": "24", "critical": "8"},
    })

    result = _call_action(
        ACTIONS["add-sla"], fresh_db,
        name="Gold", priorities=priorities, working_hours="9-17",
    )

    assert result["status"] == "ok"
    sla = result["sla"]
    assert sla["name"] == "Gold"
    assert isinstance(sla["priority_response_times"], dict)
    assert sla["priority_response_times"]["high"] == "8"
    assert isinstance(sla["priority_resolution_times"], dict)
    assert sla["priority_resolution_times"]["high"] == "24"


# ---------------------------------------------------------------------------
# 2. list SLAs
# ---------------------------------------------------------------------------

def test_list_slas(fresh_db):
    create_test_company(fresh_db)

    create_test_sla(fresh_db, name="Bronze SLA")
    create_test_sla(fresh_db, name="Silver SLA")

    result = _call_action(ACTIONS["list-slas"], fresh_db)

    assert result["status"] == "ok"
    assert result["total"] == 2


# ---------------------------------------------------------------------------
# 3. default SLA auto-applied to new issues
# ---------------------------------------------------------------------------

def test_default_sla_auto_applied(fresh_db):
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id=company_id)
    sla_id = create_test_sla(fresh_db, name="Default SLA", is_default=True)

    result = _call_action(
        ACTIONS["add-issue"], fresh_db,
        customer_id=customer_id, subject="Network issue", priority="medium",
    )

    assert result["status"] == "ok"
    assert result["issue"]["sla_id"] is not None
    assert result["issue"]["sla_id"] == sla_id


# ---------------------------------------------------------------------------
# 4. SLA due date calculation
# ---------------------------------------------------------------------------

def test_sla_due_date_calculation(fresh_db):
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id=company_id)

    sla_id = create_test_sla(
        fresh_db,
        name="Strict SLA",
        response_times={"high": "8"},
        resolution_times={"high": "24"},
    )

    result = _call_action(
        ACTIONS["add-issue"], fresh_db,
        customer_id=customer_id, subject="Server down",
        priority="high", sla_id=sla_id,
    )

    assert result["status"] == "ok"
    issue = result["issue"]
    assert issue["response_due"] is not None
    assert issue["resolution_due"] is not None

    # Parse the due dates and verify they are approximately correct
    # response_due should be ~8 hours after creation
    # resolution_due should be ~24 hours after creation
    response_due = datetime.fromisoformat(issue["response_due"])
    resolution_due = datetime.fromisoformat(issue["resolution_due"])

    now = datetime.utcnow()
    response_delta = response_due - now
    resolution_delta = resolution_due - now

    # response_due should be roughly 8 hours from now (allow 60s tolerance)
    assert timedelta(hours=7, minutes=59) <= response_delta <= timedelta(hours=8, minutes=1)
    # resolution_due should be roughly 24 hours from now (allow 60s tolerance)
    assert timedelta(hours=23, minutes=59) <= resolution_delta <= timedelta(hours=24, minutes=1)
