"""Tests for Sprint 3: Inventory & Projections (Features #4, #5, #6).

Feature #4: get-projected-qty
Feature #5: Item Variants (add-item-attribute, create-item-variant,
            generate-item-variants, list-item-variants)
Feature #6: Min Order Qty (add-item-supplier, list-item-suppliers)
"""
import json
import uuid

import pytest
from inventory_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
    seed_company, seed_account, seed_fiscal_year, seed_cost_center,
    seed_item, seed_warehouse, seed_stock_entry_sle, seed_naming_series,
    build_inventory_env, _uuid,
)

mod = load_db_query()


# ── Seed helpers for Sprint 3 ──

def seed_supplier(conn, company_id, name="Test Supplier") -> str:
    sid = _uuid()
    conn.execute(
        """INSERT INTO supplier (id, name, company_id, status)
           VALUES (?, ?, ?, 'active')""",
        (sid, f"{name} {sid[:6]}", company_id),
    )
    conn.commit()
    return sid


def seed_customer(conn, company_id, name="Test Customer") -> str:
    cid = _uuid()
    conn.execute(
        """INSERT INTO customer (id, name, company_id, status)
           VALUES (?, ?, ?, 'active')""",
        (cid, f"{name} {cid[:6]}", company_id),
    )
    conn.commit()
    return cid


def seed_purchase_order_with_items(conn, supplier_id, company_id, items, status="confirmed"):
    """Seed a PO with items. items = [(item_id, qty, received_qty, warehouse_id), ...]"""
    po_id = _uuid()
    conn.execute(
        """INSERT INTO purchase_order (id, supplier_id, order_date, status, company_id)
           VALUES (?, ?, '2026-01-15', ?, ?)""",
        (po_id, supplier_id, status, company_id),
    )
    for item_id, qty, received_qty, wh_id in items:
        poi_id = _uuid()
        conn.execute(
            """INSERT INTO purchase_order_item
               (id, purchase_order_id, item_id, quantity, received_qty, warehouse_id, rate, amount, net_amount)
               VALUES (?, ?, ?, ?, ?, ?, '10.00', ?, ?)""",
            (poi_id, po_id, item_id, qty, received_qty, wh_id,
             str(int(qty) * 10), str(int(qty) * 10)),
        )
    conn.commit()
    return po_id


def seed_sales_order_with_items(conn, customer_id, company_id, items, status="confirmed"):
    """Seed a SO with items. items = [(item_id, qty, delivered_qty, warehouse_id), ...]"""
    so_id = _uuid()
    conn.execute(
        """INSERT INTO sales_order (id, customer_id, order_date, status, company_id)
           VALUES (?, ?, '2026-01-15', ?, ?)""",
        (so_id, customer_id, status, company_id),
    )
    for item_id, qty, delivered_qty, wh_id in items:
        soi_id = _uuid()
        conn.execute(
            """INSERT INTO sales_order_item
               (id, sales_order_id, item_id, quantity, delivered_qty, warehouse_id, rate, amount, net_amount)
               VALUES (?, ?, ?, ?, ?, ?, '20.00', ?, ?)""",
            (soi_id, so_id, item_id, qty, delivered_qty, wh_id,
             str(int(qty) * 20), str(int(qty) * 20)),
        )
    conn.commit()
    return so_id


def seed_template_item(conn, name="T-Shirt", code="TSHIRT-001") -> str:
    """Create an item that will be used as a variant template."""
    iid = _uuid()
    conn.execute(
        """INSERT INTO item (id, item_code, item_name, stock_uom, is_stock_item,
           item_type, standard_rate, status, has_variants)
           VALUES (?, ?, ?, 'Each', 1, 'stock', '25.00', 'active', 0)""",
        (iid, f"{code}-{iid[:6]}", f"{name} {iid[:6]}"),
    )
    conn.commit()
    return iid


