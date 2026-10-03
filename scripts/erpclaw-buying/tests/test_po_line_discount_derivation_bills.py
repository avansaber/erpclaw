"""Bills and debit notes carry their exact share of the order-line discount.

Task m658b2: bill (purchase-invoice) and debit-note lines store the same
discount_share preview as receipt lines, re-derived at submit; posting and
header totals stay gross.
"""
import json
import pytest
from decimal import Decimal
from buying_helpers import call_action, ns, is_error, is_ok, load_db_query
from erpclaw_lib.query import P, Q, Table, fn
from erpclaw_lib.vendor.pypika.terms import ValueWrapper

mod = load_db_query()


def _disc_po_items(env, qty="3", rate="10.00", discount_amount="1.00",
                   item_key="item1"):
    return json.dumps([{"item_id": env[item_key], "qty": qty, "rate": rate,
                        "discount_amount": discount_amount,
                        "warehouse_id": env["warehouse"]}])


def _create_confirmed_po(conn, env, items_str=None):
    items_str = items_str or _disc_po_items(env)
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


def _po_item_id(conn, po_id):
    row = conn.execute(
        "SELECT id FROM purchase_order_item WHERE purchase_order_id = ?",
        (po_id,)).fetchone()
    assert row is not None
    return row["id"]


def _make_receipt(conn, env, po_id, qty, po_item_id=None):
    po_item_id = po_item_id or _po_item_id(conn, po_id)
    items = json.dumps([{"purchase_order_item_id": po_item_id,
                         "qty": qty}])
    res = call_action(mod.create_purchase_receipt, conn, ns(
        purchase_order_id=po_id, company_id=env["company_id"],
        posting_date="2026-06-20", items=items,
        purchase_receipt_id=None,
    ))
    assert is_ok(res), f"receipt create failed: {res}"
    return res["purchase_receipt_id"]


def _submit_receipt(conn, pr_id):
    res = call_action(mod.submit_purchase_receipt, conn, ns(
        purchase_receipt_id=pr_id,
    ))
    assert is_ok(res), f"receipt submit failed: {res}"
    return res


def _bill_from_order(conn, env, po_id):
    res = call_action(mod.create_purchase_invoice, conn, ns(
        purchase_order_id=po_id, purchase_receipt_id=None,
        supplier_id=None, company_id=env["company_id"],
        posting_date="2026-06-21", due_date=None,
        items=None, tax_template_id=None,
    ))
    assert is_ok(res), f"bill from order failed: {res}"
    return res["purchase_invoice_id"]


def _bill_from_receipt(conn, env, pr_id):
    res = call_action(mod.create_purchase_invoice, conn, ns(
        purchase_order_id=None, purchase_receipt_id=pr_id,
        supplier_id=None, company_id=env["company_id"],
        posting_date="2026-06-21", due_date=None,
        items=None, tax_template_id=None,
    ))
    assert is_ok(res), f"bill from receipt failed: {res}"
    return res["purchase_invoice_id"]


def _bill_lines(conn, bill_id):
    return conn.execute(
        "SELECT * FROM purchase_invoice_item WHERE purchase_invoice_id = ?"
        " ORDER BY rowid", (bill_id,)).fetchall()


def _submit_bill(conn, bill_id):
    res = call_action(mod.submit_purchase_invoice, conn, ns(
        purchase_invoice_id=bill_id,
    ))
    assert is_ok(res), f"bill submit failed: {res}"
    return res


def _bill_status(conn, bill_id):
    return conn.execute(
        "SELECT status FROM purchase_invoice WHERE id = ?",
        (bill_id,)).fetchone()["status"]


def _trim_bill_to_one(conn, bill_id):
    """Direct PyPika update: shrink a draft 3-unit bill to a 1-unit bill."""
    line_id = _bill_lines(conn, bill_id)[0]["id"]
    pii_t = Table("purchase_invoice_item")
    uq = (Q.update(pii_t)
          .set(pii_t.quantity, P())
          .set(pii_t.amount, P())
          .where(pii_t.id == P()))
    conn.execute(uq.get_sql(), ("1.00", "10.00", line_id))
    pi_t = Table("purchase_invoice")
    uq = (Q.update(pi_t)
          .set(pi_t.total_amount, P())
          .set(pi_t.grand_total, P())
          .set(pi_t.outstanding_amount, P())
          .where(pi_t.id == P()))
    conn.execute(uq.get_sql(), ("10.00", "10.00", "10.00", bill_id))
    conn.commit()


