"""Tests for erpclaw-selling delivery notes and sales invoices.

Actions tested:
  - create-delivery-note, get-delivery-note, list-delivery-notes
  - submit-delivery-note, cancel-delivery-note
  - create-sales-invoice, update-sales-invoice, get-sales-invoice
  - list-sales-invoices, submit-sales-invoice, cancel-sales-invoice
  - create-credit-note, list-credit-notes
  - update-invoice-outstanding
"""
import json
import pytest
from decimal import Decimal
from selling_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
)

mod = load_db_query()


def _items(env, *specs):
    """Build items JSON. Each spec = (item_key, qty, rate).
    Auto-adds warehouse_id for stock operations."""
    return json.dumps([
        {"item_id": env[k], "qty": q, "rate": r, "warehouse_id": env["warehouse"]}
        for k, q, r in specs
    ])


def _create_confirmed_so(conn, env, items_str=None):
    """Helper: create and confirm a sales order."""
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


# ──────────────────────────────────────────────────────────────────────────────
# Delivery Notes
# ──────────────────────────────────────────────────────────────────────────────

class TestCreateDeliveryNote:
    def test_create_from_so(self, conn, env):
        so_id = _create_confirmed_so(conn, env)
        result = call_action(mod.create_delivery_note, conn, ns(
            sales_order_id=so_id, posting_date="2026-06-20",
            items=None,
        ))
        assert is_ok(result)
        assert "delivery_note_id" in result

        dn = conn.execute("SELECT * FROM delivery_note WHERE id=?",
                          (result["delivery_note_id"],)).fetchone()
        assert dn is not None
        assert dn["status"] == "draft"
        assert dn["sales_order_id"] == so_id

    def test_create_missing_so_fails(self, conn, env):
        result = call_action(mod.create_delivery_note, conn, ns(
            sales_order_id=None, posting_date="2026-06-20",
            items=None,
        ))
        assert is_error(result)

    def test_create_from_draft_so_fails(self, conn, env):
        items = _items(env, ("item1", "5", "50.00"))
        so = call_action(mod.add_sales_order, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", items=items,
            delivery_date="2026-07-01", tax_template_id=None,
        ))
        result = call_action(mod.create_delivery_note, conn, ns(
            sales_order_id=so["sales_order_id"], posting_date="2026-06-20",
            items=None,
        ))
        assert is_error(result)


class TestGetDeliveryNote:
    def test_get_with_items(self, conn, env):
        so_id = _create_confirmed_so(conn, env)
        dn = call_action(mod.create_delivery_note, conn, ns(
            sales_order_id=so_id, posting_date="2026-06-20",
            items=None,
        ))
        result = call_action(mod.get_delivery_note, conn, ns(
            delivery_note_id=dn["delivery_note_id"],
        ))
        assert is_ok(result)
        assert "items" in result

    def test_get_nonexistent_fails(self, conn):
        result = call_action(mod.get_delivery_note, conn, ns(
            delivery_note_id="fake-id",
        ))
        assert is_error(result)


