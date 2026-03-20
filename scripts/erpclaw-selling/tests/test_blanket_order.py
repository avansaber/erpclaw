"""Tests for blanket sales order actions.

Actions tested:
  - add-blanket-order
  - submit-blanket-order
  - get-blanket-order
  - list-blanket-orders
  - create-so-from-blanket
"""
import json
import pytest
from decimal import Decimal
from selling_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
)

mod = load_db_query()


def _items(env, *specs):
    """Build items JSON. Each spec = (item_key, qty, rate)."""
    return json.dumps([
        {"item_id": env[k], "qty": q, "rate": r}
        for k, q, r in specs
    ])


def _create_blanket(conn, env, items_str=None, **overrides):
    """Create a draft blanket order and return the result dict."""
    items_str = items_str or _items(env, ("item1", "100", "50.00"))
    defaults = dict(
        customer_id=env["customer"], company_id=env["company_id"],
        items=items_str, valid_from="2026-01-01", valid_to="2027-12-31",
        blanket_order_id=None, doc_status=None,
        tax_template_id=None, posting_date=None, delivery_date=None,
        valid_till=None, name=None,
    )
    defaults.update(overrides)
    return call_action(mod.add_blanket_order, conn, ns(**defaults))


def _submit_blanket(conn, env, blanket_order_id):
    """Submit a blanket order."""
    return call_action(mod.submit_blanket_order, conn, ns(
        blanket_order_id=blanket_order_id,
    ))


# --------------------------------------------------------------------------
# add-blanket-order
# --------------------------------------------------------------------------

class TestAddBlanketOrder:
    def test_basic_create(self, conn, env):
        result = _create_blanket(conn, env)
        assert is_ok(result)
        assert "blanket_order_id" in result
        assert Decimal(result["total_qty"]) == Decimal("100")
        assert result["valid_from"] == "2026-01-01"
        assert result["valid_to"] == "2027-12-31"

    def test_multi_item(self, conn, env):
        items = _items(env,
            ("item1", "100", "50.00"),
            ("item2", "50", "100.00"),
        )
        result = _create_blanket(conn, env, items_str=items)
        assert is_ok(result)
        assert Decimal(result["total_qty"]) == Decimal("150")

    def test_missing_customer_fails(self, conn, env):
        result = call_action(mod.add_blanket_order, conn, ns(
            customer_id=None, company_id=env["company_id"],
            items=_items(env, ("item1", "10", "50.00")),
            valid_from="2026-01-01", valid_to="2027-12-31",
            blanket_order_id=None, doc_status=None,
            tax_template_id=None, posting_date=None, delivery_date=None,
            valid_till=None, name=None,
        ))
        assert is_error(result)

    def test_invalid_dates_fails(self, conn, env):
        result = _create_blanket(conn, env,
            valid_from="2027-01-01", valid_to="2026-01-01")
        assert is_error(result)

    def test_missing_items_fails(self, conn, env):
        result = call_action(mod.add_blanket_order, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            items=None, valid_from="2026-01-01", valid_to="2027-12-31",
            blanket_order_id=None, doc_status=None,
            tax_template_id=None, posting_date=None, delivery_date=None,
            valid_till=None, name=None,
        ))
        assert is_error(result)


# --------------------------------------------------------------------------
# submit-blanket-order
# --------------------------------------------------------------------------

class TestSubmitBlanketOrder:
    def test_submit(self, conn, env):
        bo = _create_blanket(conn, env)
        result = _submit_blanket(conn, env, bo["blanket_order_id"])
        assert is_ok(result)
        assert result["doc_status"] == "active"

    def test_submit_already_active_fails(self, conn, env):
        bo = _create_blanket(conn, env)
        _submit_blanket(conn, env, bo["blanket_order_id"])
        result = _submit_blanket(conn, env, bo["blanket_order_id"])
        assert is_error(result)


# --------------------------------------------------------------------------
# get-blanket-order
# --------------------------------------------------------------------------

class TestGetBlanketOrder:
    def test_get_with_items(self, conn, env):
        items = _items(env, ("item1", "100", "50.00"), ("item2", "50", "100.00"))
        bo = _create_blanket(conn, env, items_str=items)
        result = call_action(mod.get_blanket_order, conn, ns(
            blanket_order_id=bo["blanket_order_id"],
        ))
        assert is_ok(result)
        assert result["blanket_order_type"] == "selling"
        assert len(result["items"]) == 2

    def test_not_found(self, conn, env):
        result = call_action(mod.get_blanket_order, conn, ns(
            blanket_order_id="nonexistent",
        ))
        assert is_error(result)


# --------------------------------------------------------------------------
# list-blanket-orders
# --------------------------------------------------------------------------