# ══════════════════════════════════════════════════════════════════════════════
# Feature #4: get-projected-qty
# ══════════════════════════════════════════════════════════════════════════════

class TestGetProjectedQty:
    def test_basic_projected_qty_no_orders(self, conn, env):
        """Projected qty equals actual qty when no open POs/SOs."""
        result = call_action(mod.get_projected_qty, conn, ns(
            item_id=env["item1"], warehouse_id=env["warehouse"],
        ))
        assert is_ok(result)
        assert result["actual_qty"] == "100.00"
        assert result["ordered_qty"] == "0.00"
        assert result["reserved_qty"] == "0.00"
        assert result["projected_qty"] == "100.00"

    def test_projected_with_open_po(self, conn, env):
        """Pending PO receipt increases projected qty."""
        supplier = seed_supplier(conn, env["company_id"])
        seed_purchase_order_with_items(conn, supplier, env["company_id"], [
            (env["item1"], "50", "0", env["warehouse"]),
        ], status="confirmed")

        result = call_action(mod.get_projected_qty, conn, ns(
            item_id=env["item1"], warehouse_id=env["warehouse"],
        ))
        assert is_ok(result)
        assert result["actual_qty"] == "100.00"
        assert result["ordered_qty"] == "50.00"
        assert result["reserved_qty"] == "0.00"
        assert result["projected_qty"] == "150.00"

    def test_projected_with_open_so(self, conn, env):
        """Confirmed SO reduces projected qty."""
        customer = seed_customer(conn, env["company_id"])
        seed_sales_order_with_items(conn, customer, env["company_id"], [
            (env["item1"], "30", "0", env["warehouse"]),
        ], status="confirmed")

        result = call_action(mod.get_projected_qty, conn, ns(
            item_id=env["item1"], warehouse_id=env["warehouse"],
        ))
        assert is_ok(result)
        assert result["actual_qty"] == "100.00"
        assert result["ordered_qty"] == "0.00"
        assert result["reserved_qty"] == "30.00"
        assert result["projected_qty"] == "70.00"

    def test_projected_with_partial_receipt_and_delivery(self, conn, env):
        """Partially received PO and partially delivered SO."""
        supplier = seed_supplier(conn, env["company_id"])
        customer = seed_customer(conn, env["company_id"])

        # PO: ordered 100, received 40 => pending = 60
        seed_purchase_order_with_items(conn, supplier, env["company_id"], [
            (env["item1"], "100", "40", env["warehouse"]),
        ], status="partially_received")

        # SO: ordered 50, delivered 20 => reserved = 30
        seed_sales_order_with_items(conn, customer, env["company_id"], [
            (env["item1"], "50", "20", env["warehouse"]),
        ], status="partially_delivered")

        result = call_action(mod.get_projected_qty, conn, ns(
            item_id=env["item1"], warehouse_id=env["warehouse"],
        ))
        assert is_ok(result)
        assert result["actual_qty"] == "100.00"
        assert result["ordered_qty"] == "60.00"
        assert result["reserved_qty"] == "30.00"
        # projected = 100 + 60 - 30 = 130
        assert result["projected_qty"] == "130.00"

    def test_projected_ignores_cancelled_orders(self, conn, env):
        """Cancelled POs/SOs are not included in projection."""
        supplier = seed_supplier(conn, env["company_id"])
        customer = seed_customer(conn, env["company_id"])

        seed_purchase_order_with_items(conn, supplier, env["company_id"], [
            (env["item1"], "200", "0", env["warehouse"]),
        ], status="cancelled")

        seed_sales_order_with_items(conn, customer, env["company_id"], [
            (env["item1"], "200", "0", env["warehouse"]),
        ], status="cancelled")

        result = call_action(mod.get_projected_qty, conn, ns(
            item_id=env["item1"], warehouse_id=env["warehouse"],
        ))
        assert is_ok(result)
        assert result["ordered_qty"] == "0.00"
        assert result["reserved_qty"] == "0.00"
        assert result["projected_qty"] == "100.00"

    def test_projected_missing_item_fails(self, conn, env):
        result = call_action(mod.get_projected_qty, conn, ns(
            item_id="fake-item-id", warehouse_id=env["warehouse"],
        ))
        assert is_error(result)

    def test_projected_missing_warehouse_fails(self, conn, env):
        result = call_action(mod.get_projected_qty, conn, ns(
            item_id=env["item1"], warehouse_id="fake-wh-id",
        ))
        assert is_error(result)

    def test_projected_item_with_no_stock(self, conn, env):
        """Item with zero actual stock shows only pending orders."""
        supplier = seed_supplier(conn, env["company_id"])
        seed_purchase_order_with_items(conn, supplier, env["company_id"], [
            (env["item2"], "75", "0", env["warehouse"]),
        ], status="confirmed")

        result = call_action(mod.get_projected_qty, conn, ns(
            item_id=env["item2"], warehouse_id=env["warehouse"],
        ))
        assert is_ok(result)
        assert result["actual_qty"] == "0.00"
        assert result["ordered_qty"] == "75.00"
        assert result["projected_qty"] == "75.00"


