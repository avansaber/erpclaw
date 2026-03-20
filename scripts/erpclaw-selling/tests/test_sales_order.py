"""Tests for erpclaw-selling sales order lifecycle.

Actions tested:
  - add-sales-order
  - update-sales-order
  - get-sales-order
  - list-sales-orders
  - submit-sales-order
  - cancel-sales-order
"""
import json
import pytest
from decimal import Decimal
from selling_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
    seed_company, seed_customer,
)

mod = load_db_query()


def _items(env, *specs):
    """Build items JSON. Each spec = (item_key, qty, rate)."""
    return json.dumps([
        {"item_id": env[k], "qty": q, "rate": r}
        for k, q, r in specs
    ])


def _create_so(conn, env, items_str=None, **overrides):
    """Helper: create a draft SO and return the result dict."""
    items_str = items_str or _items(env, ("item1", "10", "100.00"))
    defaults = dict(
        customer_id=env["customer"], company_id=env["company_id"],
        posting_date="2026-06-15", items=items_str,
        delivery_date="2026-07-01", tax_template_id=None,
    )
    defaults.update(overrides)
    return call_action(mod.add_sales_order, conn, ns(**defaults))


class TestAddSalesOrder:
    def test_basic_create(self, conn, env):
        result = _create_so(conn, env)
        assert is_ok(result)
        assert "sales_order_id" in result
        assert Decimal(result["total_amount"]) == Decimal("1000.00")
        assert Decimal(result["grand_total"]) == Decimal("1000.00")

        row = conn.execute("SELECT status FROM sales_order WHERE id=?",
                           (result["sales_order_id"],)).fetchone()
        assert row["status"] == "draft"

    def test_multi_item(self, conn, env):
        items = _items(env,
            ("item1", "10", "100.00"),
            ("item2", "5", "200.00"),
        )
        result = _create_so(conn, env, items_str=items)
        assert Decimal(result["total_amount"]) == Decimal("2000.00")

        # Verify line items
        count = conn.execute(
            "SELECT COUNT(*) as cnt FROM sales_order_item WHERE sales_order_id=?",
            (result["sales_order_id"],)
        ).fetchone()["cnt"]
        assert count == 2

    def test_missing_customer_fails(self, conn, env):
        items = _items(env, ("item1", "1", "10.00"))
        result = call_action(mod.add_sales_order, conn, ns(
            customer_id=None, company_id=env["company_id"],
            posting_date="2026-06-15", items=items,
            delivery_date=None, tax_template_id=None,
        ))
        assert is_error(result)

    def test_missing_items_fails(self, conn, env):
        result = call_action(mod.add_sales_order, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", items=None,
            delivery_date=None, tax_template_id=None,
        ))
        assert is_error(result)


class TestUpdateSalesOrder:
    def test_update_delivery_date(self, conn, env):
        so = _create_so(conn, env)
        result = call_action(mod.update_sales_order, conn, ns(
            sales_order_id=so["sales_order_id"],
            delivery_date="2026-08-01", items=None,
        ))
        assert is_ok(result)
        assert "delivery_date" in result["updated_fields"]

    def test_update_items(self, conn, env):
        so = _create_so(conn, env)
        new_items = _items(env, ("item1", "20", "150.00"))
        result = call_action(mod.update_sales_order, conn, ns(
            sales_order_id=so["sales_order_id"],
            delivery_date=None, items=new_items,
        ))
        assert is_ok(result)
        assert "items" in result["updated_fields"]

    def test_update_submitted_fails(self, conn, env):
        so = _create_so(conn, env)
        call_action(mod.submit_sales_order, conn, ns(
            sales_order_id=so["sales_order_id"],
        ))
        result = call_action(mod.update_sales_order, conn, ns(
            sales_order_id=so["sales_order_id"],
            delivery_date="2026-09-01", items=None,
        ))
        assert is_error(result)


