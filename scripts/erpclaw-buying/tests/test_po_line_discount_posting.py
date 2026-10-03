"""Receipts post stock at the discounted net (task m658c1).

The derivation base already stores each order-linked receipt line's share of
the order-line discount in `discount_amount` (re-derived at submit). This
suite pins the posting half for receipts: a discounted incoming line values
the stock ledger at exactly `amount - discount_amount` through the
`incoming_value` stock-library input, while an undiscounted line posts
byte-identically to today.

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
    call_action, ns, is_error, is_ok, load_db_query, _uuid,
)
from erpclaw_lib.query import P, Q, Table, fn

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


def _inv24(conn):
    if _inv_engine is None:
        pytest.skip("invariant_engine harness not present (published skill tree)")
    _inv_engine._ensure_decimal_sum(conn)
    return _inv_engine._check_inv24_stock_account_gl_matches_ledger(conn)


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


def _create_confirmed_po(conn, env, items_str=None):
    """Create and confirm a PO. Default is the suite's standard order line:
    3 x 10.00 with discount_amount "1.00" on item1 (moving average), no tax."""
    items_str = items_str or _disc_items(env)
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


def _seed_fifo_item(conn, name="FIFO Discount Widget"):
    """Seed an item with valuation_method='fifo' (buying seed_item defaults to
    the schema default 'moving_average'). Mirrors test_landed_cost.py."""
    iid = _uuid()
    conn.execute(
        """INSERT INTO item (id, item_name, item_code, stock_uom,
           is_stock_item, item_type, valuation_method, standard_rate, status)
           VALUES (?, ?, ?, 'Each', 1, 'stock', 'fifo', '0', 'active')""",
        (iid, name, f"FIFO-{iid[:6]}")
    )
    conn.commit()
    return iid


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


def _submit(conn, pr_id):
    res = call_action(mod.submit_purchase_receipt, conn, ns(
        purchase_receipt_id=pr_id,
    ))
    assert is_ok(res), f"submit failed: {res}"
    return res


def _sle_tuples(conn, pr_id):
    rows = conn.execute(
        "SELECT actual_qty, incoming_rate, stock_value_difference, "
        "stock_value, valuation_rate FROM stock_ledger_entry "
        "WHERE voucher_type='purchase_receipt' AND voucher_id=? "
        "AND is_cancelled=0 ORDER BY rowid",
        (pr_id,)).fetchall()
    return [(r["actual_qty"], r["incoming_rate"],
             r["stock_value_difference"], r["stock_value"],
             r["valuation_rate"]) for r in rows]


def _gl_net(conn, voucher_id, account_id, voucher_type="purchase_receipt"):
    """Net debit-minus-credit for one account on one voucher, as text."""
    rows = conn.execute(
        "SELECT debit, credit FROM gl_entry "
        "WHERE voucher_type=? AND voucher_id=? AND account_id=? "
        "AND is_cancelled=0",
        (voucher_type, voucher_id, account_id)).fetchall()
    net = sum((Decimal(r["debit"]) - Decimal(r["credit"]) for r in rows),
              Decimal("0"))
    return str(net)


def _receipt_lines(conn, pr_id):
    return conn.execute(
        "SELECT * FROM purchase_receipt_item WHERE purchase_receipt_id = ?"
        " ORDER BY rowid", (pr_id,)).fetchall()


# ──────────────────────────────────────────────────────────────────────────────
# 1. Split receipts value the discounted net; INV-24 stays green
# ──────────────────────────────────────────────────────────────────────────────

def test_two_then_one_split(conn, env):
    po_id = _create_confirmed_po(conn, env)
    poi = _po_item_id(conn, po_id)
    pr1 = _make_partial(conn, env, po_id, "2", poi)
    _submit(conn, pr1)
    pr2 = _make_partial(conn, env, po_id, "1", poi)
    res2 = _submit(conn, pr2)
    assert "discount_true_up" not in res2
    assert _sle_tuples(conn, pr1) == [
        ("2.00", "9.67", "19.33", "19.33", "9.67")]
    assert _sle_tuples(conn, pr2) == [
        ("1.00", "9.67", "9.67", "29.01", "9.67")]
    total_stock = (Decimal(_gl_net(conn, pr1, env["stock_acct"]))
                   + Decimal(_gl_net(conn, pr2, env["stock_acct"])))
    assert str(total_stock) == "29.00"
    assert _inv24(conn) is None


# ──────────────────────────────────────────────────────────────────────────────
# 2. Cancel reverses at the net; re-receipt re-derives the remainder
# ──────────────────────────────────────────────────────────────────────────────

def test_cancel_and_rereceive(conn, env):
    po_id = _create_confirmed_po(conn, env)
    poi = _po_item_id(conn, po_id)
    pr1 = _make_partial(conn, env, po_id, "1", poi)
    _submit(conn, pr1)
    pr2 = _make_partial(conn, env, po_id, "2", poi)
    _submit(conn, pr2)

    cancel = call_action(mod.cancel_purchase_receipt, conn, ns(
        purchase_receipt_id=pr2,
    ))
    assert is_ok(cancel), f"cancel failed: {cancel}"

    rev_sle = conn.execute(
        "SELECT stock_value_difference FROM stock_ledger_entry "
        "WHERE voucher_type='purchase_receipt' AND voucher_id=? "
        "AND CAST(actual_qty AS NUMERIC) < 0",
        (pr2,)).fetchall()
    assert [r["stock_value_difference"] for r in rev_sle] == ["-19.33"]

    gl_rows = conn.execute(
        "SELECT account_id, debit, credit FROM gl_entry "
        "WHERE voucher_type='purchase_receipt' AND voucher_id=?",
        (pr2,)).fetchall()
    by_acct = {}
    for r in gl_rows:
        by_acct.setdefault(r["account_id"], []).append(
            (r["debit"], r["credit"]))
    stock_rev = [leg for leg in by_acct[env["stock_acct"]]
                 if Decimal(leg[1]) > 0]
    srnb_rev = [leg for leg in by_acct[env["srnb"]]
                if Decimal(leg[0]) > 0]
    assert len(stock_rev) == 1 and len(srnb_rev) == 1
    assert str(Decimal(stock_rev[0][0]) - Decimal(stock_rev[0][1])) == "-19.33"
    assert str(Decimal(srnb_rev[0][0]) - Decimal(srnb_rev[0][1])) == "19.33"

    pr3 = _make_partial(conn, env, po_id, "2", poi)
    assert _receipt_lines(conn, pr3)[0]["discount_amount"] == "0.67"
    _submit(conn, pr3)
    assert _sle_tuples(conn, pr3)[0][2] == "19.33"


# ──────────────────────────────────────────────────────────────────────────────
# 3. A legacy discount that swallows the line is refused before posting
# ──────────────────────────────────────────────────────────────────────────────

def test_legacy_overflow_refused(conn, env):
    po_id = _create_confirmed_po(conn, env,
                                 _items(env, ("item1", "3", "1.00")))
    poi = _po_item_id(conn, po_id)
    pr1 = _make_partial(conn, env, po_id, "2", poi)
    _submit(conn, pr1)

    poi_t = Table("purchase_order_item")
    q = (Q.update(poi_t).set(poi_t.net_amount, P())
         .where(poi_t.id == P()))
    conn.execute(q.get_sql(), ("1.50", poi))
    conn.commit()

    pr2 = _make_partial(conn, env, po_id, "1", poi)
    item_id = _receipt_lines(conn, pr2)[0]["item_id"]
    result = call_action(mod.submit_purchase_receipt, conn, ns(
        purchase_receipt_id=pr2,
    ))
    assert is_error(result), f"overflow submit should refuse: {result}"
    assert result["message"] == (
        f"Item {item_id}: the remaining order discount 1.50 is not less "
        f"than this line's amount 1.00; cancel and re-receive the earlier "
        f"receipts of this order line so the discount is spread, then retry")
    conn.rollback()

    poi_row = conn.execute(
        "SELECT received_qty FROM purchase_order_item WHERE id=?",
        (poi,)).fetchone()
    assert Decimal(poi_row["received_qty"]) == Decimal("2")
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM stock_ledger_entry WHERE voucher_id=?",
        (pr2,)).fetchone()["n"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM gl_entry WHERE voucher_id=?",
        (pr2,)).fetchone()["n"] == 0
    assert conn.execute(
        "SELECT status FROM purchase_receipt WHERE id=?",
        (pr2,)).fetchone()["status"] == "draft"


# ──────────────────────────────────────────────────────────────────────────────
# 4. FIFO refuses a net no per-unit rate can hold; a divisible net posts
# ──────────────────────────────────────────────────────────────────────────────

def test_fifo(conn, env):
    fifo_item = _seed_fifo_item(conn)
    fifo_items = json.dumps([{
        "item_id": fifo_item, "qty": "3", "rate": "10.00",
        "discount_amount": "1.00", "warehouse_id": env["warehouse"]}])
    po_id = _create_confirmed_po(conn, env, fifo_items)
    poi = _po_item_id(conn, po_id)
    pr1 = _make_partial(conn, env, po_id, "2", poi)
    result = call_action(mod.submit_purchase_receipt, conn, ns(
        purchase_receipt_id=pr1,
    ))
    assert is_error(result), f"FIFO split submit should refuse: {result}"
    assert result["message"] == (
        f"Item {fifo_item} is FIFO-valued and its discounted net 19.33 "
        f"for 2.00 units cannot be held at a per-unit rate; receive it "
        f"undiscounted or wait for FIFO layer values")
    conn.rollback()

    poi_row = conn.execute(
        "SELECT received_qty FROM purchase_order_item WHERE id=?",
        (poi,)).fetchone()
    assert Decimal(poi_row["received_qty"]) == Decimal("0")
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM stock_fifo_layer "
        "WHERE source_voucher_id=?",
        (pr1,)).fetchone()["n"] == 0

    po2 = _create_confirmed_po(conn, env, json.dumps([{
        "item_id": fifo_item, "qty": "3", "rate": "10.00",
        "discount_amount": "3.00", "warehouse_id": env["warehouse"]}]))
    pr2 = _make_full(conn, env, po2)
    _submit(conn, pr2)
    assert _sle_tuples(conn, pr2)[0][2] == "27.00"
    layer = conn.execute(
        "SELECT rate FROM stock_fifo_layer WHERE source_voucher_id=?",
        (pr2,)).fetchone()
    assert layer is not None
    assert layer["rate"] == "9.00"


# ──────────────────────────────────────────────────────────────────────────────
# 5. incoming_value refusals and exact-value posting at the library seam
# ──────────────────────────────────────────────────────────────────────────────

def test_incoming_value_refusals(conn, env):
    from erpclaw_lib.stock_posting import insert_sle_entries

    item_id = env["item1"]
    bad_out = {
        "item_id": item_id, "warehouse_id": env["warehouse"],
        "actual_qty": "-1", "incoming_rate": "10.00",
        "incoming_value": "5.00",
    }
    with pytest.raises(ValueError) as exc_out:
        insert_sle_entries(conn, [bad_out], voucher_type="purchase_receipt",
                           voucher_id=str(uuid.uuid4()),
                           posting_date="2026-06-20",
                           company_id=env["company_id"])
    assert str(exc_out.value) == (
        "incoming_value is only valid on an incoming entry with a positive "
        f"value (item {item_id})")

    bad_zero = {
        "item_id": item_id, "warehouse_id": env["warehouse"],
        "actual_qty": "2", "incoming_rate": "10.00",
        "incoming_value": "0", "require_rate": True,
    }
    with pytest.raises(ValueError) as exc_zero:
        insert_sle_entries(conn, [bad_zero], voucher_type="purchase_receipt",
                           voucher_id=str(uuid.uuid4()),
                           posting_date="2026-06-20",
                           company_id=env["company_id"])
    assert str(exc_zero.value) == (
        "incoming_value is only valid on an incoming entry with a positive "
        f"value (item {item_id})")

    vid = str(uuid.uuid4())
    insert_sle_entries(
        conn, [{
            "item_id": item_id, "warehouse_id": env["warehouse"],
            "actual_qty": "2", "incoming_rate": "10.00",
            "incoming_value": "19.33", "require_rate": True,
        }],
        voucher_type="purchase_receipt", voucher_id=vid,
        posting_date="2026-06-20", company_id=env["company_id"])
    row = conn.execute(
        "SELECT actual_qty, incoming_rate, stock_value_difference, "
        "stock_value, valuation_rate FROM stock_ledger_entry "
        "WHERE voucher_id=? AND is_cancelled=0",
        (vid,)).fetchone()
    assert row["incoming_rate"] == "9.67"
    assert row["stock_value_difference"] == "19.33"


# ──────────────────────────────────────────────────────────────────────────────
# 6. Undiscounted receipts post exactly as before (pre/post-change parity)
# ──────────────────────────────────────────────────────────────────────────────

def test_undiscounted_receipt_parity(conn, env):
    po_a = _create_confirmed_po(conn, env, _items(env, ("item1", "0.3", "10.01")))
    pr_a = _make_full(conn, env, po_a)
    _submit(conn, pr_a)
    assert _sle_tuples(conn, pr_a) == [
        ("0.30", "10.01", "3.00", "3.00", "10.00")]
    assert _gl_net(conn, pr_a, env["stock_acct"]) == "3.00"

    po_b = _create_confirmed_po(conn, env, _items(env, ("item2", "3", "10.00")))
    pr_b = _make_full(conn, env, po_b)
    _submit(conn, pr_b)
    assert _sle_tuples(conn, pr_b) == [
        ("3.00", "10.00", "30.00", "30.00", "10.00")]


# ──────────────────────────────────────────────────────────────────────────────
# 7. The full invariant suite stays green across the split
# ──────────────────────────────────────────────────────────────────────────────

def test_invariants_green_after_split(conn, env):
    po_id = _create_confirmed_po(conn, env)
    poi = _po_item_id(conn, po_id)
    pr1 = _make_partial(conn, env, po_id, "2", poi)
    _submit(conn, pr1)
    pr2 = _make_partial(conn, env, po_id, "1", poi)
    _submit(conn, pr2)
    assert _inv24(conn) is None
    outcomes = _evaluate(conn)
    # INV-10 is excluded here and named in CHANGES.md: it fails identically
    # before and after this change because the shared buying seed helper
    # writes bare naming prefixes ("PO-") while INV-10 requires
    # "{PREFIX}{YEAR}-". Pre-existing, out of scope, left alone.
    failures = [o for o in outcomes
                if o.is_failure and o.inv_id != "INV-10"]
    assert failures == [], [(o.inv_id, o.status, o.detail) for o in failures]
