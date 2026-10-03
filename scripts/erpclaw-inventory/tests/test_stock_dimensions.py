"""Stock documents carry accounting dimensions from draft to ledger (m716b).

Tagged stock entries, reconciliations and revaluations store their header
dimensions and copy them onto every ledger row they post, so each tagged
voucher balances per dimension value. Untagged documents behave as before.
"""
import importlib.util
import json
import os
from decimal import Decimal

import pytest
from inventory_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
    seed_account, _uuid,
)

from erpclaw_lib.query import Q, P, Table, Field, fn

mod = load_db_query()

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(os.path.dirname(_TESTS_DIR))


def _load_reports():
    spec = importlib.util.spec_from_file_location(
        "db_query_reports_m716b",
        os.path.join(_SCRIPTS_DIR, "erpclaw-reports", "db_query.py"))
    rep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rep)
    return rep


REP = _load_reports()

E = '{"department": "Engineering"}'
Sv = '{"department": "Sales"}'

COMPANY_TABLES = ("stock_entry", "stock_reconciliation", "stock_revaluation",
                  "stock_ledger_entry", "gl_entry", "audit_log")


def _se_ns(env, **extra):
    base = dict(
        entry_type=None, company_id=env["company_id"], posting_date="2026-06-15",
        items=None, supplier_warehouse_id=None, work_order_id=None,
        warehouse=None, from_item_id=None, from_qty=None, to_item_id=None,
        to_qty=None, standard_rate=None, item_id=None, qty=None, rate=None,
        dimensions=None, dimension_key=None, dimension_value=None,
    )
    base.update(extra)
    return ns(**base)


def _sr_ns(env, **extra):
    base = dict(
        company_id=env["company_id"], posting_date="2026-06-16", items=None,
        dimensions=None, dimension_key=None, dimension_value=None,
    )
    base.update(extra)
    return ns(**base)


def _rv_ns(env, **extra):
    base = dict(
        item_id=None, warehouse_id=None, new_rate=None,
        posting_date="2026-06-17", reason=None,
        dimensions=None, dimension_key=None, dimension_value=None,
    )
    base.update(extra)
    return ns(**base)


def _base_ns(**kw):
    base = {"company_id": None, "company_name": None, "from_date": None,
            "to_date": None, "as_of_date": None, "account_id": None,
            "cost_center_id": None, "project_id": None,
            "fiscal_year_id": None, "party_type": None, "party_id": None,
            "voucher_type": None, "periods": None, "group_by": None,
            "dimension_key": None, "dimension_value": None,
            "limit": "100", "offset": "0", "aging_buckets": "30,60,90,120"}
    base.update(kw)
    return ns(**base)


def _ensure_cogs(env, conn):
    return seed_account(conn, env["company_id"], "COGS", "expense",
                        "cost_of_goods_sold", "5100")


def _gl(conn, voucher_id, voucher_type="stock_entry"):
    t = Table("gl_entry")
    q = (Q.from_(t).select(t.account_id, t.debit, t.credit, t.dimensions_json)
         .where(t.voucher_id == P())
         .where(t.voucher_type == P())
         .where(t.is_cancelled == 0))
    rows = conn.execute(q.get_sql(), (voucher_id, voucher_type)).fetchall()
    return sorted((r["account_id"], r["debit"], r["credit"],
                   r["dimensions_json"]) for r in rows)


def _doc_text(conn, table, doc_id):
    t = Table(table)
    q = Q.from_(t).select(Field("dimensions_json")).where(Field("id") == P())
    return conn.execute(q.get_sql(), (doc_id,)).fetchone()["dimensions_json"]


def _counts(conn, company_id):
    out = {}
    for tbl in COMPANY_TABLES:
        t = Table(tbl)
        q = Q.from_(t).select(fn.Count("*").as_("cnt"))
        out[tbl] = conn.execute(q.get_sql()).fetchone()["cnt"]
    nt = Table("naming_series")
    q = (Q.from_(nt).select(nt.entity_type, nt.prefix, nt.current_value)
         .where(nt.company_id == P()).orderby(nt.entity_type))
    out["naming_series"] = sorted(
        (r["entity_type"], r["prefix"], r["current_value"])
        for r in conn.execute(q.get_sql(), (company_id,)).fetchall())
    return out


