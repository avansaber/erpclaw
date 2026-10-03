"""Receipt lines carry their exact share of the order-line discount.

Task m658b1: discount_share pure function, receipt create preview,
submit re-derivation, landed-cost net basis; posting unchanged.
"""
import json
import pytest
from decimal import Decimal
from buying_helpers import call_action, ns, is_error, is_ok, load_db_query
from erpclaw_lib.query import P, Q, Table, fn

mod = load_db_query()


def _disc_items(env, qty="3", rate="10.00", discount_amount="1.00",
                item_key="item1"):
    return json.dumps([{"item_id": env[item_key], "qty": qty, "rate": rate,
                        "discount_amount": discount_amount,
                        "warehouse_id": env["warehouse"]}])


def _pct_items(env, qty="7", rate="3.00", pct="12.5", item_key="item1"):
    return json.dumps([{"item_id": env[item_key], "qty": qty, "rate": rate,
                        "discount_percentage": pct,
                        "warehouse_id": env["warehouse"]}])


def _plain_items(env, qty="1", rate="10.00", item_key="item2"):
    return json.dumps([{"item_id": env[item_key], "qty": qty, "rate": rate,
                        "warehouse_id": env["warehouse"]}])


def _create_confirmed_po(conn, env, items_str):
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


def _make_partial(conn, env, po_id, qty, po_item_id=None):
    po_item_id = po_item_id or _po_item_id(conn, po_id)
    items = json.dumps([{"purchase_order_item_id": po_item_id,
                         "qty": qty}])
    res = call_action(mod.create_purchase_receipt, conn, ns(
        purchase_order_id=po_id, company_id=env["company_id"],
        posting_date="2026-06-20", items=items,
        purchase_receipt_id=None,
    ))
    assert is_ok(res), f"partial receipt failed: {res}"
    return res["purchase_receipt_id"]


def _make_full(conn, env, po_id):
    res = call_action(mod.create_purchase_receipt, conn, ns(
        purchase_order_id=po_id, company_id=env["company_id"],
        posting_date="2026-06-20", items=None,
        purchase_receipt_id=None,
    ))
    assert is_ok(res), f"full receipt failed: {res}"
    return res["purchase_receipt_id"]


def _lines(conn, pr_id):
    return conn.execute(
        "SELECT * FROM purchase_receipt_item WHERE purchase_receipt_id = ?"
        " ORDER BY rowid", (pr_id,)).fetchall()


def _submit(conn, pr_id):
    res = call_action(mod.submit_purchase_receipt, conn, ns(
        purchase_receipt_id=pr_id,
    ))
    assert is_ok(res), f"submit failed: {res}"
    return res


def test_discount_share_table():
    f = mod.discount_share
    D = Decimal
    assert f(D("1.00"), D("3"), D("0"), D("0"), D("1")) == D("0.33")
    assert f(D("1.00"), D("3"), D("1"), D("0.33"), D("2")) == D("0.67")
    s1 = f(D("1.00"), D("3"), D("0"), D("0"), D("1"))
    s2 = f(D("1.00"), D("3"), D("1"), D("0.33"), D("1"))
    s3 = f(D("1.00"), D("3"), D("2"), D("0.66"), D("1"))
    assert (s1, s2, s3) == (D("0.33"), D("0.33"), D("0.34"))
    assert f(D("2.62"), D("7"), D("0"), D("0"), D("2")) == D("0.75")
    assert f(D("2.62"), D("7"), D("2"), D("0.75"), D("5")) == D("1.87")
    assert f(D("1.00"), D("3"), D("0"), D("0"), D("3.3")) == D("1.10")
    assert f(D("1.00"), D("3"), D("3"), D("1.00"), D("0.3")) == D("0.10")
    assert f(D("0"), D("3"), D("0"), D("0"), D("1")) == D("0")
    assert f(D("0"), D("3"), D("1"), D("0"), D("2")) == D("0")