def _mark_bill_paid(conn, bill_id):
    pi_t = Table("purchase_invoice")
    uq = (Q.update(pi_t)
          .set(pi_t.status, ValueWrapper("paid"))
          .where(pi_t.id == P()))
    conn.execute(uq.get_sql(), (bill_id,))
    conn.commit()


def test_bill_lines(conn, env):
    # A bill from the order after receipts 1+2 stores the whole discount.
    po_a = _create_confirmed_po(conn, env)
    poi_a = _po_item_id(conn, po_a)
    _submit_receipt(conn, _make_receipt(conn, env, po_a, "1", poi_a))
    _submit_receipt(conn, _make_receipt(conn, env, po_a, "2", poi_a))
    bill_a = _bill_from_order(conn, env, po_a)
    assert _bill_lines(conn, bill_a)[0]["discount_amount"] == "1.00"
    res_a = _submit_bill(conn, bill_a)
    assert "discount_rederived" not in res_a
    assert _bill_lines(conn, bill_a)[0]["discount_amount"] == "1.00"

    # A bill from the second receipt copies that receipt line's share.
    po_b = _create_confirmed_po(conn, env)
    poi_b = _po_item_id(conn, po_b)
    _submit_receipt(conn, _make_receipt(conn, env, po_b, "1", poi_b))
    pr_b2 = _make_receipt(conn, env, po_b, "2", poi_b)
    _submit_receipt(conn, pr_b2)
    bill_b = _bill_from_receipt(conn, env, pr_b2)
    assert _bill_lines(conn, bill_b)[0]["discount_amount"] == "0.67"
    res_b = _submit_bill(conn, bill_b)
    assert "discount_rederived" not in res_b

    # Two 1-unit bills (first marked paid) then a third stores the remainder.
    po_c = _create_confirmed_po(conn, env)
    _submit_receipt(conn, _make_receipt(conn, env, po_c, "3"))
    bill_c1 = _bill_from_order(conn, env, po_c)
    _trim_bill_to_one(conn, bill_c1)
    _submit_bill(conn, bill_c1)
    assert _bill_lines(conn, bill_c1)[0]["discount_amount"] == "0.33"
    _mark_bill_paid(conn, bill_c1)
    bill_c2 = _bill_from_order(conn, env, po_c)
    _trim_bill_to_one(conn, bill_c2)
    _submit_bill(conn, bill_c2)
    assert _bill_lines(conn, bill_c2)[0]["discount_amount"] == "0.33"
    assert _bill_status(conn, bill_c2) == "submitted"
    bill_c3 = _bill_from_order(conn, env, po_c)
    assert _bill_lines(conn, bill_c3)[0]["discount_amount"] == "0.34"
    _submit_bill(conn, bill_c3)
    assert _bill_lines(conn, bill_c3)[0]["discount_amount"] == "0.34"

    # A bill from a draft receipt is refused and adds no invoice row.
    po_d = _create_confirmed_po(conn, env)
    pr_d = _make_receipt(conn, env, po_d, "3")
    before = conn.execute(
        "SELECT COUNT(*) AS n FROM purchase_invoice").fetchone()["n"]
    refused = call_action(mod.create_purchase_invoice, conn, ns(
        purchase_order_id=None, purchase_receipt_id=pr_d,
        supplier_id=None, company_id=env["company_id"],
        posting_date="2026-06-21", due_date=None,
        items=None, tax_template_id=None,
    ))
    assert is_error(refused), refused
    assert refused["message"] == (
        f"Cannot create a bill from receipt {pr_d}: "
        "it is 'draft' (must be 'submitted')")
    after = conn.execute(
        "SELECT COUNT(*) AS n FROM purchase_invoice").fetchone()["n"]
    assert after == before

    # A stale draft preview is re-derived at submit, with audit trail.
    po_e = _create_confirmed_po(conn, env)
    _submit_receipt(conn, _make_receipt(conn, env, po_e, "3"))
    bill_e = _bill_from_order(conn, env, po_e)
    line_e = _bill_lines(conn, bill_e)[0]["id"]
    assert _bill_lines(conn, bill_e)[0]["discount_amount"] == "1.00"
    pii_t = Table("purchase_invoice_item")
    uq = (Q.update(pii_t)
          .set(pii_t.discount_amount, P())
          .where(pii_t.id == P()))
    conn.execute(uq.get_sql(), ("0.50", line_e))
    conn.commit()
    res_e = _submit_bill(conn, bill_e)
    assert res_e["discount_rederived"] == [
        {"line": line_e, "old": "0.50", "new": "1.00"}]
    assert _bill_lines(conn, bill_e)[0]["discount_amount"] == "1.00"
    audit_row = conn.execute(
        "SELECT old_values, new_values FROM audit_log WHERE action = ?"
        " AND entity_id = ? ORDER BY rowid DESC LIMIT 1",
        ("submit-purchase-invoice", bill_e)).fetchone()
    assert audit_row is not None
    old_v = json.loads(audit_row["old_values"] or "{}")
    new_v = json.loads(audit_row["new_values"] or "{}")
    assert old_v["discount_amount"][line_e] == "0.50"
    assert {"line": line_e, "old": "0.50", "new": "1.00"} in \
        new_v["discount_rederived"]

    # A receipt-derived line that drifted refuses submit and posts nothing.
    po_f = _create_confirmed_po(conn, env)
    pr_f = _make_receipt(conn, env, po_f, "1")
    _submit_receipt(conn, pr_f)
    assert conn.execute(
        "SELECT discount_amount FROM purchase_receipt_item"
        " WHERE purchase_receipt_id = ?",
        (pr_f,)).fetchone()["discount_amount"] == "0.33"
    bill_f = _bill_from_receipt(conn, env, pr_f)
    line_f = _bill_lines(conn, bill_f)[0]
    conn.execute(uq.get_sql(), ("0", line_f["id"]))
    conn.commit()
    bad = call_action(mod.submit_purchase_invoice, conn, ns(
        purchase_invoice_id=bill_f,
    ))
    assert is_error(bad), bad
    assert bad["message"] == (
        f"Bill line for item {line_f['item_id']}: discount 0 differs from"
        " its receipt line discount 0.33; a bill derived from a receipt"
        " carries the receipt's discount")
    assert _bill_status(conn, bill_f) == "draft"
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM gl_entry WHERE voucher_id = ?",
        (bill_f,)).fetchone()["n"] == 0


