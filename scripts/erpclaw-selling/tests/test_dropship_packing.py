"""Tests for Drop Shipment (#15) and Packing Slip (#16), Sprint 7.

Actions tested:
  - create-drop-ship-order
  - add-packing-slip
  - get-packing-slip
  - list-packing-slips
"""
import json
import pytest
import uuid
from decimal import Decimal
from selling_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
    seed_company, seed_account, seed_fiscal_year, seed_cost_center,
    seed_customer, seed_supplier, seed_item, seed_warehouse,
    seed_naming_series, seed_stock_entry, build_selling_env,
)

mod = load_db_query()


def _seed_supplier(conn, company_id, name="Test Supplier"):
    """Insert a supplier."""
    sid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO supplier (id, name, company_id, supplier_type, status)
           VALUES (?, ?, ?, 'company', 'active')""",
        (sid, name, company_id)
    )
    conn.commit()
    return sid


def _seed_so_with_dropship(conn, customer_id, company_id, item_id, warehouse_id,
                            qty="10", rate="50.00", is_drop_ship=1):
    """Insert a confirmed sales order with drop-ship items."""
    so_id = str(uuid.uuid4())
    amount = str(Decimal(qty) * Decimal(rate))
    conn.execute(
        """INSERT INTO sales_order
           (id, customer_id, order_date, total_amount, tax_amount, grand_total,
            status, company_id)
           VALUES (?, ?, '2026-06-01', ?, '0', ?, 'confirmed', ?)""",
        (so_id, customer_id, amount, amount, company_id)
    )

    soi_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO sales_order_item
           (id, sales_order_id, item_id, quantity, uom, rate, amount,
            discount_percentage, net_amount, warehouse_id, is_drop_ship)
           VALUES (?, ?, ?, ?, 'Each', ?, ?, '0', ?, ?, ?)""",
        (soi_id, so_id, item_id, qty, rate, amount, amount, warehouse_id, is_drop_ship)
    )
    conn.commit()
    return so_id


def _seed_dn_with_items(conn, customer_id, company_id, item_id, qty="10",
                         rate="50.00"):
    """Insert a delivery note with items for packing slip tests."""
    dn_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO delivery_note
           (id, customer_id, posting_date, status, total_qty, company_id)
           VALUES (?, ?, '2026-06-01', 'draft', ?, ?)""",
        (dn_id, customer_id, qty, company_id)
    )

    dni_id = str(uuid.uuid4())
    amount = str(Decimal(qty) * Decimal(rate))
    conn.execute(
        """INSERT INTO delivery_note_item
           (id, delivery_note_id, item_id, quantity, uom, rate, amount)
           VALUES (?, ?, ?, ?, 'Each', ?, ?)""",
        (dni_id, dn_id, item_id, qty, rate, amount)
    )
    conn.commit()
    return dn_id, dni_id


# ──────────────────────────────────────────────────────────────────────────────
# create-drop-ship-order
# ──────────────────────────────────────────────────────────────────────────────

class TestCreateDropShipOrder:
    def test_basic_drop_ship(self, conn, env):
        """Create a PO from SO drop-ship items."""
        supplier = _seed_supplier(conn, env["company_id"])

        so_id = _seed_so_with_dropship(
            conn, env["customer"], env["company_id"],
            env["item1"], env["warehouse"],
            qty="5", rate="100.00", is_drop_ship=1,
        )

        result = call_action(mod.create_drop_ship_order, conn, ns(
            sales_order_id=so_id,
            supplier_id=supplier,
            posting_date="2026-06-15",
            # Include all ns fields the main function checks
            company_id=env["company_id"],
            items=None,
        ))
        assert is_ok(result)
        assert result["item_count"] == 1
        assert Decimal(result["total_amount"]) == Decimal("500.00")
        assert result["supplier_id"] == supplier
        assert "purchase_order_id" in result

        # Verify PO in DB
        po = conn.execute(
            "SELECT * FROM purchase_order WHERE id = ?",
            (result["purchase_order_id"],)
        ).fetchone()
        assert po is not None
        assert po["status"] == "draft"
        assert po["supplier_id"] == supplier

    def test_no_drop_ship_items(self, conn, env):
        """Reject SO with no drop-ship items."""
        supplier = _seed_supplier(conn, env["company_id"])

        # Create SO without drop_ship flag
        so_id = _seed_so_with_dropship(
            conn, env["customer"], env["company_id"],
            env["item1"], env["warehouse"],
            qty="5", rate="100.00", is_drop_ship=0,
        )

        result = call_action(mod.create_drop_ship_order, conn, ns(
            sales_order_id=so_id,
            supplier_id=supplier,
            posting_date="2026-06-15",
            company_id=env["company_id"],
            items=None,
        ))
        assert is_error(result)
        assert "drop-ship" in result["message"].lower()

    def test_invalid_supplier(self, conn, env):
        """Reject if supplier doesn't exist."""
        so_id = _seed_so_with_dropship(
            conn, env["customer"], env["company_id"],
            env["item1"], env["warehouse"],
        )

        result = call_action(mod.create_drop_ship_order, conn, ns(
            sales_order_id=so_id,
            supplier_id="nonexistent",
            posting_date="2026-06-15",
            company_id=env["company_id"],
            items=None,
        ))
        assert is_error(result)
        assert "not found" in result["message"]

    def test_drop_ship_preserves_customer_address(self, conn, env):
        """PO delivery_address should be customer's primary_address."""
        # Set customer address
        conn.execute(
            "UPDATE customer SET primary_address = ? WHERE id = ?",
            ("123 Main St, Anytown USA", env["customer"])
        )
        conn.commit()

        supplier = _seed_supplier(conn, env["company_id"])

        so_id = _seed_so_with_dropship(
            conn, env["customer"], env["company_id"],
            env["item1"], env["warehouse"],
        )

        result = call_action(mod.create_drop_ship_order, conn, ns(
            sales_order_id=so_id,
            supplier_id=supplier,
            posting_date="2026-06-15",
            company_id=env["company_id"],
            items=None,
        ))
        assert is_ok(result)
        assert result["delivery_address"] == "123 Main St, Anytown USA"


