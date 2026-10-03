"""Bills and debit notes post the discounted net, so payables equal the order.

Task m658c2: the derivation base already stores each order-linked bill and
debit-note line's share of the order-line discount in `discount_amount`
(re-derived at submit). This suite pins the posting half for bills: the
header, tax, expense/clearing leg, payable and payment ledger use the net
(`amount - discount_amount`), a stock-moving bill values stock at the net,
and a debit note mirrors it. An undiscounted line posts byte-identically
to today.

Money is exact Decimal text, never float. SLE tuples are
(actual_qty, incoming_rate, stock_value_difference, stock_value,
valuation_rate); GL assertions use debit-minus-credit per account per
voucher. Exact strings throughout.
"""
import importlib.util
import json
import os
import uuid
from decimal import Decimal

import pytest

from buying_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
)
from erpclaw_lib.query import P, Q, Table, fn, insert_row
from erpclaw_lib.vendor.pypika.terms import ValueWrapper

mod = load_db_query()


def _load_invariant_engine():
    """Defensive monorepo-harness import (test_inv25_flows.py pattern): the
    published skill tree has no testing/ dir, so engine-backed pins skip."""
    cur = os.path.dirname(os.path.abspath(__file__))
    while True:
        if os.path.exists(os.path.join(cur, "CLAUDE.md")) or \
                os.path.isdir(os.path.join(cur, ".git")):
            break
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent
    path = os.path.join(cur, "testing", "invariant_engine.py")
    if not os.path.exists(path):
        return None
    spec = importlib.util.spec_from_file_location("invariant_engine_pin_buy", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


_inv_engine = _load_invariant_engine()


def _evaluate(conn):
    if _inv_engine is None:
        pytest.skip("invariant_engine harness not present (published skill tree)")
    _inv_engine._ensure_decimal_sum(conn)
    return _inv_engine.evaluate_invariants(conn)


def _items(env, *specs):
    return json.dumps([
        {"item_id": env[k], "qty": q, "rate": r, "warehouse_id": env["warehouse"]}
        for k, q, r in specs
    ])


def _disc_items(env, qty="3", rate="10.00", discount_amount="1.00",
                item_key="item1"):
    return json.dumps([{"item_id": env[item_key], "qty": qty, "rate": rate,
                        "discount_amount": discount_amount,
                        "warehouse_id": env["warehouse"]}])


def _create_confirmed_po(conn, env, items_str=None, tax_template_id=None):
    """Create and confirm a PO."""
    items_str = items_str or _items(env, ("item1", "10", "50.00"))
    po = call_action(mod.add_purchase_order, conn, ns(
        supplier_id=env["supplier"], company_id=env["company_id"],
        posting_date="2026-06-15", items=items_str,
        tax_template_id=tax_template_id, name=None,
    ))
    assert is_ok(po), f"PO creation failed: {po}"
    submit = call_action(mod.submit_purchase_order, conn, ns(
        purchase_order_id=po["purchase_order_id"],
    ))
    assert is_ok(submit), f"PO submit failed: {submit}"
    return po["purchase_order_id"]


def _u():
    return str(uuid.uuid4())


def _insert(conn, table, row):
    sql, _cols = insert_row(table, {key: P() for key in row})
    conn.execute(sql, tuple(row.values()))
    conn.commit()


def _tax_template(conn, env, rate, account_name="Input Tax"):
    tax_account = _u()
    _insert(conn, "account", {
        "id": tax_account, "name": account_name,
        "account_number": f"1400-{tax_account[:6]}", "root_type": "asset",
        "account_type": "tax", "balance_direction": "debit_normal",
        "company_id": env["company_id"], "depth": 0})
    tpl = _u()
    _insert(conn, "tax_template", {
        "id": tpl, "name": f"Purchase Tax {rate}-{tpl[:4]}",
        "tax_type": "purchase", "company_id": env["company_id"]})
    _insert(conn, "tax_template_line", {
        "id": _u(), "tax_template_id": tpl, "tax_account_id": tax_account,
        "rate": rate, "charge_type": "on_net_total", "row_order": 0,
        "add_deduct": "add"})
    return tpl, tax_account


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


def _submit_receipt(conn, pr_id):
    res = call_action(mod.submit_purchase_receipt, conn, ns(
        purchase_receipt_id=pr_id,
    ))
    assert is_ok(res), f"receipt submit failed: {res}"
    return res


def _bill_from_order(conn, env, po_id, tax_template_id=None):
    res = call_action(mod.create_purchase_invoice, conn, ns(
        purchase_order_id=po_id, purchase_receipt_id=None,
        supplier_id=None, company_id=env["company_id"],
        posting_date="2026-06-21", due_date=None,
        items=None, tax_template_id=tax_template_id,
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


def _bill_header(conn, bill_id):
    return conn.execute(
        "SELECT total_amount, tax_amount, grand_total, outstanding_amount,"
        " update_stock FROM purchase_invoice WHERE id = ?",
        (bill_id,)).fetchone()


def _submit_bill(conn, bill_id):
    res = call_action(mod.submit_purchase_invoice, conn, ns(
        purchase_invoice_id=bill_id,
    ))
    assert is_ok(res), f"bill submit failed: {res}"
    return res


def _receipt_lines(conn, pr_id):
    return conn.execute(
        "SELECT * FROM purchase_receipt_item WHERE purchase_receipt_id = ?"
        " ORDER BY rowid", (pr_id,)).fetchall()


def _sle_tuples(conn, voucher_id, voucher_type):
    rows = conn.execute(
        "SELECT actual_qty, incoming_rate, stock_value_difference, "
        "stock_value, valuation_rate FROM stock_ledger_entry "
        "WHERE voucher_type=? AND voucher_id=? "
        "AND is_cancelled=0 ORDER BY rowid",
        (voucher_type, voucher_id)).fetchall()
    return [(r["actual_qty"], r["incoming_rate"],
             r["stock_value_difference"], r["stock_value"],
             r["valuation_rate"]) for r in rows]


def _gl_net(conn, voucher_id, account_id, voucher_type):
    """Net debit-minus-credit for one account on one voucher, as text."""
    rows = conn.execute(
        "SELECT debit, credit FROM gl_entry "
        "WHERE voucher_type=? AND voucher_id=? AND account_id=? "
        "AND is_cancelled=0",
        (voucher_type, voucher_id, account_id)).fetchall()
    net = sum((Decimal(r["debit"]) - Decimal(r["credit"]) for r in rows),
              Decimal("0"))
    return str(net)


def _gl_balance(conn, account_id):
    """Net debit-minus-credit for one account across all vouchers, as text."""
    rows = conn.execute(
        "SELECT debit, credit FROM gl_entry "
        "WHERE account_id=? AND is_cancelled=0",
        (account_id,)).fetchall()
    net = sum((Decimal(r["debit"]) - Decimal(r["credit"]) for r in rows),
              Decimal("0"))
    return str(net)


def _ple_amount(conn, voucher_id):
    rows = conn.execute(
        "SELECT amount FROM payment_ledger_entry "
        "WHERE voucher_id=? AND delinked=0",
        (voucher_id,)).fetchall()
    assert len(rows) == 1, f"expected one active PLE row: {rows}"
    return rows[0]["amount"]


def _create_return(conn, bill_id, qty, item_key="item1", env=None):
    res = call_action(mod.create_debit_note, conn, ns(
        against_invoice_id=bill_id, posting_date="2026-06-22",
        reason="short delivery",
        items=json.dumps([{"item_id": env[item_key], "qty": qty}]),
    ))
    assert is_ok(res), f"debit note create failed: {res}"
    return res["debit_note_id"]


# ──────────────────────────────────────────────────────────────────────────────
# 1. Receipts then bill equal the order net
# ──────────────────────────────────────────────────────────────────────────────

def test_receipts_then_bill_equal_order_net(conn, env):
    po_id = _create_confirmed_po(conn, env, _disc_items(env))
    poi = _po_item_id(conn, po_id)
    pr1 = _make_partial(conn, env, po_id, "1", poi)
    _submit_receipt(conn, pr1)
    pr2 = _make_partial(conn, env, po_id, "2", poi)
    _submit_receipt(conn, pr2)
    assert _sle_tuples(conn, pr1, "purchase_receipt") == [
        ("1.00", "9.67", "9.67", "9.67", "9.67")]
    assert _sle_tuples(conn, pr2, "purchase_receipt") == [
        ("2.00", "9.67", "19.33", "29.00", "9.67")]
    assert _gl_net(conn, pr1, env["stock_acct"], "purchase_receipt") == "9.67"
    assert _gl_net(conn, pr1, env["srnb"], "purchase_receipt") == "-9.67"
    assert _gl_net(conn, pr2, env["stock_acct"], "purchase_receipt") == "19.33"
    assert _gl_net(conn, pr2, env["srnb"], "purchase_receipt") == "-19.33"

    bill = _bill_from_order(conn, env, po_id)
    lines = _bill_lines(conn, bill)
    assert [(r["amount"], r["discount_amount"]) for r in lines] == [
        ("30.00", "1.00")]
    draft = _bill_header(conn, bill)
    assert (draft["total_amount"], draft["tax_amount"],
            draft["grand_total"], draft["outstanding_amount"]) == (
        "29.00", "0.00", "29.00", "29.00")
    _submit_bill(conn, bill)
    header = _bill_header(conn, bill)
    assert (header["total_amount"], header["tax_amount"],
            header["grand_total"], header["outstanding_amount"]) == (
        "29.00", "0.00", "29.00", "29.00")
    assert _gl_net(conn, bill, env["srnb"], "purchase_invoice") == "29.00"
    assert _gl_net(conn, bill, env["ap"], "purchase_invoice") == "-29.00"
    pay_leg = conn.execute(
        "SELECT party_type, party_id, debit, credit FROM gl_entry "
        "WHERE voucher_type='purchase_invoice' AND voucher_id=? "
        "AND account_id=? AND is_cancelled=0",
        (bill, env["ap"])).fetchall()
    assert len(pay_leg) == 1
    assert pay_leg[0]["party_type"] == "supplier"
    assert pay_leg[0]["party_id"] == env["supplier"]
    assert (pay_leg[0]["debit"], pay_leg[0]["credit"]) == ("0.00", "29.00")
    assert _ple_amount(conn, bill) == "29.00"
    assert _gl_balance(conn, env["stock_acct"]) == "29.00"
    assert _gl_balance(conn, env["srnb"]) == "0.00"
    assert _gl_balance(conn, env["ap"]) == "-29.00"


# ──────────────────────────────────────────────────────────────────────────────
# 2. A taxed bill taxes the net
# ──────────────────────────────────────────────────────────────────────────────

def test_taxed_bill_on_net(conn, env):
    tpl, tax_acct = _tax_template(conn, env, "10")
    po_id = _create_confirmed_po(conn, env, _disc_items(env),
                                 tax_template_id=tpl)
    _submit_receipt(conn, _make_full(conn, env, po_id))
    bill = _bill_from_order(conn, env, po_id)
    assert _bill_header(conn, bill)["update_stock"] == 0
    _submit_bill(conn, bill)
    header = _bill_header(conn, bill)
    assert (header["total_amount"], header["tax_amount"],
            header["grand_total"], header["outstanding_amount"]) == (
        "29.00", "2.90", "31.90", "31.90")
    assert _gl_net(conn, bill, env["srnb"], "purchase_invoice") == "29.00"
    assert _gl_net(conn, bill, tax_acct, "purchase_invoice") == "2.90"
    assert _gl_net(conn, bill, env["ap"], "purchase_invoice") == "-31.90"
    assert _ple_amount(conn, bill) == "31.90"


# ──────────────────────────────────────────────────────────────────────────────
# 3. Partial bills clear the accrual line by line
# ──────────────────────────────────────────────────────────────────────────────

def test_interim_srnb(conn, env):
    po_id = _create_confirmed_po(conn, env, _disc_items(env))
    poi = _po_item_id(conn, po_id)
    pr1 = _make_partial(conn, env, po_id, "1", poi)
    _submit_receipt(conn, pr1)
    pr2 = _make_partial(conn, env, po_id, "2", poi)
    _submit_receipt(conn, pr2)
    bill2 = _bill_from_receipt(conn, env, pr2)
    assert [(r["amount"], r["discount_amount"])
            for r in _bill_lines(conn, bill2)] == [("20.00", "0.67")]
    _submit_bill(conn, bill2)
    assert _gl_balance(conn, env["srnb"]) == "-9.67"
    bill1 = _bill_from_receipt(conn, env, pr1)
    assert [(r["amount"], r["discount_amount"])
            for r in _bill_lines(conn, bill1)] == [("10.00", "0.33")]
    _submit_bill(conn, bill1)
    assert _gl_balance(conn, env["srnb"]) == "0.00"


# ──────────────────────────────────────────────────────────────────────────────
# 4. A pre-change discount is trued up, then billed at the net
# ──────────────────────────────────────────────────────────────────────────────

def test_legacy_true_up(conn, env):
    po_id = _create_confirmed_po(conn, env, _items(env, ("item1", "3", "10.00")))
    poi = _po_item_id(conn, po_id)
    pr1 = _make_partial(conn, env, po_id, "1", poi)
    _submit_receipt(conn, pr1)
    assert _sle_tuples(conn, pr1, "purchase_receipt") == [
        ("1.00", "10.00", "10.00", "10.00", "10.00")]
    conn.execute(
        "UPDATE purchase_order_item SET net_amount=? WHERE id=?",
        ("29.00", poi))
    conn.execute(
        "UPDATE purchase_order SET total_amount=?, grand_total=?"
        " WHERE id=?", ("29.00", "29.00", po_id))
    conn.commit()
    pr2 = _make_partial(conn, env, po_id, "2", poi)
    assert _receipt_lines(conn, pr2)[0]["discount_amount"] == "1.00"
    res2 = _submit_receipt(conn, pr2)
    assert res2.get("discount_true_up") is True
    assert _sle_tuples(conn, pr2, "purchase_receipt") == [
        ("2.00", "9.50", "19.00", "29.00", "9.67")]
    bill = _bill_from_order(conn, env, po_id)
    assert _bill_header(conn, bill)["total_amount"] == "29.00"
    _submit_bill(conn, bill)
    assert _bill_header(conn, bill)["total_amount"] == "29.00"
    assert _gl_balance(conn, env["stock_acct"]) == "29.00"
    assert _gl_balance(conn, env["srnb"]) == "0.00"


# ──────────────────────────────────────────────────────────────────────────────
# 5. A standalone stock bill values stock at the net
# ──────────────────────────────────────────────────────────────────────────────

def test_stock_moving_standalone_bill(conn, env):
    create = call_action(mod.create_purchase_invoice, conn, ns(
        purchase_order_id=None, purchase_receipt_id=None,
        supplier_id=env["supplier"], company_id=env["company_id"],
        posting_date="2026-06-21", due_date=None,
        items=json.dumps([{"item_id": env["item1"], "qty": "3",
                           "rate": "10.00", "discount_amount": "1.00"}]),
        tax_template_id=None,
    ))
    assert is_ok(create), create
    assert create["total_amount"] == "29.00"
    bill = create["purchase_invoice_id"]
    assert _bill_header(conn, bill)["update_stock"] == 1
    _submit_bill(conn, bill)
    assert _bill_header(conn, bill)["total_amount"] == "29.00"
    assert _sle_tuples(conn, bill, "purchase_invoice") == [
        ("3.00", "9.67", "29.00", "29.00", "9.67")]
    assert _gl_net(conn, bill, env["expense"], "purchase_invoice") == "29.00"
    assert _gl_net(conn, bill, env["ap"], "purchase_invoice") == "-29.00"
    assert _gl_net(conn, bill, env["stock_acct"], "purchase_invoice") == "29.00"
    assert _gl_net(conn, bill, env["srnb"], "purchase_invoice") == "-29.00"


# ──────────────────────────────────────────────────────────────────────────────
# 6. Undiscounted bills post exactly as before (pre/post-change parity)
# ──────────────────────────────────────────────────────────────────────────────

def test_undiscounted_bill_parity(conn, env):
    po_a = _create_confirmed_po(conn, env, _items(env, ("item1", "0.3", "10.01")))
    _submit_receipt(conn, _make_full(conn, env, po_a))
    bill_a = _bill_from_order(conn, env, po_a)
    assert _bill_header(conn, bill_a)["total_amount"] == "3.00"
    _submit_bill(conn, bill_a)
    assert _bill_header(conn, bill_a)["total_amount"] == "3.00"

    po_b = _create_confirmed_po(conn, env, _items(env, ("item2", "3", "10.00")))
    _submit_receipt(conn, _make_full(conn, env, po_b))
    bill_b = _bill_from_order(conn, env, po_b)
    assert _bill_header(conn, bill_b)["total_amount"] == "30.00"
    _submit_bill(conn, bill_b)
    assert _bill_header(conn, bill_b)["total_amount"] == "30.00"


# ──────────────────────────────────────────────────────────────────────────────
# 7. Debit notes mirror the discounted net
# ──────────────────────────────────────────────────────────────────────────────

def test_debit_note_mirror(conn, env):
    po_id = _create_confirmed_po(conn, env, _disc_items(env))
    poi = _po_item_id(conn, po_id)
    _submit_receipt(conn, _make_partial(conn, env, po_id, "1", poi))
    _submit_receipt(conn, _make_partial(conn, env, po_id, "2", poi))
    bill = _bill_from_order(conn, env, po_id)
    _submit_bill(conn, bill)

    dn1 = _create_return(conn, bill, "1", env=env)
    row1 = _bill_lines(conn, dn1)[0]
    assert (row1["amount"], row1["discount_amount"]) == ("-10.00", "-0.33")
    assert _bill_header(conn, dn1)["total_amount"] == "-9.67"
    _submit_bill(conn, dn1)
    header1 = _bill_header(conn, dn1)
    assert (header1["total_amount"], header1["grand_total"],
            header1["outstanding_amount"]) == ("-9.67", "-9.67", "-9.67")
    assert _gl_net(conn, dn1, env["expense"], "debit_note") == "-9.67"
    assert _gl_net(conn, dn1, env["ap"], "debit_note") == "9.67"
    assert _ple_amount(conn, dn1) == "-9.67"

    dn2 = _create_return(conn, bill, "2", env=env)
    row2 = _bill_lines(conn, dn2)[0]
    assert (row2["amount"], row2["discount_amount"]) == ("-20.00", "-0.67")
    assert _bill_header(conn, dn2)["total_amount"] == "-19.33"
    _submit_bill(conn, dn2)
    assert _bill_header(conn, dn2)["total_amount"] == "-19.33"


# ──────────────────────────────────────────────────────────────────────────────
# 8. The full invariant suite stays green across bills and returns
# ──────────────────────────────────────────────────────────────────────────────

def _assert_no_failures_except_empty(outcomes):
    # INV-10 is excluded here and named in CHANGES.md: it fails identically
    # before and after this change because the shared buying seed helper
    # writes bare naming prefixes ("PO-") while INV-10 requires
    # "{PREFIX}{YEAR}-". Pre-existing, out of scope, left alone.
    # Every other invariant is asserted green below; outcomes the engine
    # could not examine are tolerated but returned so the caller names them.
    failing = [o for o in outcomes
               if o.is_failure and o.inv_id not in ("INV-10",)]
    empty = [o for o in failing
             if o.status == "unexamined"
             or "examined nothing" in (o.detail or "")]
    rest = [o for o in failing if o not in empty]
    assert rest == [], [(o.inv_id, o.status, o.detail) for o in rest]
    return [o.inv_id for o in empty]


def test_invariants_green(conn, env):
    po_id = _create_confirmed_po(conn, env, _disc_items(env))
    poi = _po_item_id(conn, po_id)
    _submit_receipt(conn, _make_partial(conn, env, po_id, "1", poi))
    _submit_receipt(conn, _make_partial(conn, env, po_id, "2", poi))
    bill = _bill_from_order(conn, env, po_id)
    _submit_bill(conn, bill)
    dn1 = _create_return(conn, bill, "1", env=env)
    _submit_bill(conn, dn1)
    dn2 = _create_return(conn, bill, "2", env=env)
    _submit_bill(conn, dn2)
    assert _assert_no_failures_except_empty(_evaluate(conn)) == []

    po_id = _create_confirmed_po(conn, env, _items(env, ("item2", "3", "10.00")))
    poi = _po_item_id(conn, po_id)
    _submit_receipt(conn, _make_partial(conn, env, po_id, "1", poi))
    conn.execute(
        "UPDATE purchase_order_item SET net_amount=? WHERE id=?",
        ("29.00", poi))
    conn.execute(
        "UPDATE purchase_order SET total_amount=?, grand_total=?"
        " WHERE id=?", ("29.00", "29.00", po_id))
    conn.commit()
    _submit_receipt(conn, _make_partial(conn, env, po_id, "2", poi))
    bill = _bill_from_order(conn, env, po_id)
    _submit_bill(conn, bill)
    assert _assert_no_failures_except_empty(_evaluate(conn)) == []


# ──────────────────────────────────────────────────────────────────────────────
# 9. A stale draft header is recomputed at submit
# ──────────────────────────────────────────────────────────────────────────────

def test_stale_bill_header_recomputed(conn, env):
    tpl, tax_acct = _tax_template(conn, env, "10")
    po_id = _create_confirmed_po(conn, env, _disc_items(env),
                                 tax_template_id=tpl)
    poi = _po_item_id(conn, po_id)
    _submit_receipt(conn, _make_partial(conn, env, po_id, "1", poi))
    _submit_receipt(conn, _make_partial(conn, env, po_id, "2", poi))
    bill = _bill_from_order(conn, env, po_id)
    line_id = _bill_lines(conn, bill)[0]["id"]
    pii_t = Table("purchase_invoice_item")
    uq = (Q.update(pii_t)
          .set(pii_t.discount_amount, P())
          .where(pii_t.id == P()))
    conn.execute(uq.get_sql(), ("0.50", line_id))
    pi_t = Table("purchase_invoice")
    uq = (Q.update(pi_t)
          .set(pi_t.total_amount, P())
          .set(pi_t.tax_amount, P())
          .set(pi_t.grand_total, P())
          .set(pi_t.outstanding_amount, P())
          .where(pi_t.id == P()))
    conn.execute(uq.get_sql(),
                 ("29.50", "2.95", "32.45", "32.45", bill))
    conn.commit()
    res = _submit_bill(conn, bill)
    assert res["discount_rederived"] == [
        {"line": line_id, "old": "0.50", "new": "1.00"}]
    assert _bill_lines(conn, bill)[0]["discount_amount"] == "1.00"
    header = _bill_header(conn, bill)
    assert (header["total_amount"], header["tax_amount"],
            header["grand_total"], header["outstanding_amount"]) == (
        "29.00", "2.90", "31.90", "31.90")
    assert _gl_net(conn, bill, env["srnb"], "purchase_invoice") == "29.00"
    assert _gl_net(conn, bill, tax_acct, "purchase_invoice") == "2.90"
    assert _gl_net(conn, bill, env["ap"], "purchase_invoice") == "-31.90"
    assert _ple_amount(conn, bill) == "31.90"


# ──────────────────────────────────────────────────────────────────────────────
# 10. A bill the legacy overflow swallows is refused and posts nothing
# ──────────────────────────────────────────────────────────────────────────────

def _trim_bill_to_qty(conn, bill_id, qty_text, amount_text):
    """Direct PyPika update: shrink a draft bill to a smaller quantity.

    Same shape as the b2 derivation suite's `_trim_bill_to_one`: the line
    quantity/amount and the draft header move together, the discount stays
    as derived, and submit re-derives from the order.
    """
    line_id = _bill_lines(conn, bill_id)[0]["id"]
    pii_t = Table("purchase_invoice_item")
    uq = (Q.update(pii_t)
          .set(pii_t.quantity, P())
          .set(pii_t.amount, P())
          .where(pii_t.id == P()))
    conn.execute(uq.get_sql(), (qty_text, amount_text, line_id))
    pi_t = Table("purchase_invoice")
    uq = (Q.update(pi_t)
          .set(pi_t.total_amount, P())
          .set(pi_t.grand_total, P())
          .set(pi_t.outstanding_amount, P())
          .where(pi_t.id == P()))
    conn.execute(uq.get_sql(),
                 (amount_text, amount_text, amount_text, bill_id))
    conn.commit()


def test_bill_legacy_overflow_refused(conn, env):
    po_id = _create_confirmed_po(conn, env, _items(env, ("item1", "3", "1.00")))
    poi = _po_item_id(conn, po_id)
    _submit_receipt(conn, _make_full(conn, env, po_id))
    bill1 = _bill_from_order(conn, env, po_id)
    _trim_bill_to_qty(conn, bill1, "2.00", "2.00")
    _submit_bill(conn, bill1)
    conn.execute(
        "UPDATE purchase_order_item SET net_amount=? WHERE id=?",
        ("1.50", poi))
    conn.commit()
    bill2 = _bill_from_order(conn, env, po_id)
    assert [(r["quantity"], r["amount"], r["discount_amount"])
            for r in _bill_lines(conn, bill2)] == [("1.00", "1.00", "1.50")]
    bad = call_action(mod.submit_purchase_invoice, conn, ns(
        purchase_invoice_id=bill2,
    ))
    assert is_error(bad), bad
    assert bad["message"] == (
        f"Item {env['item1']}: the remaining order discount 1.50 "
        "is not less than this line's amount 1.00; cancel and "
        "re-receive the earlier receipts of this order line so the discount "
        "is spread, then retry")
    conn.rollback()
    assert Decimal(conn.execute(
        "SELECT invoiced_qty FROM purchase_order_item WHERE id = ?",
        (poi,)).fetchone()["invoiced_qty"]) == Decimal("2")
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM gl_entry WHERE voucher_id = ?",
        (bill2,)).fetchone()["n"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM payment_ledger_entry WHERE voucher_id = ?",
        (bill2,)).fetchone()["n"] == 0
    assert conn.execute(
        "SELECT status FROM purchase_invoice WHERE id = ?",
        (bill2,)).fetchone()["status"] == "draft"


# ──────────────────────────────────────────────────────────────────────────────
# 11. A discounted FIFO line no per-unit rate can hold is refused
# ──────────────────────────────────────────────────────────────────────────────

def _seed_fifo_item(conn, name="FIFO Import Widget"):
    """Seed an item with valuation_method='fifo' (the buying seed_item
    defaults to the schema default 'moving_average')."""
    iid = _u()
    conn.execute(
        "INSERT INTO item (id, item_name, item_code, stock_uom, "
        "is_stock_item, item_type, valuation_method, standard_rate, status) "
        "VALUES (?, ?, ?, 'Each', 1, 'stock', 'fifo', '0', 'active')",
        (iid, name, f"FIFO-{iid[:6]}"))
    conn.commit()
    return iid


def test_bill_fifo_discounted_net_refused(conn, env):
    fifo_item = _seed_fifo_item(conn)
    create = call_action(mod.create_purchase_invoice, conn, ns(
        purchase_order_id=None, purchase_receipt_id=None,
        supplier_id=env["supplier"], company_id=env["company_id"],
        posting_date="2026-06-21", due_date=None,
        items=json.dumps([{"item_id": fifo_item, "qty": "2",
                           "rate": "10.00", "discount_amount": "0.67"}]),
        tax_template_id=None,
    ))
    assert is_ok(create), create
    assert create["total_amount"] == "19.33"
    bill = create["purchase_invoice_id"]
    assert _bill_header(conn, bill)["update_stock"] == 1
    bad = call_action(mod.submit_purchase_invoice, conn, ns(
        purchase_invoice_id=bill,
    ))
    assert is_error(bad), bad
    expected = (
        f"Item {fifo_item} is FIFO-valued and its discounted net 19.33 "
        "for 2.00 units cannot be held at a per-unit rate; "
        "receive it undiscounted or wait for FIFO layer values")
    assert bad["message"] == expected
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM stock_ledger_entry WHERE voucher_id = ?",
        (bill,)).fetchone()["n"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM gl_entry WHERE voucher_id = ?",
        (bill,)).fetchone()["n"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM stock_fifo_layer "
        "WHERE source_voucher_id = ?",
        (bill,)).fetchone()["n"] == 0
    assert conn.execute(
        "SELECT status FROM purchase_invoice WHERE id = ?",
        (bill,)).fetchone()["status"] == "draft"
    assert mod.check_fifo_discounted_net(
        conn, fifo_item, Decimal("19.33"), "2.00") == expected
    assert mod.check_fifo_discounted_net(
        conn, fifo_item, Decimal("19.34"), "2.00") is None


# ──────────────────────────────────────────────────────────────────────────────
# 12. A stale debit-note discount is re-derived and repriced at submit
# ──────────────────────────────────────────────────────────────────────────────

def test_debit_note_stale_discount_repricing(conn, env):
    tpl, _tax_acct = _tax_template(conn, env, "10")
    po_id = _create_confirmed_po(conn, env, _disc_items(env))
    _submit_receipt(conn, _make_full(conn, env, po_id))
    bill = _bill_from_order(conn, env, po_id)
    _submit_bill(conn, bill)
    dn = _create_return(conn, bill, "1", env=env)
    line_id = _bill_lines(conn, dn)[0]["id"]
    assert [(r["amount"], r["discount_amount"])
            for r in _bill_lines(conn, dn)] == [("-10.00", "-0.33")]
    # Stale the draft line and header, and attach a tax template: a return
    # still reprices tax to zero (the bill rule would charge 10% here).
    pii_t = Table("purchase_invoice_item")
    uq = (Q.update(pii_t)
          .set(pii_t.discount_amount, P())
          .where(pii_t.id == P()))
    conn.execute(uq.get_sql(), ("-0.10", line_id))
    pi_t = Table("purchase_invoice")
    uq = (Q.update(pi_t)
          .set(pi_t.total_amount, P())
          .set(pi_t.grand_total, P())
          .set(pi_t.outstanding_amount, P())
          .set(pi_t.tax_template_id, P())
          .where(pi_t.id == P()))
    conn.execute(uq.get_sql(),
                 ("-9.90", "-9.90", "-9.90", tpl, dn))
    conn.commit()
    res = _submit_bill(conn, dn)
    assert res["discount_rederived"] == [
        {"line": line_id, "old": "-0.10", "new": "-0.33"}]
    assert _bill_lines(conn, dn)[0]["discount_amount"] == "-0.33"
    header = _bill_header(conn, dn)
    assert (header["total_amount"], header["tax_amount"],
            header["grand_total"], header["outstanding_amount"]) == (
        "-9.67", "0.00", "-9.67", "-9.67")
    assert _gl_net(conn, dn, env["expense"], "debit_note") == "-9.67"
    assert _gl_net(conn, dn, env["ap"], "debit_note") == "9.67"
    assert _ple_amount(conn, dn) == "-9.67"
