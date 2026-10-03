"""Read-only report of posted landed-cost vouchers with integrity defects.

Lists submitted vouchers that priced another company's receipt line
(other-company-receipt) or that name the same receipt line more times than
the voucher's charge count allows (repeated-receipt-item). The report reads
only; vouchers it names stay posted.
"""
import json

from buying_helpers import (
    call_action, ns, is_error, is_ok, load_db_query, _uuid, build_buying_env,
)
from erpclaw_lib.query import Q, P, Table, fn

mod = load_db_query()


def _seed_fifo_item(conn, name="FIFO Import Widget"):
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


def _setup_two_companies(conn):
    env_a = build_buying_env(conn)
    env_b = build_buying_env(conn)
    item_a = _seed_fifo_item(conn)
    item_b = _seed_fifo_item(conn)
    pr_a = _submitted_receipt(conn, env_a, item_a)
    pr_b = _submitted_receipt(conn, env_b, item_b)
    return env_a, env_b, item_a, item_b, pr_a, pr_b


def _receipt_line_id(conn, pr_id):
    t = Table("purchase_receipt_item")
    q = Q.from_(t).select(t.id).where(t.purchase_receipt_id == P())
    rows = conn.execute(q.get_sql(), (pr_id,)).fetchall()
    assert len(rows) == 1, f"expected one line for {pr_id}, got {len(rows)}"
    return rows[0]["id"]


def _insert_voucher(conn, company_id, naming, posting_date,
                    status="submitted", total="100.00"):
    vid = _uuid()
    t = Table("landed_cost_voucher")
    q = (Q.into(t)
         .columns("id", "naming_series", "posting_date", "total_landed_cost",
                  "status", "company_id")
         .insert(P(), P(), P(), P(), P(), P()))
    conn.execute(q.get_sql(),
                 (vid, naming, posting_date, total, status, company_id))
    conn.commit()
    return vid


def _insert_charge(conn, voucher_id, expense_id, amount="100.00",
                   desc="Freight"):
    cid = _uuid()
    t = Table("landed_cost_charge")
    q = (Q.into(t)
         .columns("id", "landed_cost_voucher_id", "description", "amount",
                  "expense_account_id", "allocation_method")
         .insert(P(), P(), P(), P(), P(), P()))
    conn.execute(q.get_sql(),
                 (cid, voucher_id, desc, amount, expense_id, "by_amount"))
    conn.commit()
    return cid


def _insert_item(conn, voucher_id, pr_id, line_id, charges="50.00",
                 original="50.00", final="55.00"):
    iid = _uuid()
    t = Table("landed_cost_item")
    q = (Q.into(t)
         .columns("id", "landed_cost_voucher_id", "purchase_receipt_id",
                  "purchase_receipt_item_id", "applicable_charges",
                  "original_rate", "final_rate")
         .insert(P(), P(), P(), P(), P(), P(), P()))
    conn.execute(q.get_sql(),
                 (iid, voucher_id, pr_id, line_id, charges, original, final))
    conn.commit()
    return iid


def _item_count(conn, voucher_id, line_id):
    t = Table("landed_cost_item")
    q = (Q.from_(t).select(fn.Count("*").as_("cnt"))
         .where(t.landed_cost_voucher_id == P())
         .where(t.purchase_receipt_item_id == P()))
    return conn.execute(q.get_sql(), (voucher_id, line_id)).fetchone()["cnt"]


def _table_count(conn, table):
    t = Table(table)
    q = Q.from_(t).select(fn.Count("*").as_("cnt"))
    return conn.execute(q.get_sql()).fetchone()["cnt"]


def _report(conn, **kw):
    args = {"limit": "20", "offset": "0"}
    args.update(kw)
    return call_action(mod.list_landed_cost_voucher_anomalies, conn, ns(**args))