def _seed_work_order(conn, company_id, item_id, status="in_process"):
    bom_id = _uuid()
    bom_t = Table("bom")
    conn.execute(
        Q.into(bom_t).columns("id", "item_id", "quantity", "company_id")
        .insert(P(), P(), P(), P()).get_sql(),
        (bom_id, item_id, "1", company_id))
    wo_id = _uuid()
    wo_t = Table("work_order")
    conn.execute(
        Q.into(wo_t).columns("id", "item_id", "bom_id", "qty", "status",
                             "company_id")
        .insert(P(), P(), P(), P(), P(), P()).get_sql(),
        (wo_id, item_id, bom_id, "10", status, company_id))
    conn.commit()
    return wo_id


def test_receive_tagged_by_json(conn, env):
    items = [{"item_id": env["item2"], "qty": "4", "rate": "25.00",
              "to_warehouse_id": env["warehouse"]}]
    se = call_action(mod.add_stock_entry, conn, _se_ns(
        env, entry_type="receive", items=json.dumps(items), dimensions=E))
    assert is_ok(se), se
    assert _doc_text(conn, "stock_entry", se["stock_entry_id"]) == E
    got = call_action(mod.get_stock_entry, conn,
                      ns(stock_entry_id=se["stock_entry_id"]))
    assert is_ok(got), got
    assert got["dimensions_json"] == E
    result = call_action(mod.submit_stock_entry, conn,
                         _se_ns(env, stock_entry_id=se["stock_entry_id"]))
    assert is_ok(result), result
    assert result["gl_entries_created"] == 2
    assert _gl(conn, se["stock_entry_id"]) == sorted([
        (env["stock_acct"], "100.00", "0.00", E),
        (env["srnb"], "0.00", "100.00", E),
    ])


def test_issue_tagged_by_pairs(conn, env):
    cogs = _ensure_cogs(env, conn)
    items = [{"item_id": env["item1"], "qty": "5", "rate": "50.00",
              "from_warehouse_id": env["warehouse"]}]
    se = call_action(mod.add_stock_entry, conn, _se_ns(
        env, entry_type="issue", items=json.dumps(items),
        dimension_key=["department"], dimension_value=["Sales"]))
    assert is_ok(se), se
    result = call_action(mod.submit_stock_entry, conn,
                         _se_ns(env, stock_entry_id=se["stock_entry_id"]))
    assert is_ok(result), result
    assert _gl(conn, se["stock_entry_id"]) == sorted([
        (cogs, "250.00", "0.00", Sv),
        (env["stock_acct"], "0.00", "250.00", Sv),
    ])


def test_reconciliation_tagged(conn, env):
    items = [{"item_id": env["item1"], "warehouse_id": env["warehouse"],
              "qty": "90", "valuation_rate": "50.00"}]
    sr = call_action(mod.add_stock_reconciliation, conn, _sr_ns(
        env, items=json.dumps(items), dimensions=Sv))
    assert is_ok(sr), sr
    assert sr["difference_amount"] == "-500.00"
    assert _doc_text(conn, "stock_reconciliation",
                     sr["stock_reconciliation_id"]) == Sv
    result = call_action(mod.submit_stock_reconciliation, conn, ns(
        stock_reconciliation_id=sr["stock_reconciliation_id"]))
    assert is_ok(result), result
    assert _gl(conn, sr["stock_reconciliation_id"],
               "stock_reconciliation") == sorted([
        (env["stock_acct"], "0.00", "500.00", Sv),
        (env["stock_adj"], "500.00", "0.00", Sv),
    ])