class TestListDeliveryNotes:
    def test_list(self, conn, env):
        so_id = _create_confirmed_so(conn, env)
        call_action(mod.create_delivery_note, conn, ns(
            sales_order_id=so_id, posting_date="2026-06-20",
            items=None,
        ))
        result = call_action(mod.list_delivery_notes, conn, ns(
            company_id=env["company_id"], customer_id=None,
            sales_order_id=None, doc_status=None,
            from_date=None, to_date=None,
            search=None, limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1


class TestSubmitDeliveryNote:
    def test_submit_posts_sle(self, conn, env):
        so_id = _create_confirmed_so(conn, env)
        dn = call_action(mod.create_delivery_note, conn, ns(
            sales_order_id=so_id, posting_date="2026-06-20",
            items=None,
        ))
        result = call_action(mod.submit_delivery_note, conn, ns(
            delivery_note_id=dn["delivery_note_id"],
        ))
        assert is_ok(result)

        # DN should be marked submitted in DB
        row = conn.execute("SELECT status FROM delivery_note WHERE id=?",
                           (dn["delivery_note_id"],)).fetchone()
        assert row["status"] == "submitted"

        # SLE entries should have been posted
        assert result.get("sle_entries_created", 0) >= 1


class TestCancelDeliveryNote:
    def test_cancel_submitted(self, conn, env):
        so_id = _create_confirmed_so(conn, env)
        dn = call_action(mod.create_delivery_note, conn, ns(
            sales_order_id=so_id, posting_date="2026-06-20",
            items=None,
        ))
        call_action(mod.submit_delivery_note, conn, ns(
            delivery_note_id=dn["delivery_note_id"],
        ))
        result = call_action(mod.cancel_delivery_note, conn, ns(
            delivery_note_id=dn["delivery_note_id"],
        ))
        assert is_ok(result)

        row = conn.execute("SELECT status FROM delivery_note WHERE id=?",
                           (dn["delivery_note_id"],)).fetchone()
        assert row["status"] == "cancelled"


# ──────────────────────────────────────────────────────────────────────────────
# Sales Invoices
# ──────────────────────────────────────────────────────────────────────────────

class TestCreateSalesInvoice:
    def test_create_from_so(self, conn, env):
        so_id = _create_confirmed_so(conn, env)
        result = call_action(mod.create_sales_invoice, conn, ns(
            sales_order_id=so_id, delivery_note_id=None,
            customer_id=None, company_id=None,
            posting_date="2026-06-20", due_date=None,
            items=None, tax_template_id=None,
            payment_terms_id=None,
        ))
        assert is_ok(result)
        assert "sales_invoice_id" in result
        assert Decimal(result["grand_total"]) == Decimal("1000.00")

        si = conn.execute("SELECT * FROM sales_invoice WHERE id=?",
                          (result["sales_invoice_id"],)).fetchone()
        assert si["status"] == "draft"
        assert si["sales_order_id"] == so_id

    def test_create_standalone(self, conn, env):
        items = _items(env, ("item1", "5", "200.00"))
        result = call_action(mod.create_sales_invoice, conn, ns(
            sales_order_id=None, delivery_note_id=None,
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date="2026-07-20",
            items=items, tax_template_id=None,
            payment_terms_id=None,
        ))
        assert is_ok(result)
        assert Decimal(result["grand_total"]) == Decimal("1000.00")

    def test_create_missing_customer_standalone_fails(self, conn, env):
        items = _items(env, ("item1", "1", "10.00"))
        result = call_action(mod.create_sales_invoice, conn, ns(
            sales_order_id=None, delivery_note_id=None,
            customer_id=None, company_id=env["company_id"],
            posting_date="2026-06-20", due_date=None,
            items=items, tax_template_id=None,
            payment_terms_id=None,
        ))
        assert is_error(result)


class TestGetSalesInvoice:
    def test_get_with_items(self, conn, env):
        items = _items(env, ("item1", "3", "100.00"))
        create = call_action(mod.create_sales_invoice, conn, ns(
            sales_order_id=None, delivery_note_id=None,
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date=None,
            items=items, tax_template_id=None,
            payment_terms_id=None,
        ))
        result = call_action(mod.get_sales_invoice, conn, ns(
            sales_invoice_id=create["sales_invoice_id"],
        ))
        assert is_ok(result)
        assert "items" in result
        assert len(result["items"]) == 1


class TestListSalesInvoices:
    def test_list(self, conn, env):
        items = _items(env, ("item1", "1", "10.00"))
        call_action(mod.create_sales_invoice, conn, ns(
            sales_order_id=None, delivery_note_id=None,
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date=None,
            items=items, tax_template_id=None,
            payment_terms_id=None,
        ))
        result = call_action(mod.list_sales_invoices, conn, ns(
            company_id=env["company_id"], customer_id=None,
            sales_order_id=None, doc_status=None,
            from_date=None, to_date=None,
            search=None, limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1


class TestSubmitSalesInvoice:
    def test_submit_posts_gl(self, conn, env):
        items = _items(env, ("item1", "5", "100.00"))
        create = call_action(mod.create_sales_invoice, conn, ns(
            sales_order_id=None, delivery_note_id=None,
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date="2026-07-20",
            items=items, tax_template_id=None,
            payment_terms_id=None,
        ))
        result = call_action(mod.submit_sales_invoice, conn, ns(
            sales_invoice_id=create["sales_invoice_id"],
        ))
        assert is_ok(result)

        # SI should be marked submitted in DB
        si = conn.execute("SELECT status FROM sales_invoice WHERE id=?",
                          (create["sales_invoice_id"],)).fetchone()
        assert si["status"] == "submitted"

        # Should have posted GL entries
        gl_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM gl_entry WHERE voucher_id=?",
            (create["sales_invoice_id"],)
        ).fetchone()["cnt"]
        assert gl_count >= 2  # At least AR debit + Revenue credit

    def test_submit_nonexistent_fails(self, conn):
        result = call_action(mod.submit_sales_invoice, conn, ns(
            sales_invoice_id="fake-id",
        ))
        assert is_error(result)


class TestCancelSalesInvoice:
    def test_cancel_submitted(self, conn, env):
        items = _items(env, ("item1", "3", "100.00"))
        create = call_action(mod.create_sales_invoice, conn, ns(
            sales_order_id=None, delivery_note_id=None,
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date="2026-07-20",
            items=items, tax_template_id=None,
            payment_terms_id=None,
        ))
        call_action(mod.submit_sales_invoice, conn, ns(
            sales_invoice_id=create["sales_invoice_id"],
        ))
        result = call_action(mod.cancel_sales_invoice, conn, ns(
            sales_invoice_id=create["sales_invoice_id"],
        ))
        assert is_ok(result)

        si = conn.execute("SELECT status FROM sales_invoice WHERE id=?",
                          (create["sales_invoice_id"],)).fetchone()
        assert si["status"] == "cancelled"


class TestCreateCreditNote:
    def test_credit_note_against_invoice(self, conn, env):
        items = _items(env, ("item1", "5", "100.00"))
        create = call_action(mod.create_sales_invoice, conn, ns(
            sales_order_id=None, delivery_note_id=None,
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date="2026-07-20",
            items=items, tax_template_id=None,
            payment_terms_id=None,
        ))
        call_action(mod.submit_sales_invoice, conn, ns(
            sales_invoice_id=create["sales_invoice_id"],
        ))
        # Credit note requires items specifying return quantities
        return_items = json.dumps([
            {"item_id": env["item1"], "qty": "2", "rate": "100.00"}
        ])
        result = call_action(mod.create_credit_note, conn, ns(
            against_invoice_id=create["sales_invoice_id"],
            reason="Returned goods", posting_date="2026-06-25",
            items=return_items,
        ))
        assert is_ok(result)
        assert "credit_note_id" in result


class TestListCreditNotes:
    def test_list(self, conn, env):
        result = call_action(mod.list_credit_notes, conn, ns(
            company_id=env["company_id"], customer_id=None,
            doc_status=None, from_date=None, to_date=None,
            limit=None, offset=None,
        ))
        assert is_ok(result)


class TestUpdateInvoiceOutstanding:
    def test_reduce_outstanding(self, conn, env):
        items = _items(env, ("item1", "5", "100.00"))
        create = call_action(mod.create_sales_invoice, conn, ns(
            sales_order_id=None, delivery_note_id=None,
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date="2026-07-20",
            items=items, tax_template_id=None,
            payment_terms_id=None,
        ))
        call_action(mod.submit_sales_invoice, conn, ns(
            sales_invoice_id=create["sales_invoice_id"],
        ))
        result = call_action(mod.update_invoice_outstanding, conn, ns(
            sales_invoice_id=create["sales_invoice_id"],
            amount="200.00",
        ))
        assert is_ok(result)

        si = conn.execute(
            "SELECT outstanding_amount FROM sales_invoice WHERE id=?",
            (create["sales_invoice_id"],)
        ).fetchone()
        assert Decimal(si["outstanding_amount"]) == Decimal("300.00")
