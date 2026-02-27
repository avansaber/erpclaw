"""Tests for CRM campaign actions."""

import uuid
import pytest
from helpers import (
    _call_action,
    create_test_company,
    create_test_lead,
    create_test_campaign,
)
from db_query import ACTIONS


# ── 1. test_add_campaign_with_budget ─────────────────────────────────────────

def test_add_campaign_with_budget(fresh_db):
    """Add a campaign with budget and verify all fields are stored correctly."""
    company_id = create_test_company(fresh_db)

    result = _call_action(ACTIONS["add-campaign"], fresh_db,
                          name="Summer Sale",
                          campaign_type="email",
                          budget="5000.00",
                          start_date="2026-03-01",
                          end_date="2026-06-30")

    assert result["status"] == "ok"
    campaign = result["campaign"]
    assert campaign["id"] is not None
    assert campaign["name"] == "Summer Sale"
    assert campaign["campaign_type"] == "email"
    assert campaign["budget"] == "5000.00"
    assert campaign["start_date"] == "2026-03-01"
    assert campaign["end_date"] == "2026-06-30"
    assert campaign["status"] == "planned"


# ── 2. test_add_campaign_with_lead_linking ───────────────────────────────────

def test_add_campaign_with_lead_linking(fresh_db):
    """Add a campaign with a linked lead and verify the link is created."""
    company_id = create_test_company(fresh_db)
    lead_id = create_test_lead(fresh_db)

    result = _call_action(ACTIONS["add-campaign"], fresh_db,
                          name="Lead Campaign",
                          campaign_type="social",
                          lead_id=lead_id)

    assert result["status"] == "ok"
    campaign_id = result["campaign"]["id"]
    assert result["lead_linked"] == lead_id

    # Verify the campaign_lead row exists
    link = fresh_db.execute(
        "SELECT * FROM campaign_lead WHERE campaign_id = ? AND lead_id = ?",
        (campaign_id, lead_id)
    ).fetchone()
    assert link is not None


# ── 3. test_list_campaigns_with_filters ──────────────────────────────────────

def test_list_campaigns_with_filters(fresh_db):
    """List campaigns and filter by status."""
    company_id = create_test_company(fresh_db)

    create_test_campaign(fresh_db, name="Campaign A")
    create_test_campaign(fresh_db, name="Campaign B")

    # List all campaigns
    result = _call_action(ACTIONS["list-campaigns"], fresh_db)
    assert result["status"] == "ok"
    assert result["total"] == 2

    # Filter by status=planned (both default to planned)
    result = _call_action(ACTIONS["list-campaigns"], fresh_db,
                          status="planned")
    assert result["total"] == 2


# ── 4. test_list_campaigns_with_lead_counts ──────────────────────────────────

def test_list_campaigns_with_lead_counts(fresh_db):
    """Verify that list-campaigns returns correct total_leads and converted_leads."""
    company_id = create_test_company(fresh_db)
    lead1_id = create_test_lead(fresh_db, lead_name="Lead Alpha")
    lead2_id = create_test_lead(fresh_db, lead_name="Lead Beta")

    # Add campaign with lead1 linked
    result = _call_action(ACTIONS["add-campaign"], fresh_db,
                          name="Multi-Lead Campaign",
                          campaign_type="email",
                          lead_id=lead1_id)
    campaign_id = result["campaign"]["id"]

    # Link lead2 directly via SQL
    fresh_db.execute(
        "INSERT INTO campaign_lead (id, campaign_id, lead_id) VALUES (?, ?, ?)",
        (str(uuid.uuid4()), campaign_id, lead2_id)
    )
    fresh_db.commit()

    # Convert lead1 to opportunity (this should set campaign_lead.converted=1)
    conv = _call_action(ACTIONS["convert-lead-to-opportunity"], fresh_db,
                        lead_id=lead1_id,
                        opportunity_name="Alpha Deal")
    assert conv["status"] == "ok"

    # List campaigns and check lead counts
    result = _call_action(ACTIONS["list-campaigns"], fresh_db)

    assert result["status"] == "ok"
    assert result["total"] == 1
    campaign = result["campaigns"][0]
    assert campaign["total_leads"] == 2
    assert campaign["converted_leads"] == 1
