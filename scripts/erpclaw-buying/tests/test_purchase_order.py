"""Tests for erpclaw-buying purchase order lifecycle.

Actions tested: add-purchase-order, update-purchase-order, get-purchase-order,
                list-purchase-orders, submit-purchase-order, cancel-purchase-order
"""
import json
import pytest
from decimal import Decimal
from buying_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
)

mod = load_db_query()


def _items(env, *specs):
    """Build items JSON. Each spec = (item_key, qty, rate)."""
    return json.dumps([
        {"item_id": env[k], "qty": q, "rate": r, "warehouse_id": env["warehouse"]}
        for k, q, r in specs
    ])


def _create_po(conn, env, items_str=None, **overrides):
    """Create a draft PO and return the result dict."""
    items_str = items_str or _items(env, ("item1", "10", "50.00"))
    defaults = dict(
        supplier_id=env["supplier"], company_id=env["company_id"],
        posting_date="2026-06-15", items=items_str,
        tax_template_id=None, name=None,
    )
    defaults.update(overrides)
    return call_action(mod.add_purchase_order, conn, ns(**defaults))


class TestAddPurchaseOrder:
    def test_basic_create(self, conn, env):
        result = _create_po(conn, env)
        assert is_ok(result)
        assert "purchase_order_id" in result
        assert Decimal(result["total_amount"]) == Decimal("500.00")

    def test_multi_item(self, conn, env):
        items = _items(env,
            ("item1", "10", "50.00"),
            ("item2", "5", "100.00"),
        )
        result = _create_po(conn, env, items_str=items)
        assert is_ok(result)
        assert Decimal(result["total_amount"]) == Decimal("1000.00")

    def test_missing_supplier_fails(self, conn, env):
        items = _items(env, ("item1", "1", "10.00"))
        result = call_action(mod.add_purchase_order, conn, ns(
            supplier_id=None, company_id=env["company_id"],
            posting_date="2026-06-15", items=items,
            tax_template_id=None, name=None,
        ))
        assert is_error(result)

    def test_missing_items_fails(self, conn, env):
        result = call_action(mod.add_purchase_order, conn, ns(
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-06-15", items=None,
            tax_template_id=None, name=None,
        ))
        assert is_error(result)


class TestUpdatePurchaseOrder:
    def test_update_items(self, conn, env):
        po = _create_po(conn, env)
        new_items = _items(env, ("item1", "20", "75.00"))
        result = call_action(mod.update_purchase_order, conn, ns(
            purchase_order_id=po["purchase_order_id"],
            company_id=env["company_id"],
            items=new_items, posting_date=None,
            tax_template_id=None, supplier_id=None, name=None,
        ))
        assert is_ok(result)
        assert Decimal(result["total_amount"]) == Decimal("1500.00")


class TestGetPurchaseOrder:
    def test_get_with_items(self, conn, env):
        po = _create_po(conn, env)
        result = call_action(mod.get_purchase_order, conn, ns(
            purchase_order_id=po["purchase_order_id"],
            company_id=env["company_id"],
        ))
        assert is_ok(result)
        assert "items" in result

    def test_get_nonexistent_fails(self, conn, env):
        result = call_action(mod.get_purchase_order, conn, ns(
            purchase_order_id="fake-id",
            company_id=env["company_id"],
        ))
        assert is_error(result)


class TestListPurchaseOrders:
    def test_list(self, conn, env):
        _create_po(conn, env)
        result = call_action(mod.list_purchase_orders, conn, ns(
            company_id=env["company_id"], search=None,
            from_date=None, to_date=None,
            po_status=None, supplier_id=None,
            limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1


class TestSubmitPurchaseOrder:
    def test_submit_draft(self, conn, env):
        po = _create_po(conn, env)
        result = call_action(mod.submit_purchase_order, conn, ns(
            purchase_order_id=po["purchase_order_id"],
        ))
        assert is_ok(result)

        row = conn.execute("SELECT status FROM purchase_order WHERE id=?",
                           (po["purchase_order_id"],)).fetchone()
        assert row["status"] == "confirmed"

    def test_submit_already_confirmed_fails(self, conn, env):
        po = _create_po(conn, env)
        call_action(mod.submit_purchase_order, conn, ns(
            purchase_order_id=po["purchase_order_id"],
        ))
        result = call_action(mod.submit_purchase_order, conn, ns(
            purchase_order_id=po["purchase_order_id"],
        ))
        assert is_error(result)


class TestCancelPurchaseOrder:
    def test_cancel_confirmed(self, conn, env):
        po = _create_po(conn, env)
        call_action(mod.submit_purchase_order, conn, ns(
            purchase_order_id=po["purchase_order_id"],
        ))
        result = call_action(mod.cancel_purchase_order, conn, ns(
            purchase_order_id=po["purchase_order_id"],
        ))
        assert is_ok(result)

        row = conn.execute("SELECT status FROM purchase_order WHERE id=?",
                           (po["purchase_order_id"],)).fetchone()
        assert row["status"] == "cancelled"

    def test_cancel_nonexistent_fails(self, conn):
        result = call_action(mod.cancel_purchase_order, conn, ns(
            purchase_order_id="fake-id",
        ))
        assert is_error(result)
