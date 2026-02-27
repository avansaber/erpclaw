"""Tests for CRM opportunity actions.

Covers: add-opportunity, update-opportunity, get-opportunity, list-opportunities,
        convert-opportunity-to-quotation, mark-opportunity-won, mark-opportunity-lost.
"""
import json
from unittest.mock import patch, MagicMock

from helpers import (
    _call_action,
    create_test_company,
    create_test_customer,
    create_test_lead,
    create_test_opportunity,
    create_test_activity,
)
from db_query import ACTIONS


# ---------------------------------------------------------------------------
# 1. test_add_opportunity_with_customer
# ---------------------------------------------------------------------------

def test_add_opportunity_with_customer(fresh_db):
    """Add opportunity linked to a customer; verify weighted_revenue and stage."""
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id)

    result = _call_action(
        ACTIONS["add-opportunity"], fresh_db,
        opportunity_name="Enterprise Deal",
        customer_id=customer_id,
        expected_revenue="25000.00",
        probability="40",
    )

    assert result["status"] == "ok"
    opp = result["opportunity"]
    assert opp["customer_id"] == customer_id
    assert opp["stage"] == "new"
    assert opp["expected_revenue"] == "25000.00"
    assert opp["probability"] == "40"
    assert opp["weighted_revenue"] == "10000.00"


# ---------------------------------------------------------------------------
# 2. test_add_opportunity_with_lead
# ---------------------------------------------------------------------------

def test_add_opportunity_with_lead(fresh_db):
    """Add opportunity linked to a lead; verify source inherited from lead."""
    company_id = create_test_company(fresh_db)
    lead_id = create_test_lead(
        fresh_db, lead_name="Jane Smith", source="referral",
    )

    result = _call_action(
        ACTIONS["add-opportunity"], fresh_db,
        opportunity_name="Referral Deal",
        lead_id=lead_id,
        expected_revenue="15000.00",
        probability="30",
    )

    assert result["status"] == "ok"
    opp = result["opportunity"]
    assert opp["lead_id"] == lead_id
    assert opp["stage"] == "new"

    # Verify source was inherited from lead by reading DB directly
    row = fresh_db.execute(
        "SELECT source FROM opportunity WHERE id = ?", (opp["id"],)
    ).fetchone()
    assert row["source"] == "referral"


# ---------------------------------------------------------------------------
# 3. test_update_opportunity_recalculates_weighted
# ---------------------------------------------------------------------------

def test_update_opportunity_recalculates_weighted(fresh_db):
    """Updating probability recalculates weighted_revenue automatically."""
    company_id = create_test_company(fresh_db)
    opp_id = create_test_opportunity(
        fresh_db, probability="50", expected_revenue="20000.00",
    )

    # Confirm initial weighted = 20000 * 50% = 10000
    get_result = _call_action(
        ACTIONS["get-opportunity"], fresh_db, opportunity_id=opp_id,
    )
    assert get_result["opportunity"]["weighted_revenue"] == "10000.00"

    # Update probability to 80
    update_result = _call_action(
        ACTIONS["update-opportunity"], fresh_db,
        opportunity_id=opp_id,
        probability="80",
    )

    assert update_result["status"] == "ok"
    assert update_result["opportunity"]["weighted_revenue"] == "16000.00"
    assert update_result["opportunity"]["probability"] == "80"


# ---------------------------------------------------------------------------
# 4. test_cannot_update_won_opportunity
# ---------------------------------------------------------------------------

def test_cannot_update_won_opportunity(fresh_db):
    """A won opportunity is in a terminal state and cannot be updated."""
    company_id = create_test_company(fresh_db)
    opp_id = create_test_opportunity(fresh_db)

    # Mark as won
    won_result = _call_action(
        ACTIONS["mark-opportunity-won"], fresh_db, opportunity_id=opp_id,
    )
    assert won_result["status"] == "ok"
    assert won_result["opportunity"]["stage"] == "won"

    # Attempt to update — should error
    update_result = _call_action(
        ACTIONS["update-opportunity"], fresh_db,
        opportunity_id=opp_id,
        probability="90",
    )

    assert update_result["status"] == "error"
    assert "terminal" in update_result["message"].lower() or "won" in update_result["message"].lower()


