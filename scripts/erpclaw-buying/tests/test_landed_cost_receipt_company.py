"""A landed-cost voucher refuses a purchase receipt of another company.

Each test builds two companies (A and B) with build_buying_env, seeds a FIFO
item per company, and submits a receipt of 10 @ 50.00 per company.
"""
import json

from decimal import Decimal
from buying_helpers import (
    call_action, ns, is_error, is_ok, load_db_query, _uuid, build_buying_env,
)

mod = load_db_query()


def _seed_fifo_item(conn, name="FIFO Import Widget"):
    """Seed an item with valuation_method='fifo' (buying seed_item defaults to
    the schema default 'moving_average')."""
    iid = _uuid()
    conn.execute(
        """INSERT INTO item (id, item_name, item_code, stock_uom,
           is_stock_item, item_type, valuation_method, standard_rate, status)
           VALUES (?, ?, ?, 'Each', 1, 'stock', 'fifo', '0', 'active')""",
        (iid, name, f"FIFO-{iid[:6]}")
    )
    conn.commit()
    return iid


def _submitted_receipt(conn, env, item_id, qty="10", rate="50.00"):
    """PO -> confirm -> PR -> submit; returns the purchase_receipt id."""
    items = json.dumps([{"item_id": item_id, "qty": qty, "rate": rate,
                         "warehouse_id": env["warehouse"]}])
    po = call_action(mod.add_purchase_order, conn, ns(
        supplier_id=env["supplier"], company_id=env["company_id"],
        posting_date="2026-06-15", items=items,
        tax_template_id=None, name=None,
    ))
    assert is_ok(po), f"PO creation failed: {po}"
    submit_po = call_action(mod.submit_purchase_order, conn, ns(
        purchase_order_id=po["purchase_order_id"],
    ))
    assert is_ok(submit_po), f"PO submit failed: {submit_po}"
    pr = call_action(mod.create_purchase_receipt, conn, ns(
        purchase_order_id=po["purchase_order_id"], company_id=env["company_id"],
        posting_date="2026-06-20", items=None, purchase_receipt_id=None,
    ))
    assert is_ok(pr), f"PR creation failed: {pr}"
    submit_pr = call_action(mod.submit_purchase_receipt, conn, ns(
        purchase_receipt_id=pr["purchase_receipt_id"],
    ))
    assert is_ok(submit_pr), f"PR submit failed: {submit_pr}"
    return pr["purchase_receipt_id"]


def _add_lcv(conn, env, pr_ids, charges):
    return call_action(mod.add_landed_cost_voucher, conn, ns(
        purchase_receipt_ids=json.dumps(pr_ids),
        charges=json.dumps(charges),
        company_id=env["company_id"],
    ))


def _freight_100(env):
    return [{"description": "Ocean freight", "amount": "100.00",
             "expense_account_id": env["expense"]}]


def _gl_rows(conn, lcv_id):
    return conn.execute(
        "SELECT * FROM gl_entry WHERE voucher_type = 'landed_cost_voucher' "
        "AND voucher_id = ? ORDER BY account_id, debit, credit", (lcv_id,)
    ).fetchall()


def _sle_rows(conn, lcv_id):
    return conn.execute(
        "SELECT * FROM stock_ledger_entry WHERE voucher_type = 'landed_cost_voucher' "
        "AND voucher_id = ? ORDER BY item_id, warehouse_id, actual_qty, "
        "stock_value_difference", (lcv_id,)
    ).fetchall()


def _layers(conn, item_id):
    return conn.execute(
        "SELECT * FROM stock_fifo_layer WHERE item_id = ? ORDER BY posting_date, created_at",
        (item_id,)
    ).fetchall()


_TABLES = ("landed_cost_voucher", "landed_cost_charge", "landed_cost_item",
           "gl_entry", "stock_ledger_entry", "audit_log")


def _counts(conn):
    return {t: conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
            for t in _TABLES}


def _setup_two_companies(conn):
    env_a = build_buying_env(conn)
    env_b = build_buying_env(conn)
    item_a = _seed_fifo_item(conn)
    item_b = _seed_fifo_item(conn)
    pr_a = _submitted_receipt(conn, env_a, item_a)
    pr_b = _submitted_receipt(conn, env_b, item_b)
    return env_a, env_b, item_a, item_b, pr_a, pr_b


def _message(result):
    return result.get("message", result.get("error", ""))


