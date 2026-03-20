"""Tests for close-sales-order, amend-sales-order, and get-amendment-history.

Sprint 2 — Document Lifecycle features:
  Feature #11: SO Close with partial deliveries
  Feature #10: SO Amendment chain
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
        {"item_id": env[k], "qty": q, "rate": r, "warehouse_id": env["warehouse"]}
        for k, q, r in specs
    ])


def _create_confirmed_so(conn, env, items_str=None):
    """Create and confirm a sales order."""
    items_str = items_str or _items(env, ("item1", "10", "100.00"))
    so = call_action(mod.add_sales_order, conn, ns(
        customer_id=env["customer"], company_id=env["company_id"],
        posting_date="2026-06-15", items=items_str,
        delivery_date="2026-07-01", tax_template_id=None,
    ))
    assert is_ok(so), f"SO creation failed: {so}"
    submit = call_action(mod.submit_sales_order, conn, ns(
        sales_order_id=so["sales_order_id"],
    ))
    assert is_ok(submit), f"SO submit failed: {submit}"
    return so["sales_order_id"]


def _create_dn(conn, so_id, items_str=None):
    """Create a delivery note from a sales order."""
    return call_action(mod.create_delivery_note, conn, ns(
        sales_order_id=so_id, posting_date="2026-06-20",
        items=items_str,
    ))


# ──────────────────────────────────────────────────────────────────────────────
# Feature #11: close-sales-order
# ──────────────────────────────────────────────────────────────────────────────

class TestCloseSalesOrder:
    def test_close_so_sets_status_closed(self, conn, env):
        """Closing a confirmed SO sets status to 'closed'."""
        so_id = _create_confirmed_so(conn, env)
        result = call_action(mod.close_sales_order, conn, ns(
            sales_order_id=so_id, reason="Customer requested partial close",
            closed_by="admin@example.com",
        ))
        assert is_ok(result)
        assert result["doc_status"] == "closed"

        row = conn.execute("SELECT status FROM sales_order WHERE id=?",
                           (so_id,)).fetchone()
        assert row["status"] == "closed"

    def test_close_so_blocks_new_delivery_note(self, conn, env):
        """Once an SO is closed, new delivery notes are rejected."""
        so_id = _create_confirmed_so(conn, env)
        call_action(mod.close_sales_order, conn, ns(
            sales_order_id=so_id, reason=None, closed_by=None,
        ))
        result = _create_dn(conn, so_id)
        assert is_error(result)
        assert "closed" in result.get("message", "").lower()

    def test_close_so_blocks_new_invoice(self, conn, env):
        """Once an SO is closed, new invoices are rejected."""
        so_id = _create_confirmed_so(conn, env)
        call_action(mod.close_sales_order, conn, ns(
            sales_order_id=so_id, reason=None, closed_by=None,
        ))
        result = call_action(mod.create_sales_invoice, conn, ns(
            sales_order_id=so_id, delivery_note_id=None,
            customer_id=None, company_id=None,
            posting_date="2026-06-25", items=None,
            tax_template_id=None, due_date=None,
        ))
        assert is_error(result)
        assert "closed" in result.get("message", "").lower()

    def test_close_so_preserves_existing_dns(self, conn, env):
        """Closing an SO does NOT cancel existing delivery notes."""
        so_id = _create_confirmed_so(conn, env)
        dn = _create_dn(conn, so_id)
        assert is_ok(dn)
        dn_id = dn["delivery_note_id"]

        close_result = call_action(mod.close_sales_order, conn, ns(
            sales_order_id=so_id, reason="Partial fulfillment complete",
            closed_by="admin@example.com",
        ))
        assert is_ok(close_result)

        # DN still exists and is not cancelled
        dn_row = conn.execute("SELECT status FROM delivery_note WHERE id=?",
                              (dn_id,)).fetchone()
        assert dn_row is not None
        assert dn_row["status"] != "cancelled"

    def test_close_so_with_reason(self, conn, env):
        """Close reason and closed_by are stored on the SO."""
        so_id = _create_confirmed_so(conn, env)
        result = call_action(mod.close_sales_order, conn, ns(
            sales_order_id=so_id,
            reason="Customer cancelled remaining items",
            closed_by="sales_manager@company.com",
        ))
        assert is_ok(result)
        assert result["close_reason"] == "Customer cancelled remaining items"
        assert result["closed_by"] == "sales_manager@company.com"

        row = conn.execute(
            "SELECT close_reason, closed_by FROM sales_order WHERE id=?",
            (so_id,)).fetchone()
        assert row["close_reason"] == "Customer cancelled remaining items"
        assert row["closed_by"] == "sales_manager@company.com"

    def test_close_draft_so_fails(self, conn, env):
        """Cannot close a draft SO (must be confirmed first)."""
        items = _items(env, ("item1", "5", "50.00"))
        so = call_action(mod.add_sales_order, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", items=items,
            delivery_date=None, tax_template_id=None,
        ))
        result = call_action(mod.close_sales_order, conn, ns(
            sales_order_id=so["sales_order_id"], reason=None, closed_by=None,
        ))
        assert is_error(result)

    def test_close_already_closed_fails(self, conn, env):
        """Cannot close an SO that is already closed."""
        so_id = _create_confirmed_so(conn, env)
        call_action(mod.close_sales_order, conn, ns(
            sales_order_id=so_id, reason=None, closed_by=None,
        ))
        result = call_action(mod.close_sales_order, conn, ns(
            sales_order_id=so_id, reason=None, closed_by=None,
        ))
        assert is_error(result)


# ──────────────────────────────────────────────────────────────────────────────
# Feature #10: amend-sales-order
# ──────────────────────────────────────────────────────────────────────────────

class TestAmendSalesOrder:
    def test_amend_so_creates_new_with_link(self, conn, env):
        """Amending creates a new SO with amended_from pointing to original."""
        so_id = _create_confirmed_so(conn, env)
        new_items = _items(env, ("item1", "20", "100.00"))
        result = call_action(mod.amend_sales_order, conn, ns(
            sales_order_id=so_id, items=new_items,
        ))
        assert is_ok(result)
        assert result["amended_from"] == so_id
        assert result["new_sales_order_id"] != so_id

        # Verify amended_from in DB
        new_so = conn.execute("SELECT amended_from FROM sales_order WHERE id=?",
                              (result["new_sales_order_id"],)).fetchone()
        assert new_so["amended_from"] == so_id

    def test_amend_so_cancels_original(self, conn, env):
        """Original SO is cancelled when amended."""
        so_id = _create_confirmed_so(conn, env)
        result = call_action(mod.amend_sales_order, conn, ns(
            sales_order_id=so_id, items=None,
        ))
        assert is_ok(result)
        assert result["original_status"] == "cancelled"

        row = conn.execute("SELECT status FROM sales_order WHERE id=?",
                           (so_id,)).fetchone()
        assert row["status"] == "cancelled"

    def test_amend_so_copies_items(self, conn, env):
        """When no --items given, amendment copies items from original."""
        items = _items(env, ("item1", "10", "100.00"), ("item2", "5", "200.00"))
        so_id = _create_confirmed_so(conn, env, items_str=items)

        result = call_action(mod.amend_sales_order, conn, ns(
            sales_order_id=so_id, items=None,
        ))
        assert is_ok(result)

        # Original had total 10*100 + 5*200 = 2000
        assert Decimal(result["total_amount"]) == Decimal("2000.00")

        # Verify new SO has 2 items
        new_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM sales_order_item WHERE sales_order_id=?",
            (result["new_sales_order_id"],)).fetchone()["cnt"]
        assert new_count == 2

    def test_amend_so_blocks_if_dns_exist(self, conn, env):
        """Cannot amend if active delivery notes exist."""
        so_id = _create_confirmed_so(conn, env)
        dn = _create_dn(conn, so_id)
        assert is_ok(dn)

        result = call_action(mod.amend_sales_order, conn, ns(
            sales_order_id=so_id, items=None,
        ))
        assert is_error(result)
        assert "delivery note" in result.get("message", "").lower()

    def test_amend_so_blocks_if_invoices_exist(self, conn, env):
        """Cannot amend if active invoices exist."""
        so_id = _create_confirmed_so(conn, env)
        inv = call_action(mod.create_sales_invoice, conn, ns(
            sales_order_id=so_id, delivery_note_id=None,
            customer_id=None, company_id=None,
            posting_date="2026-06-25", items=None,
            tax_template_id=None, due_date=None,
        ))
        assert is_ok(inv)

        result = call_action(mod.amend_sales_order, conn, ns(
            sales_order_id=so_id, items=None,
        ))
        assert is_error(result)
        assert "invoice" in result.get("message", "").lower()

    def test_amend_draft_fails(self, conn, env):
        """Cannot amend a draft SO."""
        items = _items(env, ("item1", "5", "50.00"))
        so = call_action(mod.add_sales_order, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", items=items,
            delivery_date=None, tax_template_id=None,
        ))
        result = call_action(mod.amend_sales_order, conn, ns(
            sales_order_id=so["sales_order_id"], items=None,
        ))
        assert is_error(result)

    def test_amend_with_new_items(self, conn, env):
        """Amendment with new items creates SO with updated totals."""
        so_id = _create_confirmed_so(conn, env)
        new_items = _items(env, ("item1", "5", "300.00"))
        result = call_action(mod.amend_sales_order, conn, ns(
            sales_order_id=so_id, items=new_items,
        ))
        assert is_ok(result)
        assert Decimal(result["total_amount"]) == Decimal("1500.00")


class TestGetAmendmentHistory:
    def test_get_amendment_history(self, conn, env):
        """Amendment chain traces full lineage."""
        so1_id = _create_confirmed_so(conn, env)
        # Amend so1 -> so2
        r1 = call_action(mod.amend_sales_order, conn, ns(
            sales_order_id=so1_id, items=None,
        ))
        assert is_ok(r1)
        so2_id = r1["new_sales_order_id"]

        # Submit so2, then amend so2 -> so3
        call_action(mod.submit_sales_order, conn, ns(sales_order_id=so2_id))
        r2 = call_action(mod.amend_sales_order, conn, ns(
            sales_order_id=so2_id, items=None,
        ))
        assert is_ok(r2)
        so3_id = r2["new_sales_order_id"]

        # Get history from so2 (middle of chain)
        hist = call_action(mod.get_amendment_history, conn, ns(
            sales_order_id=so2_id,
        ))
        assert is_ok(hist)
        assert hist["chain_length"] == 3
        chain_ids = [e["sales_order_id"] for e in hist["amendment_chain"]]
        assert chain_ids == [so1_id, so2_id, so3_id]

    def test_get_amendment_history_no_amendments(self, conn, env):
        """Single SO has a chain of length 1 (just itself)."""
        so_id = _create_confirmed_so(conn, env)
        hist = call_action(mod.get_amendment_history, conn, ns(
            sales_order_id=so_id,
        ))
        assert is_ok(hist)
        assert hist["chain_length"] == 1
        assert hist["amendment_chain"][0]["sales_order_id"] == so_id
        assert hist["amendment_chain"][0]["amended_from"] is None

    def test_get_amendment_history_nonexistent_fails(self, conn):
        """Requesting history for a nonexistent SO fails."""
        result = call_action(mod.get_amendment_history, conn, ns(
            sales_order_id="fake-id",
        ))
        assert is_error(result)