def test_receipt_lines_carry_share(conn, env):
    po_id = _create_confirmed_po(conn, env, _disc_items(env))
    poi = _po_item_id(conn, po_id)
    pr1 = _make_partial(conn, env, po_id, "1", poi)
    assert _lines(conn, pr1)[0]["discount_amount"] == "0.33"
    assert "discount_true_up" not in _submit(conn, pr1)
    pr2 = _make_partial(conn, env, po_id, "2", poi)
    assert _lines(conn, pr2)[0]["discount_amount"] == "0.67"
    assert "discount_true_up" not in _submit(conn, pr2)

    po_full = _create_confirmed_po(conn, env, _disc_items(env))
    pr_full = _make_full(conn, env, po_full)
    assert _lines(conn, pr_full)[0]["discount_amount"] == "1.00"
    assert "discount_true_up" not in _submit(conn, pr_full)

    po_two = _create_confirmed_po(conn, env, _disc_items(env))
    poi_two = _po_item_id(conn, po_two)
    items = json.dumps([{"purchase_order_item_id": poi_two, "qty": "1"},
                        {"purchase_order_item_id": poi_two, "qty": "2"}])
    res = call_action(mod.create_purchase_receipt, conn, ns(
        purchase_order_id=po_two, company_id=env["company_id"],
        posting_date="2026-06-20", items=items,
        purchase_receipt_id=None,
    ))
    assert is_ok(res), res
    rows = _lines(conn, res["purchase_receipt_id"])
    assert [r["discount_amount"] for r in rows] == ["0.33", "0.67"]
    assert "discount_true_up" not in _submit(conn, res["purchase_receipt_id"])

    tol = call_action(mod.update_receipt_tolerance, conn, ns(
        company_id=env["company_id"], tolerance_pct="10",
    ))
    assert is_ok(tol), tol
    po_tol = _create_confirmed_po(conn, env, _disc_items(env))
    poi_tol = _po_item_id(conn, po_tol)
    pr_tol = _make_partial(conn, env, po_tol, "3.3", poi_tol)
    row = _lines(conn, pr_tol)[0]
    assert row["amount"] == "33.00"
    assert row["discount_amount"] == "1.10"
    assert "discount_true_up" not in _submit(conn, pr_tol)

    po_pct = _create_confirmed_po(conn, env, _pct_items(env))
    poi_pct = _po_item_id(conn, po_pct)
    pr_p1 = _make_partial(conn, env, po_pct, "2", poi_pct)
    assert _lines(conn, pr_p1)[0]["discount_amount"] == "0.75"
    assert "discount_true_up" not in _submit(conn, pr_p1)
    pr_p2 = _make_partial(conn, env, po_pct, "5", poi_pct)
    assert _lines(conn, pr_p2)[0]["discount_amount"] == "1.87"
    assert "discount_true_up" not in _submit(conn, pr_p2)


def test_cancelled_sibling_excluded(conn, env):
    po_id = _create_confirmed_po(conn, env, _disc_items(env))
    poi = _po_item_id(conn, po_id)
    pr1 = _make_partial(conn, env, po_id, "1", poi)
    assert _lines(conn, pr1)[0]["discount_amount"] == "0.33"
    _submit(conn, pr1)
    pr2 = _make_partial(conn, env, po_id, "1", poi)
    assert _lines(conn, pr2)[0]["discount_amount"] == "0.33"
    assert "discount_rederived" not in _submit(conn, pr2)
    assert _lines(conn, pr2)[0]["discount_amount"] == "0.33"
    canc = call_action(mod.cancel_purchase_receipt, conn, ns(
        purchase_receipt_id=pr2,
    ))
    assert is_ok(canc), canc
    pr3 = _make_partial(conn, env, po_id, "1", poi)
    assert _lines(conn, pr3)[0]["discount_amount"] == "0.33"


def test_stale_receipt_draft_rederived(conn, env):
    po_id = _create_confirmed_po(conn, env, _disc_items(env))
    poi = _po_item_id(conn, po_id)
    pr1 = _make_partial(conn, env, po_id, "1", poi)
    pr2 = _make_partial(conn, env, po_id, "1", poi)
    pr3 = _make_partial(conn, env, po_id, "1", poi)
    assert _lines(conn, pr1)[0]["discount_amount"] == "0.33"
    assert _lines(conn, pr2)[0]["discount_amount"] == "0.33"
    assert _lines(conn, pr3)[0]["discount_amount"] == "0.33"
    r1 = _submit(conn, pr1)
    assert "discount_rederived" not in r1
    r2 = _submit(conn, pr2)
    assert "discount_rederived" not in r2
    r3 = _submit(conn, pr3)
    line3 = _lines(conn, pr3)[0]["id"]
    assert r3["discount_rederived"] == [
        {"line": line3, "old": "0.33", "new": "0.34"}]
    assert _lines(conn, pr3)[0]["discount_amount"] == "0.34"
    audit_row = conn.execute(
        "SELECT old_values, new_values FROM audit_log WHERE action = ?"
        " AND entity_id = ? ORDER BY rowid DESC LIMIT 1",
        ("submit-purchase-receipt", pr3)).fetchone()
    assert audit_row is not None
    old_v = json.loads(audit_row["old_values"] or "{}")
    new_v = json.loads(audit_row["new_values"] or "{}")
    assert old_v["discount_amount"][line3] == "0.33"
    assert {"line": line3, "old": "0.33", "new": "0.34"} in \
        new_v["discount_rederived"]