# ---------------------------------------------------------------------------
# 5. test_get_opportunity_with_activities
# ---------------------------------------------------------------------------

def test_get_opportunity_with_activities(fresh_db):
    """get-opportunity returns activities list, lead info, and customer info."""
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id)
    lead_id = create_test_lead(fresh_db)
    opp_id = create_test_opportunity(
        fresh_db, lead_id=lead_id, customer_id=customer_id,
    )

    # Add 2 activities for the opportunity
    create_test_activity(
        fresh_db,
        activity_type="call",
        subject="Discovery call",
        activity_date="2026-02-15",
        opportunity_id=opp_id,
    )
    create_test_activity(
        fresh_db,
        activity_type="email",
        subject="Follow-up email",
        activity_date="2026-02-16",
        opportunity_id=opp_id,
    )

    result = _call_action(
        ACTIONS["get-opportunity"], fresh_db, opportunity_id=opp_id,
    )

    assert result["status"] == "ok"
    opp = result["opportunity"]

    # Activities
    assert len(opp["activities"]) == 2

    # Lead info present
    assert "lead" in opp
    assert opp["lead"]["id"] == lead_id
    assert opp["lead"]["lead_name"] == "John Doe"

    # Customer info present
    assert "customer" in opp
    assert opp["customer"]["id"] == customer_id
    assert opp["customer"]["name"] == "Acme Corp"


# ---------------------------------------------------------------------------
# 6. test_list_opportunities_by_stage
# ---------------------------------------------------------------------------

def test_list_opportunities_by_stage(fresh_db):
    """List opportunities filtered by stage returns correct subset."""
    company_id = create_test_company(fresh_db)

    opp1_id = create_test_opportunity(fresh_db, opportunity_name="Deal A")
    opp2_id = create_test_opportunity(fresh_db, opportunity_name="Deal B")
    opp3_id = create_test_opportunity(fresh_db, opportunity_name="Deal C")

    # Update opp2 to qualified stage
    _call_action(
        ACTIONS["update-opportunity"], fresh_db,
        opportunity_id=opp2_id,
        stage="qualified",
    )

    # Mark opp3 as won
    _call_action(
        ACTIONS["mark-opportunity-won"], fresh_db,
        opportunity_id=opp3_id,
    )

    # List stage=new — should get only opp1
    new_result = _call_action(
        ACTIONS["list-opportunities"], fresh_db, stage="new",
    )
    assert new_result["status"] == "ok"
    assert new_result["total"] == 1
    assert new_result["opportunities"][0]["id"] == opp1_id

    # List stage=won — should get only opp3
    won_result = _call_action(
        ACTIONS["list-opportunities"], fresh_db, stage="won",
    )
    assert won_result["status"] == "ok"
    assert won_result["total"] == 1
    assert won_result["opportunities"][0]["id"] == opp3_id


# ---------------------------------------------------------------------------
# 7. test_convert_opportunity_to_quotation_mock
# ---------------------------------------------------------------------------

