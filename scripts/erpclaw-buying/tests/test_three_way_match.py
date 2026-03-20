"""Tests for 3-way match (PO-GRN-Invoice) validation.

Tests that submit-purchase-invoice:
- Passes when invoice qty is within received qty
- Fails when invoice qty exceeds received qty
- Respects the 'disabled' policy
- Considers previously invoiced quantities
"""
import json
import pytest
from decimal import Decimal
from buying_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
    build_buying_env, seed_account, _uuid,
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


def _create_and_submit_receipt(conn, env, po_id, items_arg=None):
    """Create and submit a purchase receipt from a PO."""
    pr = call_action(mod.create_purchase_receipt, conn, ns(
        purchase_order_id=po_id, company_id=env["company_id"],
        posting_date="2026-06-20", items=items_arg,
        purchase_receipt_id=None,
    ))
    assert is_ok(pr), f"PR creation failed: {pr}"
    submit = call_action(mod.submit_purchase_receipt, conn, ns(
        purchase_receipt_id=pr["purchase_receipt_id"],
    ))
    assert is_ok(submit), f"PR submit failed: {submit}"
    return pr["purchase_receipt_id"]


def _set_three_way_policy(conn, company_id, policy):
    """Set the 3-way match policy on a company."""
    conn.execute(
        "UPDATE company SET three_way_match_policy = ? WHERE id = ?",
        (policy, company_id),
    )
    conn.commit()


def _seed_srnb_account(conn, env):
    """Ensure a Stock Received Not Billed account exists."""
    srnb = seed_account(conn, env["company_id"], "SRNB", "liability",
                        "stock_received_not_billed", "2100")
    return srnb


class TestSubmitPIPassesWhenQtyWithinReceived:
    def test_submit_pi_passes_when_qty_within_received(self, conn, env):
        """Invoice qty <= received qty should pass 3-way match."""
        _seed_srnb_account(conn, env)
        po_id = _create_confirmed_po(conn, env)
        _create_and_submit_receipt(conn, env, po_id)

        # Create invoice from PO (qty=10, same as received)
        pi = call_action(mod.create_purchase_invoice, conn, ns(
            purchase_order_id=po_id, purchase_receipt_id=None,
            supplier_id=None, company_id=env["company_id"],
            posting_date="2026-06-25", due_date="2026-07-25",
            items=None, tax_template_id=None,
        ))
        assert is_ok(pi), f"PI creation failed: {pi}"

        result = call_action(mod.submit_purchase_invoice, conn, ns(
            purchase_invoice_id=pi["purchase_invoice_id"],
        ))
        assert is_ok(result), f"PI submit failed: {result}"


class TestSubmitPIFailsWhenQtyExceedsReceived:
    def test_submit_pi_fails_when_qty_exceeds_received(self, conn, env):
        """Invoice qty > received qty should fail 3-way match (strict policy)."""
        _seed_srnb_account(conn, env)

        # PO for 10 units
        po_id = _create_confirmed_po(conn, env)

        # Partial receipt of 5 units
        poi = conn.execute(
            "SELECT id FROM purchase_order_item WHERE purchase_order_id = ?",
            (po_id,)
        ).fetchone()
        partial_items = json.dumps([{
            "purchase_order_item_id": poi["id"],
            "qty": "5",
        }])
        _create_and_submit_receipt(conn, env, po_id, items_arg=partial_items)

        # Create invoice from PO — will try to invoice full 10 (remaining uninvoiced)
        # But only 5 were received. 3-way match should block this.
        pi = call_action(mod.create_purchase_invoice, conn, ns(
            purchase_order_id=po_id, purchase_receipt_id=None,
            supplier_id=None, company_id=env["company_id"],
            posting_date="2026-06-25", due_date="2026-07-25",
            items=None, tax_template_id=None,
        ))
        assert is_ok(pi), f"PI creation failed: {pi}"

        result = call_action(mod.submit_purchase_invoice, conn, ns(
            purchase_invoice_id=pi["purchase_invoice_id"],
        ))
        assert is_error(result), "Expected 3-way match to block submission"
        assert "exceeds received qty" in result.get("message", "")


class TestSubmitPIRespectsDisabledPolicy:
    def test_submit_pi_respects_disabled_policy(self, conn, env):
        """With 'disabled' policy, invoice qty > received qty should pass."""
        _seed_srnb_account(conn, env)
        _set_three_way_policy(conn, env["company_id"], "disabled")

        po_id = _create_confirmed_po(conn, env)

        # Partial receipt of 5
        poi = conn.execute(
            "SELECT id FROM purchase_order_item WHERE purchase_order_id = ?",
            (po_id,)
        ).fetchone()
        partial_items = json.dumps([{
            "purchase_order_item_id": poi["id"],
            "qty": "5",
        }])
        _create_and_submit_receipt(conn, env, po_id, items_arg=partial_items)

        # Invoice for full 10 — should pass because policy is disabled
        pi = call_action(mod.create_purchase_invoice, conn, ns(
            purchase_order_id=po_id, purchase_receipt_id=None,
            supplier_id=None, company_id=env["company_id"],
            posting_date="2026-06-25", due_date="2026-07-25",
            items=None, tax_template_id=None,
        ))
        assert is_ok(pi), f"PI creation failed: {pi}"

        result = call_action(mod.submit_purchase_invoice, conn, ns(
            purchase_invoice_id=pi["purchase_invoice_id"],
        ))
        assert is_ok(result), f"Expected disabled policy to allow submit: {result}"


