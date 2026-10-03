"""Internal store-to-store transfers post store to store (m710).

A stock entry that only moves stock between the company's own stores
(transfer, manufacture, repack, send to subcontractor) credits each source
store's stock account and debits each target store's stock account; it posts
to cost of goods sold or stock-received-not-billed only for a non-zero
difference between issue and receipt value.
"""
import json

import pytest
from inventory_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
    seed_account, _uuid,
)

mod = load_db_query()


def _ensure_cogs(env, conn):
    return seed_account(conn, env["company_id"], "COGS", "expense",
                        "cost_of_goods_sold", "5100")


def _ensure_stock2(env, conn):
    stock2 = seed_account(conn, env["company_id"], "Stock Secondary", "asset",
                          "stock", "1210")
    conn.execute("UPDATE warehouse SET account_id=? WHERE id=?",
                 (stock2, env["warehouse2"]))
    conn.commit()
    return stock2


def _clear_default_warehouse(env, conn):
    from erpclaw_lib.query import Q, P, Table
    from erpclaw_lib.vendor.pypika.terms import LiteralValue
    co = Table("company")
    q = (Q.update(co).set(co.default_warehouse_id, LiteralValue("NULL"))
         .where(co.id == P()))
    conn.execute(q.get_sql(), (env["company_id"],))
    conn.commit()


def _ns(env, **extra):
    base = dict(
        entry_type=None, company_id=env["company_id"], posting_date="2026-06-15",
        items=None, supplier_warehouse_id=None, work_order_id=None,
        warehouse=None, from_item_id=None, from_qty=None, to_item_id=None,
        to_qty=None, standard_rate=None, item_id=None, qty=None, rate=None,
    )
    base.update(extra)
    return ns(**base)


def _submit(conn, se_id):
    return call_action(mod.submit_stock_entry, conn, ns(stock_entry_id=se_id))


def _cancel(conn, se_id):
    return call_action(mod.cancel_stock_entry, conn, ns(stock_entry_id=se_id))


def _legs(conn, voucher_id):
    rows = conn.execute(
        "SELECT account_id, debit, credit FROM gl_entry "
        "WHERE voucher_id=? AND voucher_type='stock_entry' AND is_cancelled=0",
        (voucher_id,)).fetchall()
    return sorted((r["account_id"], r["debit"], r["credit"]) for r in rows)


def _mirror_legs(conn, voucher_id):
    rows = conn.execute(
        "SELECT account_id, debit, credit FROM gl_entry "
        "WHERE voucher_id=? AND voucher_type='stock_entry' "
        "AND remarks LIKE 'Reversal of %'",
        (voucher_id,)).fetchall()
    return sorted((r["account_id"], r["debit"], r["credit"]) for r in rows)


def _seed_typed_warehouse(conn, company_id, name, warehouse_type, account_id=None):
    wid = _uuid()
    conn.execute(
        "INSERT INTO warehouse (id, name, warehouse_type, company_id, account_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (wid, name, warehouse_type, company_id, account_id),
    )
    conn.commit()
    return wid


def _assert_cancel_mirrors(conn, se_id, original_legs):
    result = _cancel(conn, se_id)
    assert is_ok(result), result
    assert result["gl_reversals"] == len(original_legs)
    assert _legs(conn, se_id) == []
    expected_mirror = sorted((a, c, d) for (a, d, c) in original_legs)
    assert _mirror_legs(conn, se_id) == expected_mirror
    return result


def test_transfer_posts_store_to_store(conn, env):
    cogs = _ensure_cogs(env, conn)
    stock2 = _ensure_stock2(env, conn)
    stock_acct = env["stock_acct"]
    srnb = env["srnb"]
    items = [
        {"item_id": env["item1"], "qty": "5", "rate": "50.00",
         "from_warehouse_id": env["warehouse"],
         "to_warehouse_id": env["warehouse2"]},
    ]
    se = call_action(mod.add_stock_entry, conn, _ns(
        env, entry_type="transfer", items=json.dumps(items)))
    assert is_ok(se), se
    result = _submit(conn, se["stock_entry_id"])
    assert is_ok(result), result
    legs = _legs(conn, se["stock_entry_id"])
    assert legs == sorted([(stock_acct, "0.00", "250.00"),
                           (stock2, "250.00", "0.00")])
    assert result["gl_entries_created"] == 2
    for (acct, _d, _c) in legs:
        assert acct != srnb
        assert acct != cogs
    _assert_cancel_mirrors(conn, se["stock_entry_id"], legs)


