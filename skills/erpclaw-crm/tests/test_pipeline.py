"""Tests for CRM pipeline report and status actions."""

import pytest
from helpers import (
    _call_action,
    create_test_company,
    create_test_lead,
    create_test_customer,
    create_test_opportunity,
    create_test_campaign,
    create_test_activity,
)
from db_query import ACTIONS


# ── 1. test_pipeline_stage_aggregation ───────────────────────────────────────

def test_pipeline_stage_aggregation(fresh_db):
    """Pipeline report aggregates revenue and weighted revenue by stage."""
    company_id = create_test_company(fresh_db)

    # opp1: new stage, rev=10000, prob=30
    opp1_id = create_test_opportunity(fresh_db,
                                       opportunity_name="Deal A",
                                       expected_revenue="10000.00",
                                       probability="30")

    # opp2: new stage, rev=20000, prob=50
    opp2_id = create_test_opportunity(fresh_db,
                                       opportunity_name="Deal B",
                                       expected_revenue="20000.00",
                                       probability="50")

    # opp3: starts as new, then update to qualified stage, rev=15000, prob=80
    opp3_id = create_test_opportunity(fresh_db,
                                       opportunity_name="Deal C",
                                       expected_revenue="15000.00",
                                       probability="80")

    _call_action(ACTIONS["update-opportunity"], fresh_db,
                 opportunity_id=opp3_id,
                 stage="qualified")

    # Call pipeline-report
    result = _call_action(ACTIONS["pipeline-report"], fresh_db)
    assert result["status"] == "ok"

    stages = {s["stage"]: s for s in result["pipeline"]["stages"]}

    # "new" stage: 2 opportunities
    assert stages["new"]["count"] == 2
    # total_expected_revenue = 10000 + 20000 = 30000
    assert stages["new"]["total_expected_revenue"] == "30000.00"
    # weighted: 10000*0.30 + 20000*0.50 = 3000 + 10000 = 13000
    assert stages["new"]["total_weighted_revenue"] == "13000.00"

    # "qualified" stage: 1 opportunity
    assert stages["qualified"]["count"] == 1


# ── 2. test_pipeline_conversion_rate ─────────────────────────────────────────

def test_pipeline_conversion_rate(fresh_db):
    """Pipeline report calculates conversion rate from closed opportunities."""
    company_id = create_test_company(fresh_db)

    opp1_id = create_test_opportunity(fresh_db, opportunity_name="Win Deal")
    opp2_id = create_test_opportunity(fresh_db, opportunity_name="Lose Deal")
    opp3_id = create_test_opportunity(fresh_db, opportunity_name="Open Deal")

    # Mark opp1 as won
    _call_action(ACTIONS["mark-opportunity-won"], fresh_db,
                 opportunity_id=opp1_id)

    # Mark opp2 as lost with reason
    _call_action(ACTIONS["mark-opportunity-lost"], fresh_db,
                 opportunity_id=opp2_id,
                 lost_reason="Price too high")

    # opp3 stays open (new stage)

    result = _call_action(ACTIONS["pipeline-report"], fresh_db)
    assert result["status"] == "ok"

    # Conversion rate = won / (won + lost) = 1 / 2 = 50%
    assert result["pipeline"]["conversion_rate_pct"] == "50.00"


# ── 3. test_status_summary ──────────────────────────────────────────────────

def test_status_summary(fresh_db):
    """Status action returns correct counts for leads, opportunities, campaigns, activities."""
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id=company_id)

    lead1_id = create_test_lead(fresh_db, lead_name="Status Lead 1")
    lead2_id = create_test_lead(fresh_db, lead_name="Status Lead 2")

    opp_id = create_test_opportunity(fresh_db, opportunity_name="Status Deal",
                                      customer_id=customer_id)

    create_test_campaign(fresh_db, name="Status Campaign")

    # Activity for lead
    create_test_activity(fresh_db, lead_id=lead1_id,
                         activity_type="call", subject="Lead call")
    # Activity for opportunity
    create_test_activity(fresh_db, opportunity_id=opp_id,
                         activity_type="meeting", subject="Opp meeting")

    result = _call_action(ACTIONS["status"], fresh_db)
    assert result["status"] == "ok"

    crm = result["crm_status"]
    assert crm["leads"]["total"] == 2
    assert crm["leads"]["active"] == 2
    assert crm["opportunities"]["total"] == 1
    assert crm["opportunities"]["open"] == 1
    assert crm["campaigns"]["total"] == 1
    assert crm["activities"]["total"] == 2
