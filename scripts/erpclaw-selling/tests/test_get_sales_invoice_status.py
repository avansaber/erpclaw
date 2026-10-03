"""get-sales-invoice surfaces the document state as document_status; envelope status stays ok."""
import json

import pytest

from selling_helpers import (
    build_selling_env, call_action, is_ok, load_db_query, ns,
)

mod = load_db_query()


def _items(env, *specs):
    return json.dumps([
        {"item_id": env[k], "qty": q, "rate": r, "warehouse_id": env["warehouse"]}
        for k, q, r in specs
    ])


def test_get_sales_invoice_reports_draft_then_submitted(conn):
    env = build_selling_env(conn)
    created = call_action(mod.create_sales_invoice, conn, ns(
        sales_order_id=None, delivery_note_id=None,
        customer_id=env["customer"], company_id=env["company_id"],
        posting_date="2026-06-20", due_date=None,
        items=_items(env, ("item1", "1", "10.00")), tax_template_id=None,
        payment_terms_id=None,
    ))
    assert is_ok(created), created
    si_id = created["sales_invoice_id"]

    draft = call_action(mod.get_sales_invoice, conn, ns(sales_invoice_id=si_id))
    assert draft["status"] == "ok"
    assert draft["document_status"] == "draft"

    submitted = call_action(mod.submit_sales_invoice, conn, ns(sales_invoice_id=si_id))
    assert is_ok(submitted), submitted

    fetched = call_action(mod.get_sales_invoice, conn, ns(sales_invoice_id=si_id))
    assert fetched["status"] == "ok"
    assert fetched["document_status"] == "submitted"
