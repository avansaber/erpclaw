"""Buying documents carry accounting dimensions from draft to ledger (m714).

A buying document tagged with accounting dimensions (``department`` is
seeded active on every fresh install) stores the tags on the document,
derived documents inherit them, and every ledger row the document posts
carries them, so each tagged voucher balances per value.
"""
import importlib.util
import json
import os
import sys
import uuid
from decimal import Decimal

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from buying_helpers import (  # noqa: E402
    call_action, is_error, is_ok, load_db_query, ns,
)
from erpclaw_lib.query import (  # noqa: E402
    Field, P, Q, Table, fn, insert_row,
)

mod = load_db_query()

E = '{"department": "Engineering"}'
OPS = '{"department": "Ops"}'
SV = '{"department": "Sales"}'
FUND_TEXT = '{"department": "Sales", "fund": "F1"}'


def _load_reports():
    path = os.path.join(
        os.path.dirname(_TESTS_DIR), "..", "erpclaw-reports", "db_query.py")
    path = os.path.normpath(path)
    spec = importlib.util.spec_from_file_location(
        "db_query_reports_dims", path)
    rep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rep)
    return rep


REP = _load_reports()


def _ns(**overrides):
    base = dict(
        supplier_id=None, company_id=None, items=None,
        tax_template_id=None, posting_date=None, due_date=None,
        purchase_order_id=None, purchase_receipt_id=None,
        purchase_invoice_id=None, against_invoice_id=None, reason=None,
        name=None, po_status=None, pr_status=None, pi_status=None,
        mr_status=None, blanket_order_id=None, blanket_status=None,
        valid_from=None, valid_to=None, sales_order_id=None,
        template_id=None, frequency=None, start_date=None, end_date=None,
        as_of_date=None, auto_submit=False, template_status=None,
        item_id=None, purchase_uom=None, conversion_factor=None,
        subcontracting_order_id=None, received_qty=None,
        subcontract_charge_rate=None, cwip_asset_id=None,
        material_request_id=None, request_type=None,
        dimensions=None, dimension_key=None, dimension_value=None,
        db_path=None, limit="20", offset="0",
    )
    base.update(overrides)
    return ns(**base)


def _rep_ns(**kw):
    base = {"company_id": None, "company_name": None, "from_date": None,
            "to_date": None, "as_of_date": None, "account_id": None,
            "cost_center_id": None, "project_id": None,
            "fiscal_year_id": None, "party_type": None, "party_id": None,
            "voucher_type": None, "periods": None, "group_by": None,
            "dimension_key": None, "dimension_value": None,
            "limit": "100", "offset": "0", "aging_buckets": "30,60,90,120"}
    base.update(kw)
    return ns(**base)


def _items(env, *specs):
    return json.dumps([
        {"item_id": env[k], "qty": q, "rate": r,
         "warehouse_id": env["warehouse"]}
        for k, q, r in specs
    ])


def _u():
    return str(uuid.uuid4())


def _insert(conn, table, row):
    sql, cols = insert_row(table, {key: P() for key in row})
    conn.execute(sql, [row[c] for c in cols])
    conn.commit()


def _tax_template(conn, env, rate, account_name="Input Tax"):
    tax_account = _u()
    _insert(conn, "account", {
        "id": tax_account, "name": account_name,
        "account_number": "1400-%s" % tax_account[:6], "root_type": "asset",
        "account_type": "tax", "balance_direction": "debit_normal",
        "company_id": env["company_id"], "depth": 0})
    tpl = _u()
    _insert(conn, "tax_template", {
        "id": tpl, "name": "Purchase Tax %s-%s" % (rate, tpl[:4]),
        "tax_type": "purchase", "company_id": env["company_id"]})
    _insert(conn, "tax_template_line", {
        "id": _u(), "tax_template_id": tpl, "tax_account_id": tax_account,
        "rate": rate, "charge_type": "on_net_total", "row_order": 0,
        "add_deduct": "add"})
    return tpl, tax_account


def _register_fund(conn):
    t = Table("dimension_registry")
    conn.execute(
        Q.into(t).columns(
            "id", "key", "label", "data_type", "referenced_table",
            "allowed_values_json", "is_required_on_account_types_json",
            "is_active").insert(
                P(), P(), P(), P(), P(), P(), P(), P()).get_sql(),
        (_u(), "fund", "fund", "text", None, None, None, 1))
    conn.commit()