# ──────────────────────────────────────────────────────────────────────────────
# add-packing-slip / get-packing-slip / list-packing-slips
# ──────────────────────────────────────────────────────────────────────────────

class TestPackingSlip:
    def test_basic_packing_slip(self, conn, env):
        """Create a packing slip for a delivery note."""
        dn_id, dni_id = _seed_dn_with_items(
            conn, env["customer"], env["company_id"], env["item1"],
            qty="10", rate="50.00",
        )

        items_json = json.dumps([
            {"delivery_note_item_id": dni_id, "qty_packed": "10"},
        ])
        result = call_action(mod.add_packing_slip, conn, ns(
            delivery_note_id=dn_id,
            items=items_json,
            posting_date="2026-06-15",
            notes=None,
            reason=None,
            company_id=env["company_id"],
        ))
        assert is_ok(result)
        assert result["item_count"] == 1
        assert "packing_slip_id" in result

    def test_packed_qty_exceeds_dn_qty(self, conn, env):
        """Reject if packed qty exceeds DN qty."""
        dn_id, dni_id = _seed_dn_with_items(
            conn, env["customer"], env["company_id"], env["item1"],
            qty="10", rate="50.00",
        )

        items_json = json.dumps([
            {"delivery_note_item_id": dni_id, "qty_packed": "15"},
        ])
        result = call_action(mod.add_packing_slip, conn, ns(
            delivery_note_id=dn_id,
            items=items_json,
            posting_date="2026-06-15",
            notes=None,
            reason=None,
            company_id=env["company_id"],
        ))
        assert is_error(result)
        assert "exceeds" in result["message"]

    def test_get_packing_slip(self, conn, env):
        """Get a packing slip with items."""
        dn_id, dni_id = _seed_dn_with_items(
            conn, env["customer"], env["company_id"], env["item1"],
            qty="10", rate="50.00",
        )

        items_json = json.dumps([
            {"delivery_note_item_id": dni_id, "qty_packed": "5"},
        ])
        add_result = call_action(mod.add_packing_slip, conn, ns(
            delivery_note_id=dn_id,
            items=items_json,
            posting_date="2026-06-15",
            notes=None,
            reason=None,
            company_id=env["company_id"],
        ))
        ps_id = add_result["packing_slip_id"]

        result = call_action(mod.get_packing_slip, conn, ns(
            packing_slip_id=ps_id,
        ))
        assert is_ok(result)
        assert result["delivery_note_id"] == dn_id
        assert len(result["items"]) == 1
        assert result["items"][0]["qty_packed"] == "5.00"

    def test_list_packing_slips(self, conn, env):
        """List packing slips for a delivery note."""
        dn_id, dni_id = _seed_dn_with_items(
            conn, env["customer"], env["company_id"], env["item1"],
            qty="10", rate="50.00",
        )

        items_json = json.dumps([
            {"delivery_note_item_id": dni_id, "qty_packed": "5"},
        ])
        call_action(mod.add_packing_slip, conn, ns(
            delivery_note_id=dn_id,
            items=items_json,
            posting_date="2026-06-15",
            notes=None,
            reason=None,
            company_id=env["company_id"],
        ))

        result = call_action(mod.list_packing_slips, conn, ns(
            delivery_note_id=dn_id,
            company_id=None,
            limit="20",
            offset="0",
        ))
        assert is_ok(result)
        assert result["count"] == 1

    def test_partial_packing_across_slips(self, conn, env):
        """Create two packing slips for same DN, total must not exceed DN qty."""
        dn_id, dni_id = _seed_dn_with_items(
            conn, env["customer"], env["company_id"], env["item1"],
            qty="10", rate="50.00",
        )

        # First packing slip: 6 units
        items1 = json.dumps([{"delivery_note_item_id": dni_id, "qty_packed": "6"}])
        r1 = call_action(mod.add_packing_slip, conn, ns(
            delivery_note_id=dn_id, items=items1,
            posting_date="2026-06-15", notes=None, reason=None,
            company_id=env["company_id"],
        ))
        assert is_ok(r1)

        # Second packing slip: 5 units (6+5=11 > 10) should fail
        items2 = json.dumps([{"delivery_note_item_id": dni_id, "qty_packed": "5"}])
        r2 = call_action(mod.add_packing_slip, conn, ns(
            delivery_note_id=dn_id, items=items2,
            posting_date="2026-06-15", notes=None, reason=None,
            company_id=env["company_id"],
        ))
        assert is_error(r2)
        assert "exceeds" in r2["message"]

        # Third packing slip: 4 units (6+4=10) should succeed
        items3 = json.dumps([{"delivery_note_item_id": dni_id, "qty_packed": "4"}])
        r3 = call_action(mod.add_packing_slip, conn, ns(
            delivery_note_id=dn_id, items=items3,
            posting_date="2026-06-15", notes=None, reason=None,
            company_id=env["company_id"],
        ))
        assert is_ok(r3)