def test_transfer_without_cogs_account_posts(conn, env):
    stock2 = _ensure_stock2(env, conn)
    stock_acct = env["stock_acct"]
    items = [
        {"item_id": env["item1"], "qty": "5", "rate": "50.00",
         "from_warehouse_id": env["warehouse"],
         "to_warehouse_id": env["warehouse2"]},
    ]
    se = call_action(mod.add_stock_entry, conn, _ns(
        env, entry_type="transfer", items=json.dumps(items)))
    assert is_ok(se), se
    result = _submit(conn, se["stock_entry_id"])
    assert is_ok(result), result
    legs = _legs(conn, se["stock_entry_id"])
    assert legs == sorted([(stock_acct, "0.00", "250.00"),
                           (stock2, "250.00", "0.00")])
    assert result["gl_entries_created"] == 2
    _assert_cancel_mirrors(conn, se["stock_entry_id"], legs)


def test_manufacture_balanced(conn, env):
    _ensure_cogs(env, conn)
    stock2 = _ensure_stock2(env, conn)
    _clear_default_warehouse(env, conn)
    stock_acct = env["stock_acct"]
    items = [
        {"item_id": env["item1"], "qty": "10", "rate": "50.00",
         "from_warehouse_id": env["warehouse"]},
        {"item_id": env["item2"], "qty": "5", "rate": "100.00",
         "to_warehouse_id": env["warehouse2"]},
    ]
    se = call_action(mod.add_stock_entry, conn, _ns(
        env, entry_type="manufacture", items=json.dumps(items)))
    assert is_ok(se), se
    result = _submit(conn, se["stock_entry_id"])
    assert is_ok(result), result
    legs = _legs(conn, se["stock_entry_id"])
    assert legs == sorted([(stock_acct, "0.00", "500.00"),
                           (stock2, "500.00", "0.00")])
    _assert_cancel_mirrors(conn, se["stock_entry_id"], legs)


def test_manufacture_value_added_and_lost(conn, env):
    cogs = _ensure_cogs(env, conn)
    stock2 = _ensure_stock2(env, conn)
    _clear_default_warehouse(env, conn)
    stock_acct = env["stock_acct"]
    srnb = env["srnb"]
    cc = env["cc"]
    items_gain = [
        {"item_id": env["item1"], "qty": "10", "rate": "50.00",
         "from_warehouse_id": env["warehouse"]},
        {"item_id": env["item2"], "qty": "5", "rate": "120.00",
         "to_warehouse_id": env["warehouse2"]},
    ]
    se_gain = call_action(mod.add_stock_entry, conn, _ns(
        env, entry_type="manufacture", items=json.dumps(items_gain)))
    assert is_ok(se_gain), se_gain
    result_gain = _submit(conn, se_gain["stock_entry_id"])
    assert is_ok(result_gain), result_gain
    legs_gain = _legs(conn, se_gain["stock_entry_id"])
    assert legs_gain == sorted([(stock_acct, "0.00", "500.00"),
                                (stock2, "600.00", "0.00"),
                                (srnb, "0.00", "100.00")])
    row = conn.execute(
        "SELECT cost_center_id FROM gl_entry WHERE voucher_id=? "
        "AND voucher_type='stock_entry' AND account_id=? AND is_cancelled=0",
        (se_gain["stock_entry_id"], srnb)).fetchone()
    assert row is not None
    assert row["cost_center_id"] == cc
    items_loss = [
        {"item_id": env["item1"], "qty": "10", "rate": "50.00",
         "from_warehouse_id": env["warehouse"]},
        {"item_id": env["item2"], "qty": "5", "rate": "80.00",
         "to_warehouse_id": env["warehouse2"]},
    ]
    se_loss = call_action(mod.add_stock_entry, conn, _ns(
        env, entry_type="manufacture", items=json.dumps(items_loss)))
    assert is_ok(se_loss), se_loss
    result_loss = _submit(conn, se_loss["stock_entry_id"])
    assert is_ok(result_loss), result_loss
    legs_loss = _legs(conn, se_loss["stock_entry_id"])
    assert legs_loss == sorted([(stock_acct, "0.00", "500.00"),
                                (stock2, "400.00", "0.00"),
                                (cogs, "100.00", "0.00")])
    row2 = conn.execute(
        "SELECT cost_center_id FROM gl_entry WHERE voucher_id=? "
        "AND voucher_type='stock_entry' AND account_id=? AND is_cancelled=0",
        (se_loss["stock_entry_id"], cogs)).fetchone()
    assert row2 is not None
    assert row2["cost_center_id"] == cc
    _assert_cancel_mirrors(conn, se_gain["stock_entry_id"], legs_gain)