def _set_dimension_active(conn, key, active):
    t = Table("dimension_registry")
    conn.execute(
        Q.update(t).set(t.is_active, P()).where(t.key == P()).get_sql(),
        (active, key))
    conn.commit()


def _gl(conn, voucher_id):
    t = Table("gl_entry")
    q = (Q.from_(t).select(
        t.account_id, t.debit, t.credit, t.dimensions_json)
        .where(t.voucher_id == P())
        .where(t.is_cancelled == 0))
    rows = conn.execute(q.get_sql(), (voucher_id,)).fetchall()
    return sorted([(r["account_id"], r["debit"], r["credit"],
                    r["dimensions_json"]) for r in rows])


def _gl_all(conn, voucher_id):
    t = Table("gl_entry")
    q = (Q.from_(t).select(
        t.account_id, t.debit, t.credit, t.dimensions_json)
        .where(t.voucher_id == P()))
    rows = conn.execute(q.get_sql(), (voucher_id,)).fetchall()
    return [(r["account_id"], r["debit"], r["credit"],
             r["dimensions_json"]) for r in rows]


def _doc_dims(conn, table, doc_id):
    t = Table(table)
    q = Q.from_(t).select(t.dimensions_json).where(t.id == P())
    row = conn.execute(q.get_sql(), (doc_id,)).fetchone()
    assert row is not None
    return row["dimensions_json"]


def _doc_field(conn, table, doc_id, field):
    t = Table(table)
    col = getattr(t, field)
    q = Q.from_(t).select(col).where(t.id == P())
    row = conn.execute(q.get_sql(), (doc_id,)).fetchone()
    assert row is not None
    return row[field]


def _count(conn, table):
    t = Table(table)
    q = Q.from_(t).select(fn.Count("*").as_("n"))
    return conn.execute(q.get_sql()).fetchone()["n"]


def _naming(conn, company_id):
    t = Table("naming_series")
    q = (Q.from_(t).select(
        t.entity_type, t.prefix, t.current_value)
        .where(t.company_id == P()))
    rows = conn.execute(q.get_sql(), (company_id,)).fetchall()
    return sorted([(r["entity_type"], r["prefix"], r["current_value"])
                   for r in rows])


def _snapshot(conn, company_id):
    return {
        "purchase_order": _count(conn, "purchase_order"),
        "purchase_order_item": _count(conn, "purchase_order_item"),
        "purchase_receipt": _count(conn, "purchase_receipt"),
        "purchase_invoice": _count(conn, "purchase_invoice"),
        "audit_log": _count(conn, "audit_log"),
        "naming_series": _naming(conn, company_id),
    }


def _setup_chain(conn, env):
    po = call_action(mod.add_purchase_order, conn, _ns(
        supplier_id=env["supplier"], company_id=env["company_id"],
        posting_date="2026-03-10",
        items=_items(env, ("item1", "10", "50.00")),
        dimensions=E))
    assert is_ok(po), po
    po_id = po["purchase_order_id"]
    assert _doc_dims(conn, "purchase_order", po_id) == E
    assert is_ok(call_action(mod.submit_purchase_order, conn, _ns(
        purchase_order_id=po_id))), "submit PO"
    pr = call_action(mod.create_purchase_receipt, conn, _ns(
        purchase_order_id=po_id, posting_date="2026-03-10"))
    assert is_ok(pr), pr
    pr_id = pr["purchase_receipt_id"]
    assert _doc_dims(conn, "purchase_receipt", pr_id) == E
    sub = call_action(mod.submit_purchase_receipt, conn, _ns(
        purchase_receipt_id=pr_id))
    assert is_ok(sub), sub
    pi = call_action(mod.create_purchase_invoice, conn, _ns(
        purchase_order_id=po_id, posting_date="2026-03-10"))
    assert is_ok(pi), pi
    pi_id = pi["purchase_invoice_id"]
    assert _doc_dims(conn, "purchase_invoice", pi_id) == E
    sub2 = call_action(mod.submit_purchase_invoice, conn, _ns(
        purchase_invoice_id=pi_id))
    assert is_ok(sub2), sub2
    return {"po_id": po_id, "pr_id": pr_id, "pi_id": pi_id,
            "receipt_gl": sub["gl_entries_created"],
            "invoice_submit": sub2}