def test_clean_vouchers_are_not_listed(conn, monkeypatch):
    monkeypatch.setattr(mod, "_today", lambda: "2026-06-25")
    env = build_buying_env(conn)
    item = _seed_fifo_item(conn)
    pr1 = _submitted_receipt(conn, env, item)
    pr2 = _submitted_receipt(conn, env, item)
    r1 = _add_lcv(conn, env, [pr1], _freight_100(env))
    assert is_ok(r1), f"LCV failed: {r1}"
    charges2 = [{"description": "Freight", "amount": "60.00",
                 "expense_account_id": env["expense"]},
                {"description": "Duty", "amount": "40.00",
                 "expense_account_id": env["expense"]}]
    r2 = _add_lcv(conn, env, [pr2], charges2)
    assert is_ok(r2), f"LCV failed: {r2}"
    line2 = _receipt_line_id(conn, pr2)
    assert int(_item_count(conn, r2["landed_cost_voucher_id"], line2)) == 2
    out = _report(conn, company_id=env["company_id"])
    assert is_ok(out), f"report failed: {out}"
    assert out["anomalies"] == []
    assert out["total_count"] == 0


def test_other_company_receipt_is_listed(conn, monkeypatch):
    monkeypatch.setattr(mod, "_today", lambda: "2026-06-25")
    env_a, env_b, item_a, item_b, pr_a, pr_b = _setup_two_companies(conn)
    line_a = _receipt_line_id(conn, pr_a)
    line_b = _receipt_line_id(conn, pr_b)
    v0 = _insert_voucher(conn, env_a["company_id"], "LCV-V0", "2026-06-18")
    _insert_charge(conn, v0, env_a["expense"])
    _insert_item(conn, v0, pr_a, line_a)
    v1 = _insert_voucher(conn, env_a["company_id"], "LCV-V1", "2026-06-20")
    _insert_charge(conn, v1, env_a["expense"])
    _insert_item(conn, v1, pr_b, line_b)
    out = _report(conn, company_id=env_a["company_id"])
    assert is_ok(out), f"report failed: {out}"
    assert out["total_count"] == 1
    assert out["anomalies"] == [
        {"landed_cost_voucher_id": v1, "naming_series": "LCV-V1",
         "posting_date": "2026-06-20", "kind": "other-company-receipt",
         "purchase_receipt_id": pr_b,
         "purchase_receipt_item_ids": [line_b]}]
    out_b = _report(conn, company_id=env_b["company_id"])
    assert is_ok(out_b), f"report failed: {out_b}"
    assert out_b["anomalies"] == []
    assert out_b["total_count"] == 0


def test_repeated_receipt_item_is_listed(conn, monkeypatch):
    monkeypatch.setattr(mod, "_today", lambda: "2026-06-25")
    env = build_buying_env(conn)
    item = _seed_fifo_item(conn)
    pr = _submitted_receipt(conn, env, item)
    line = _receipt_line_id(conn, pr)
    v2 = _insert_voucher(conn, env["company_id"], "LCV-V2", "2026-06-20")
    _insert_charge(conn, v2, env["expense"])
    _insert_item(conn, v2, pr, line)
    _insert_item(conn, v2, pr, line)
    v3 = _insert_voucher(conn, env["company_id"], "LCV-V3", "2026-06-21")
    _insert_charge(conn, v3, env["expense"])
    _insert_charge(conn, v3, env["expense"])
    _insert_item(conn, v3, pr, line)
    _insert_item(conn, v3, pr, line)
    out = _report(conn, company_id=env["company_id"])
    assert is_ok(out), f"report failed: {out}"
    assert out["total_count"] == 1
    assert out["anomalies"] == [
        {"landed_cost_voucher_id": v2, "naming_series": "LCV-V2",
         "posting_date": "2026-06-20", "kind": "repeated-receipt-item",
         "purchase_receipt_id": pr,
         "purchase_receipt_item_ids": [line]}]
    _insert_item(conn, v3, pr, line)
    _insert_item(conn, v3, pr, line)
    out2 = _report(conn, company_id=env["company_id"])
    assert is_ok(out2), f"report failed: {out2}"
    assert out2["total_count"] == 2
    assert [a["landed_cost_voucher_id"] for a in out2["anomalies"]] == [v3, v2]


