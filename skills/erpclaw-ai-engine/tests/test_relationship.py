"""Tests for relationship scoring and listing.

5 tests covering:
  - score_relationship with no invoice history (default scores)
  - score_relationship with paid invoices (payment_score=100)
  - score_relationship for supplier party type
  - list_relationship_scores filtered by party_type
  - volume_trend field validation
"""
from helpers import (
    _call_action,
    setup_ai_environment,
    create_test_sales_invoice,
    create_test_purchase_invoice,
)
from db_query import score_relationship, list_relationship_scores


# ---------------------------------------------------------------------------
# 1. Score customer with no history -- default scores
# ---------------------------------------------------------------------------

def test_score_customer_no_history(fresh_db):
    """Score a customer with no invoices. Should return defaults:
    overall_score='50', payment_score='50', volume_trend='stable',
    lifetime_value='0'."""
    env = setup_ai_environment(fresh_db)

    result = _call_action(
        score_relationship, fresh_db,
        party_type="customer",
        party_id=env["customer_id"],
    )

    assert result["status"] == "ok"
    rs = result["relationship_score"]
    assert rs["party_type"] == "customer"
    assert rs["party_id"] == env["customer_id"]
    assert rs["overall_score"] == "50"
    assert rs["payment_score"] == "50"
    assert rs["volume_trend"] == "stable"
    assert rs["lifetime_value"] == "0"


# ---------------------------------------------------------------------------
# 2. Score customer with paid invoices -- payment_score=100
# ---------------------------------------------------------------------------

def test_score_customer_with_invoices(fresh_db):
    """Create 2 sales invoices with outstanding=0 (fully paid).
    Score should have payment_score=100, lifetime_value='2000.0',
    overall_score > 50."""
    env = setup_ai_environment(fresh_db)

    # Two fully paid invoices
    create_test_sales_invoice(
        fresh_db, env["company_id"], env["customer_id"],
        grand_total="1000", outstanding="0",
        posting_date="2026-01-01", due_date="2026-01-31",
        status="submitted",
    )
    create_test_sales_invoice(
        fresh_db, env["company_id"], env["customer_id"],
        grand_total="1000", outstanding="0",
        posting_date="2026-01-15", due_date="2026-02-15",
        status="submitted",
    )

    result = _call_action(
        score_relationship, fresh_db,
        party_type="customer",
        party_id=env["customer_id"],
    )

    assert result["status"] == "ok"
    rs = result["relationship_score"]
    # No overdue invoices => payment_score = 100
    assert rs["payment_score"] == "100"
    # Lifetime value = 1000 + 1000 = 2000
    assert float(rs["lifetime_value"]) == 2000.0
    # Overall score should be above default 50
    assert int(rs["overall_score"]) > 50


# ---------------------------------------------------------------------------
# 3. Score supplier -- validates supplier party type works
# ---------------------------------------------------------------------------

def test_score_supplier(fresh_db):
    """Create a purchase invoice for supplier, score with party_type='supplier'.
    Verify it returns a valid relationship_score."""
    env = setup_ai_environment(fresh_db)

    create_test_purchase_invoice(
        fresh_db, env["company_id"], env["supplier_id"],
        grand_total="500", outstanding="0",
        posting_date="2026-01-10", due_date="2026-02-10",
        status="submitted",
    )

    result = _call_action(
        score_relationship, fresh_db,
        party_type="supplier",
        party_id=env["supplier_id"],
    )

    assert result["status"] == "ok"
    rs = result["relationship_score"]
    assert rs["party_type"] == "supplier"
    assert rs["party_id"] == env["supplier_id"]
    assert "overall_score" in rs
    assert "payment_score" in rs
    assert "lifetime_value" in rs
    assert float(rs["lifetime_value"]) == 500.0


# ---------------------------------------------------------------------------
# 4. List by party_type -- filter returns only matching type
# ---------------------------------------------------------------------------

def test_list_by_party_type(fresh_db):
    """Score both a customer and a supplier. Listing with
    party_type='customer' should return only 1 result."""
    env = setup_ai_environment(fresh_db)

    # Score customer (no invoices -> defaults)
    _call_action(
        score_relationship, fresh_db,
        party_type="customer",
        party_id=env["customer_id"],
    )

    # Score supplier (no invoices -> defaults)
    _call_action(
        score_relationship, fresh_db,
        party_type="supplier",
        party_id=env["supplier_id"],
    )

    # List only customer scores
    result = _call_action(
        list_relationship_scores, fresh_db,
        party_type="customer",
    )

    assert result["status"] == "ok"
    assert result["total_count"] == 1
    assert result["relationship_scores"][0]["party_type"] == "customer"


# ---------------------------------------------------------------------------
# 5. Volume trend calculated -- verify field has valid value
# ---------------------------------------------------------------------------

def test_volume_trend_calculated(fresh_db):
    """Score a customer with invoices. Verify volume_trend is one of
    'growing', 'stable', or 'declining'."""
    env = setup_ai_environment(fresh_db)

    # Create invoices so the scoring engine has data for volume_trend calc
    create_test_sales_invoice(
        fresh_db, env["company_id"], env["customer_id"],
        grand_total="2000", outstanding="0",
        posting_date="2026-01-05", due_date="2026-02-05",
        status="submitted",
    )
    create_test_sales_invoice(
        fresh_db, env["company_id"], env["customer_id"],
        grand_total="3000", outstanding="0",
        posting_date="2026-01-20", due_date="2026-02-20",
        status="submitted",
    )

    result = _call_action(
        score_relationship, fresh_db,
        party_type="customer",
        party_id=env["customer_id"],
    )

    assert result["status"] == "ok"
    rs = result["relationship_score"]
    assert rs["volume_trend"] in ("growing", "stable", "declining")