def _setup_untagged_only(conn, env):
    untagged = call_action(mod.create_purchase_invoice, conn, _ns(
        supplier_id=env["supplier"], company_id=env["company_id"],
        posting_date="2026-03-10",
        items=_items(env, ("item1", "1", "30.00"))))
    assert is_ok(untagged), untagged
    untagged_id = untagged["purchase_invoice_id"]
    assert _doc_dims(conn, "purchase_invoice", untagged_id) == "{}"
    usub = call_action(mod.submit_purchase_invoice, conn, _ns(
        purchase_invoice_id=untagged_id))
    assert is_ok(usub), usub
    return {"untagged_id": untagged_id, "untagged_submit": usub}


def _setup_test2(conn, env):
    tpl, tax = _tax_template(conn, env, "10.00")
    tagged = call_action(mod.create_purchase_invoice, conn, _ns(
        supplier_id=env["supplier"], company_id=env["company_id"],
        posting_date="2026-03-10",
        items=_items(env, ("item2", "4", "25.00")),
        tax_template_id=tpl,
        dimension_key=["department"], dimension_value=["Sales"]))
    assert is_ok(tagged), tagged
    tagged_id = tagged["purchase_invoice_id"]
    assert _doc_dims(conn, "purchase_invoice", tagged_id) == SV
    tsub = call_action(mod.submit_purchase_invoice, conn, _ns(
        purchase_invoice_id=tagged_id))
    assert is_ok(tsub), tsub
    out = {"tagged_id": tagged_id, "tax": tax, "tagged_submit": tsub}
    out.update(_setup_untagged_only(conn, env))
    return out


def _seed_customer(conn, company_id, name="Test Customer"):
    cid = _u()
    _insert(conn, "customer", {
        "id": cid, "name": name, "company_id": company_id,
        "customer_type": "company", "status": "active",
        "credit_limit": "0"})
    return cid


def _seed_sales_order(conn, env, customer_id, items):
    so_id = _u()
    total = Decimal("0")
    for item_id, qty, rate, uom, wh in items:
        total += Decimal(str(qty)) * Decimal(str(rate))
    _insert(conn, "sales_order", {
        "id": so_id, "customer_id": customer_id,
        "order_date": "2026-06-01", "total_amount": str(total),
        "tax_amount": "0", "grand_total": str(total),
        "status": "confirmed", "company_id": env["company_id"]})
    for item_id, qty, rate, uom, wh in items:
        amount = str(Decimal(str(qty)) * Decimal(str(rate)))
        _insert(conn, "sales_order_item", {
            "id": _u(), "sales_order_id": so_id, "item_id": item_id,
            "quantity": str(qty), "uom": uom, "rate": str(rate),
            "amount": amount, "discount_percentage": "0",
            "net_amount": amount, "warehouse_id": wh})
    return so_id


def _seed_item_supplier(conn, item_id, supplier_id, priority=0):
    _insert(conn, "item_supplier", {
        "id": _u(), "item_id": item_id, "supplier_id": supplier_id,
        "priority": priority})


def _set_so_dims(conn, so_id, text):
    t = Table("sales_order")
    conn.execute(
        Q.update(t).set(t.dimensions_json, P()).where(t.id == P()).get_sql(),
        (text, so_id))
    conn.commit()


def _make_blanket(conn, env):
    bo = call_action(mod.add_blanket_po, conn, _ns(
        supplier_id=env["supplier"], company_id=env["company_id"],
        items=json.dumps([{"item_id": env["item1"], "qty": "100",
                           "rate": "50.00"}]),
        valid_from="2026-01-01", valid_to="2099-12-31"))
    assert is_ok(bo), bo
    sub = call_action(mod.submit_blanket_po, conn, _ns(
        blanket_order_id=bo["blanket_order_id"]))
    assert is_ok(sub), sub
    return bo["blanket_order_id"]


def _make_mr(conn, env):
    mr = call_action(mod.add_material_request, conn, _ns(
        request_type="purchase", company_id=env["company_id"],
        items=json.dumps([{"item_id": env["item1"], "qty": "5",
                           "warehouse_id": env["warehouse"]}])))
    assert is_ok(mr), mr
    mr_id = mr["material_request_id"]
    assert is_ok(call_action(mod.submit_material_request, conn, _ns(
        material_request_id=mr_id))), "submit MR"
    return mr_id


