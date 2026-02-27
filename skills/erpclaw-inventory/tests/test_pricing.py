"""Tests for pricing: add-price-list, add-item-price, get-item-price, add-pricing-rule.

7 tests covering price list creation, duplicate detection, item price CRUD,
quantity-tier pricing lookups, and pricing rule creation.
"""
import json
import uuid

from db_query import ACTIONS
from helpers import (
    _call_action,
    create_test_company,
    create_test_item,
)


def _create_price_list(conn, name="Standard Selling", currency="USD",
                       is_buying="0", is_selling="1"):
    """Helper: create a price list and return the price_list_id."""
    result = _call_action(
        ACTIONS["add-price-list"], conn,
        name=name,
        currency=currency,
        is_buying=is_buying,
        is_selling=is_selling,
    )
    assert result["status"] == "ok", f"_create_price_list failed: {result}"
    return result["price_list_id"]


# ---------------------------------------------------------------------------
# 1. test_add_price_list
# ---------------------------------------------------------------------------

def test_add_price_list(fresh_db):
    """Create a selling price list and verify ok response."""
    result = _call_action(
        ACTIONS["add-price-list"], fresh_db,
        name="Standard Selling",
        currency="USD",
        is_buying="0",
        is_selling="1",
    )

    assert result["status"] == "ok"
    assert "price_list_id" in result
    assert result["name"] == "Standard Selling"


# ---------------------------------------------------------------------------
# 2. test_add_price_list_duplicate
# ---------------------------------------------------------------------------

def test_add_price_list_duplicate(fresh_db):
    """Creating a price list with the same name should fail."""
    # First creation succeeds
    result1 = _call_action(
        ACTIONS["add-price-list"], fresh_db,
        name="Wholesale",
        currency="USD",
        is_buying="0",
        is_selling="1",
    )
    assert result1["status"] == "ok"

    # Second creation with same name should error
    result2 = _call_action(
        ACTIONS["add-price-list"], fresh_db,
        name="Wholesale",
        currency="USD",
        is_buying="0",
        is_selling="1",
    )

    assert result2["status"] == "error"
    assert "failed" in result2["message"].lower() or "unique" in result2["message"].lower()


# ---------------------------------------------------------------------------
# 3. test_add_item_price
# ---------------------------------------------------------------------------

def test_add_item_price(fresh_db):
    """Create an item price and verify ok response."""
    item_id = create_test_item(fresh_db)
    pl_id = _create_price_list(fresh_db)

    result = _call_action(
        ACTIONS["add-item-price"], fresh_db,
        item_id=item_id,
        price_list_id=pl_id,
        rate="30.00",
        min_qty="1",
    )

    assert result["status"] == "ok"
    assert "item_price_id" in result
    assert result["rate"] == "30.00"


# ---------------------------------------------------------------------------
# 4. test_add_item_price_invalid_item
# ---------------------------------------------------------------------------

def test_add_item_price_invalid_item(fresh_db):
    """Adding an item price for a non-existent item should error."""
    pl_id = _create_price_list(fresh_db)
    fake_item_id = str(uuid.uuid4())

    result = _call_action(
        ACTIONS["add-item-price"], fresh_db,
        item_id=fake_item_id,
        price_list_id=pl_id,
        rate="30.00",
    )

    assert result["status"] == "error"
    assert "not found" in result["message"].lower()


# ---------------------------------------------------------------------------
# 5. test_get_item_price
# ---------------------------------------------------------------------------

def test_get_item_price(fresh_db):
    """Create a price, then retrieve it via get-item-price."""
    item_id = create_test_item(fresh_db)
    pl_id = _create_price_list(fresh_db)

    add_result = _call_action(
        ACTIONS["add-item-price"], fresh_db,
        item_id=item_id,
        price_list_id=pl_id,
        rate="42.50",
        min_qty="1",
    )
    assert add_result["status"] == "ok"

    get_result = _call_action(
        ACTIONS["get-item-price"], fresh_db,
        item_id=item_id,
        price_list_id=pl_id,
        qty="1",
    )

    assert get_result["status"] == "ok"
    assert get_result["item_id"] == item_id
    assert get_result["price_list_id"] == pl_id
    assert get_result["rate"] == "42.50"


# ---------------------------------------------------------------------------
# 6. test_get_item_price_qty_tier
# ---------------------------------------------------------------------------

def test_get_item_price_qty_tier(fresh_db):
    """Create two prices with different min_qty, verify correct tier returned."""
    item_id = create_test_item(fresh_db)
    pl_id = _create_price_list(fresh_db)

    # Tier 1: min_qty = 1, rate = 50.00
    r1 = _call_action(
        ACTIONS["add-item-price"], fresh_db,
        item_id=item_id,
        price_list_id=pl_id,
        rate="50.00",
        min_qty="1",
    )
    assert r1["status"] == "ok"

    # Tier 2: min_qty = 100, rate = 40.00 (bulk discount)
    r2 = _call_action(
        ACTIONS["add-item-price"], fresh_db,
        item_id=item_id,
        price_list_id=pl_id,
        rate="40.00",
        min_qty="100",
    )
    assert r2["status"] == "ok"

    # Query for qty=5: should return tier 1 (min_qty=1, rate=50.00)
    small_result = _call_action(
        ACTIONS["get-item-price"], fresh_db,
        item_id=item_id,
        price_list_id=pl_id,
        qty="5",
    )
    assert small_result["status"] == "ok"
    assert small_result["rate"] == "50.00"
    assert small_result["min_qty"] == "1"

    # Query for qty=200: should return tier 2 (min_qty=100, rate=40.00)
    bulk_result = _call_action(
        ACTIONS["get-item-price"], fresh_db,
        item_id=item_id,
        price_list_id=pl_id,
        qty="200",
    )
    assert bulk_result["status"] == "ok"
    assert bulk_result["rate"] == "40.00"
    assert bulk_result["min_qty"] == "100"


# ---------------------------------------------------------------------------
# 7. test_add_pricing_rule
# ---------------------------------------------------------------------------

def test_add_pricing_rule(fresh_db):
    """Create a discount pricing rule and verify ok response."""
    item_id = create_test_item(fresh_db)
    company_id = create_test_company(fresh_db, name="Pricing Co", abbr="PC")

    result = _call_action(
        ACTIONS["add-pricing-rule"], fresh_db,
        name="10% Item Discount",
        applies_to="item",
        entity_id=item_id,
        discount_percentage="10",
        company_id=company_id,
    )

    assert result["status"] == "ok"
    assert "pricing_rule_id" in result
    assert result["name"] == "10% Item Discount"

    # Verify the rule was persisted in the database
    row = fresh_db.execute(
        "SELECT * FROM pricing_rule WHERE id = ?",
        (result["pricing_rule_id"],),
    ).fetchone()
    assert row is not None
    assert row["applies_to"] == "item"
    assert row["entity_id"] == item_id
    assert row["discount_percentage"] == "10"
    assert row["company_id"] == company_id
