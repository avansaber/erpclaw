"""Tests for CRM activity actions."""

import pytest
from helpers import (
    _call_action,
    create_test_company,
    create_test_lead,
    create_test_customer,
    create_test_opportunity,
    create_test_activity,
)
from db_query import ACTIONS


# ── 1. test_add_activity_for_lead ────────────────────────────────────────────

def test_add_activity_for_lead(fresh_db):
    """Add an activity linked to a lead and verify all fields."""
    company_id = create_test_company(fresh_db)
    lead_id = create_test_lead(fresh_db)

    result = _call_action(ACTIONS["add-activity"], fresh_db,
                          activity_type="call",
                          subject="Intro call",
                          activity_date="2026-02-16",
                          lead_id=lead_id)

    assert result["status"] == "ok"
    activity = result["activity"]
    assert activity["id"] is not None
    assert activity["activity_type"] == "call"
    assert activity["subject"] == "Intro call"
    assert activity["activity_date"] == "2026-02-16"
    assert activity["lead_id"] == lead_id


# ── 2. test_add_activity_for_opportunity ─────────────────────────────────────

def test_add_activity_for_opportunity(fresh_db):
    """Add an activity linked to an opportunity and verify success."""
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id=company_id)
    opportunity_id = create_test_opportunity(fresh_db,
                                             customer_id=customer_id)

    result = _call_action(ACTIONS["add-activity"], fresh_db,
                          activity_type="meeting",
                          subject="Demo meeting",
                          activity_date="2026-02-17",
                          opportunity_id=opportunity_id)

    assert result["status"] == "ok"
    activity = result["activity"]
    assert activity["activity_type"] == "meeting"
    assert activity["subject"] == "Demo meeting"
    assert activity["activity_date"] == "2026-02-17"
    assert activity["opportunity_id"] == opportunity_id


# ── 3. test_list_activities_by_lead ──────────────────────────────────────────

def test_list_activities_by_lead(fresh_db):
    """List activities filtered by lead_id returns only that lead's activities."""
    company_id = create_test_company(fresh_db)
    lead1_id = create_test_lead(fresh_db, lead_name="Lead One")
    lead2_id = create_test_lead(fresh_db, lead_name="Lead Two")

    # Two activities for lead1
    create_test_activity(fresh_db, lead_id=lead1_id,
                         activity_type="call", subject="Call 1")
    create_test_activity(fresh_db, lead_id=lead1_id,
                         activity_type="email", subject="Email 1")

    # One activity for lead2
    create_test_activity(fresh_db, lead_id=lead2_id,
                         activity_type="meeting", subject="Meeting 1")

    result = _call_action(ACTIONS["list-activities"], fresh_db,
                          lead_id=lead1_id)

    assert result["status"] == "ok"
    assert result["total"] == 2


# ── 4. test_list_activities_by_type ──────────────────────────────────────────

def test_list_activities_by_type(fresh_db):
    """List activities filtered by activity_type returns only matching type."""
    company_id = create_test_company(fresh_db)
    lead_id = create_test_lead(fresh_db)

    # Add three activities of different types for the same lead
    create_test_activity(fresh_db, lead_id=lead_id,
                         activity_type="call", subject="Sales call")
    create_test_activity(fresh_db, lead_id=lead_id,
                         activity_type="email", subject="Follow-up email")
    create_test_activity(fresh_db, lead_id=lead_id,
                         activity_type="meeting", subject="On-site visit")

    result = _call_action(ACTIONS["list-activities"], fresh_db,
                          activity_type="call")

    assert result["status"] == "ok"
    assert result["total"] == 1
    assert result["activities"][0]["activity_type"] == "call"