def test_repack_one_cent_remainder(conn, env):
    cogs = _ensure_cogs(env, conn)
    stock_acct = env["stock_acct"]
    items = [
        {"item_id": env["item1"], "qty": "100", "rate": "50.00",
         "from_warehouse_id": env["warehouse"]},
        {"item_id": env["item2"], "qty": "1", "rate": "4999.99",
         "to_warehouse_id": env["warehouse"]},
    ]
    se = call_action(mod.add_stock_entry, conn, _ns(
        env, entry_type="repack", items=json.dumps(items)))
    assert is_ok(se), se
    result = _submit(conn, se["stock_entry_id"])
    assert is_ok(result), result
    legs = _legs(conn, se["stock_entry_id"])
    assert legs == sorted([(cogs, "0.01", "0.00"),
                           (stock_acct, "0.00", "5000.00"),
                           (stock_acct, "4999.99", "0.00")])
    row = conn.execute(
        "SELECT cost_center_id FROM gl_entry WHERE voucher_id=? "
        "AND voucher_type='stock_entry' AND account_id=? AND debit='0.01' "
        "AND is_cancelled=0",
        (se["stock_entry_id"], cogs)).fetchone()
    assert row is not None
    assert row["cost_center_id"] == env["cc"]
    _assert_cancel_mirrors(conn, se["stock_entry_id"], legs)


def test_send_to_subcontractor_store_to_store(conn, env):
    _ensure_cogs(env, conn)
    stock_acct = env["stock_acct"]
    stock_sub = seed_account(conn, env["company_id"], "Stock Subcontract", "asset",
                             "stock", "1215")
    sub_wh = _seed_typed_warehouse(
        conn, env["company_id"], "Subcontractor Store", "transit", stock_sub)
    items = [
        {"item_id": env["item1"], "qty": "30", "rate": "50.00",
         "from_warehouse_id": env["warehouse"]},
    ]
    se = call_action(mod.add_stock_entry, conn, _ns(
        env, entry_type="subcontract", items=json.dumps(items),
        supplier_warehouse_id=sub_wh))
    assert is_ok(se), se
    result = _submit(conn, se["stock_entry_id"])
    assert is_ok(result), result
    legs = _legs(conn, se["stock_entry_id"])
    assert legs == sorted([(stock_acct, "0.00", "1500.00"),
                           (stock_sub, "1500.00", "0.00")])
    _assert_cancel_mirrors(conn, se["stock_entry_id"], legs)


def test_external_receipt_and_issue_unchanged(conn, env):
    cogs = _ensure_cogs(env, conn)
    stock_acct = env["stock_acct"]
    srnb = env["srnb"]
    items = [
        {"item_id": env["item1"], "qty": "10", "rate": "50.00",
         "to_warehouse_id": env["warehouse"]},
        {"item_id": env["item2"], "qty": "4", "rate": "25.00",
         "to_warehouse_id": env["warehouse"]},
    ]
    se = call_action(mod.add_stock_entry, conn, _ns(
        env, entry_type="receive", items=json.dumps(items)))
    assert is_ok(se), se
    result = _submit(conn, se["stock_entry_id"])
    assert is_ok(result), result
    legs = _legs(conn, se["stock_entry_id"])
    assert legs == sorted([(srnb, "0.00", "100.00"),
                           (srnb, "0.00", "500.00"),
                           (stock_acct, "100.00", "0.00"),
                           (stock_acct, "500.00", "0.00")])
    items2 = [
        {"item_id": env["item1"], "qty": "5", "rate": "50.00",
         "from_warehouse_id": env["warehouse"]},
    ]
    se2 = call_action(mod.add_stock_entry, conn, _ns(
        env, entry_type="issue", items=json.dumps(items2)))
    assert is_ok(se2), se2
    result2 = _submit(conn, se2["stock_entry_id"])
    assert is_ok(result2), result2
    legs2 = _legs(conn, se2["stock_entry_id"])
    assert legs2 == sorted([(cogs, "250.00", "0.00"),
                            (stock_acct, "0.00", "250.00")])