def _mr_overrides(env):
    return json.dumps([{"item_id": env["item1"], "qty": "5",
                        "rate": "50.00"}])


class TestProcureChainTagged:
    def test_chain(self, conn, env):
        ids = _setup_chain(conn, env)
        assert _gl(conn, ids["pr_id"]) == sorted([
            (env["stock_acct"], "500.00", "0.00", E),
            (env["srnb"], "0.00", "500.00", E),
        ])
        assert _gl(conn, ids["pi_id"]) == sorted([
            (env["srnb"], "500.00", "0.00", E),
            (env["ap"], "0.00", "500.00", E),
        ])
        assert ids["invoice_submit"]["gl_entries_created"] == 2
        assert _doc_field(
            conn, "purchase_invoice", ids["pi_id"], "update_stock") == 0


class TestStandaloneTaxed:
    def test_tagged_and_untagged(self, conn, env):
        ids = _setup_test2(conn, env)
        assert _gl(conn, ids["tagged_id"]) == sorted([
            (env["expense"], "100.00", "0.00", SV),
            (ids["tax"], "10.00", "0.00", SV),
            (env["ap"], "0.00", "110.00", SV),
            (env["stock_acct"], "100.00", "0.00", SV),
            (env["srnb"], "0.00", "100.00", SV),
        ])
        assert ids["tagged_submit"]["gl_entries_created"] == 5
        assert _gl(conn, ids["untagged_id"]) == sorted([
            (env["expense"], "30.00", "0.00", "{}"),
            (env["ap"], "0.00", "30.00", "{}"),
            (env["stock_acct"], "30.00", "0.00", "{}"),
            (env["srnb"], "0.00", "30.00", "{}"),
        ])


class TestPerValueBalanceAndReports:
    def test_reports(self, conn, env):
        ids = _setup_test2(conn, env)
        trial = call_action(REP.multi_dim_trial_balance, conn, _rep_ns(
            company_id=env["company_id"], from_date="2026-01-01",
            to_date="2026-12-31", group_by="department"))
        assert trial["status"] == "ok", trial
        groups = sorted(trial["groups"],
                        key=lambda g: str(g.get("department")))
        assert groups == sorted([
            {"department": "Sales", "debit": "210.00", "credit": "210.00",
             "balance": "0.00"},
            {"department": None, "debit": "60.00", "credit": "60.00",
             "balance": "0.00"},
        ], key=lambda g: str(g.get("department")))
        assert trial["total_debit"] == "270.00"
        assert trial["total_credit"] == "270.00"
        pnl = call_action(REP.profit_and_loss, conn, _rep_ns(
            company_id=env["company_id"], from_date="2026-01-01",
            to_date="2026-12-31", group_by="department"))
        assert pnl["status"] == "ok", pnl
        assert pnl["groups"] == [
            {"department": "Sales", "revenue": "0.00",
             "expenses": "100.00", "net": "-100.00"},
            {"department": "(untagged)", "revenue": "0.00",
             "expenses": "30.00", "net": "-30.00"},
        ]
        assert pnl["income_total"] == "0.00"
        assert pnl["expense_total"] == "130.00"
        assert pnl["net_income"] == "-130.00"


