"""Tests for GRN receipt tolerance (Feature #17).

Tests that create-purchase-receipt:
- Accepts exact qty (within PO remaining)
- Accepts qty within tolerance percentage
- Rejects qty over tolerance
- Default tolerance is zero (strict)
"""
import json
import pytest
from decimal import Decimal
from buying_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
    build_buying_env, _uuid,
)

mod = load_db_query()


def _items(env, *specs):
    return json.dumps([
        {"item_id": env[k], "qty": q, "rate": r, "warehouse_id": env["warehouse"]}
        for k, q, r in specs
    ])


def _create_confirmed_po(conn, env, items_str=None):
    """Create and confirm a PO."""
    items_str = items_str or _items(env, ("item1", "10", "50.00"))
    po = call_action(mod.add_purchase_order, conn, ns(
        supplier_id=env["supplier"], company_id=env["company_id"],
        posting_date="2026-06-15", items=items_str,
        tax_template_id=None, name=None,
    ))
    assert is_ok(po), f"PO creation failed: {po}"
    submit = call_action(mod.submit_purchase_order, conn, ns(
        purchase_order_id=po["purchase_order_id"],
    ))
    assert is_ok(submit), f"PO submit failed: {submit}"
    return po["purchase_order_id"]


def _set_tolerance(conn, company_id, pct):
    """Set the receipt tolerance percentage for a company."""
    conn.execute(
        "UPDATE company SET receipt_tolerance_pct = ? WHERE id = ?",
        (str(pct), company_id),
    )
    conn.commit()


class TestReceiptAcceptsExactQty:
    def test_receipt_accepts_exact_qty(self, conn, env):
        """Receiving exactly the ordered qty should always succeed."""
        po_id = _create_confirmed_po(conn, env)

        # Full receipt (10 units, exact match)
        result = call_action(mod.create_purchase_receipt, conn, ns(
            purchase_order_id=po_id, company_id=env["company_id"],
            posting_date="2026-06-20", items=None,
            purchase_receipt_id=None,
        ))
        assert is_ok(result), f"Exact receipt failed: {result}"
        assert Decimal(result["total_qty"]) == Decimal("10")


class TestReceiptAcceptsWithinTolerance:
    def test_receipt_accepts_within_tolerance(self, conn, env):
        """With 10% tolerance, receiving 11 out of 10 ordered should pass."""
        _set_tolerance(conn, env["company_id"], "10")

        po_id = _create_confirmed_po(conn, env)

        # Get the PO item ID for partial receipt
        poi = conn.execute(
            "SELECT id FROM purchase_order_item WHERE purchase_order_id = ?",
            (po_id,)
        ).fetchone()

        # Try to receive 11 (10% over the remaining 10)
        partial_items = json.dumps([{
            "purchase_order_item_id": poi["id"],
            "qty": "11",
        }])
        result = call_action(mod.create_purchase_receipt, conn, ns(
            purchase_order_id=po_id, company_id=env["company_id"],
            posting_date="2026-06-20", items=partial_items,
            purchase_receipt_id=None,
        ))
        assert is_ok(result), f"Receipt within tolerance failed: {result}"
        assert Decimal(result["total_qty"]) == Decimal("11")


class TestReceiptRejectsOverTolerance:
    def test_receipt_rejects_over_tolerance(self, conn, env):
        """With 5% tolerance, receiving 11 out of 10 ordered should fail."""
        _set_tolerance(conn, env["company_id"], "5")

        po_id = _create_confirmed_po(conn, env)

        poi = conn.execute(
            "SELECT id FROM purchase_order_item WHERE purchase_order_id = ?",
            (po_id,)
        ).fetchone()

        # Try to receive 11 (10% over, but tolerance is only 5%)
        partial_items = json.dumps([{
            "purchase_order_item_id": poi["id"],
            "qty": "11",
        }])
        result = call_action(mod.create_purchase_receipt, conn, ns(
            purchase_order_id=po_id, company_id=env["company_id"],
            posting_date="2026-06-20", items=partial_items,
            purchase_receipt_id=None,
        ))
        assert is_error(result), f"Expected rejection over tolerance: {result}"
        assert "exceeds allowed" in result.get("message", "")


class TestDefaultToleranceIsZeroStrict:
    def test_default_tolerance_is_zero_strict(self, conn, env):
        """Default tolerance is 0 (strict). Even 1 unit over should fail."""
        # Don't set tolerance — default is 0
        po_id = _create_confirmed_po(conn, env)

        poi = conn.execute(
            "SELECT id FROM purchase_order_item WHERE purchase_order_id = ?",
            (po_id,)
        ).fetchone()

        # Try to receive 11 out of 10 with 0% tolerance
        partial_items = json.dumps([{
            "purchase_order_item_id": poi["id"],
            "qty": "11",
        }])
        result = call_action(mod.create_purchase_receipt, conn, ns(
            purchase_order_id=po_id, company_id=env["company_id"],
            posting_date="2026-06-20", items=partial_items,
            purchase_receipt_id=None,
        ))
        assert is_error(result), f"Expected strict rejection: {result}"
        assert "exceeds remaining" in result.get("message", "")


class TestUpdateReceiptTolerance:
    def test_update_receipt_tolerance_action(self, conn, env):
        """update-receipt-tolerance action sets the tolerance on the company."""
        result = call_action(mod.update_receipt_tolerance, conn, ns(
            company_id=env["company_id"],
            tolerance_pct="5",
        ))
        assert is_ok(result)
        assert result["receipt_tolerance_pct"] == "5.00"

        # Verify in DB
        row = conn.execute(
            "SELECT receipt_tolerance_pct FROM company WHERE id = ?",
            (env["company_id"],)
        ).fetchone()
        assert Decimal(row["receipt_tolerance_pct"]) == Decimal("5.00")

    def test_update_receipt_tolerance_rejects_negative(self, conn, env):
        """Negative tolerance percentage should be rejected."""
        result = call_action(mod.update_receipt_tolerance, conn, ns(
            company_id=env["company_id"],
            tolerance_pct="-5",
        ))
        assert is_error(result)
