"""Tests for tax category actions."""
import pytest

from db_query import ACTIONS
from helpers import (
    _call_action,
    create_test_tax_category,
)


def test_add_tax_category(fresh_db):
    """Create a tax category."""
    conn = fresh_db
    result = _call_action(
        ACTIONS["add-tax-category"], conn,
        name="Interstate Sales",
        description="Tax category for interstate sales",
    )
    assert result["status"] == "ok"
    assert result["tax_category_id"]
    assert result["name"] == "Interstate Sales"

    # Verify in DB
    row = conn.execute(
        "SELECT * FROM tax_category WHERE id = ?",
        (result["tax_category_id"],),
    ).fetchone()
    assert row is not None
    assert row["name"] == "Interstate Sales"


def test_list_tax_categories(fresh_db):
    """List tax categories, verifying ordering by name."""
    conn = fresh_db
    create_test_tax_category(conn, name="B Category")
    create_test_tax_category(conn, name="A Category")

    result = _call_action(
        ACTIONS["list-tax-categories"], conn,
    )
    assert result["status"] == "ok"
    assert len(result["categories"]) == 2
    # Should be ordered by name
    assert result["categories"][0]["name"] == "A Category"
    assert result["categories"][1]["name"] == "B Category"