class TestOverrideAndInheritance:
    def test_derive(self, conn, env):
        po = call_action(mod.add_purchase_order, conn, _ns(
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-03-10",
            items=_items(env, ("item1", "10", "50.00")),
            dimensions=E))
        assert is_ok(po), po
        po_id = po["purchase_order_id"]
        assert is_ok(call_action(mod.submit_purchase_order, conn, _ns(
            purchase_order_id=po_id)))

        pr = call_action(mod.create_purchase_receipt, conn, _ns(
            purchase_order_id=po_id, posting_date="2026-03-10",
            dimensions=OPS))
        assert is_ok(pr), pr
        assert _doc_dims(conn, "purchase_receipt", pr["purchase_receipt_id"]) == OPS
        sub = call_action(mod.submit_purchase_receipt, conn, _ns(
            purchase_receipt_id=pr["purchase_receipt_id"]))
        assert is_ok(sub), sub

        pi_pr = call_action(mod.create_purchase_invoice, conn, _ns(
            purchase_receipt_id=pr["purchase_receipt_id"],
            posting_date="2026-03-10"))
        assert is_ok(pi_pr), pi_pr
        assert _doc_dims(
            conn, "purchase_invoice", pi_pr["purchase_invoice_id"]) == OPS

        pi_po = call_action(mod.create_purchase_invoice, conn, _ns(
            purchase_order_id=po_id, posting_date="2026-03-10"))
        assert is_ok(pi_po), pi_po
        assert _doc_dims(
            conn, "purchase_invoice", pi_po["purchase_invoice_id"]) == E

        customer_id = _seed_customer(conn, env["company_id"])
        _seed_item_supplier(conn, env["item1"], env["supplier"])
        so_id = _seed_sales_order(conn, env, customer_id, [
            (env["item1"], "10", "50.00", "Each", env["warehouse"]),
        ])
        _set_so_dims(conn, so_id, E)
        so_po = call_action(mod.create_po_from_so, conn, _ns(
            sales_order_id=so_id, posting_date="2026-03-10"))
        assert is_ok(so_po), so_po
        assert so_po["purchase_orders_created"] == 1
        assert _doc_dims(
            conn, "purchase_order",
            so_po["purchase_orders"][0]["purchase_order_id"]) == E
        so_po2 = call_action(mod.create_po_from_so, conn, _ns(
            sales_order_id=so_id, posting_date="2026-03-10",
            dimensions=OPS))
        assert is_ok(so_po2), so_po2
        assert _doc_dims(
            conn, "purchase_order",
            so_po2["purchase_orders"][0]["purchase_order_id"]) == OPS

        mr_id = _make_mr(conn, env)
        mr_po = call_action(mod.create_po_from_material_request, conn, _ns(
            material_request_id=mr_id, supplier_id=env["supplier"],
            items=_mr_overrides(env), posting_date="2026-03-10",
            dimension_key=["department"], dimension_value=["Ops"]))
        assert is_ok(mr_po), mr_po
        assert _doc_dims(
            conn, "purchase_order", mr_po["purchase_order_id"]) == OPS
        mr_id2 = _make_mr(conn, env)
        mr_po2 = call_action(mod.create_po_from_material_request, conn, _ns(
            material_request_id=mr_id2, supplier_id=env["supplier"],
            items=_mr_overrides(env), posting_date="2026-03-10"))
        assert is_ok(mr_po2), mr_po2
        assert _doc_dims(
            conn, "purchase_order", mr_po2["purchase_order_id"]) == "{}"

        bo_id = _make_blanket(conn, env)
        bpo = call_action(mod.create_po_from_blanket, conn, _ns(
            blanket_order_id=bo_id, posting_date="2026-03-10",
            items=json.dumps([{"item_id": env["item1"], "qty": "10"}]),
            dimensions=OPS))
        assert is_ok(bpo), bpo
        assert _doc_dims(
            conn, "purchase_order", bpo["purchase_order_id"]) == OPS