class TestSubmitPIConsidersPreviousInvoices:
    def test_submit_pi_considers_previous_invoices(self, conn, env):
        """Second invoice that would push total invoiced over received should fail."""
        _seed_srnb_account(conn, env)

        # PO for 10
        po_id = _create_confirmed_po(conn, env)

        # Full receipt of 10
        _create_and_submit_receipt(conn, env, po_id)

        poi = conn.execute(
            "SELECT id, item_id FROM purchase_order_item WHERE purchase_order_id = ?",
            (po_id,)
        ).fetchone()

        # First invoice: 7 units, linked to PO item
        # Create as standalone so we control the qty
        pi1_items = json.dumps([{
            "item_id": poi["item_id"],
            "qty": "7",
            "rate": "50.00",
            "warehouse_id": env["warehouse"],
        }])
        pi1 = call_action(mod.create_purchase_invoice, conn, ns(
            purchase_order_id=None, purchase_receipt_id=None,
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-06-25", due_date="2026-07-25",
            items=pi1_items, tax_template_id=None,
        ))
        assert is_ok(pi1), f"PI1 creation failed: {pi1}"

        # Link PI1 to PO item BEFORE submit so we can test later
        conn.execute(
            "UPDATE purchase_invoice SET purchase_order_id = ? WHERE id = ?",
            (po_id, pi1["purchase_invoice_id"]),
        )
        conn.execute(
            "UPDATE purchase_invoice_item SET purchase_order_item_id = ? "
            "WHERE purchase_invoice_id = ?",
            (poi["id"], pi1["purchase_invoice_id"]),
        )
        conn.commit()

        # Submit first invoice — should pass (7 <= 10 received)
        sub1 = call_action(mod.submit_purchase_invoice, conn, ns(
            purchase_invoice_id=pi1["purchase_invoice_id"],
        ))
        assert is_ok(sub1), f"PI1 submit failed: {sub1}"

        # Second invoice: 3 more units, also linked to PO item
        pi2_items = json.dumps([{
            "item_id": poi["item_id"],
            "qty": "3",
            "rate": "50.00",
            "warehouse_id": env["warehouse"],
        }])
        pi2 = call_action(mod.create_purchase_invoice, conn, ns(
            purchase_order_id=None, purchase_receipt_id=None,
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-06-28", due_date="2026-07-28",
            items=pi2_items, tax_template_id=None,
        ))
        assert is_ok(pi2), f"PI2 creation failed: {pi2}"

        # Link PI2 to PO
        conn.execute(
            "UPDATE purchase_invoice SET purchase_order_id = ? WHERE id = ?",
            (po_id, pi2["purchase_invoice_id"]),
        )
        conn.execute(
            "UPDATE purchase_invoice_item SET purchase_order_item_id = ? "
            "WHERE purchase_invoice_id = ?",
            (poi["id"], pi2["purchase_invoice_id"]),
        )
        conn.commit()

        # This should pass — 7 + 3 = 10 = received
        result2 = call_action(mod.submit_purchase_invoice, conn, ns(
            purchase_invoice_id=pi2["purchase_invoice_id"],
        ))
        assert is_ok(result2), f"PI2 submit should pass (7+3=10=received): {result2}"

    def test_third_invoice_over_received_fails(self, conn, env):
        """Third invoice that pushes total over received qty should fail."""
        _seed_srnb_account(conn, env)

        # PO for 10
        po_id = _create_confirmed_po(conn, env)

        # Full receipt of 10
        _create_and_submit_receipt(conn, env, po_id)

        poi = conn.execute(
            "SELECT id, item_id FROM purchase_order_item WHERE purchase_order_id = ?",
            (po_id,)
        ).fetchone()

        # First invoice: 6 units
        pi1_items = json.dumps([{
            "item_id": poi["item_id"],
            "qty": "6",
            "rate": "50.00",
            "warehouse_id": env["warehouse"],
        }])
        pi1 = call_action(mod.create_purchase_invoice, conn, ns(
            purchase_order_id=None, purchase_receipt_id=None,
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-06-25", due_date="2026-07-25",
            items=pi1_items, tax_template_id=None,
        ))
        assert is_ok(pi1)
        conn.execute(
            "UPDATE purchase_invoice SET purchase_order_id = ? WHERE id = ?",
            (po_id, pi1["purchase_invoice_id"]),
        )
        conn.execute(
            "UPDATE purchase_invoice_item SET purchase_order_item_id = ? "
            "WHERE purchase_invoice_id = ?",
            (poi["id"], pi1["purchase_invoice_id"]),
        )
        conn.commit()
        sub1 = call_action(mod.submit_purchase_invoice, conn, ns(
            purchase_invoice_id=pi1["purchase_invoice_id"],
        ))
        assert is_ok(sub1)

        # Second invoice: 5 more units = 6 + 5 = 11 > 10 received
        pi2_items = json.dumps([{
            "item_id": poi["item_id"],
            "qty": "5",
            "rate": "50.00",
            "warehouse_id": env["warehouse"],
        }])
        pi2 = call_action(mod.create_purchase_invoice, conn, ns(
            purchase_order_id=None, purchase_receipt_id=None,
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-06-28", due_date="2026-07-28",
            items=pi2_items, tax_template_id=None,
        ))
        assert is_ok(pi2)
        conn.execute(
            "UPDATE purchase_invoice SET purchase_order_id = ? WHERE id = ?",
            (po_id, pi2["purchase_invoice_id"]),
        )
        conn.execute(
            "UPDATE purchase_invoice_item SET purchase_order_item_id = ? "
            "WHERE purchase_invoice_id = ?",
            (poi["id"], pi2["purchase_invoice_id"]),
        )
        conn.commit()

        # This should FAIL — 6 + 5 = 11 > 10 received
        result2 = call_action(mod.submit_purchase_invoice, conn, ns(
            purchase_invoice_id=pi2["purchase_invoice_id"],
        ))
        assert is_error(result2), f"Expected 3-way match to fail: {result2}"
        assert "exceeds received qty" in result2.get("message", "")