def test_standalone_bill_and_update_refusals(conn, env):
    standalone = call_action(mod.create_purchase_invoice, conn, ns(
        purchase_order_id=None, purchase_receipt_id=None,
        supplier_id=env["supplier"], company_id=env["company_id"],
        posting_date="2026-06-21", due_date=None,
        items=json.dumps([{"item_id": env["item1"], "qty": "3",
                           "rate": "10.00", "discount_amount": "1.00"}]),
        tax_template_id=None,
    ))
    assert is_ok(standalone), standalone
    assert standalone["total_amount"] == "29.00"
    assert _bill_lines(
        conn, standalone["purchase_invoice_id"])[0]["discount_amount"] == "1.00"

    neg = call_action(mod.create_purchase_invoice, conn, ns(
        purchase_order_id=None, purchase_receipt_id=None,
        supplier_id=env["supplier"], company_id=env["company_id"],
        posting_date="2026-06-21", due_date=None,
        items=json.dumps([{"item_id": env["item1"], "qty": "3",
                           "rate": "10.00", "discount_amount": "-1"}]),
        tax_template_id=None,
    ))
    assert is_error(neg), neg
    assert neg["message"] == "Item 0: discount_amount must not be negative"

    big = call_action(mod.create_purchase_invoice, conn, ns(
        purchase_order_id=None, purchase_receipt_id=None,
        supplier_id=env["supplier"], company_id=env["company_id"],
        posting_date="2026-06-21", due_date=None,
        items=json.dumps([{"item_id": env["item1"], "qty": "3",
                           "rate": "10.00", "discount_amount": "30.00"}]),
        tax_template_id=None,
    ))
    assert is_error(big), big
    assert big["message"] == (
        "Item 0: discount 30.00 must be less than the line amount 30.00")

    po = _create_confirmed_po(conn, env)
    _submit_receipt(conn, _make_receipt(conn, env, po, "3"))
    derived = _bill_from_order(conn, env, po)
    lines_before = [dict(r) for r in _bill_lines(conn, derived)]
    blocked = call_action(mod.update_purchase_invoice, conn, ns(
        purchase_invoice_id=derived, due_date=None,
        items=json.dumps([{"item_id": env["item1"], "qty": "1",
                           "rate": "10.00"}]),
    ))
    assert is_error(blocked), blocked
    assert blocked["message"] == (
        f"Cannot replace the lines of bill {derived}: it carries"
        " order-derived lines; create the bill again from its order or receipt")
    assert [dict(r) for r in _bill_lines(conn, derived)] == lines_before

    plain = call_action(mod.create_purchase_invoice, conn, ns(
        purchase_order_id=None, purchase_receipt_id=None,
        supplier_id=env["supplier"], company_id=env["company_id"],
        posting_date="2026-06-21", due_date=None,
        items=json.dumps([{"item_id": env["item2"], "qty": "1",
                           "rate": "10.00"}]),
        tax_template_id=None,
    ))
    assert is_ok(plain), plain
    changed = call_action(mod.update_purchase_invoice, conn, ns(
        purchase_invoice_id=plain["purchase_invoice_id"], due_date=None,
        items=json.dumps([{"item_id": env["item2"], "qty": "2",
                           "rate": "10.00"}]),
    ))
    assert is_ok(changed), changed