# ══════════════════════════════════════════════════════════════════════════════
# Feature #5: Item Variants
# ══════════════════════════════════════════════════════════════════════════════

class TestAddItemAttribute:
    def test_add_attribute(self, conn, env):
        """Add a Color attribute to a template item."""
        item = seed_template_item(conn)
        result = call_action(mod.add_item_attribute, conn, ns(
            item_id=item,
            attribute_name="Color",
            attribute_values='["Red", "Blue", "Green"]',
        ))
        assert is_ok(result)
        assert result["attribute_name"] == "Color"
        assert result["values"] == ["Red", "Blue", "Green"]

        # Verify item.has_variants is now 1
        row = conn.execute("SELECT has_variants FROM item WHERE id = ?", (item,)).fetchone()
        assert row["has_variants"] == 1

    def test_add_multiple_attributes(self, conn, env):
        """Add multiple attributes to the same template."""
        item = seed_template_item(conn)
        r1 = call_action(mod.add_item_attribute, conn, ns(
            item_id=item, attribute_name="Color",
            attribute_values='["Red", "Blue"]',
        ))
        assert is_ok(r1)

        r2 = call_action(mod.add_item_attribute, conn, ns(
            item_id=item, attribute_name="Size",
            attribute_values='["S", "M", "L"]',
        ))
        assert is_ok(r2)

    def test_duplicate_attribute_fails(self, conn, env):
        """Cannot add same attribute name twice."""
        item = seed_template_item(conn)
        call_action(mod.add_item_attribute, conn, ns(
            item_id=item, attribute_name="Color",
            attribute_values='["Red", "Blue"]',
        ))
        result = call_action(mod.add_item_attribute, conn, ns(
            item_id=item, attribute_name="Color",
            attribute_values='["Green", "Yellow"]',
        ))
        assert is_error(result)

    def test_missing_item_fails(self, conn, env):
        result = call_action(mod.add_item_attribute, conn, ns(
            item_id="fake-id", attribute_name="Color",
            attribute_values='["Red"]',
        ))
        assert is_error(result)

    def test_missing_name_fails(self, conn, env):
        item = seed_template_item(conn)
        result = call_action(mod.add_item_attribute, conn, ns(
            item_id=item, attribute_name=None,
            attribute_values='["Red"]',
        ))
        assert is_error(result)

    def test_empty_values_fails(self, conn, env):
        item = seed_template_item(conn)
        result = call_action(mod.add_item_attribute, conn, ns(
            item_id=item, attribute_name="Color",
            attribute_values='[]',
        ))
        assert is_error(result)

    def test_cannot_add_attribute_to_variant(self, conn, env):
        """Variants cannot have attributes added directly."""
        item = seed_template_item(conn)
        call_action(mod.add_item_attribute, conn, ns(
            item_id=item, attribute_name="Color",
            attribute_values='["Red", "Blue"]',
        ))
        result = call_action(mod.create_item_variant, conn, ns(
            template_item_id=item,
            attributes='{"Color": "Red"}',
        ))
        assert is_ok(result)
        variant_id = result["variant_id"]

        # Try adding attribute to the variant — should fail
        result = call_action(mod.add_item_attribute, conn, ns(
            item_id=variant_id, attribute_name="Size",
            attribute_values='["S", "M"]',
        ))
        assert is_error(result)


