"""Creating a credit note says it is a draft and names the submit step (m797).

A new credit note posts nothing until ``submit-sales-invoice`` runs on it.
The create response must say so: ``document_status`` draft, ``posted`` false,
and a ``next_step`` naming the submit command with the real credit note id.
"""
import json
from decimal import Decimal

from selling_helpers import call_action, ns, is_ok, load_db_query

mod = load_db_query()


def _items(env, *specs):
    return json.dumps([
        {"item_id": env[k], "qty": q, "rate": r, "warehouse_id": env["warehouse"]}
        for k, q, r in specs
    ])


def _create_submitted_invoice(conn, env, qty="2", rate="100.00"):
    create = call_action(mod.create_sales_invoice, conn, ns(
        sales_order_id=None, delivery_note_id=None,
        customer_id=env["customer"], company_id=env["company_id"],
        posting_date="2026-06-20", due_date="2026-07-20",
        items=_items(env, ("item1", qty, rate)), tax_template_id=None,
        payment_terms_id=None,
    ))
    assert is_ok(create), create
    si_id = create["sales_invoice_id"]
    result = call_action(mod.submit_sales_invoice, conn, ns(sales_invoice_id=si_id))
    assert is_ok(result), result
    return si_id


def _create_credit_note(conn, env, against_invoice_id, qty="1", rate="100.00"):
    return call_action(mod.create_credit_note, conn, ns(
        against_invoice_id=against_invoice_id,
        reason="Returned goods",
        posting_date="2026-06-25",
        items=json.dumps([{"item_id": env["item1"], "qty": qty, "rate": rate}]),
    ))


def _ledger_counts(conn, voucher_id):
    gl_rows = conn.execute(
        "SELECT id FROM gl_entry WHERE voucher_id=? AND is_cancelled=0",
        (voucher_id,)).fetchall()
    ple_rows = conn.execute(
        "SELECT id FROM payment_ledger_entry WHERE voucher_id=? AND delinked=0",
        (voucher_id,)).fetchall()
    return len(gl_rows), len(ple_rows)


def test_create_credit_note_reports_draft(conn, env):
    si_id = _create_submitted_invoice(conn, env)
    result = _create_credit_note(conn, env, si_id)
    assert is_ok(result), result
    assert result["status"] == "ok"
    assert result["document_status"] == "draft"
    assert result["posted"] is False
    cn_id = result["credit_note_id"]
    assert cn_id
    assert "submit-sales-invoice" in result["next_step"]
    assert cn_id in result["next_step"]
    assert result["against_invoice_id"] == si_id
    assert Decimal(result["grand_total"]) == Decimal("-100.00")
    assert result["is_return"] is True
    row = conn.execute(
        "SELECT status FROM sales_invoice WHERE id=?", (cn_id,)).fetchone()
    assert row["status"] == "draft"


def test_draft_credit_note_has_no_ledger_rows(conn, env):
    si_id = _create_submitted_invoice(conn, env)
    result = _create_credit_note(conn, env, si_id)
    assert is_ok(result), result
    gl_count, ple_count = _ledger_counts(conn, result["credit_note_id"])
    assert gl_count == 0
    assert ple_count == 0


def test_submit_after_hint_posts(conn, env):
    si_id = _create_submitted_invoice(conn, env)
    result = _create_credit_note(conn, env, si_id)
    assert is_ok(result), result
    cn_id = result["credit_note_id"]
    assert cn_id in result["next_step"]
    submitted = call_action(mod.submit_sales_invoice, conn, ns(sales_invoice_id=cn_id))
    assert is_ok(submitted), submitted
    row = conn.execute(
        "SELECT status FROM sales_invoice WHERE id=?", (cn_id,)).fetchone()
    assert row["status"] == "submitted"
    gl_count, ple_count = _ledger_counts(conn, cn_id)
    assert gl_count > 0
    assert ple_count > 0