def test_cancelled_and_draft_vouchers_are_ignored(conn, monkeypatch):
    monkeypatch.setattr(mod, "_today", lambda: "2026-06-25")
    env_a, env_b, item_a, item_b, pr_a, pr_b = _setup_two_companies(conn)
    line_b = _receipt_line_id(conn, pr_b)
    vc = _insert_voucher(conn, env_a["company_id"], "LCV-C", "2026-06-20",
                         status="cancelled")
    _insert_charge(conn, vc, env_a["expense"])
    _insert_item(conn, vc, pr_b, line_b)
    vd = _insert_voucher(conn, env_a["company_id"], "LCV-D", "2026-06-21",
                         status="draft")
    _insert_charge(conn, vd, env_a["expense"])
    _insert_item(conn, vd, pr_b, line_b)
    out = _report(conn, company_id=env_a["company_id"])
    assert is_ok(out), f"report failed: {out}"
    assert out["anomalies"] == []
    assert out["total_count"] == 0


def test_ordering_paging_and_both_kinds(conn, monkeypatch):
    monkeypatch.setattr(mod, "_today", lambda: "2026-06-25")
    env_a, env_b, item_a, item_b, pr_a, pr_b = _setup_two_companies(conn)
    line_a = _receipt_line_id(conn, pr_a)
    line_b = _receipt_line_id(conn, pr_b)
    vo = _insert_voucher(conn, env_a["company_id"], "LCV-OLD", "2026-06-01")
    _insert_charge(conn, vo, env_a["expense"])
    _insert_item(conn, vo, pr_a, line_a)
    _insert_item(conn, vo, pr_a, line_a)
    vn = _insert_voucher(conn, env_a["company_id"], "LCV-NEW", "2026-06-10")
    _insert_charge(conn, vn, env_a["expense"])
    _insert_item(conn, vn, pr_b, line_b)
    _insert_item(conn, vn, pr_b, line_b)
    out = _report(conn, company_id=env_a["company_id"])
    assert is_ok(out), f"report failed: {out}"
    assert out["total_count"] == 3
    got = [(a["landed_cost_voucher_id"], a["kind"], a["posting_date"],
            a["purchase_receipt_id"]) for a in out["anomalies"]]
    assert got == [(vn, "other-company-receipt", "2026-06-10", pr_b),
                   (vn, "repeated-receipt-item", "2026-06-10", pr_b),
                   (vo, "repeated-receipt-item", "2026-06-01", pr_a)]
    page = _report(conn, company_id=env_a["company_id"], limit="1", offset="1")
    assert is_ok(page), f"report failed: {page}"
    assert page["total_count"] == 3
    assert page["anomalies"] == [out["anomalies"][1]]


def test_report_writes_nothing(conn, monkeypatch):
    monkeypatch.setattr(mod, "_today", lambda: "2026-06-25")
    env = build_buying_env(conn)
    item = _seed_fifo_item(conn)
    pr = _submitted_receipt(conn, env, item)
    r1 = _add_lcv(conn, env, [pr], _freight_100(env))
    assert is_ok(r1), f"LCV failed: {r1}"
    tables = ("landed_cost_voucher", "landed_cost_charge", "landed_cost_item",
              "gl_entry", "stock_ledger_entry", "audit_log")
    before = {t: _table_count(conn, t) for t in tables}
    out = _report(conn, company_id=env["company_id"])
    assert is_ok(out), f"report failed: {out}"
    after = {t: _table_count(conn, t) for t in tables}
    assert before == after


def test_company_scope(conn, monkeypatch):
    monkeypatch.setattr(mod, "_today", lambda: "2026-06-25")
    build_buying_env(conn)
    build_buying_env(conn)
    out = _report(conn, company_id=None)
    assert is_error(out), f"expected scope refusal, got: {out}"
    assert out["error"] == ("Multiple companies found. "
                            "Please specify the company by name.")
    bogus = _report(conn, company_id="bogus")
    assert bogus == {"status": "error", "error": "Company not found: bogus",
                     "message": "Company not found: bogus"}
