"""Tests for CRM lead actions."""
import pytest
from helpers import (
    _call_action,
    create_test_company,
    create_test_lead,
    create_test_activity,
    setup_crm_environment,
)
from db_query import ACTIONS


def test_add_lead(fresh_db):
    """Create a lead with all fields and verify it is created correctly."""
    conn = fresh_db
    company_id = create_test_company(conn)

    result = _call_action(ACTIONS["add-lead"], conn,
        lead_name="John Smith",
        company_name="Acme Corp",
        email="john@example.com",
        phone="555-0100",
        source="website",
        territory="United States",
        industry="Technology",
    )

    assert result["status"] == "ok"
    lead = result["lead"]
    assert lead["id"] is not None
    assert lead["naming_series"].startswith("LEAD-")
    assert lead["status"] == "new"
    assert lead["lead_name"] == "John Smith"
    assert lead["company_name"] == "Acme Corp"
    assert lead["email"] == "john@example.com"
    assert lead["phone"] == "555-0100"
    assert lead["source"] == "website"


def test_add_lead_missing_name(fresh_db):
    """Adding a lead without lead_name should fail."""
    conn = fresh_db
    company_id = create_test_company(conn)

    result = _call_action(ACTIONS["add-lead"], conn,
        company_name="Acme Corp",
        email="john@example.com",
        phone="555-0100",
        source="website",
    )

    assert result["status"] == "error"


def test_add_lead_invalid_source(fresh_db):
    """Adding a lead with an invalid source should fail."""
    conn = fresh_db
    company_id = create_test_company(conn)

    result = _call_action(ACTIONS["add-lead"], conn,
        lead_name="John Smith",
        company_name="Acme Corp",
        email="john@example.com",
        source="invalid_source",
    )

    assert result["status"] == "error"
    assert "source" in result["message"].lower()


def test_update_lead(fresh_db):
    """Update a lead's status and phone, verify changes are reflected."""
    conn = fresh_db
    company_id = create_test_company(conn)
    lead_id = create_test_lead(conn, lead_name="Jane Doe", source="website")

    result = _call_action(ACTIONS["update-lead"], conn,
        lead_id=lead_id,
        status="contacted",
        phone="555-0200",
    )

    assert result["status"] == "ok"
    lead = result["lead"]
    assert lead["status"] == "contacted"
    assert lead["phone"] == "555-0200"


def test_cannot_update_converted_lead(fresh_db):
    """A lead that has been converted to an opportunity cannot be updated."""
    conn = fresh_db
    company_id = create_test_company(conn)
    lead_id = create_test_lead(conn, lead_name="Bob Wilson", source="website")

    # Convert lead to opportunity
    convert_result = _call_action(ACTIONS["convert-lead-to-opportunity"], conn,
        lead_id=lead_id,
        opportunity_name="Bob's Deal",
        expected_revenue="10000.00",
        probability="50",
    )
    assert convert_result["status"] == "ok"

    # Attempt to update the converted lead
    result = _call_action(ACTIONS["update-lead"], conn,
        lead_id=lead_id,
        phone="555-9999",
    )

    assert result["status"] == "error"
    assert "converted" in result["message"].lower()


def test_get_lead_with_activities(fresh_db):
    """Get a lead and verify its associated activities are included."""
    conn = fresh_db
    company_id = create_test_company(conn)
    lead_id = create_test_lead(conn, lead_name="Alice Brown", source="referral")

    # Add two activities for this lead
    create_test_activity(conn,
        activity_type="call",
        subject="Initial call",
        activity_date="2026-02-10",
        lead_id=lead_id,
    )
    create_test_activity(conn,
        activity_type="email",
        subject="Follow-up email",
        activity_date="2026-02-11",
        lead_id=lead_id,
    )

    result = _call_action(ACTIONS["get-lead"], conn, lead_id=lead_id)

    assert result["status"] == "ok"
    lead = result["lead"]
    assert lead["id"] == lead_id
    assert len(lead["activities"]) == 2


def test_list_leads_with_filters(fresh_db):
    """List leads with various filter combinations."""
    conn = fresh_db
    company_id = create_test_company(conn)

    # Lead 1: website, new
    create_test_lead(conn, lead_name="Lead One", source="website")

    # Lead 2: referral, new
    create_test_lead(conn, lead_name="Lead Two", source="referral")

    # Lead 3: website, contacted (create then update status)
    lead3_id = create_test_lead(conn, lead_name="Lead Three", source="website")
    _call_action(ACTIONS["update-lead"], conn,
        lead_id=lead3_id,
        status="contacted",
    )

    # Filter by status=new — should get Lead One and Lead Two
    result_new = _call_action(ACTIONS["list-leads"], conn, status="new")
    assert result_new["status"] == "ok"
    assert len(result_new["leads"]) == 2

    # Filter by source=website — should get Lead One and Lead Three
    result_website = _call_action(ACTIONS["list-leads"], conn, source="website")
    assert result_website["status"] == "ok"
    assert len(result_website["leads"]) == 2

    # Filter by both status=new AND source=website — should get only Lead One
    result_both = _call_action(ACTIONS["list-leads"], conn,
        status="new",
        source="website",
    )
    assert result_both["status"] == "ok"
    assert len(result_both["leads"]) == 1
    assert result_both["leads"][0]["lead_name"] == "Lead One"


def test_convert_lead_to_opportunity(fresh_db):
    """Convert a lead to an opportunity and verify all side effects."""
    conn = fresh_db
    company_id = create_test_company(conn)
    lead_id = create_test_lead(conn,
        lead_name="Charlie Davis",
        company_name="Davis Industries",
        source="website",
    )

    result = _call_action(ACTIONS["convert-lead-to-opportunity"], conn,
        lead_id=lead_id,
        opportunity_name="Davis Deal",
        expected_revenue="50000.00",
        probability="60",
    )

    assert result["status"] == "ok"

    # Verify opportunity was created correctly
    opportunity = result["opportunity"]
    assert opportunity["id"] is not None
    assert opportunity["expected_revenue"] == "50000.00"
    assert opportunity["probability"] == "60"
    # weighted_revenue = 50000.00 * 60 / 100 = 30000.00
    assert opportunity["weighted_revenue"] == "30000.00"

    # Verify lead was updated to converted status
    lead_result = _call_action(ACTIONS["get-lead"], conn, lead_id=lead_id)
    assert lead_result["status"] == "ok"
    lead = lead_result["lead"]
    assert lead["status"] == "converted"
    assert lead["converted_to_opportunity"] == opportunity["id"]