class TestCreateItemVariant:
    def test_create_single_variant(self, conn, env):
        item = seed_template_item(conn, "T-Shirt", "TS")
        call_action(mod.add_item_attribute, conn, ns(
            item_id=item, attribute_name="Color",
            attribute_values='["Red", "Blue"]',
        ))
        result = call_action(mod.create_item_variant, conn, ns(
            template_item_id=item,
            attributes='{"Color": "Red"}',
        ))
        assert is_ok(result)
        assert "Red" in result["item_code"]
        assert result["template_item_id"] == item
        assert result["attributes"] == {"Color": "Red"}

        # Verify variant_of is set
        row = conn.execute("SELECT variant_of FROM item WHERE id = ?",
                           (result["variant_id"],)).fetchone()
        assert row["variant_of"] == item

    def test_create_variant_inherits_template_properties(self, conn, env):
        item = seed_template_item(conn, "Widget", "WDG")
        call_action(mod.add_item_attribute, conn, ns(
            item_id=item, attribute_name="Color",
            attribute_values='["Red"]',
        ))
        result = call_action(mod.create_item_variant, conn, ns(
            template_item_id=item,
            attributes='{"Color": "Red"}',
        ))
        assert is_ok(result)

        # Verify variant inherits template properties
        template = conn.execute("SELECT * FROM item WHERE id = ?", (item,)).fetchone()
        variant = conn.execute("SELECT * FROM item WHERE id = ?",
                               (result["variant_id"],)).fetchone()
        assert variant["item_type"] == template["item_type"]
        assert variant["stock_uom"] == template["stock_uom"]
        assert variant["standard_rate"] == template["standard_rate"]

    def test_invalid_attribute_value_fails(self, conn, env):
        item = seed_template_item(conn)
        call_action(mod.add_item_attribute, conn, ns(
            item_id=item, attribute_name="Color",
            attribute_values='["Red", "Blue"]',
        ))
        result = call_action(mod.create_item_variant, conn, ns(
            template_item_id=item,
            attributes='{"Color": "Yellow"}',
        ))
        assert is_error(result)

    def test_unknown_attribute_fails(self, conn, env):
        item = seed_template_item(conn)
        call_action(mod.add_item_attribute, conn, ns(
            item_id=item, attribute_name="Color",
            attribute_values='["Red"]',
        ))
        result = call_action(mod.create_item_variant, conn, ns(
            template_item_id=item,
            attributes='{"Material": "Cotton"}',
        ))
        assert is_error(result)

    def test_duplicate_variant_fails(self, conn, env):
        item = seed_template_item(conn)
        call_action(mod.add_item_attribute, conn, ns(
            item_id=item, attribute_name="Color",
            attribute_values='["Red"]',
        ))
        call_action(mod.create_item_variant, conn, ns(
            template_item_id=item,
            attributes='{"Color": "Red"}',
        ))
        result = call_action(mod.create_item_variant, conn, ns(
            template_item_id=item,
            attributes='{"Color": "Red"}',
        ))
        assert is_error(result)

    def test_non_template_fails(self, conn, env):
        """Cannot create variant from a non-template item."""
        result = call_action(mod.create_item_variant, conn, ns(
            template_item_id=env["item1"],
            attributes='{"Color": "Red"}',
        ))
        assert is_error(result)