def test_voucher_refuses_another_company_receipt(conn, monkeypatch):
    monkeypatch.setattr(mod, "_today", lambda: "2026-06-25")
    env_a, env_b, item_a, item_b, pr_a, pr_b = _setup_two_companies(conn)
    before = _counts(conn)
    result = _add_lcv(conn, env_a, [pr_b], _freight_100(env_a))
    assert is_error(result), f"expected refusal, got: {result}"
    assert _message(result) == f"Purchase receipt {pr_b} belongs to another company"
    assert _counts(conn) == before
    layers_b = _layers(conn, item_b)
    assert len(layers_b) == 1
    assert layers_b[0]["rate"] == "50.00"


def test_voucher_refuses_mixed_receipts(conn, monkeypatch):
    monkeypatch.setattr(mod, "_today", lambda: "2026-06-25")
    env_a, env_b, item_a, item_b, pr_a, pr_b = _setup_two_companies(conn)
    before = _counts(conn)
    result = _add_lcv(conn, env_a, [pr_a, pr_b], _freight_100(env_a))
    assert is_error(result), f"expected refusal, got: {result}"
    assert _message(result) == f"Purchase receipt {pr_b} belongs to another company"
    assert _counts(conn) == before
    layers_a = _layers(conn, item_a)
    layers_b = _layers(conn, item_b)
    assert len(layers_a) == 1
    assert len(layers_b) == 1
    assert layers_a[0]["rate"] == "50.00"
    assert layers_b[0]["rate"] == "50.00"


def test_voucher_refuses_a_receipt_listed_twice(conn, monkeypatch):
    monkeypatch.setattr(mod, "_today", lambda: "2026-06-25")
    env_a, env_b, item_a, item_b, pr_a, pr_b = _setup_two_companies(conn)
    before = _counts(conn)
    result = _add_lcv(conn, env_a, [pr_a, pr_a], _freight_100(env_a))
    assert is_error(result), f"expected refusal, got: {result}"
    assert _message(result) == f"Purchase receipt {pr_a} is listed more than once"
    assert _counts(conn) == before
    layers_a = _layers(conn, item_a)
    assert len(layers_a) == 1
    assert layers_a[0]["rate"] == "50.00"


def test_voucher_refuses_a_repeat_after_another_receipt(conn, monkeypatch):
    monkeypatch.setattr(mod, "_today", lambda: "2026-06-25")
    env_a = build_buying_env(conn)
    item_1 = _seed_fifo_item(conn, name="FIFO Import Widget One")
    item_2 = _seed_fifo_item(conn, name="FIFO Import Widget Two")
    pr_a1 = _submitted_receipt(conn, env_a, item_1)
    pr_a2 = _submitted_receipt(conn, env_a, item_2)
    before = _counts(conn)
    result = _add_lcv(conn, env_a, [pr_a1, pr_a2, pr_a1], _freight_100(env_a))
    assert is_error(result), f"expected refusal, got: {result}"
    assert _message(result) == f"Purchase receipt {pr_a1} is listed more than once"
    assert _counts(conn) == before
    layers_1 = _layers(conn, item_1)
    layers_2 = _layers(conn, item_2)
    assert len(layers_1) == 1
    assert len(layers_2) == 1
    assert layers_1[0]["rate"] == "50.00"
    assert layers_2[0]["rate"] == "50.00"


def test_voucher_own_company_receipt_posts(conn, monkeypatch):
    monkeypatch.setattr(mod, "_today", lambda: "2026-06-25")
    env_a, env_b, item_a, item_b, pr_a, pr_b = _setup_two_companies(conn)
    result = _add_lcv(conn, env_a, [pr_a], _freight_100(env_a))
    assert is_ok(result), f"LCV failed: {result}"
    rows = _gl_rows(conn, result["landed_cost_voucher_id"])
    assert len(rows) == 2
    by_acct = {r["account_id"]: r for r in rows}
    assert Decimal(by_acct[env_a["stock_acct"]]["debit"]) == Decimal("100.00")
    assert Decimal(by_acct[env_a["expense"]]["credit"]) == Decimal("100.00")
    sle_rows = _sle_rows(conn, result["landed_cost_voucher_id"])
    assert len(sle_rows) == 1
    sle = sle_rows[0]
    assert Decimal(sle["actual_qty"]) == Decimal("0")
    assert Decimal(sle["stock_value_difference"]) == Decimal("100.00")
    assert Decimal(sle["qty_after_transaction"]) == Decimal("10")
    assert Decimal(sle["valuation_rate"]) == Decimal("60.00")
    assert Decimal(sle["stock_value"]) == Decimal("600.00")
    assert sle["warehouse_id"] == env_a["warehouse"]
    layers_a = _layers(conn, item_a)
    assert len(layers_a) == 1
    assert Decimal(layers_a[0]["rate"]) == Decimal("60.00")