def test_revaluation_tagged_then_cancelled(conn, env):
    rv = call_action(mod.revalue_stock, conn, _rv_ns(
        env, item_id=env["item1"], warehouse_id=env["warehouse"],
        new_rate="60.00", dimensions=E))
    assert is_ok(rv), rv
    assert rv["adjustment_amount"] == "1000.00"
    assert _doc_text(conn, "stock_revaluation", rv["revaluation_id"]) == E
    got = call_action(mod.get_stock_revaluation, conn,
                      ns(revaluation_id=rv["revaluation_id"]))
    assert is_ok(got), got
    assert got["dimensions_json"] == E
    assert _gl(conn, rv["revaluation_id"], "stock_revaluation") == sorted([
        (env["stock_acct"], "1000.00", "0.00", E),
        (env["stock_adj"], "0.00", "1000.00", E),
    ])
    cancel = call_action(mod.cancel_stock_revaluation, conn,
                         ns(revaluation_id=rv["revaluation_id"]))
    assert is_ok(cancel), cancel
    t = Table("gl_entry")
    q = (Q.from_(t).select(t.account_id, t.debit, t.credit, t.dimensions_json)
         .where(t.voucher_id == P()).where(t.voucher_type == P()))
    rows = conn.execute(
        q.get_sql(), (rv["revaluation_id"], "stock_revaluation")).fetchall()
    nets = {}
    for r in rows:
        key = (r["account_id"], r["dimensions_json"])
        nets[key] = nets.get(key, Decimal("0.00")) + (
            Decimal(r["debit"]) - Decimal(r["credit"]))
    assert nets == {(env["stock_acct"], E): Decimal("0.00"),
                    (env["stock_adj"], E): Decimal("0.00")}


def test_revaluation_decrease_tagged(conn, env):
    rv = call_action(mod.revalue_stock, conn, _rv_ns(
        env, item_id=env["item1"], warehouse_id=env["warehouse"],
        new_rate="40.00", dimensions=E))
    assert is_ok(rv), rv
    assert rv["adjustment_amount"] == "-1000.00"
    assert _gl(conn, rv["revaluation_id"], "stock_revaluation") == sorted([
        (env["stock_acct"], "0.00", "1000.00", E),
        (env["stock_adj"], "1000.00", "0.00", E),
    ])


def test_repack_shortcut_tagged(conn, env):
    conn.execute("UPDATE item SET standard_rate='250.00' WHERE id=?",
                 (env["item2"],))
    conn.commit()
    se = call_action(mod.add_repack_stock_entry, conn, _se_ns(
        env, warehouse=env["warehouse"], from_item_id=env["item1"],
        from_qty="100", to_item_id=env["item2"], to_qty="20",
        standard_rate="250.00",
        dimension_key=["department"], dimension_value=["Sales"]))
    assert is_ok(se), se
    assert _doc_text(conn, "stock_entry", se["stock_entry_id"]) == Sv
    result = call_action(mod.submit_stock_entry, conn,
                         _se_ns(env, stock_entry_id=se["stock_entry_id"]))
    assert is_ok(result), result
    assert _gl(conn, se["stock_entry_id"]) == sorted([
        (env["stock_acct"], "0.00", "5000.00", Sv),
        (env["stock_acct"], "5000.00", "0.00", Sv),
    ])


def test_material_consumption_shortcut_tagged(conn, env):
    cogs = _ensure_cogs(env, conn)
    wo_id = _seed_work_order(conn, env["company_id"], env["item1"])
    se = call_action(mod.add_material_consumption, conn, _se_ns(
        env, warehouse=env["warehouse"], work_order_id=wo_id,
        item_id=env["item1"], qty="25", rate="50.00", dimensions=E))
    assert is_ok(se), se
    result = call_action(mod.submit_stock_entry, conn,
                         _se_ns(env, stock_entry_id=se["stock_entry_id"]))
    assert is_ok(result), result
    assert _gl(conn, se["stock_entry_id"]) == sorted([
        (cogs, "1250.00", "0.00", E),
        (env["stock_acct"], "0.00", "1250.00", E),
    ])


