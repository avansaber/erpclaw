"""Tests for back-to-back order actions (SO -> PO).

Actions tested:
  - create-po-from-so
"""
import json
import pytest
import uuid
from decimal import Decimal
from buying_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
    seed_supplier,
)

mod = load_db_query()


def _seed_customer(conn, company_id, name="Test Customer"):
    """Insert a customer for SO creation."""
    cid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO customer (id, name, company_id, customer_type, status, credit_limit)
           VALUES (?, ?, ?, 'company', 'active', '0')""",
        (cid, name, company_id)
    )
    conn.commit()
    return cid


def _seed_sales_order(conn, customer_id, company_id, items):
    """Insert a sales order with items directly for testing.
    items = list of (item_id, qty, rate, uom, warehouse_id)
    """
    so_id = str(uuid.uuid4())
    total = Decimal("0")
    for item_id, qty, rate, uom, wh in items:
        total += Decimal(str(qty)) * Decimal(str(rate))
    conn.execute(
        """INSERT INTO sales_order
           (id, customer_id, order_date, total_amount, tax_amount, grand_total,
            status, company_id)
           VALUES (?, ?, '2026-06-01', ?, '0', ?, 'confirmed', ?)""",
        (so_id, customer_id, str(total), str(total), company_id)
    )
    for item_id, qty, rate, uom, wh in items:
        soi_id = str(uuid.uuid4())
        amount = str(Decimal(str(qty)) * Decimal(str(rate)))
        conn.execute(
            """INSERT INTO sales_order_item
               (id, sales_order_id, item_id, quantity, uom, rate, amount,
                discount_percentage, net_amount, warehouse_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, '0', ?, ?)""",
            (soi_id, so_id, item_id, str(qty), uom, str(rate), amount, amount, wh)
        )
    conn.commit()
    return so_id


def _seed_item_supplier(conn, item_id, supplier_id, priority=0):
    """Map item to supplier."""
    isid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO item_supplier (id, item_id, supplier_id, priority)
           VALUES (?, ?, ?, ?)""",
        (isid, item_id, supplier_id, priority)
    )
    conn.commit()
    return isid


