"""Tests for warehouse actions: add-warehouse, update-warehouse, list-warehouses."""
import pytest
from helpers import (
    _call_action,
    create_test_company,
    create_test_account,
    create_test_warehouse,
)
from db_query import ACTIONS


class TestAddWarehouse:
    """Tests for the add-warehouse action."""

    def test_add_warehouse(self, fresh_db):
        """Create a warehouse with a valid company_id and verify success."""
        company_id = create_test_company(fresh_db)

        result = _call_action(
            ACTIONS["add-warehouse"], fresh_db,
            name="Main Store",
            company_id=company_id,
        )
        assert result["status"] == "ok"
        assert result["name"] == "Main Store"
        assert "warehouse_id" in result

        # Verify persisted in database
        row = fresh_db.execute(
            "SELECT * FROM warehouse WHERE id = ?", (result["warehouse_id"],)
        ).fetchone()
        assert row is not None
        assert row["name"] == "Main Store"
        assert row["company_id"] == company_id
        assert row["warehouse_type"] == "stores"  # default

    def test_add_warehouse_with_account(self, fresh_db):
        """Create a warehouse linked to a stock account."""
        company_id = create_test_company(fresh_db)
        acct_id = create_test_account(
            fresh_db, company_id, "Stock In Hand", "asset",
            account_type="stock",
        )

        result = _call_action(
            ACTIONS["add-warehouse"], fresh_db,
            name="Linked Warehouse",
            company_id=company_id,
            account_id=acct_id,
        )
        assert result["status"] == "ok"
        assert result["name"] == "Linked Warehouse"

        # Verify the account_id is stored
        row = fresh_db.execute(
            "SELECT * FROM warehouse WHERE id = ?", (result["warehouse_id"],)
        ).fetchone()
        assert row["account_id"] == acct_id

    def test_add_warehouse_invalid_company(self, fresh_db):
        """Creating a warehouse with a non-existent company should return an error."""
        result = _call_action(
            ACTIONS["add-warehouse"], fresh_db,
            name="Orphan Warehouse",
            company_id="00000000-0000-0000-0000-000000000000",
        )
        assert result["status"] == "error"
        assert "not found" in result["message"].lower()


class TestUpdateWarehouse:
    """Tests for the update-warehouse action."""

    def test_update_warehouse(self, fresh_db):
        """Update the name of an existing warehouse."""
        company_id = create_test_company(fresh_db)
        wh_id = create_test_warehouse(fresh_db, company_id, name="Old Name")

        result = _call_action(
            ACTIONS["update-warehouse"], fresh_db,
            warehouse_id=wh_id,
            name="New Name",
        )
        assert result["status"] == "ok"
        assert result["warehouse_id"] == wh_id
        assert "name" in result["updated_fields"]

        # Verify in database
        row = fresh_db.execute(
            "SELECT * FROM warehouse WHERE id = ?", (wh_id,)
        ).fetchone()
        assert row["name"] == "New Name"

    def test_update_warehouse_not_found(self, fresh_db):
        """Updating a non-existent warehouse should return an error."""
        result = _call_action(
            ACTIONS["update-warehouse"], fresh_db,
            warehouse_id="00000000-0000-0000-0000-000000000000",
            name="Ghost Warehouse",
        )
        assert result["status"] == "error"
        assert "not found" in result["message"].lower()


class TestListWarehouses:
    """Tests for the list-warehouses action."""

    def test_list_warehouses(self, fresh_db):
        """Create two warehouses for a company and verify both appear in the listing."""
        company_id = create_test_company(fresh_db)
        create_test_warehouse(fresh_db, company_id, name="Warehouse Alpha")
        create_test_warehouse(fresh_db, company_id, name="Warehouse Beta")

        result = _call_action(
            ACTIONS["list-warehouses"], fresh_db,
            company_id=company_id,
        )
        assert result["status"] == "ok"
        names = [wh["name"] for wh in result["warehouses"]]
        assert "Warehouse Alpha" in names
        assert "Warehouse Beta" in names
        assert len(result["warehouses"]) == 2
