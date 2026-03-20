"""Tests for close-purchase-order.

Sprint 2 — Document Lifecycle feature:
  Feature #18: Close Partially Received PO
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


def _create_receipt(conn, env, po_id, items_str=None):
    """Create a purchase receipt from a PO."""
    return call_action(mod.create_purchase_receipt, conn, ns(
        purchase_order_id=po_id, company_id=env["company_id"],
        posting_date="2026-06-20", items=items_str,
        purchase_receipt_id=None,
    ))


# ──────────────────────────────────────────────────────────────────────────────
# Feature #18: close-purchase-order
# ──────────────────────────────────────────────────────────────────────────────

class TestClosePurchaseOrder:
    def test_close_po_sets_status_closed(self, conn, env):
        """Closing a confirmed PO sets status to 'closed'."""
        po_id = _create_confirmed_po(conn, env)
        result = call_action(mod.close_purchase_order, conn, ns(
            purchase_order_id=po_id, reason="Vendor cannot fulfill remaining",
            closed_by="buyer@company.com",
        ))
        assert is_ok(result)
        assert result["doc_status"] == "closed"

        row = conn.execute("SELECT status FROM purchase_order WHERE id=?",
                           (po_id,)).fetchone()
        assert row["status"] == "closed"

    def test_close_po_blocks_new_receipt(self, conn, env):
        """Once a PO is closed, new purchase receipts are rejected."""
        po_id = _create_confirmed_po(conn, env)
        call_action(mod.close_purchase_order, conn, ns(
            purchase_order_id=po_id, reason=None, closed_by=None,
        ))
        result = _create_receipt(conn, env, po_id)
        assert is_error(result)
        assert "closed" in result.get("message", "").lower()

    def test_close_po_blocks_new_invoice(self, conn, env):
        """Once a PO is closed, new purchase invoices are rejected."""
        po_id = _create_confirmed_po(conn, env)
        call_action(mod.close_purchase_order, conn, ns(
            purchase_order_id=po_id, reason=None, closed_by=None,
        ))
        result = call_action(mod.create_purchase_invoice, conn, ns(
            purchase_order_id=po_id, purchase_receipt_id=None,
            supplier_id=None, company_id=None,
            posting_date="2026-06-25", items=None,
            tax_template_id=None, due_date=None,
        ))
        assert is_error(result)
        assert "closed" in result.get("message", "").lower()

    def test_close_po_preserves_existing_receipts(self, conn, env):
        """Closing a PO does NOT cancel existing purchase receipts."""
        po_id = _create_confirmed_po(conn, env)
        pr = _create_receipt(conn, env, po_id)
        assert is_ok(pr)
        pr_id = pr["purchase_receipt_id"]

        close_result = call_action(mod.close_purchase_order, conn, ns(
            purchase_order_id=po_id, reason="Partial receipt sufficient",
            closed_by="buyer@company.com",
        ))
        assert is_ok(close_result)

        # Receipt still exists and is not cancelled
        pr_row = conn.execute("SELECT status FROM purchase_receipt WHERE id=?",
                              (pr_id,)).fetchone()
        assert pr_row is not None
        assert pr_row["status"] != "cancelled"

    def test_close_po_with_reason(self, conn, env):
        """Close reason and closed_by are stored on the PO."""
        po_id = _create_confirmed_po(conn, env)
        result = call_action(mod.close_purchase_order, conn, ns(
            purchase_order_id=po_id,
            reason="Vendor bankrupt, partial delivery received",
            closed_by="procurement_lead@company.com",
        ))
        assert is_ok(result)
        assert result["close_reason"] == "Vendor bankrupt, partial delivery received"
        assert result["closed_by"] == "procurement_lead@company.com"

        row = conn.execute(
            "SELECT close_reason, closed_by FROM purchase_order WHERE id=?",
            (po_id,)).fetchone()
        assert row["close_reason"] == "Vendor bankrupt, partial delivery received"
        assert row["closed_by"] == "procurement_lead@company.com"

    def test_close_draft_po_fails(self, conn, env):
        """Cannot close a draft PO (must be confirmed first)."""
        items = _items(env, ("item1", "5", "50.00"))
        po = call_action(mod.add_purchase_order, conn, ns(
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-06-15", items=items,
            tax_template_id=None, name=None,
        ))
        result = call_action(mod.close_purchase_order, conn, ns(
            purchase_order_id=po["purchase_order_id"],
            reason=None, closed_by=None,
        ))
        assert is_error(result)

    def test_close_already_closed_fails(self, conn, env):
        """Cannot close a PO that is already closed."""
        po_id = _create_confirmed_po(conn, env)
        call_action(mod.close_purchase_order, conn, ns(
            purchase_order_id=po_id, reason=None, closed_by=None,
        ))
        result = call_action(mod.close_purchase_order, conn, ns(
            purchase_order_id=po_id, reason=None, closed_by=None,
        ))
        assert is_error(result)

    def test_close_nonexistent_po_fails(self, conn):
        """Closing a nonexistent PO returns an error."""
        result = call_action(mod.close_purchase_order, conn, ns(
            purchase_order_id="fake-id", reason=None, closed_by=None,
        ))
        assert is_error(result)