class TestCreatePOFromSO:
    def test_basic_back_to_back(self, conn, env):
        """Single supplier for all items -> one PO."""
        customer_id = _seed_customer(conn, env["company_id"])
        _seed_item_supplier(conn, env["item1"], env["supplier"])
        _seed_item_supplier(conn, env["item2"], env["supplier"])

        so_id = _seed_sales_order(conn, customer_id, env["company_id"], [
            (env["item1"], "10", "50.00", "Each", env["warehouse"]),
            (env["item2"], "5", "100.00", "Each", env["warehouse"]),
        ])

        result = call_action(mod.create_po_from_so, conn, ns(
            sales_order_id=so_id, company_id=env["company_id"],
            posting_date="2026-06-15", tax_template_id=None,
            supplier_id=None, name=None, items=None,
            blanket_order_id=None, blanket_status=None,
            template_id=None, frequency=None, start_date=None,
            end_date=None, as_of_date=None, auto_submit=False,
            template_status=None,
        ))
        assert is_ok(result)
        assert result["purchase_orders_created"] == 1
        assert len(result["purchase_orders"]) == 1
        po = result["purchase_orders"][0]
        assert po["supplier_id"] == env["supplier"]
        assert po["items_count"] == 2
        assert Decimal(po["grand_total"]) == Decimal("1000.00")

    def test_multiple_suppliers(self, conn, env):
        """Two suppliers -> two POs."""
        customer_id = _seed_customer(conn, env["company_id"])
        supplier2 = seed_supplier(conn, env["company_id"], "Second Supplier")
        _seed_item_supplier(conn, env["item1"], env["supplier"])
        _seed_item_supplier(conn, env["item2"], supplier2)

        so_id = _seed_sales_order(conn, customer_id, env["company_id"], [
            (env["item1"], "10", "50.00", "Each", env["warehouse"]),
            (env["item2"], "5", "100.00", "Each", env["warehouse"]),
        ])

        result = call_action(mod.create_po_from_so, conn, ns(
            sales_order_id=so_id, company_id=env["company_id"],
            posting_date="2026-06-15", tax_template_id=None,
            supplier_id=None, name=None, items=None,
            blanket_order_id=None, blanket_status=None,
            template_id=None, frequency=None, start_date=None,
            end_date=None, as_of_date=None, auto_submit=False,
            template_status=None,
        ))
        assert is_ok(result)
        assert result["purchase_orders_created"] == 2

    def test_no_supplier_mapping_fails(self, conn, env):
        """Items without supplier mapping -> error."""
        customer_id = _seed_customer(conn, env["company_id"])
        so_id = _seed_sales_order(conn, customer_id, env["company_id"], [
            (env["item1"], "10", "50.00", "Each", env["warehouse"]),
        ])

        result = call_action(mod.create_po_from_so, conn, ns(
            sales_order_id=so_id, company_id=env["company_id"],
            posting_date="2026-06-15", tax_template_id=None,
            supplier_id=None, name=None, items=None,
            blanket_order_id=None, blanket_status=None,
            template_id=None, frequency=None, start_date=None,
            end_date=None, as_of_date=None, auto_submit=False,
            template_status=None,
        ))
        assert is_error(result)

    def test_so_not_found_fails(self, conn, env):
        result = call_action(mod.create_po_from_so, conn, ns(
            sales_order_id="nonexistent", company_id=env["company_id"],
            posting_date=None, tax_template_id=None,
            supplier_id=None, name=None, items=None,
            blanket_order_id=None, blanket_status=None,
            template_id=None, frequency=None, start_date=None,
            end_date=None, as_of_date=None, auto_submit=False,
            template_status=None,
        ))
        assert is_error(result)

    def test_partial_supplier_mapping(self, conn, env):
        """Some items have suppliers, some don't -> creates PO for mapped, reports unmapped."""
        customer_id = _seed_customer(conn, env["company_id"])
        _seed_item_supplier(conn, env["item1"], env["supplier"])
        # item2 has no supplier mapping

        so_id = _seed_sales_order(conn, customer_id, env["company_id"], [
            (env["item1"], "10", "50.00", "Each", env["warehouse"]),
            (env["item2"], "5", "100.00", "Each", env["warehouse"]),
        ])

        result = call_action(mod.create_po_from_so, conn, ns(
            sales_order_id=so_id, company_id=env["company_id"],
            posting_date="2026-06-15", tax_template_id=None,
            supplier_id=None, name=None, items=None,
            blanket_order_id=None, blanket_status=None,
            template_id=None, frequency=None, start_date=None,
            end_date=None, as_of_date=None, auto_submit=False,
            template_status=None,
        ))
        assert is_ok(result)
        assert result["purchase_orders_created"] == 1
        assert len(result["items_without_supplier"]) == 1
        assert env["item2"] in result["items_without_supplier"]

    def test_supplier_priority(self, conn, env):
        """Item with multiple suppliers should use lowest priority (preferred)."""
        customer_id = _seed_customer(conn, env["company_id"])
        supplier2 = seed_supplier(conn, env["company_id"], "Better Supplier")
        # supplier2 has priority 0 (better), env["supplier"] has priority 1
        _seed_item_supplier(conn, env["item1"], supplier2, priority=0)
        _seed_item_supplier(conn, env["item1"], env["supplier"], priority=1)

        so_id = _seed_sales_order(conn, customer_id, env["company_id"], [
            (env["item1"], "10", "50.00", "Each", env["warehouse"]),
        ])

        result = call_action(mod.create_po_from_so, conn, ns(
            sales_order_id=so_id, company_id=env["company_id"],
            posting_date="2026-06-15", tax_template_id=None,
            supplier_id=None, name=None, items=None,
            blanket_order_id=None, blanket_status=None,
            template_id=None, frequency=None, start_date=None,
            end_date=None, as_of_date=None, auto_submit=False,
            template_status=None,
        ))
        assert is_ok(result)
        assert result["purchase_orders"][0]["supplier_id"] == supplier2
