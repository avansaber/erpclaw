"""Tests for cost center actions.

Test IDs: GL-CC-01 through GL-CC-03
"""
import db_query
from helpers import _call_action, create_test_company


# ---------------------------------------------------------------------------
# GL-CC-01: add-cost-center
# ---------------------------------------------------------------------------
def test_add_cost_center(fresh_db):
    company_id = create_test_company(fresh_db)
    result = _call_action(
        db_query.add_cost_center, fresh_db,
        company_id=company_id, name="Main Cost Center",
    )
    assert result["status"] == "ok"
    assert "cost_center_id" in result
    assert result["name"] == "Main Cost Center"

    # Verify in DB
    row = fresh_db.execute(
        "SELECT * FROM cost_center WHERE id = ?", (result["cost_center_id"],)
    ).fetchone()
    assert row["name"] == "Main Cost Center"
    assert row["company_id"] == company_id
    assert row["is_group"] == 0


# ---------------------------------------------------------------------------
# GL-CC-02: list-cost-centers
# ---------------------------------------------------------------------------
def test_list_cost_centers(fresh_db):
    company_id = create_test_company(fresh_db)
    _call_action(
        db_query.add_cost_center, fresh_db,
        company_id=company_id, name="Sales",
    )
    _call_action(
        db_query.add_cost_center, fresh_db,
        company_id=company_id, name="Engineering",
    )

    result = _call_action(
        db_query.list_cost_centers, fresh_db,
        company_id=company_id,
    )
    assert result["status"] == "ok"
    assert len(result["cost_centers"]) == 2
    names = [cc["name"] for cc in result["cost_centers"]]
    assert "Sales" in names
    assert "Engineering" in names


# ---------------------------------------------------------------------------
# GL-CC-03: hierarchical cost centers (parent_id)
# ---------------------------------------------------------------------------
def test_hierarchical_cost_centers(fresh_db):
    company_id = create_test_company(fresh_db)

    # Create parent group
    parent = _call_action(
        db_query.add_cost_center, fresh_db,
        company_id=company_id, name="Corporate",
        is_group=True,
    )
    parent_id = parent["cost_center_id"]

    # Create child under parent
    child = _call_action(
        db_query.add_cost_center, fresh_db,
        company_id=company_id, name="R&D",
        parent_id=parent_id,
    )
    assert child["status"] == "ok"

    # List with parent_id filter
    result = _call_action(
        db_query.list_cost_centers, fresh_db,
        company_id=company_id, parent_id=parent_id,
    )
    assert result["status"] == "ok"
    assert len(result["cost_centers"]) == 1
    assert result["cost_centers"][0]["name"] == "R&D"
    assert result["cost_centers"][0]["parent_id"] == parent_id