def test_landed_cost_uses_net(conn, env):
    po_a = _create_confirmed_po(conn, env, _disc_items(env))
    poi_a = _po_item_id(conn, po_a)
    pr_a = _make_partial(conn, env, po_a, "1", poi_a)
    _submit(conn, pr_a)
    line_a = _lines(conn, pr_a)[0]
    assert line_a["discount_amount"] == "0.33"

    po_b = _create_confirmed_po(conn, env, _plain_items(env))
    pr_b = _make_full(conn, env, po_b)
    _submit(conn, pr_b)

    res = call_action(mod.add_landed_cost_voucher, conn, ns(
        purchase_receipt_ids=json.dumps([pr_a, pr_b]),
        charges=json.dumps([{"description": "Freight", "amount": "19.67",
                             "expense_account_id": env["expense"]}]),
        company_id=env["company_id"],
    ))
    assert is_ok(res), res
    lcv_id = res["landed_cost_voucher_id"]
    items = conn.execute(
        "SELECT * FROM landed_cost_item WHERE landed_cost_voucher_id = ?",
        (lcv_id,)).fetchall()
    by_pr = {r["purchase_receipt_id"]: r for r in items}
    assert by_pr[pr_a]["applicable_charges"] == "9.67"
    assert by_pr[pr_b]["applicable_charges"] == "10.00"
    assert by_pr[pr_a]["original_rate"] == "9.67"
    assert by_pr[pr_b]["original_rate"] == "10.00"
    sles = conn.execute(
        "SELECT * FROM stock_ledger_entry WHERE voucher_type = ?"
        " AND voucher_id = ?", ("landed_cost_voucher", lcv_id)).fetchall()
    by_item = {r["item_id"]: r for r in sles}
    assert by_item[env["item1"]]["stock_value_difference"] == "9.67"
    assert by_item[env["item2"]]["stock_value_difference"] == "10.00"


def test_posting_at_net(conn, env):
    po_id = _create_confirmed_po(conn, env, _disc_items(env))
    poi = _po_item_id(conn, po_id)
    pr = _make_partial(conn, env, po_id, "1", poi)
    _submit(conn, pr)
    sle = conn.execute(
        "SELECT stock_value_difference, incoming_rate FROM stock_ledger_entry"
        " WHERE voucher_type = ? AND voucher_id = ? AND is_cancelled = 0",
        ("purchase_receipt", pr)).fetchone()
    assert sle["stock_value_difference"] == "9.67"
    assert sle["incoming_rate"] == "9.67"
    bill = call_action(mod.create_purchase_invoice, conn, ns(
        purchase_order_id=po_id, purchase_receipt_id=None,
        supplier_id=None, company_id=env["company_id"],
        posting_date="2026-06-20", due_date=None,
        items=None, tax_template_id=None,
    ))
    assert is_ok(bill), bill
    assert bill["total_amount"] == "29.00"


def test_discount_true_up(conn, env):
    po_id = _create_confirmed_po(conn, env, _disc_items(env))
    poi = _po_item_id(conn, po_id)
    pr1 = _make_partial(conn, env, po_id, "1", poi)
    _submit(conn, pr1)
    line1 = _lines(conn, pr1)[0]["id"]
    conn.execute(
        "UPDATE purchase_receipt_item SET discount_amount = ? WHERE id = ?",
        ("0", line1))
    conn.commit()
    pr2 = _make_partial(conn, env, po_id, "2", poi)
    assert _lines(conn, pr2)[0]["discount_amount"] == "1.00"
    res = _submit(conn, pr2)
    assert res.get("discount_true_up") is True