def test_convert_opportunity_to_quotation_mock(fresh_db):
    """Convert opportunity to quotation with mocked subprocess call."""
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id)
    opp_id = create_test_opportunity(
        fresh_db, customer_id=customer_id,
        opportunity_name="Quotation Deal",
        expected_revenue="50000.00",
        probability="75",
    )

    mock_response = {
        "status": "ok",
        "quotation": {
            "id": "QTN-MOCK-001",
            "naming_series": "QTN-2026-00001",
        },
    }

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = json.dumps(mock_response)
    mock_proc.stderr = ""

    items_json = json.dumps([{"item_id": "item1", "qty": "5", "rate": "100.00"}])

    with patch("db_query.subprocess.run", return_value=mock_proc) as mock_run, \
         patch("db_query.os.path.exists", return_value=True):
        result = _call_action(
            ACTIONS["convert-opportunity-to-quotation"], fresh_db,
            opportunity_id=opp_id,
            items=items_json,
        )

    assert result["status"] == "ok"
    assert result["quotation"]["id"] == "QTN-MOCK-001"
    assert result["quotation"]["naming_series"] == "QTN-2026-00001"
    assert result["opportunity_id"] == opp_id

    # Verify quotation_id was stored on opportunity
    row = fresh_db.execute(
        "SELECT quotation_id FROM opportunity WHERE id = ?", (opp_id,)
    ).fetchone()
    assert row["quotation_id"] == "QTN-MOCK-001"


# ---------------------------------------------------------------------------
# 8. test_convert_opportunity_to_quotation_failure
# ---------------------------------------------------------------------------

def test_convert_opportunity_to_quotation_failure(fresh_db):
    """Subprocess failure during quotation conversion returns error."""
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id)
    opp_id = create_test_opportunity(
        fresh_db, customer_id=customer_id,
        opportunity_name="Failing Deal",
    )

    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.stdout = json.dumps({"status": "error", "message": "Item not found"})
    mock_proc.stderr = ""

    items_json = json.dumps([{"item_id": "item1", "qty": "5", "rate": "100.00"}])

    with patch("db_query.subprocess.run", return_value=mock_proc), \
         patch("db_query.os.path.exists", return_value=True):
        result = _call_action(
            ACTIONS["convert-opportunity-to-quotation"], fresh_db,
            opportunity_id=opp_id,
            items=items_json,
        )

    assert result["status"] == "error"
    assert "Item not found" in result["message"] or "Failed" in result["message"]


# ---------------------------------------------------------------------------
# 9. test_mark_opportunity_won
# ---------------------------------------------------------------------------

def test_mark_opportunity_won(fresh_db):
    """Marking won sets probability=100, weighted_revenue = expected_revenue."""
    company_id = create_test_company(fresh_db)
    opp_id = create_test_opportunity(
        fresh_db,
        opportunity_name="Big Win",
        expected_revenue="30000.00",
        probability="70",
    )

    result = _call_action(
        ACTIONS["mark-opportunity-won"], fresh_db, opportunity_id=opp_id,
    )

    assert result["status"] == "ok"
    opp = result["opportunity"]
    assert opp["stage"] == "won"
    assert opp["probability"] == "100"
    assert opp["expected_revenue"] == "30000.00"
    assert opp["weighted_revenue"] == "30000.00"


# ---------------------------------------------------------------------------
# 10. test_mark_opportunity_lost
# ---------------------------------------------------------------------------

def test_mark_opportunity_lost(fresh_db):
    """Marking lost sets probability=0, weighted_revenue=0, lost_reason.

    Also verifies that a lost opportunity cannot be marked as won.
    """
    company_id = create_test_company(fresh_db)
    opp_id = create_test_opportunity(
        fresh_db,
        opportunity_name="Lost Deal",
        expected_revenue="20000.00",
        probability="60",
    )

    result = _call_action(
        ACTIONS["mark-opportunity-lost"], fresh_db,
        opportunity_id=opp_id,
        lost_reason="Budget cut",
    )

    assert result["status"] == "ok"
    opp = result["opportunity"]
    assert opp["stage"] == "lost"
    assert opp["probability"] == "0"
    assert opp["weighted_revenue"] == "0"
    assert opp["lost_reason"] == "Budget cut"

    # Now try to mark the same opportunity as won — should fail
    won_result = _call_action(
        ACTIONS["mark-opportunity-won"], fresh_db, opportunity_id=opp_id,
    )
    assert won_result["status"] == "error"
    assert "terminal" in won_result["message"].lower() or "lost" in won_result["message"].lower()
