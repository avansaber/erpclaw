"""Tests for erpclaw-buying purchase receipts and invoices.

Actions tested:
  - create-purchase-receipt, get-purchase-receipt, list-purchase-receipts
  - submit-purchase-receipt, cancel-purchase-receipt
  - create-purchase-invoice, update-purchase-invoice, get-purchase-invoice
  - list-purchase-invoices, submit-purchase-invoice, cancel-purchase-invoice
  - create-debit-note, update-invoice-outstanding
"""
import json
import pytest
from decimal import Decimal
from buying_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
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


# ──────────────────────────────────────────────────────────────────────────────
# Purchase Receipts
# ──────────────────────────────────────────────────────────────────────────────

class TestCreatePurchaseReceipt:
    def test_create_from_po(self, conn, env):
        po_id = _create_confirmed_po(conn, env)
        result = call_action(mod.create_purchase_receipt, conn, ns(
            purchase_order_id=po_id, company_id=env["company_id"],
            posting_date="2026-06-20", items=None,
            purchase_receipt_id=None,
        ))
        assert is_ok(result)
        assert "purchase_receipt_id" in result

    def test_create_missing_po_fails(self, conn, env):
        result = call_action(mod.create_purchase_receipt, conn, ns(
            purchase_order_id=None, company_id=env["company_id"],
            posting_date="2026-06-20", items=None,
            purchase_receipt_id=None,
        ))
        assert is_error(result)


class TestGetPurchaseReceipt:
    def test_get(self, conn, env):
        po_id = _create_confirmed_po(conn, env)
        pr = call_action(mod.create_purchase_receipt, conn, ns(
            purchase_order_id=po_id, company_id=env["company_id"],
            posting_date="2026-06-20", items=None,
            purchase_receipt_id=None,
        ))
        result = call_action(mod.get_purchase_receipt, conn, ns(
            purchase_receipt_id=pr["purchase_receipt_id"],
            company_id=env["company_id"],
        ))
        assert is_ok(result)
        assert "items" in result