class TestGenerateItemVariants:
    def test_generate_all_variants(self, conn, env):
        item = seed_template_item(conn, "Shirt", "SH")
        call_action(mod.add_item_attribute, conn, ns(
            item_id=item, attribute_name="Color",
            attribute_values='["Red", "Blue"]',
        ))
        call_action(mod.add_item_attribute, conn, ns(
            item_id=item, attribute_name="Size",
            attribute_values='["S", "M", "L"]',
        ))

        result = call_action(mod.generate_item_variants, conn, ns(
            template_item_id=item,
        ))
        assert is_ok(result)
        # 2 colors * 3 sizes = 6 variants
        assert result["created"] == 6
        assert result["skipped"] == 0
        assert len(result["variants"]) == 6

    def test_generate_skips_existing(self, conn, env):
        item = seed_template_item(conn, "Hat", "HT")
        call_action(mod.add_item_attribute, conn, ns(
            item_id=item, attribute_name="Color",
            attribute_values='["Red", "Blue"]',
        ))
        # Create one variant first
        call_action(mod.create_item_variant, conn, ns(
            template_item_id=item,
            attributes='{"Color": "Red"}',
        ))
        # Generate should skip the existing Red variant
        result = call_action(mod.generate_item_variants, conn, ns(
            template_item_id=item,
        ))
        assert is_ok(result)
        assert result["created"] == 1  # only Blue
        assert result["skipped"] == 1  # Red was skipped

    def test_generate_no_attributes_fails(self, conn, env):
        item = seed_template_item(conn, "Plain", "PL")
        # Mark as template but don't add attributes
        conn.execute("UPDATE item SET has_variants = 1 WHERE id = ?", (item,))
        conn.commit()

        result = call_action(mod.generate_item_variants, conn, ns(
            template_item_id=item,
        ))
        assert is_error(result)

    def test_generate_non_template_fails(self, conn, env):
        result = call_action(mod.generate_item_variants, conn, ns(
            template_item_id=env["item1"],
        ))
        assert is_error(result)


class TestListItemVariants:
    def test_list_variants(self, conn, env):
        item = seed_template_item(conn, "Jacket", "JK")
        call_action(mod.add_item_attribute, conn, ns(
            item_id=item, attribute_name="Color",
            attribute_values='["Black", "Brown"]',
        ))
        call_action(mod.generate_item_variants, conn, ns(
            template_item_id=item,
        ))

        result = call_action(mod.list_item_variants, conn, ns(
            template_item_id=item,
        ))
        assert is_ok(result)
        assert result["count"] == 2
        codes = [v["item_code"] for v in result["variants"]]
        assert any("Black" in c for c in codes)
        assert any("Brown" in c for c in codes)

    def test_list_empty_variants(self, conn, env):
        item = seed_template_item(conn)
        result = call_action(mod.list_item_variants, conn, ns(
            template_item_id=item,
        ))
        assert is_ok(result)
        assert result["count"] == 0

    def test_list_variants_nonexistent_item_fails(self, conn, env):
        result = call_action(mod.list_item_variants, conn, ns(
            template_item_id="fake-id",
        ))
        assert is_error(result)


# ══════════════════════════════════════════════════════════════════════════════
# Feature #6: Item Supplier (Min Order Qty)
# ══════════════════════════════════════════════════════════════════════════════