def test_transfer_tagged_store_to_store(conn, env):
    stock2 = seed_account(conn, env["company_id"], "Stock Secondary", "asset",
                          "stock", "1210")
    wh_t = Table("warehouse")
    conn.execute(
        Q.update(wh_t).set(wh_t.account_id, P()).where(wh_t.id == P())
        .get_sql(), (stock2, env["warehouse2"]))
    conn.commit()
    items = [{"item_id": env["item1"], "qty": "10", "rate": "50.00",
              "from_warehouse_id": env["warehouse"],
              "to_warehouse_id": env["warehouse2"]}]
    se = call_action(mod.add_stock_entry, conn, _se_ns(
        env, entry_type="transfer", items=json.dumps(items), dimensions=E))
    assert is_ok(se), se
    result = call_action(mod.submit_stock_entry, conn,
                         _se_ns(env, stock_entry_id=se["stock_entry_id"]))
    assert is_ok(result), result
    assert result["gl_entries_created"] == 2
    assert _gl(conn, se["stock_entry_id"]) == sorted([
        (env["stock_acct"], "0.00", "500.00", E),
        (stock2, "500.00", "0.00", E),
    ])


def test_repack_remainder_leg_tagged(conn, env):
    cogs = _ensure_cogs(env, conn)
    items = [
        {"item_id": env["item1"], "qty": "100", "rate": "50.00",
         "from_warehouse_id": env["warehouse"]},
        {"item_id": env["item2"], "qty": "1", "rate": "4999.99",
         "to_warehouse_id": env["warehouse"]},
    ]
    se = call_action(mod.add_stock_entry, conn, _se_ns(
        env, entry_type="repack", items=json.dumps(items), dimensions=Sv))
    assert is_ok(se), se
    result = call_action(mod.submit_stock_entry, conn,
                         _se_ns(env, stock_entry_id=se["stock_entry_id"]))
    assert is_ok(result), result
    assert _gl(conn, se["stock_entry_id"]) == sorted([
        (cogs, "0.01", "0.00", Sv),
        (env["stock_acct"], "0.00", "5000.00", Sv),
        (env["stock_acct"], "4999.99", "0.00", Sv),
    ])


def test_per_value_balance_and_reports(conn, env):
    cogs = _ensure_cogs(env, conn)
    tagged_items = [{"item_id": env["item1"], "qty": "5", "rate": "50.00",
                     "from_warehouse_id": env["warehouse"]}]
    tagged = call_action(mod.add_stock_entry, conn, _se_ns(
        env, entry_type="issue", items=json.dumps(tagged_items),
        dimension_key=["department"], dimension_value=["Sales"]))
    assert is_ok(tagged), tagged
    assert is_ok(call_action(mod.submit_stock_entry, conn, _se_ns(
        env, stock_entry_id=tagged["stock_entry_id"]))), tagged
    untagged_items = [{"item_id": env["item1"], "qty": "2", "rate": "50.00",
                       "from_warehouse_id": env["warehouse"]}]
    untagged = call_action(mod.add_stock_entry, conn, _se_ns(
        env, entry_type="issue", items=json.dumps(untagged_items)))
    assert is_ok(untagged), untagged
    assert is_ok(call_action(mod.submit_stock_entry, conn, _se_ns(
        env, stock_entry_id=untagged["stock_entry_id"]))), untagged
    assert _gl(conn, untagged["stock_entry_id"]) == sorted([
        (cogs, "100.00", "0.00", "{}"),
        (env["stock_acct"], "0.00", "100.00", "{}"),
    ])
    recon_items = [{"item_id": env["item1"], "warehouse_id": env["warehouse"],
                    "qty": "90", "valuation_rate": "50.00"}]
    sr = call_action(mod.add_stock_reconciliation, conn, _sr_ns(
        env, items=json.dumps(recon_items), dimensions=Sv))
    assert is_ok(sr), sr
    assert is_ok(call_action(mod.submit_stock_reconciliation, conn, ns(
        stock_reconciliation_id=sr["stock_reconciliation_id"]))), sr
    trial = call_action(REP.multi_dim_trial_balance, conn, _base_ns(
        company_id=env["company_id"], from_date="2026-01-01",
        to_date="2026-12-31", group_by="department"))
    assert is_ok(trial), trial
    assert sorted(trial["groups"],
                  key=lambda g: (g["department"] is None,
                                 g["department"])) == [
        {"department": "Sales", "debit": "400.00", "credit": "400.00",
         "balance": "0.00"},
        {"department": None, "debit": "100.00", "credit": "100.00",
         "balance": "0.00"},
    ]
    pnl = call_action(REP.profit_and_loss, conn, _base_ns(
        company_id=env["company_id"], from_date="2026-01-01",
        to_date="2026-12-31", group_by="department"))
    assert is_ok(pnl), pnl
    assert pnl["groups"] == [
        {"department": "Sales", "revenue": "0.00", "expenses": "400.00",
         "net": "-400.00"},
        {"department": "(untagged)", "revenue": "0.00",
         "expenses": "100.00", "net": "-100.00"},
    ]
    assert pnl["income_total"] == "0.00"
    assert pnl["expense_total"] == "500.00"