class TestListBlanketOrders:
    def test_list(self, conn, env):
        _create_blanket(conn, env)
        result = call_action(mod.list_blanket_orders, conn, ns(
            company_id=env["company_id"], customer_id=None,
            doc_status=None, limit="20", offset="0",
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1
        assert all(bo["blanket_order_type"] == "selling"
                   for bo in result["blanket_orders"])


# --------------------------------------------------------------------------
# create-so-from-blanket
# --------------------------------------------------------------------------

class TestCreateSOFromBlanket:
    def test_create_so(self, conn, env):
        bo = _create_blanket(conn, env)
        _submit_blanket(conn, env, bo["blanket_order_id"])

        so_items = json.dumps([
            {"item_id": env["item1"], "qty": "30"}
        ])
        result = call_action(mod.create_so_from_blanket, conn, ns(
            blanket_order_id=bo["blanket_order_id"],
            items=so_items, posting_date="2026-06-01",
            tax_template_id=None, delivery_date=None,
            company_id=env["company_id"],
        ))
        assert is_ok(result)
        assert "sales_order_id" in result
        assert Decimal(result["total_amount"]) == Decimal("1500.00")

    def test_exceeds_blanket_qty_fails(self, conn, env):
        bo = _create_blanket(conn, env)
        _submit_blanket(conn, env, bo["blanket_order_id"])

        so_items = json.dumps([
            {"item_id": env["item1"], "qty": "200"}
        ])
        result = call_action(mod.create_so_from_blanket, conn, ns(
            blanket_order_id=bo["blanket_order_id"],
            items=so_items, posting_date="2026-06-01",
            tax_template_id=None, delivery_date=None,
            company_id=env["company_id"],
        ))
        assert is_error(result)

    def test_from_draft_blanket_fails(self, conn, env):
        bo = _create_blanket(conn, env)
        so_items = json.dumps([{"item_id": env["item1"], "qty": "10"}])
        result = call_action(mod.create_so_from_blanket, conn, ns(
            blanket_order_id=bo["blanket_order_id"],
            items=so_items, posting_date="2026-06-01",
            tax_template_id=None, delivery_date=None,
            company_id=env["company_id"],
        ))
        assert is_error(result)

    def test_item_not_in_blanket_fails(self, conn, env):
        bo = _create_blanket(conn, env)
        _submit_blanket(conn, env, bo["blanket_order_id"])

        so_items = json.dumps([
            {"item_id": env["item2"], "qty": "10"}  # item2 not in blanket
        ])
        result = call_action(mod.create_so_from_blanket, conn, ns(
            blanket_order_id=bo["blanket_order_id"],
            items=so_items, posting_date="2026-06-01",
            tax_template_id=None, delivery_date=None,
            company_id=env["company_id"],
        ))
        assert is_error(result)

    def test_cumulative_drawdown(self, conn, env):
        """Multiple SOs should accumulate ordered_qty on blanket."""
        bo = _create_blanket(conn, env)
        _submit_blanket(conn, env, bo["blanket_order_id"])

        # First SO: 40 of 100
        so_items1 = json.dumps([{"item_id": env["item1"], "qty": "40"}])
        r1 = call_action(mod.create_so_from_blanket, conn, ns(
            blanket_order_id=bo["blanket_order_id"],
            items=so_items1, posting_date="2026-06-01",
            tax_template_id=None, delivery_date=None,
            company_id=env["company_id"],
        ))
        assert is_ok(r1)

        # Second SO: 50 of remaining 60
        so_items2 = json.dumps([{"item_id": env["item1"], "qty": "50"}])
        r2 = call_action(mod.create_so_from_blanket, conn, ns(
            blanket_order_id=bo["blanket_order_id"],
            items=so_items2, posting_date="2026-06-02",
            tax_template_id=None, delivery_date=None,
            company_id=env["company_id"],
        ))
        assert is_ok(r2)

        # Third SO: 20 of remaining 10 -> should fail
        so_items3 = json.dumps([{"item_id": env["item1"], "qty": "20"}])
        r3 = call_action(mod.create_so_from_blanket, conn, ns(
            blanket_order_id=bo["blanket_order_id"],
            items=so_items3, posting_date="2026-06-03",
            tax_template_id=None, delivery_date=None,
            company_id=env["company_id"],
        ))
        assert is_error(r3)

    def test_expired_blanket_fails(self, conn, env):
        """Cannot create SO from an expired blanket order."""
        bo = _create_blanket(conn, env, valid_from="2024-01-01", valid_to="2024-12-31")
        _submit_blanket(conn, env, bo["blanket_order_id"])

        so_items = json.dumps([{"item_id": env["item1"], "qty": "10"}])
        result = call_action(mod.create_so_from_blanket, conn, ns(
            blanket_order_id=bo["blanket_order_id"],
            items=so_items, posting_date="2026-06-01",
            tax_template_id=None, delivery_date=None,
            company_id=env["company_id"],
        ))
        assert is_error(result)