class TestDebitNote:
    def test_debit_note(self, conn, env):
        ids = _setup_chain(conn, env)
        dn = call_action(mod.create_debit_note, conn, _ns(
            against_invoice_id=ids["pi_id"], posting_date="2026-03-12",
            items=json.dumps([{"item_id": env["item1"], "qty": "2",
                               "rate": "50.00"}])))
        assert is_ok(dn), dn
        dn_id = dn["debit_note_id"]
        assert _doc_dims(conn, "purchase_invoice", dn_id) == E
        sub = call_action(mod.submit_purchase_invoice, conn, _ns(
            purchase_invoice_id=dn_id))
        assert is_ok(sub), sub
        assert _gl(conn, dn_id) == sorted([
            (env["ap"], "100.00", "0.00", E),
            (env["expense"], "0.00", "100.00", E),
        ])

        before = _snapshot(conn, env["company_id"])
        bad = call_action(mod.create_debit_note, conn, _ns(
            against_invoice_id=ids["pi_id"], posting_date="2026-03-12",
            items=json.dumps([{"item_id": env["item1"], "qty": "1",
                               "rate": "50.00"}]),
            dimensions=OPS))
        assert is_error(bad)
        assert bad["message"] == (
            "Debit note dimensions must match the original invoice's (%s)"
            % E)
        assert _snapshot(conn, env["company_id"]) == before

        dn2 = call_action(mod.create_debit_note, conn, _ns(
            against_invoice_id=ids["pi_id"], posting_date="2026-03-12",
            items=json.dumps([{"item_id": env["item1"], "qty": "1",
                               "rate": "50.00"}])))
        assert is_ok(dn2), dn2
        dn2_id = dn2["debit_note_id"]
        before2 = _snapshot(conn, env["company_id"])
        bad_upd = call_action(mod.update_purchase_invoice, conn, _ns(
            purchase_invoice_id=dn2_id, dimensions=OPS))
        assert is_error(bad_upd)
        assert bad_upd["message"] == (
            "Debit note dimensions must match the original invoice's (%s)"
            % E)
        assert _snapshot(conn, env["company_id"]) == before2
        assert _doc_dims(conn, "purchase_invoice", dn2_id) == E

        _register_fund(conn)
        fund_inv = call_action(mod.create_purchase_invoice, conn, _ns(
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-03-10",
            items=_items(env, ("item1", "1", "30.00")),
            dimensions=FUND_TEXT))
        assert is_ok(fund_inv), fund_inv
        fund_inv_id = fund_inv["purchase_invoice_id"]
        assert is_ok(call_action(mod.submit_purchase_invoice, conn, _ns(
            purchase_invoice_id=fund_inv_id)))
        _set_dimension_active(conn, "fund", 0)
        fund_dn = call_action(mod.create_debit_note, conn, _ns(
            against_invoice_id=fund_inv_id, posting_date="2026-03-12",
            items=json.dumps([{"item_id": env["item1"], "qty": "1",
                               "rate": "30.00"}])))
        assert is_ok(fund_dn), fund_dn
        assert _doc_dims(
            conn, "purchase_invoice", fund_dn["debit_note_id"]) == FUND_TEXT


class TestRefusals:
    def test_refusals(self, conn, env):
        _register_fund(conn)
        bad_dims = '{"nosuch": "x"}'

        before = _snapshot(conn, env["company_id"])
        r = call_action(mod.add_purchase_order, conn, _ns(
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-03-10",
            items=_items(env, ("item1", "1", "30.00")),
            dimensions=bad_dims))
        assert is_error(r)
        assert r["message"] == (
            "Unknown or inactive dimension 'nosuch'; run list-dimensions")
        assert _snapshot(conn, env["company_id"]) == before

        po = call_action(mod.add_purchase_order, conn, _ns(
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-03-10",
            items=_items(env, ("item1", "1", "30.00"))))
        assert is_ok(po), po
        before = _snapshot(conn, env["company_id"])
        r = call_action(mod.update_purchase_order, conn, _ns(
            purchase_order_id=po["purchase_order_id"],
            items=_items(env, ("item1", "2", "30.00")),
            dimensions=bad_dims))
        assert is_error(r)
        assert r["message"] == (
            "Unknown or inactive dimension 'nosuch'; run list-dimensions")
        assert _snapshot(conn, env["company_id"]) == before

        assert is_ok(call_action(mod.submit_purchase_order, conn, _ns(
            purchase_order_id=po["purchase_order_id"])))
        before = _snapshot(conn, env["company_id"])
        r = call_action(mod.create_purchase_receipt, conn, _ns(
            purchase_order_id=po["purchase_order_id"],
            posting_date="2026-03-10",
            dimension_key=["department"],
            dimension_value=["A", "B"]))
        assert is_error(r)
        assert r["message"] == (
            "--dimension-key and --dimension-value must be given in pairs")
        assert _snapshot(conn, env["company_id"]) == before

        before = _snapshot(conn, env["company_id"])
        r = call_action(mod.create_purchase_invoice, conn, _ns(
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-03-10",
            items=_items(env, ("item1", "1", "30.00")),
            dimensions='{"department": "A"}',
            dimension_key=["department"], dimension_value=["B"]))
        assert is_error(r)
        assert r["message"] == (
            "Dimension 'department' given twice with different values")
        assert _snapshot(conn, env["company_id"]) == before

        fpo = call_action(mod.add_purchase_order, conn, _ns(
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-03-10",
            items=_items(env, ("item1", "1", "30.00")),
            dimensions='{"fund": "F1"}'))
        assert is_ok(fpo), fpo
        assert is_ok(call_action(mod.submit_purchase_order, conn, _ns(
            purchase_order_id=fpo["purchase_order_id"])))
        _set_dimension_active(conn, "fund", 0)
        before = _snapshot(conn, env["company_id"])
        r = call_action(mod.create_purchase_receipt, conn, _ns(
            purchase_order_id=fpo["purchase_order_id"],
            posting_date="2026-03-10"))
        assert is_error(r)
        assert r["message"] == (
            "Unknown or inactive dimension 'fund'; run list-dimensions")
        assert _snapshot(conn, env["company_id"]) == before


