"""Tests for item group actions: add-item-group, list-item-groups."""
import pytest
from helpers import _call_action
from db_query import ACTIONS


class TestAddItemGroup:
    """Tests for the add-item-group action."""

    def test_add_item_group(self, fresh_db):
        """Create an item group with a name and verify success."""
        result = _call_action(
            ACTIONS["add-item-group"], fresh_db,
            name="Electronics",
        )
        assert result["status"] == "ok"
        assert result["name"] == "Electronics"
        assert "item_group_id" in result

        # Verify persisted in database
        row = fresh_db.execute(
            "SELECT * FROM item_group WHERE id = ?", (result["item_group_id"],)
        ).fetchone()
        assert row is not None
        assert row["name"] == "Electronics"
        assert row["parent_id"] is None

    def test_add_item_group_duplicate(self, fresh_db):
        """Creating two item groups with the same name should fail (UNIQUE constraint)."""
        result1 = _call_action(
            ACTIONS["add-item-group"], fresh_db,
            name="Duplicate Group",
        )
        assert result1["status"] == "ok"

        result2 = _call_action(
            ACTIONS["add-item-group"], fresh_db,
            name="Duplicate Group",
        )
        assert result2["status"] == "error"
        assert "failed" in result2["message"].lower() or "integrity" in result2["message"].lower()

    def test_add_item_group_with_parent(self, fresh_db):
        """Create a parent group, then a child group referencing the parent."""
        parent_result = _call_action(
            ACTIONS["add-item-group"], fresh_db,
            name="Hardware",
        )
        assert parent_result["status"] == "ok"
        parent_id = parent_result["item_group_id"]

        child_result = _call_action(
            ACTIONS["add-item-group"], fresh_db,
            name="Peripherals",
            parent_id=parent_id,
        )
        assert child_result["status"] == "ok"
        assert child_result["name"] == "Peripherals"

        # Verify the child references the parent in the database
        row = fresh_db.execute(
            "SELECT * FROM item_group WHERE id = ?", (child_result["item_group_id"],)
        ).fetchone()
        assert row["parent_id"] == parent_id


class TestListItemGroups:
    """Tests for the list-item-groups action."""

    def test_list_item_groups(self, fresh_db):
        """Create two item groups and verify both appear in the listing."""
        _call_action(ACTIONS["add-item-group"], fresh_db, name="Category A")
        _call_action(ACTIONS["add-item-group"], fresh_db, name="Category B")

        result = _call_action(
            ACTIONS["list-item-groups"], fresh_db,
        )
        assert result["status"] == "ok"
        names = [ig["name"] for ig in result["item_groups"]]
        assert "Category A" in names
        assert "Category B" in names
        assert len(result["item_groups"]) >= 2
