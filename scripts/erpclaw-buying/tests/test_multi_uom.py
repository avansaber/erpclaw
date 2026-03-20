"""Tests for Multi-UOM on PO (Feature #19, Sprint 7).

Actions tested:
  - set-item-purchase-uom
"""
import json
import pytest
import uuid
from decimal import Decimal
from buying_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
    seed_item, build_buying_env,
)

mod = load_db_query()


def _seed_uom(conn, name):
    """Insert a UOM and return its ID."""
    uid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO uom (id, name) VALUES (?, ?)",
        (uid, name)
    )
    conn.commit()
    return uid


# ──────────────────────────────────────────────────────────────────────────────
# set-item-purchase-uom
# ──────────────────────────────────────────────────────────────────────────────

class TestSetItemPurchaseUOM:
    def test_basic_set_purchase_uom(self, conn, env):
        """Set a purchase UOM with conversion factor for an item."""
        # Seed UOMs
        each_uom = _seed_uom(conn, "Each")
        box_uom = _seed_uom(conn, "Box")

        result = call_action(mod.set_item_purchase_uom, conn, ns(
            item_id=env["item1"],
            purchase_uom="Box",
            conversion_factor="12",
            # Other args that might be needed from ns pattern
            company_id=None,
            supplier_id=None,
            name=None,
            items=None,
        ))
        assert is_ok(result)
        assert result["purchase_uom"] == "Box"
        assert Decimal(result["conversion_factor"]) == Decimal("12.00")
        assert result["action"] == "created"
        assert "uom_conversion_id" in result

        # Verify in DB
        row = conn.execute(
            "SELECT * FROM uom_conversion WHERE id = ?",
            (result["uom_conversion_id"],)
        ).fetchone()
        assert row is not None
        assert row["item_id"] == env["item1"]
        assert Decimal(row["conversion_factor"]) == Decimal("12.00")

    def test_update_existing_uom(self, conn, env):
        """Update conversion factor if mapping already exists."""
        _seed_uom(conn, "Each")
        _seed_uom(conn, "Carton")

        # First set
        r1 = call_action(mod.set_item_purchase_uom, conn, ns(
            item_id=env["item1"],
            purchase_uom="Carton",
            conversion_factor="24",
            company_id=None, supplier_id=None, name=None, items=None,
        ))
        assert is_ok(r1)
        assert r1["action"] == "created"

        # Update same mapping
        r2 = call_action(mod.set_item_purchase_uom, conn, ns(
            item_id=env["item1"],
            purchase_uom="Carton",
            conversion_factor="36",
            company_id=None, supplier_id=None, name=None, items=None,
        ))
        assert is_ok(r2)
        assert r2["action"] == "updated"
        assert Decimal(r2["conversion_factor"]) == Decimal("36.00")

    def test_invalid_item(self, conn, env):
        """Reject if item doesn't exist."""
        result = call_action(mod.set_item_purchase_uom, conn, ns(
            item_id="nonexistent",
            purchase_uom="Box",
            conversion_factor="12",
            company_id=None, supplier_id=None, name=None, items=None,
        ))
        assert is_error(result)
        assert "not found" in result["message"]

    def test_invalid_conversion_factor(self, conn, env):
        """Reject zero or negative conversion factor."""
        result = call_action(mod.set_item_purchase_uom, conn, ns(
            item_id=env["item1"],
            purchase_uom="Box",
            conversion_factor="0",
            company_id=None, supplier_id=None, name=None, items=None,
        ))
        assert is_error(result)
        assert "> 0" in result["message"]

    def test_fractional_conversion_factor(self, conn, env):
        """Accept fractional conversion factors (e.g., kg to g)."""
        _seed_uom(conn, "Each")
        _seed_uom(conn, "Dozen")

        result = call_action(mod.set_item_purchase_uom, conn, ns(
            item_id=env["item1"],
            purchase_uom="Dozen",
            conversion_factor="12",
            company_id=None, supplier_id=None, name=None, items=None,
        ))
        assert is_ok(result)
        assert Decimal(result["conversion_factor"]) == Decimal("12.00")