class TestUpdate:
    def test_update(self, conn, env):
        inv = call_action(mod.create_purchase_invoice, conn, _ns(
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-03-10",
            items=_items(env, ("item1", "1", "30.00"))))
        assert is_ok(inv), inv
        inv_id = inv["purchase_invoice_id"]
        assert _doc_dims(conn, "purchase_invoice", inv_id) == "{}"

        upd = call_action(mod.update_purchase_invoice, conn, _ns(
            purchase_invoice_id=inv_id, dimensions=OPS))
        assert is_ok(upd), upd
        assert upd["updated_fields"] == ["dimensions_json"]
        assert _doc_dims(conn, "purchase_invoice", inv_id) == OPS
        t = Table("audit_log")
        rows = conn.execute(
            Q.from_(t).select(t.old_values, t.new_values)
            .where(t.action == P())
            .where(t.entity_type == P())
            .where(t.entity_id == P()).get_sql(),
            ("update-purchase-invoice", "purchase_invoice", inv_id)).fetchall()
        assert rows, "audit row missing"
        last = rows[-1]
        assert json.loads(last["old_values"])["dimensions_json"] == "{}"
        new_vals = json.loads(last["new_values"])
        assert new_vals["dimensions_json"] == OPS
        assert new_vals["updated_fields"] == ["dimensions_json"]

        clr = call_action(mod.update_purchase_invoice, conn, _ns(
            purchase_invoice_id=inv_id, dimensions="{}"))
        assert is_ok(clr), clr
        assert _doc_dims(conn, "purchase_invoice", inv_id) == "{}"

        po = call_action(mod.add_purchase_order, conn, _ns(
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-03-10",
            items=_items(env, ("item1", "1", "30.00"))))
        assert is_ok(po), po
        po_id = po["purchase_order_id"]
        upd_po = call_action(mod.update_purchase_order, conn, _ns(
            purchase_order_id=po_id,
            items=_items(env, ("item1", "2", "30.00")),
            dimensions=OPS))
        assert is_ok(upd_po), upd_po
        assert _doc_dims(conn, "purchase_order", po_id) == OPS


class TestCancelNetsToZero:
    def test_cancel(self, conn, env):
        ids = _setup_test2(conn, env)
        assert is_ok(call_action(mod.cancel_purchase_invoice, conn, _ns(
            purchase_invoice_id=ids["tagged_id"])))
        rows = _gl_all(conn, ids["tagged_id"])
        assert len(rows) == 10
        nets = {}
        for account_id, debit, credit, dims in rows:
            nets[(account_id, dims)] = (
                nets.get((account_id, dims), Decimal("0.00"))
                + Decimal(str(debit)) - Decimal(str(credit)))
        assert nets, "no rows"
        for key, net in nets.items():
            assert net == Decimal("0.00"), key


class TestUntaggedUnchanged:
    def test_untagged(self, conn, env):
        ids = _setup_untagged_only(conn, env)
        rows = _gl(conn, ids["untagged_id"])
        assert rows, "no rows"
        for _account_id, _debit, _credit, dims in rows:
            assert dims == "{}"
        assert ids["untagged_submit"]["gl_entries_created"] == 4

        po = call_action(mod.add_purchase_order, conn, _ns(
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-03-10",
            items=_items(env, ("item1", "10", "50.00"))))
        assert is_ok(po), po
        assert is_ok(call_action(mod.submit_purchase_order, conn, _ns(
            purchase_order_id=po["purchase_order_id"])))
        pr = call_action(mod.create_purchase_receipt, conn, _ns(
            purchase_order_id=po["purchase_order_id"],
            posting_date="2026-03-10"))
        assert is_ok(pr), pr
        sub = call_action(mod.submit_purchase_receipt, conn, _ns(
            purchase_receipt_id=pr["purchase_receipt_id"]))
        assert is_ok(sub), sub
        assert sub["gl_entries_created"] == 2
        for _account_id, _debit, _credit, dims in _gl(
                conn, pr["purchase_receipt_id"]):
            assert dims == "{}"