def test_refusals_write_nothing(conn, env):
    recv_items = json.dumps([{"item_id": env["item2"], "qty": "4",
                              "rate": "25.00",
                              "to_warehouse_id": env["warehouse"]}])
    recon_items = json.dumps([{"item_id": env["item1"],
                               "warehouse_id": env["warehouse"],
                               "qty": "90", "valuation_rate": "50.00"}])
    before = _counts(conn, env["company_id"])
    bad_key = call_action(mod.add_stock_entry, conn, _se_ns(
        env, entry_type="receive", items=recv_items,
        dimensions='{"bogus": "X"}'))
    assert is_error(bad_key), bad_key
    assert bad_key["message"] == ("Unknown or inactive dimension 'bogus'; "
                                  "run list-dimensions")
    assert _counts(conn, env["company_id"]) == before
    unpaired = call_action(mod.add_stock_reconciliation, conn, _sr_ns(
        env, items=recon_items, dimension_key=["department"],
        dimension_value=None))
    assert is_error(unpaired), unpaired
    assert unpaired["message"] == ("--dimension-key and --dimension-value "
                                   "must be given in pairs")
    assert _counts(conn, env["company_id"]) == before
    twice = call_action(mod.revalue_stock, conn, _rv_ns(
        env, item_id=env["item1"], warehouse_id=env["warehouse"],
        new_rate="60.00", dimensions=Sv,
        dimension_key=["department"], dimension_value=["Ops"]))
    assert is_error(twice), twice
    assert twice["message"] == ("Dimension 'department' given twice with "
                                "different values")
    assert _counts(conn, env["company_id"]) == before
    conn.execute("UPDATE item SET standard_rate='250.00' WHERE id=?",
                 (env["item2"],))
    conn.commit()
    after_seed = _counts(conn, env["company_id"])
    not_object = call_action(mod.add_repack_stock_entry, conn, _se_ns(
        env, warehouse=env["warehouse"], from_item_id=env["item1"],
        from_qty="100", to_item_id=env["item2"], to_qty="20",
        standard_rate="250.00", dimensions="[1]"))
    assert is_error(not_object), not_object
    assert not_object["message"] == "--dimensions must be a JSON object"
    assert _counts(conn, env["company_id"]) == after_seed


def test_malformed_stored_tags_refuse_at_submit(conn, env):
    items = json.dumps([{"item_id": env["item2"], "qty": "4", "rate": "25.00",
                         "to_warehouse_id": env["warehouse"]}])
    se = call_action(mod.add_stock_entry, conn, _se_ns(
        env, entry_type="receive", items=items))
    assert is_ok(se), se
    se_t = Table("stock_entry")
    conn.execute(
        Q.update(se_t).set(se_t.dimensions_json, P()).where(se_t.id == P())
        .get_sql(), ("[1]", se["stock_entry_id"]))
    conn.commit()
    before = _counts(conn, env["company_id"])
    refused = call_action(mod.submit_stock_entry, conn, _se_ns(
        env, stock_entry_id=se["stock_entry_id"]))
    assert is_error(refused), refused
    assert refused["message"] == ("Stored dimensions are not a JSON object; "
                                  "re-create the document")
    assert _counts(conn, env["company_id"]) == before
    recon_items = json.dumps([{"item_id": env["item1"],
                               "warehouse_id": env["warehouse"],
                               "qty": "90", "valuation_rate": "50.00"}])
    sr = call_action(mod.add_stock_reconciliation, conn, _sr_ns(
        env, items=recon_items))
    assert is_ok(sr), sr
    sr_t = Table("stock_reconciliation")
    conn.execute(
        Q.update(sr_t).set(sr_t.dimensions_json, P()).where(sr_t.id == P())
        .get_sql(), ("{bad", sr["stock_reconciliation_id"]))
    conn.commit()
    before_sr = _counts(conn, env["company_id"])
    refused_sr = call_action(mod.submit_stock_reconciliation, conn, ns(
        stock_reconciliation_id=sr["stock_reconciliation_id"]))
    assert is_error(refused_sr), refused_sr
    assert refused_sr["message"] == ("Stored dimensions are not valid JSON; "
                                     "re-create the document")
    assert _counts(conn, env["company_id"]) == before_sr


