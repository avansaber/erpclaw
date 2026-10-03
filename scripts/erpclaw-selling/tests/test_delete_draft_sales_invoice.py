"""delete-sales-invoice (drafts only)."""
import importlib.util
import json
import os
import sys
import uuid
from unittest.mock import patch

import pytest

from selling_helpers import call_action, ns, is_error, is_ok, load_db_query

mod = load_db_query()

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(os.path.dirname(_TESTS_DIR))


def _load(name, rel_path):
    path = os.path.join(_SCRIPTS_DIR, rel_path)
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


pay = _load("db_query_payments_del", "erpclaw-payments/db_query.py")


def _items(env, *specs):
    return json.dumps([
        {"item_id": env[k], "qty": q, "rate": r, "warehouse_id": env["warehouse"]}
        for k, q, r in specs
    ])


def _draft_invoice(conn, env, qty="2", rate="100.00"):
    create = call_action(mod.create_sales_invoice, conn, ns(
        sales_order_id=None, delivery_note_id=None,
        customer_id=env["customer"], company_id=env["company_id"],
        posting_date="2026-06-20", due_date="2026-07-20",
        items=_items(env, ("item1", qty, rate)), tax_template_id=None,
        payment_terms_id=None,
    ))
    assert is_ok(create), create
    return create["sales_invoice_id"]


def _snapshot(conn, si_id):
    snap = {}
    snap["si"] = [tuple(r) for r in conn.execute(
        "SELECT id, status, grand_total, customer_id FROM sales_invoice WHERE id=?",
        (si_id,)).fetchall()]
    snap["items"] = [tuple(r) for r in conn.execute(
        "SELECT id, sales_invoice_id FROM sales_invoice_item WHERE sales_invoice_id=?",
        (si_id,)).fetchall()]
    snap["gl"] = [tuple(r) for r in conn.execute(
        "SELECT id FROM gl_entry WHERE voucher_id=?", (si_id,)).fetchall()]
    snap["ple"] = [tuple(r) for r in conn.execute(
        "SELECT id FROM payment_ledger_entry WHERE voucher_id=? OR against_voucher_id=?",
        (si_id, si_id)).fetchall()]
    snap["audit"] = [tuple(r) for r in conn.execute(
        "SELECT skill, action, entity_type, entity_id, old_values, new_values FROM audit_log").fetchall()]
    return snap


def test_delete_draft_removes_invoice_and_children(conn, env):
    si_id = _draft_invoice(conn, env)
    inv = conn.execute(
        "SELECT status, grand_total, customer_id FROM sales_invoice WHERE id=?",
        (si_id,)).fetchone()
    assert inv["status"] == "draft"
    n_items = conn.execute(
        "SELECT COUNT(*) FROM sales_invoice_item WHERE sales_invoice_id=?",
        (si_id,)).fetchone()[0]
    assert n_items >= 1
    r = call_action(mod.delete_sales_invoice, conn, ns(sales_invoice_id=si_id))
    assert is_ok(r), r
    assert r["document_status"] == "deleted"
    assert r["deleted"] is True
    assert r["sales_invoice_id"] == si_id
    assert conn.execute(
        "SELECT id FROM sales_invoice WHERE id=?", (si_id,)).fetchone() is None
    assert conn.execute(
        "SELECT id FROM sales_invoice_item WHERE sales_invoice_id=?",
        (si_id,)).fetchall() == []
    rows = conn.execute(
        "SELECT skill, action, entity_type, entity_id, old_values FROM audit_log "
        "WHERE skill='erpclaw-selling' AND action='delete-sales-invoice' "
        "AND entity_type='sales_invoice' AND entity_id=?", (si_id,)).fetchall()
    assert len(rows) == 1
    old = json.loads(rows[0]["old_values"])
    assert old["status"] == "draft"
    assert old["grand_total"] == inv["grand_total"]
    assert old["customer_id"] == inv["customer_id"]


def test_delete_submitted_refuses(conn, env):
    si_id = _draft_invoice(conn, env)
    assert is_ok(call_action(mod.submit_sales_invoice, conn, ns(sales_invoice_id=si_id)))
    before = _snapshot(conn, si_id)
    r = call_action(mod.delete_sales_invoice, conn, ns(sales_invoice_id=si_id))
    assert is_error(r)
    inv = conn.execute("SELECT status FROM sales_invoice WHERE id=?", (si_id,)).fetchone()
    assert r["message"] == f"Cannot delete: sales invoice is '{inv['status']}' (only 'draft' can be deleted)"
    assert _snapshot(conn, si_id) == before


def test_delete_draft_with_allocation_refuses(conn, env):
    si_id = _draft_invoice(conn, env)
    created = call_action(pay.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="receive",
        posting_date="2026-06-25", party_type="customer", party_id=env["customer"],
        paid_from_account=env["ar"], paid_to_account=env["cash"],
        paid_amount="200.00", exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None,
        allocations=json.dumps([{"voucher_type": "sales_invoice", "voucher_id": si_id,
                                 "allocated_amount": "200.00"}]),
        deductions=None,
    ))
    assert is_ok(created), created
    pe_id = created["payment_entry_id"]
    before = _snapshot(conn, si_id)
    r = call_action(mod.delete_sales_invoice, conn, ns(sales_invoice_id=si_id))
    assert is_error(r)
    assert r["message"] == f"Cannot delete: sales invoice {si_id} is referenced by payment {pe_id}"
    assert _snapshot(conn, si_id) == before