class TestListPurchaseReceipts:
    def test_list(self, conn, env):
        po_id = _create_confirmed_po(conn, env)
        call_action(mod.create_purchase_receipt, conn, ns(
            purchase_order_id=po_id, company_id=env["company_id"],
            posting_date="2026-06-20", items=None,
            purchase_receipt_id=None,
        ))
        result = call_action(mod.list_purchase_receipts, conn, ns(
            company_id=env["company_id"], search=None,
            from_date=None, to_date=None, pr_status=None,
            supplier_id=None, limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1


class TestSubmitPurchaseReceipt:
    def test_submit(self, conn, env):
        po_id = _create_confirmed_po(conn, env)
        pr = call_action(mod.create_purchase_receipt, conn, ns(
            purchase_order_id=po_id, company_id=env["company_id"],
            posting_date="2026-06-20", items=None,
            purchase_receipt_id=None,
        ))
        result = call_action(mod.submit_purchase_receipt, conn, ns(
            purchase_receipt_id=pr["purchase_receipt_id"],
        ))
        assert is_ok(result)

        row = conn.execute("SELECT status FROM purchase_receipt WHERE id=?",
                           (pr["purchase_receipt_id"],)).fetchone()
        assert row["status"] == "submitted"


class TestCancelPurchaseReceipt:
    def test_cancel(self, conn, env):
        po_id = _create_confirmed_po(conn, env)
        pr = call_action(mod.create_purchase_receipt, conn, ns(
            purchase_order_id=po_id, company_id=env["company_id"],
            posting_date="2026-06-20", items=None,
            purchase_receipt_id=None,
        ))
        call_action(mod.submit_purchase_receipt, conn, ns(
            purchase_receipt_id=pr["purchase_receipt_id"],
        ))
        result = call_action(mod.cancel_purchase_receipt, conn, ns(
            purchase_receipt_id=pr["purchase_receipt_id"],
        ))
        assert is_ok(result)

        row = conn.execute("SELECT status FROM purchase_receipt WHERE id=?",
                           (pr["purchase_receipt_id"],)).fetchone()
        assert row["status"] == "cancelled"


# ──────────────────────────────────────────────────────────────────────────────
# Purchase Invoices
# ──────────────────────────────────────────────────────────────────────────────

class TestCreatePurchaseInvoice:
    def test_create_from_po(self, conn, env):
        po_id = _create_confirmed_po(conn, env)
        result = call_action(mod.create_purchase_invoice, conn, ns(
            purchase_order_id=po_id, purchase_receipt_id=None,
            supplier_id=None, company_id=env["company_id"],
            posting_date="2026-06-20", due_date=None,
            items=None, tax_template_id=None,
        ))
        assert is_ok(result)
        assert "purchase_invoice_id" in result
        assert Decimal(result["grand_total"]) == Decimal("500.00")

    def test_create_standalone(self, conn, env):
        items = _items(env, ("item1", "5", "100.00"))
        result = call_action(mod.create_purchase_invoice, conn, ns(
            purchase_order_id=None, purchase_receipt_id=None,
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date="2026-07-20",
            items=items, tax_template_id=None,
        ))
        assert is_ok(result)
        assert Decimal(result["grand_total"]) == Decimal("500.00")

    def test_create_missing_supplier_standalone_fails(self, conn, env):
        items = _items(env, ("item1", "1", "10.00"))
        result = call_action(mod.create_purchase_invoice, conn, ns(
            purchase_order_id=None, purchase_receipt_id=None,
            supplier_id=None, company_id=env["company_id"],
            posting_date="2026-06-20", due_date=None,
            items=items, tax_template_id=None,
        ))
        assert is_error(result)


class TestGetPurchaseInvoice:
    def test_get(self, conn, env):
        items = _items(env, ("item1", "3", "100.00"))
        create = call_action(mod.create_purchase_invoice, conn, ns(
            purchase_order_id=None, purchase_receipt_id=None,
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date=None,
            items=items, tax_template_id=None,
        ))
        result = call_action(mod.get_purchase_invoice, conn, ns(
            purchase_invoice_id=create["purchase_invoice_id"],
            company_id=env["company_id"],
        ))
        assert is_ok(result)
        assert "items" in result


class TestListPurchaseInvoices:
    def test_list(self, conn, env):
        items = _items(env, ("item1", "1", "10.00"))
        call_action(mod.create_purchase_invoice, conn, ns(
            purchase_order_id=None, purchase_receipt_id=None,
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date=None,
            items=items, tax_template_id=None,
        ))
        result = call_action(mod.list_purchase_invoices, conn, ns(
            company_id=env["company_id"], search=None,
            from_date=None, to_date=None, pi_status=None,
            supplier_id=None, limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1


class TestSubmitPurchaseInvoice:
    def test_submit_posts_gl(self, conn, env):
        items = _items(env, ("item1", "5", "100.00"))
        create = call_action(mod.create_purchase_invoice, conn, ns(
            purchase_order_id=None, purchase_receipt_id=None,
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date="2026-07-20",
            items=items, tax_template_id=None,
        ))
        result = call_action(mod.submit_purchase_invoice, conn, ns(
            purchase_invoice_id=create["purchase_invoice_id"],
        ))
        assert is_ok(result)

        pi = conn.execute("SELECT status FROM purchase_invoice WHERE id=?",
                          (create["purchase_invoice_id"],)).fetchone()
        assert pi["status"] == "submitted"

        gl_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM gl_entry WHERE voucher_id=?",
            (create["purchase_invoice_id"],)
        ).fetchone()["cnt"]
        assert gl_count >= 2


class TestCancelPurchaseInvoice:
    def test_cancel(self, conn, env):
        items = _items(env, ("item1", "3", "100.00"))
        create = call_action(mod.create_purchase_invoice, conn, ns(
            purchase_order_id=None, purchase_receipt_id=None,
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date="2026-07-20",
            items=items, tax_template_id=None,
        ))
        call_action(mod.submit_purchase_invoice, conn, ns(
            purchase_invoice_id=create["purchase_invoice_id"],
        ))
        result = call_action(mod.cancel_purchase_invoice, conn, ns(
            purchase_invoice_id=create["purchase_invoice_id"],
        ))
        assert is_ok(result)

        pi = conn.execute("SELECT status FROM purchase_invoice WHERE id=?",
                          (create["purchase_invoice_id"],)).fetchone()
        assert pi["status"] == "cancelled"


class TestCreateDebitNote:
    def test_debit_note(self, conn, env):
        items = _items(env, ("item1", "5", "100.00"))
        create = call_action(mod.create_purchase_invoice, conn, ns(
            purchase_order_id=None, purchase_receipt_id=None,
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date="2026-07-20",
            items=items, tax_template_id=None,
        ))
        call_action(mod.submit_purchase_invoice, conn, ns(
            purchase_invoice_id=create["purchase_invoice_id"],
        ))
        return_items = json.dumps([
            {"item_id": env["item1"], "qty": "2", "rate": "100.00"}
        ])
        result = call_action(mod.create_debit_note, conn, ns(
            purchase_invoice_id=create["purchase_invoice_id"],
            against_invoice_id=create["purchase_invoice_id"],
            reason="Defective goods", posting_date="2026-06-25",
            items=return_items, company_id=env["company_id"],
            due_date=None, tax_template_id=None,
        ))
        assert is_ok(result)
        assert "debit_note_id" in result


class TestUpdateInvoiceOutstanding:
    def test_reduce_outstanding(self, conn, env):
        items = _items(env, ("item1", "5", "100.00"))
        create = call_action(mod.create_purchase_invoice, conn, ns(
            purchase_order_id=None, purchase_receipt_id=None,
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date="2026-07-20",
            items=items, tax_template_id=None,
        ))
        call_action(mod.submit_purchase_invoice, conn, ns(
            purchase_invoice_id=create["purchase_invoice_id"],
        ))
        result = call_action(mod.update_invoice_outstanding, conn, ns(
            purchase_invoice_id=create["purchase_invoice_id"],
            amount="200.00",
        ))
        assert is_ok(result)

        pi = conn.execute(
            "SELECT outstanding_amount FROM purchase_invoice WHERE id=?",
            (create["purchase_invoice_id"],)
        ).fetchone()
        assert Decimal(pi["outstanding_amount"]) == Decimal("300.00")