def test_debit_note_share_and_refusals(conn, env):
    po = _create_confirmed_po(conn, env)
    _submit_receipt(conn, _make_receipt(conn, env, po, "3"))
    bill = _bill_from_order(conn, env, po)
    assert _bill_lines(conn, bill)[0]["discount_amount"] == "1.00"
    _submit_bill(conn, bill)

    def _return(qty, rate=None):
        line = {"item_id": env["item1"], "qty": qty}
        if rate is not None:
            line["rate"] = rate
        res = call_action(mod.create_debit_note, conn, ns(
            against_invoice_id=bill, posting_date="2026-06-22",
            reason="short delivery", items=json.dumps([line]),
        ))
        assert is_ok(res), f"debit note create failed: {res}"
        return res["debit_note_id"]

    dn1 = _return("1")
    row1 = _bill_lines(conn, dn1)[0]
    assert (row1["amount"], row1["discount_amount"]) == ("-10.00", "-0.33")
    _submit_bill(conn, dn1)
    assert _bill_status(conn, dn1) == "submitted"

    dn2 = _return("2")
    row2 = _bill_lines(conn, dn2)[0]
    assert (row2["amount"], row2["discount_amount"]) == ("-20.00", "-0.67")
    _submit_bill(conn, dn2)

    bad_rate = call_action(mod.create_debit_note, conn, ns(
        against_invoice_id=bill, posting_date="2026-06-22",
        reason="rate given", items=json.dumps(
            [{"item_id": env["item1"], "qty": "1", "rate": "10.00"}]),
    ))
    assert is_error(bad_rate), bad_rate
    assert bad_rate["message"] == (
        "Item 0: the billed line carries a discount; omit rate so the return"
        " uses the billed net")

    mixed = call_action(mod.create_purchase_invoice, conn, ns(
        purchase_order_id=None, purchase_receipt_id=None,
        supplier_id=env["supplier"], company_id=env["company_id"],
        posting_date="2026-06-21", due_date=None,
        items=json.dumps([
            {"item_id": env["item2"], "qty": "1", "rate": "10.00",
             "discount_amount": "1.00"},
            {"item_id": env["item2"], "qty": "1", "rate": "10.00"}]),
        tax_template_id=None,
    ))
    assert is_ok(mixed), mixed
    _submit_bill(conn, mixed["purchase_invoice_id"])
    ambiguous = call_action(mod.create_debit_note, conn, ns(
        against_invoice_id=mixed["purchase_invoice_id"],
        posting_date="2026-06-22", reason="ambiguous",
        items=json.dumps([{"item_id": env["item2"], "qty": "1"}]),
    ))
    assert is_error(ambiguous), ambiguous
    assert ambiguous["message"] == (
        "Item 0: item appears on more than one discounted line of the bill;"
        " it cannot be returned by item")


def test_debit_note_rederived_at_submit(conn, env):
    po = _create_confirmed_po(conn, env)
    _submit_receipt(conn, _make_receipt(conn, env, po, "3"))
    bill = _bill_from_order(conn, env, po)
    _submit_bill(conn, bill)

    def _draft_return(qty):
        res = call_action(mod.create_debit_note, conn, ns(
            against_invoice_id=bill, posting_date="2026-06-22",
            reason="dented", items=json.dumps(
                [{"item_id": env["item1"], "qty": qty}]),
        ))
        assert is_ok(res), f"draft return failed: {res}"
        return res["debit_note_id"]

    dn1 = _draft_return("1")
    dn2 = _draft_return("1")
    dn3 = _draft_return("1")
    for dn in (dn1, dn2, dn3):
        assert _bill_lines(conn, dn)[0]["discount_amount"] == "-0.33"
    res1 = _submit_bill(conn, dn1)
    assert "discount_rederived" not in res1
    res2 = _submit_bill(conn, dn2)
    assert "discount_rederived" not in res2
    assert _bill_lines(conn, dn2)[0]["discount_amount"] == "-0.33"
    line3 = _bill_lines(conn, dn3)[0]["id"]
    res3 = _submit_bill(conn, dn3)
    assert res3["discount_rederived"] == [
        {"line": line3, "old": "-0.33", "new": "-0.34"}]
    assert _bill_lines(conn, dn3)[0]["discount_amount"] == "-0.34"
