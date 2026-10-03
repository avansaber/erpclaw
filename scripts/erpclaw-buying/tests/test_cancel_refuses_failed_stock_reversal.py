"""m727: a cancel that could not reverse its stock never reads cancelled (buying)."""
import json
import uuid

from buying_helpers import call_action, ns, is_ok, is_error, load_db_query

mod = load_db_query()


def _boom(conn, voucher_type, voucher_id, posting_date):
    raise ValueError("forced failure")


def _items(env, item_id, qty, rate):
    return json.dumps([{
        "item_id": item_id, "qty": qty, "rate": rate,
        "warehouse_id": env["warehouse"],
    }])


def _submitted_bill(conn, env, item_id, qty, rate):
    created = call_action(mod.create_purchase_invoice, conn, ns(
        purchase_order_id=None, purchase_receipt_id=None,
        supplier_id=env["supplier"], company_id=env["company_id"],
        posting_date="2026-06-20", due_date="2026-07-20",
        items=_items(env, item_id, qty, rate), tax_template_id=None,
    ))
    assert is_ok(created), created
    pi_id = created["purchase_invoice_id"]
    submitted = call_action(mod.submit_purchase_invoice, conn, ns(
        purchase_invoice_id=pi_id))
    assert is_ok(submitted), submitted
    return pi_id


def _submitted_debit_note(conn, env, pi_id, item_id, qty, rate):
    created = call_action(mod.create_debit_note, conn, ns(
        against_invoice_id=pi_id, posting_date="2026-06-25",
        reason="Goods returned",
        items=json.dumps([{"item_id": item_id, "qty": qty, "rate": rate}]),
    ))
    assert is_ok(created), created
    dn_id = created["debit_note_id"]
    submitted = call_action(mod.submit_purchase_invoice, conn, ns(
        purchase_invoice_id=dn_id))
    assert is_ok(submitted), submitted
    return dn_id


def _gl(conn, voucher_type, voucher_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM gl_entry WHERE voucher_type = ? AND voucher_id = ?"
        " ORDER BY id", (voucher_type, voucher_id))]


def _sle(conn, voucher_type, voucher_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM stock_ledger_entry WHERE voucher_type = ?"
        " AND voucher_id = ? ORDER BY id", (voucher_type, voucher_id))]


def _ple(conn, voucher_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM payment_ledger_entry WHERE voucher_id = ? ORDER BY id",
        (voucher_id,))]


def _status(conn, doc_id):
    row = conn.execute(
        "SELECT status FROM purchase_invoice WHERE id = ?", (doc_id,)).fetchone()
    return row["status"]


def _assert_nothing_reversed(conn, voucher_type, doc_id, gl_before, sle_before):
    assert _status(conn, doc_id) == "submitted"
    gl_after = _gl(conn, voucher_type, doc_id)
    assert len(gl_after) == len(gl_before)
    assert [r["is_cancelled"] for r in gl_after] == [r["is_cancelled"] for r in gl_before]
    sle_after = _sle(conn, voucher_type, doc_id)
    assert len(sle_after) == len(sle_before)
    assert all(r["is_cancelled"] == 0 for r in sle_after)
    assert all(r["delinked"] == 0 for r in _ple(conn, doc_id))


def test_invoice_cancel_refuses(conn, env, monkeypatch):
    pi_id = _submitted_bill(conn, env, env["item1"], "2", "50.00")
    assert len(_sle(conn, "purchase_invoice", pi_id)) == 1
    gl_before = _gl(conn, "purchase_invoice", pi_id)
    sle_before = _sle(conn, "purchase_invoice", pi_id)
    monkeypatch.setattr(mod, "reverse_sle_entries", _boom)
    result = call_action(mod.cancel_purchase_invoice, conn, ns(
        purchase_invoice_id=pi_id))
    assert is_error(result), result
    assert result["message"] == "SLE reversal failed: forced failure"
    _assert_nothing_reversed(conn, "purchase_invoice", pi_id, gl_before, sle_before)


def test_credit_note_cancel_refuses(conn, env, monkeypatch):
    pi_id = _submitted_bill(conn, env, env["item1"], "2", "50.00")
    dn_id = _submitted_debit_note(conn, env, pi_id, env["item1"], "1", "50.00")
    assert len(_sle(conn, "debit_note", dn_id)) == 1
    gl_before = _gl(conn, "debit_note", dn_id)
    sle_before = _sle(conn, "debit_note", dn_id)
    monkeypatch.setattr(mod, "reverse_sle_entries", _boom)
    result = call_action(mod.cancel_purchase_invoice, conn, ns(
        purchase_invoice_id=dn_id))
    assert is_error(result), result
    assert result["message"] == "SLE reversal failed: forced failure"
    _assert_nothing_reversed(conn, "debit_note", dn_id, gl_before, sle_before)


def _non_stock_item(conn):
    iid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO item (id, item_name, item_code, stock_uom, is_stock_item)"
        " VALUES (?, ?, ?, 'Each', 0)",
        (iid, "Service %s" % iid[:6], "SVC-%s" % iid[:6]))
    conn.commit()
    return iid


def test_no_stock_rows_cancels(conn, env, monkeypatch):
    svc = _non_stock_item(conn)
    pi_id = _submitted_bill(conn, env, svc, "2", "50.00")
    assert _sle(conn, "purchase_invoice", pi_id) == []
    monkeypatch.setattr(mod, "reverse_sle_entries", _boom)
    result = call_action(mod.cancel_purchase_invoice, conn, ns(
        purchase_invoice_id=pi_id))
    assert is_ok(result), result
    assert _status(conn, pi_id) == "cancelled"
    assert result["sle_reversals"] == 0


def test_unpatched_cancel_still_reverses(conn, env):
    pi_id = _submitted_bill(conn, env, env["item1"], "2", "50.00")
    result = call_action(mod.cancel_purchase_invoice, conn, ns(
        purchase_invoice_id=pi_id))
    assert is_ok(result), result
    assert _status(conn, pi_id) == "cancelled"
    assert result["sle_reversals"] == 1