class TestGetSalesOrder:
    def test_get_with_items(self, conn, env):
        so = _create_so(conn, env)
        result = call_action(mod.get_sales_order, conn, ns(
            sales_order_id=so["sales_order_id"],
        ))
        assert is_ok(result)
        assert "items" in result
        assert len(result["items"]) >= 1

        # Verify status in DB (ok() overwrites status field)
        row = conn.execute("SELECT status FROM sales_order WHERE id=?",
                           (so["sales_order_id"],)).fetchone()
        assert row["status"] == "draft"

    def test_get_nonexistent_fails(self, conn):
        result = call_action(mod.get_sales_order, conn, ns(
            sales_order_id="fake-id",
        ))
        assert is_error(result)


class TestListSalesOrders:
    def test_list_by_company(self, conn, env):
        _create_so(conn, env)
        result = call_action(mod.list_sales_orders, conn, ns(
            company_id=env["company_id"], customer_id=None,
            doc_status=None, from_date=None, to_date=None,
            search=None, limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1

    def test_list_filter_by_status(self, conn, env):
        so = _create_so(conn, env)
        call_action(mod.submit_sales_order, conn, ns(
            sales_order_id=so["sales_order_id"],
        ))
        result = call_action(mod.list_sales_orders, conn, ns(
            company_id=env["company_id"], customer_id=None,
            doc_status="confirmed", from_date=None, to_date=None,
            search=None, limit=None, offset=None,
        ))
        assert result["total_count"] >= 1


class TestSubmitSalesOrder:
    def test_submit_draft(self, conn, env):
        so = _create_so(conn, env)
        result = call_action(mod.submit_sales_order, conn, ns(
            sales_order_id=so["sales_order_id"],
        ))
        assert is_ok(result)
        assert "naming_series" in result

        row = conn.execute("SELECT status FROM sales_order WHERE id=?",
                           (so["sales_order_id"],)).fetchone()
        assert row["status"] == "confirmed"

    def test_submit_already_confirmed_fails(self, conn, env):
        so = _create_so(conn, env)
        call_action(mod.submit_sales_order, conn, ns(
            sales_order_id=so["sales_order_id"],
        ))
        result = call_action(mod.submit_sales_order, conn, ns(
            sales_order_id=so["sales_order_id"],
        ))
        assert is_error(result)

    def test_credit_limit_exceeded(self, conn, env):
        """Set tight credit limit, create large SO → should fail."""
        conn.execute("UPDATE customer SET credit_limit='100.00' WHERE id=?",
                     (env["customer"],))
        conn.commit()
        so = _create_so(conn, env)  # 10 * 100 = $1000 > $100 limit
        result = call_action(mod.submit_sales_order, conn, ns(
            sales_order_id=so["sales_order_id"],
        ))
        assert is_error(result)
        assert "credit limit" in result.get("message", "").lower()


class TestCancelSalesOrder:
    def test_cancel_confirmed(self, conn, env):
        so = _create_so(conn, env)
        call_action(mod.submit_sales_order, conn, ns(
            sales_order_id=so["sales_order_id"],
        ))
        result = call_action(mod.cancel_sales_order, conn, ns(
            sales_order_id=so["sales_order_id"],
        ))
        assert is_ok(result)

        row = conn.execute("SELECT status FROM sales_order WHERE id=?",
                           (so["sales_order_id"],)).fetchone()
        assert row["status"] == "cancelled"

    def test_cancel_already_cancelled_fails(self, conn, env):
        so = _create_so(conn, env)
        call_action(mod.submit_sales_order, conn, ns(
            sales_order_id=so["sales_order_id"],
        ))
        call_action(mod.cancel_sales_order, conn, ns(
            sales_order_id=so["sales_order_id"],
        ))
        result = call_action(mod.cancel_sales_order, conn, ns(
            sales_order_id=so["sales_order_id"],
        ))
        assert is_error(result)

    def test_cancel_nonexistent_fails(self, conn):
        result = call_action(mod.cancel_sales_order, conn, ns(
            sales_order_id="fake-id",
        ))
        assert is_error(result)