def test_delete_missing_id_and_not_found(conn, env):
    r = call_action(mod.delete_sales_invoice, conn, ns(sales_invoice_id=None))
    assert is_error(r)
    assert r["message"] == "--sales-invoice-id is required"
    r = call_action(mod.delete_sales_invoice, conn, ns(sales_invoice_id="no-such-id"))
    assert is_error(r)
    assert r["message"] == "Sales invoice no-such-id not found"


def _seed_employee(conn, company_id):
    eid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO employee (id, first_name, full_name, date_of_joining, company_id) "
        "VALUES (?, ?, ?, '2026-01-01', ?)",
        (eid, "Sam", "Sam T", company_id))
    conn.commit()
    return eid


def _seed_rate_plan(conn):
    rid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO rate_plan (id, name, plan_type, effective_from) VALUES (?, ?, 'flat', '2026-01-01')",
        (rid, f"RP-{rid[:6]}"))
    conn.commit()
    return rid


def _seed_billing_run(conn, company_id):
    rid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO billing_run (id, run_type, company_id, as_of_date) VALUES (?, 'usage_billing', ?, '2026-06-20')",
        (rid, company_id))
    conn.commit()
    return rid


def test_delete_refused_by_credit_note(conn, env):
    si_id = _draft_invoice(conn, env)
    cn_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO sales_invoice (id, customer_id, posting_date, due_date, total_amount, "
        "tax_amount, grand_total, outstanding_amount, status, is_return, return_against, "
        "update_stock, company_id) VALUES (?, ?, '2026-06-21', '2026-07-21', '-200.00', "
        "'0', '-200.00', '-200.00', 'draft', 1, ?, 1, ?)",
        (cn_id, env["customer"], si_id, env["company_id"]))
    conn.commit()
    before = _snapshot(conn, si_id)
    r = call_action(mod.delete_sales_invoice, conn, ns(sales_invoice_id=si_id))
    assert is_error(r)
    assert r["message"] == f"Cannot delete: sales invoice {si_id} has credit note {cn_id}"
    assert _snapshot(conn, si_id) == before


def test_delete_refused_by_timesheet(conn, env):
    si_id = _draft_invoice(conn, env)
    eid = _seed_employee(conn, env["company_id"])
    ts_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO timesheet (id, employee_id, start_date, end_date, sales_invoice_id, company_id) "
        "VALUES (?, ?, '2026-06-01', '2026-06-07', ?, ?)",
        (ts_id, eid, si_id, env["company_id"]))
    conn.commit()
    before = _snapshot(conn, si_id)
    r = call_action(mod.delete_sales_invoice, conn, ns(sales_invoice_id=si_id))
    assert is_error(r)
    assert r["message"] == f"Cannot delete: sales invoice {si_id} is referenced by timesheet {ts_id}"
    assert _snapshot(conn, si_id) == before


def test_delete_refused_by_billing_period(conn, env):
    si_id = _draft_invoice(conn, env)
    rp = _seed_rate_plan(conn)
    bp_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO billing_period (id, customer_id, rate_plan_id, period_start, period_end, invoice_id) "
        "VALUES (?, ?, ?, '2026-06-01', '2026-06-30', ?)",
        (bp_id, env["customer"], rp, si_id))
    conn.commit()
    before = _snapshot(conn, si_id)
    r = call_action(mod.delete_sales_invoice, conn, ns(sales_invoice_id=si_id))
    assert is_error(r)
    assert r["message"] == f"Cannot delete: sales invoice {si_id} is referenced by billing_period {bp_id}"
    assert _snapshot(conn, si_id) == before


def test_delete_refused_by_billing_run_target(conn, env):
    si_id = _draft_invoice(conn, env)
    run_id = _seed_billing_run(conn, env["company_id"])
    tgt_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO billing_run_target (id, billing_run_id, target_type, target_id, result_voucher_id) "
        "VALUES (?, ?, 'meter', ?, ?)",
        (tgt_id, run_id, str(uuid.uuid4()), si_id))
    conn.commit()
    before = _snapshot(conn, si_id)
    r = call_action(mod.delete_sales_invoice, conn, ns(sales_invoice_id=si_id))
    assert is_error(r)
    assert r["message"] == f"Cannot delete: sales invoice {si_id} is referenced by billing_run_target {tgt_id}"
    assert _snapshot(conn, si_id) == before


def test_delete_requires_confirmation_through_router(conn, env):
    router_path = os.path.join(_SCRIPTS_DIR, "db_query.py")
    spec = importlib.util.spec_from_file_location("erpclaw_router_del", router_path)
    router = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(router)
    assert "delete-sales-invoice" in router.DANGEROUS_ACTIONS
    assert router.ACTION_MAP.get("delete-sales-invoice") == "erpclaw-selling"
    import sys as _sys
    with patch.object(_sys, "argv", ["db_query.py", "--action", "delete-sales-invoice"]):
        try:
            router._gate_dangerous_action("delete-sales-invoice")
        except SystemExit as e:
            assert e.code == 2
        else:
            raise AssertionError("gate did not exit")
