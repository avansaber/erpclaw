"""Tests for material request actions: add-material-request, submit-material-request, list-material-requests."""
import json

import pytest
from helpers import (
    _call_action,
    create_test_company,
    create_test_item,
    create_test_naming_series,
    setup_buying_environment,
)
from db_query import ACTIONS


class TestAddMaterialRequest:
    """Tests for the add-material-request action."""

    def test_add_material_request(self, fresh_db):
        """Create a material request with items and verify success."""
        company_id = create_test_company(fresh_db)
        item_id = create_test_item(fresh_db)

        items_json = json.dumps([{"item_id": item_id, "qty": "10"}])
        result = _call_action(
            ACTIONS["add-material-request"], fresh_db,
            request_type="purchase",
            items=items_json,
            company_id=company_id,
        )
        assert result["status"] == "ok"
        assert "material_request_id" in result
        assert result["request_type"] == "purchase"
        assert result["item_count"] == 1

        # Verify persisted in database
        mr = fresh_db.execute(
            "SELECT * FROM material_request WHERE id = ?",
            (result["material_request_id"],),
        ).fetchone()
        assert mr is not None
        assert mr["status"] == "draft"
        assert mr["request_type"] == "purchase"
        assert mr["company_id"] == company_id

        # Verify items
        items = fresh_db.execute(
            "SELECT * FROM material_request_item WHERE material_request_id = ?",
            (result["material_request_id"],),
        ).fetchall()
        assert len(items) == 1
        assert items[0]["item_id"] == item_id
        assert items[0]["quantity"] == "10.00"

    def test_add_material_request_missing_items(self, fresh_db):
        """Creating a material request without --items should return an error."""
        company_id = create_test_company(fresh_db)

        result = _call_action(
            ACTIONS["add-material-request"], fresh_db,
            request_type="purchase",
            company_id=company_id,
        )
        assert result["status"] == "error"
        assert "items" in result["message"].lower()


class TestSubmitMaterialRequest:
    """Tests for the submit-material-request action."""

    def test_submit_material_request(self, fresh_db):
        """Submit a material request and verify status becomes submitted."""
        company_id = create_test_company(fresh_db)
        create_test_naming_series(fresh_db, company_id)
        item_id = create_test_item(fresh_db)

        # Create a draft MR
        items_json = json.dumps([{"item_id": item_id, "qty": "5"}])
        add_result = _call_action(
            ACTIONS["add-material-request"], fresh_db,
            request_type="purchase",
            items=items_json,
            company_id=company_id,
        )
        assert add_result["status"] == "ok"
        mr_id = add_result["material_request_id"]

        # Submit
        result = _call_action(
            ACTIONS["submit-material-request"], fresh_db,
            material_request_id=mr_id,
        )
        assert result["status"] == "ok"
        assert result["material_request_id"] == mr_id
        assert result["status"] == "ok"
        assert "naming_series" in result

        # Verify status in database
        mr = fresh_db.execute(
            "SELECT * FROM material_request WHERE id = ?", (mr_id,)
        ).fetchone()
        assert mr["status"] == "submitted"
        assert mr["naming_series"] is not None


class TestListMaterialRequests:
    """Tests for the list-material-requests action."""

    def test_list_material_requests(self, fresh_db):
        """Create 2 material requests and verify list returns both."""
        company_id = create_test_company(fresh_db)
        item_id = create_test_item(fresh_db)

        items_json = json.dumps([{"item_id": item_id, "qty": "10"}])

        # Create two MRs
        r1 = _call_action(
            ACTIONS["add-material-request"], fresh_db,
            request_type="purchase",
            items=items_json,
            company_id=company_id,
        )
        assert r1["status"] == "ok"

        r2 = _call_action(
            ACTIONS["add-material-request"], fresh_db,
            request_type="purchase",
            items=items_json,
            company_id=company_id,
        )
        assert r2["status"] == "ok"

        # List
        result = _call_action(
            ACTIONS["list-material-requests"], fresh_db,
            company_id=company_id,
        )
        assert result["status"] == "ok"
        assert result["total_count"] == 2
        assert len(result["material_requests"]) == 2