def test_cancel_nets_to_zero_per_dimension(conn, env):
    cogs = _ensure_cogs(env, conn)
    items = [{"item_id": env["item1"], "qty": "5", "rate": "50.00",
              "from_warehouse_id": env["warehouse"]}]
    se = call_action(mod.add_stock_entry, conn, _se_ns(
        env, entry_type="issue", items=json.dumps(items),
        dimension_key=["department"], dimension_value=["Sales"]))
    assert is_ok(se), se
    assert is_ok(call_action(mod.submit_stock_entry, conn, _se_ns(
        env, stock_entry_id=se["stock_entry_id"]))), se
    cancel = call_action(mod.cancel_stock_entry, conn, _se_ns(
        env, stock_entry_id=se["stock_entry_id"]))
    assert is_ok(cancel), cancel
    t = Table("gl_entry")
    q = (Q.from_(t).select(t.account_id, t.debit, t.credit, t.dimensions_json,
                           t.remarks)
         .where(t.voucher_id == P()).where(t.voucher_type == P()))
    rows = conn.execute(q.get_sql(), (se["stock_entry_id"], "stock_entry")
                        ).fetchall()
    assert rows, "cancel must leave original and mirror rows"
    nets = {}
    mirrors = [r for r in rows if (r["remarks"] or "").startswith("Reversal of ")]
    assert mirrors, "cancel must write reversal mirrors"
    for r in mirrors:
        assert r["dimensions_json"] == Sv
    for r in rows:
        key = (r["account_id"], r["dimensions_json"])
        nets[key] = nets.get(key, Decimal("0.00")) + (
            Decimal(r["debit"]) - Decimal(r["credit"]))
    assert nets == {(cogs, Sv): Decimal("0.00"),
                    (env["stock_acct"], Sv): Decimal("0.00")}


def test_untagged_documents_unchanged(conn, env):
    items = [{"item_id": env["item2"], "qty": "4", "rate": "25.00",
              "to_warehouse_id": env["warehouse"]}]
    se = call_action(mod.add_stock_entry, conn, _se_ns(
        env, entry_type="receive", items=json.dumps(items)))
    assert is_ok(se), se
    assert _doc_text(conn, "stock_entry", se["stock_entry_id"]) == "{}"
    result = call_action(mod.submit_stock_entry, conn, _se_ns(
        env, stock_entry_id=se["stock_entry_id"]))
    assert is_ok(result), result
    assert result["gl_entries_created"] == 2
    assert [d for (_a, _d, _c, d) in _gl(conn, se["stock_entry_id"])] == [
        "{}", "{}"]
    rv = call_action(mod.revalue_stock, conn, _rv_ns(
        env, item_id=env["item1"], warehouse_id=env["warehouse"],
        new_rate="60.00"))
    assert is_ok(rv), rv
    assert rv["gl_entries_created"] == 2
    assert _gl(conn, rv["revaluation_id"], "stock_revaluation") == sorted([
        (env["stock_acct"], "1000.00", "0.00", "{}"),
        (env["stock_adj"], "0.00", "1000.00", "{}"),
    ])