class TestAddItemSupplier:
    def test_basic_link(self, conn, env):
        supplier = seed_supplier(conn, env["company_id"])
        result = call_action(mod.add_item_supplier, conn, ns(
            item_id=env["item1"], supplier_id=supplier,
            min_order_qty="100", lead_time_days="7", priority=1,
        ))
        assert is_ok(result)
        assert result["min_order_qty"] == "100.00"
        assert result["lead_time_days"] == 7
        assert result["priority"] == 1

    def test_link_with_defaults(self, conn, env):
        supplier = seed_supplier(conn, env["company_id"])
        result = call_action(mod.add_item_supplier, conn, ns(
            item_id=env["item1"], supplier_id=supplier,
            min_order_qty=None, lead_time_days=None, priority=None,
        ))
        assert is_ok(result)
        assert result["min_order_qty"] == "0.00"

    def test_duplicate_link_fails(self, conn, env):
        supplier = seed_supplier(conn, env["company_id"])
        call_action(mod.add_item_supplier, conn, ns(
            item_id=env["item1"], supplier_id=supplier,
            min_order_qty="10", lead_time_days=None, priority=None,
        ))
        result = call_action(mod.add_item_supplier, conn, ns(
            item_id=env["item1"], supplier_id=supplier,
            min_order_qty="20", lead_time_days=None, priority=None,
        ))
        assert is_error(result)

    def test_missing_item_fails(self, conn, env):
        supplier = seed_supplier(conn, env["company_id"])
        result = call_action(mod.add_item_supplier, conn, ns(
            item_id="fake-item", supplier_id=supplier,
            min_order_qty="10", lead_time_days=None, priority=None,
        ))
        assert is_error(result)

    def test_missing_supplier_fails(self, conn, env):
        result = call_action(mod.add_item_supplier, conn, ns(
            item_id=env["item1"], supplier_id="fake-sup",
            min_order_qty="10", lead_time_days=None, priority=None,
        ))
        assert is_error(result)

    def test_multiple_suppliers_for_item(self, conn, env):
        sup1 = seed_supplier(conn, env["company_id"], "Supplier A")
        sup2 = seed_supplier(conn, env["company_id"], "Supplier B")

        r1 = call_action(mod.add_item_supplier, conn, ns(
            item_id=env["item1"], supplier_id=sup1,
            min_order_qty="50", lead_time_days="5", priority=1,
        ))
        assert is_ok(r1)

        r2 = call_action(mod.add_item_supplier, conn, ns(
            item_id=env["item1"], supplier_id=sup2,
            min_order_qty="100", lead_time_days="10", priority=2,
        ))
        assert is_ok(r2)


class TestListItemSuppliers:
    def test_list_by_item(self, conn, env):
        sup1 = seed_supplier(conn, env["company_id"], "Alpha")
        sup2 = seed_supplier(conn, env["company_id"], "Beta")
        call_action(mod.add_item_supplier, conn, ns(
            item_id=env["item1"], supplier_id=sup1,
            min_order_qty="50", lead_time_days="5", priority=1,
        ))
        call_action(mod.add_item_supplier, conn, ns(
            item_id=env["item1"], supplier_id=sup2,
            min_order_qty="100", lead_time_days="10", priority=2,
        ))

        result = call_action(mod.list_item_suppliers, conn, ns(
            item_id=env["item1"], supplier_id=None,
        ))
        assert is_ok(result)
        assert result["count"] == 2
        # Ordered by priority
        assert result["item_suppliers"][0]["priority"] == 1

    def test_list_by_supplier(self, conn, env):
        supplier = seed_supplier(conn, env["company_id"])
        call_action(mod.add_item_supplier, conn, ns(
            item_id=env["item1"], supplier_id=supplier,
            min_order_qty="50", lead_time_days=None, priority=None,
        ))
        call_action(mod.add_item_supplier, conn, ns(
            item_id=env["item2"], supplier_id=supplier,
            min_order_qty="25", lead_time_days=None, priority=None,
        ))

        result = call_action(mod.list_item_suppliers, conn, ns(
            item_id=None, supplier_id=supplier,
        ))
        assert is_ok(result)
        assert result["count"] == 2

    def test_list_no_filter_fails(self, conn, env):
        result = call_action(mod.list_item_suppliers, conn, ns(
            item_id=None, supplier_id=None,
        ))
        assert is_error(result)

    def test_list_empty(self, conn, env):
        result = call_action(mod.list_item_suppliers, conn, ns(
            item_id=env["item2"], supplier_id=None,
        ))
        assert is_ok(result)
        assert result["count"] == 0
