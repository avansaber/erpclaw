"""Selling documents carry accounting dimensions from draft to ledger (m712).

A selling document is tagged with accounting dimensions (e.g. department)
when drafted. Tags are checked against the dimension registry at draft time,
stored on the document, inherited by derived documents (quotation -> sales
order -> delivery note / sales invoice -> credit note), and copied onto every
ledger row the document posts.

``department`` (text, active) is seeded on every fresh install; ``fund``
(text, not required) is registered where a test needs a second key. Money is
exact strings; every expected value is a hand-computed literal.
"""
import importlib.util
import json
import os
import uuid
from decimal import Decimal

import pytest

from selling_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
)
from erpclaw_lib.query import Q, P, Table, Field, fn, Order  # noqa: E402

mod = load_db_query()

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(os.path.dirname(_TESTS_DIR))


def _load_reports():
    spec = importlib.util.spec_from_file_location(
        "db_query_reports_m712",
        os.path.join(_SCRIPTS_DIR, "erpclaw-reports", "db_query.py"))
    rep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rep)
    return rep


REP = _load_reports()

E = '{"department": "Engineering"}'
Sv = '{"department": "Sales"}'
POSTING = "2026-03-10"


def _items(env, *specs):
    return json.dumps([
        {"item_id": env[k], "qty": q, "rate": r,
         "warehouse_id": env["warehouse"]}
        for k, q, r in specs
    ])


def _gl(conn, voucher_id):
    t = Table("gl_entry")
    q = (Q.from_(t)
         .select(Field("account_id"), Field("debit"), Field("credit"),
                 Field("dimensions_json"))
         .where(Field("voucher_id") == P())
         .where(Field("is_cancelled") == 0))
    rows = conn.execute(q.get_sql(), (voucher_id,)).fetchall()
    return sorted([(r["account_id"], r["debit"], r["credit"],
                    r["dimensions_json"]) for r in rows])


def _gl_all(conn, voucher_id):
    t = Table("gl_entry")
    q = (Q.from_(t)
         .select(Field("account_id"), Field("debit"), Field("credit"),
                 Field("dimensions_json"))
         .where(Field("voucher_id") == P()))
    rows = conn.execute(q.get_sql(), (voucher_id,)).fetchall()
    return [(r["account_id"], r["debit"], r["credit"],
             r["dimensions_json"]) for r in rows]


def _doc_dimensions(conn, table, doc_id):
    t = Table(table)
    q = (Q.from_(t).select(Field("dimensions_json"))
         .where(Field("id") == P()))
    row = conn.execute(q.get_sql(), (doc_id,)).fetchone()
    return row["dimensions_json"]


def _register_fund(conn):
    t = Table("dimension_registry")
    conn.execute(
        Q.into(t).columns(
            "id", "key", "label", "data_type", "referenced_table",
            "allowed_values_json", "is_required_on_account_types_json",
            "is_active")
        .insert(P(), P(), P(), P(), P(), P(), P(), P()).get_sql(),
        (str(uuid.uuid4()), "fund", "Fund", "text", None, None, None, 1))
    conn.commit()


def _set_fund_inactive(conn):
    t = Table("dimension_registry")
    conn.execute(
        Q.update(t).set("is_active", P())
        .where(Field("key") == P()).get_sql(), (0, "fund"))
    conn.commit()


def _counts(conn, company_id):
    out = {}
    for tbl in ("quotation", "sales_order", "sales_invoice", "audit_log"):
        t = Table(tbl)
        row = conn.execute(
            Q.from_(t).select(fn.Count("*")).get_sql()).fetchone()
        out[tbl] = row[0]
    nt = Table("naming_series")
    rows = conn.execute(
        Q.from_(nt).select(Field("entity_type"), Field("prefix"),
                           Field("current_value"))
        .where(Field("company_id") == P()).get_sql(),
        (company_id,)).fetchall()
    out["naming"] = sorted(
        [(r["entity_type"], r["prefix"], str(r["current_value"]))
         for r in rows])
    return out


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


def _submit_standalone_invoice(conn, env, items, dims=None, dkeys=None,
                               dvals=None, posting_date=POSTING):
    kwargs = {"customer_id": env["customer"],
              "company_id": env["company_id"],
              "posting_date": posting_date, "items": items,
              "sales_order_id": None, "delivery_note_id": None,
              "tax_template_id": None, "due_date": None}
    if dims is not None or dkeys is not None or dvals is not None:
        kwargs["dimensions"] = dims
        kwargs["dimension_key"] = dkeys
        kwargs["dimension_value"] = dvals
    created = call_action(mod.create_sales_invoice, conn, ns(**kwargs))
    assert is_ok(created), f"invoice create failed: {created}"
    submitted = call_action(mod.submit_sales_invoice, conn, ns(
        sales_invoice_id=created["sales_invoice_id"]))
    assert is_ok(submitted), f"invoice submit failed: {submitted}"
    return created["sales_invoice_id"], submitted


# ──────────────────────────────────────────────────────────────────────────────
# 1. Order-to-cash chain, tagged
# ──────────────────────────────────────────────────────────────────────────────

class TestOrderToCashChainTagged:
    def test_chain_carries_dimensions(self, conn, env):
        items = _items(env, ("item1", "1", "25.00"))
        q = call_action(mod.add_quotation, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-03-01", items=items,
            valid_till=None, tax_template_id=None,
            dimensions='{"department": "Engineering"}',
            dimension_key=None, dimension_value=None))
        assert is_ok(q), f"add-quotation failed: {q}"

        sub_q = call_action(mod.submit_quotation, conn, ns(
            quotation_id=q["quotation_id"]))
        assert is_ok(sub_q), f"submit-quotation failed: {sub_q}"

        so = call_action(mod.convert_quotation_to_so, conn, ns(
            quotation_id=q["quotation_id"], delivery_date="2026-03-05",
            dimensions=None, dimension_key=None, dimension_value=None))
        assert is_ok(so), f"convert failed: {so}"
        so_id = so["sales_order_id"]

        sub_so = call_action(mod.submit_sales_order, conn, ns(
            sales_order_id=so_id))
        assert is_ok(sub_so), f"submit SO failed: {sub_so}"

        dn = call_action(mod.create_delivery_note, conn, ns(
            sales_order_id=so_id, posting_date=POSTING, items=None,
            dimensions=None, dimension_key=None, dimension_value=None))
        assert is_ok(dn), f"create DN failed: {dn}"
        dn_id = dn["delivery_note_id"]

        sub_dn = call_action(mod.submit_delivery_note, conn, ns(
            delivery_note_id=dn_id))
        assert is_ok(sub_dn), f"submit DN failed: {sub_dn}"

        si = call_action(mod.create_sales_invoice, conn, ns(
            customer_id=None, company_id=None, posting_date=POSTING,
            items=None, sales_order_id=None, delivery_note_id=dn_id,
            tax_template_id=None, due_date=None,
            dimensions=None, dimension_key=None, dimension_value=None))
        assert is_ok(si), f"create invoice failed: {si}"
        si_id = si["sales_invoice_id"]
        assert si["update_stock"] == 0

        sub_si = call_action(mod.submit_sales_invoice, conn, ns(
            sales_invoice_id=si_id))
        assert is_ok(sub_si), f"submit invoice failed: {sub_si}"

        assert _doc_dimensions(conn, "quotation", q["quotation_id"]) == E
        assert _doc_dimensions(conn, "sales_order", so_id) == E
        assert _doc_dimensions(conn, "delivery_note", dn_id) == E
        assert _doc_dimensions(conn, "sales_invoice", si_id) == E

        got_q = call_action(mod.get_quotation, conn, ns(
            quotation_id=q["quotation_id"]))
        assert is_ok(got_q) and got_q["dimensions_json"] == E
        got_so = call_action(mod.get_sales_order, conn, ns(
            sales_order_id=so_id))
        assert is_ok(got_so) and got_so["dimensions_json"] == E
        got_dn = call_action(mod.get_delivery_note, conn, ns(
            delivery_note_id=dn_id))
        assert is_ok(got_dn) and got_dn["dimensions_json"] == E
        got_si = call_action(mod.get_sales_invoice, conn, ns(
            sales_invoice_id=si_id))
        assert is_ok(got_si) and got_si["dimensions_json"] == E

        assert _gl(conn, dn_id) == sorted([
            (env["cogs"], "10.00", "0.00", E),
            (env["stock_acct"], "0.00", "10.00", E)])
        assert _gl(conn, si_id) == sorted([
            (env["ar"], "25.00", "0.00", E),
            (env["revenue"], "0.00", "25.00", E)])
        assert sub_si["gl_entries_created"] == 2


# ──────────────────────────────────────────────────────────────────────────────
# 2. Standalone tagged invoice with stock, plus an untagged one
# ──────────────────────────────────────────────────────────────────────────────

class TestStandaloneTaggedInvoice:
    def test_tagged_and_untagged(self, conn, env):
        tagged_id, tagged_sub = _submit_standalone_invoice(
            conn, env, _items(env, ("item2", "2", "30.00")),
            dims=None, dkeys=["department"], dvals=["Sales"])
        assert _doc_dimensions(conn, "sales_invoice", tagged_id) == Sv
        assert _gl(conn, tagged_id) == sorted([
            (env["ar"], "60.00", "0.00", Sv),
            (env["revenue"], "0.00", "60.00", Sv),
            (env["cogs"], "40.00", "0.00", Sv),
            (env["stock_acct"], "0.00", "40.00", Sv)])

        untagged_id, untagged_sub = _submit_standalone_invoice(
            conn, env, _items(env, ("item1", "1", "25.00")))
        assert _doc_dimensions(conn, "sales_invoice", untagged_id) == "{}"
        assert _gl(conn, untagged_id) == sorted([
            (env["ar"], "25.00", "0.00", "{}"),
            (env["revenue"], "0.00", "25.00", "{}"),
            (env["cogs"], "10.00", "0.00", "{}"),
            (env["stock_acct"], "0.00", "10.00", "{}")])


# ──────────────────────────────────────────────────────────────────────────────
# 3. Per-value balance and reports
# ──────────────────────────────────────────────────────────────────────────────

class TestPerValueBalanceAndReports:
    def test_grouped_reports(self, conn, env):
        tagged_id, _ = _submit_standalone_invoice(
            conn, env, _items(env, ("item2", "2", "30.00")),
            dims=None, dkeys=["department"], dvals=["Sales"])
        untagged_id, _ = _submit_standalone_invoice(
            conn, env, _items(env, ("item1", "1", "25.00")))

        trial = call_action(
            REP.multi_dim_trial_balance, conn, _base_ns(
                company_id=env["company_id"],
                from_date="2026-01-01", to_date="2026-12-31",
                group_by="department"))
        assert trial["status"] == "ok", f"trial failed: {trial}"
        by_dept = {g["department"]: g for g in trial["groups"]}
        assert by_dept["Sales"] == {"department": "Sales",
                                    "debit": "100.00", "credit": "100.00",
                                    "balance": "0.00"}
        assert by_dept[None] == {"department": None,
                                 "debit": "35.00", "credit": "35.00",
                                 "balance": "0.00"}

        pl = call_action(
            REP.profit_and_loss, conn, _base_ns(
                company_id=env["company_id"],
                from_date="2026-01-01", to_date="2026-12-31",
                group_by="department"))
        assert pl["status"] == "ok", f"p&l failed: {pl}"
        assert pl["groups"] == [
            {"department": "Sales", "revenue": "60.00",
             "expenses": "40.00", "net": "20.00"},
            {"department": "(untagged)", "revenue": "25.00",
             "expenses": "10.00", "net": "15.00"}]
        assert pl["income_total"] == "85.00"
        assert pl["expense_total"] == "50.00"


# ──────────────────────────────────────────────────────────────────────────────
# 4. Override on derive
# ──────────────────────────────────────────────────────────────────────────────

class TestOverrideOnDerive:
    def _tagged_submitted_so(self, conn, env):
        so = call_action(mod.add_sales_order, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-03-01",
            items=_items(env, ("item1", "1", "25.00")),
            delivery_date="2026-03-05", tax_template_id=None,
            dimensions='{"department": "Engineering"}',
            dimension_key=None, dimension_value=None))
        assert is_ok(so), f"add SO failed: {so}"
        sub = call_action(mod.submit_sales_order, conn, ns(
            sales_order_id=so["sales_order_id"]))
        assert is_ok(sub), f"submit SO failed: {sub}"
        return so["sales_order_id"]

    def test_override_stores_given(self, conn, env):
        so_id = self._tagged_submitted_so(conn, env)
        si = call_action(mod.create_sales_invoice, conn, ns(
            customer_id=None, company_id=None, posting_date=POSTING,
            items=None, sales_order_id=so_id, delivery_note_id=None,
            tax_template_id=None, due_date=None,
            dimensions='{"department": "Ops"}',
            dimension_key=None, dimension_value=None))
        assert is_ok(si), f"create invoice failed: {si}"
        assert _doc_dimensions(
            conn, "sales_invoice",
            si["sales_invoice_id"]) == '{"department": "Ops"}'

    def test_absent_input_inherits(self, conn, env):
        so_id = self._tagged_submitted_so(conn, env)
        si = call_action(mod.create_sales_invoice, conn, ns(
            customer_id=None, company_id=None, posting_date=POSTING,
            items=None, sales_order_id=so_id, delivery_note_id=None,
            tax_template_id=None, due_date=None,
            dimensions=None, dimension_key=None, dimension_value=None))
        assert is_ok(si), f"create invoice failed: {si}"
        assert _doc_dimensions(
            conn, "sales_invoice", si["sales_invoice_id"]) == E


# ──────────────────────────────────────────────────────────────────────────────
# 5. Credit note
# ──────────────────────────────────────────────────────────────────────────────

class TestCreditNote:
    def _tagged_submitted_invoice(self, conn, env):
        return _submit_standalone_invoice(
            conn, env, _items(env, ("item2", "2", "30.00")),
            dims=None, dkeys=["department"], dvals=["Sales"])

    def test_mirror_carries_dimensions(self, conn, env):
        inv_id, _ = self._tagged_submitted_invoice(conn, env)
        cn = call_action(mod.create_credit_note, conn, ns(
            against_invoice_id=inv_id,
            items=json.dumps([{"item_id": env["item2"], "qty": "2"}]),
            posting_date=POSTING, reason=None,
            dimensions=None, dimension_key=None, dimension_value=None))
        assert is_ok(cn), f"create credit note failed: {cn}"
        cn_id = cn["credit_note_id"]
        assert _doc_dimensions(conn, "sales_invoice", cn_id) == Sv
        sub = call_action(mod.submit_sales_invoice, conn, ns(
            sales_invoice_id=cn_id))
        assert is_ok(sub), f"submit credit note failed: {sub}"
        assert _gl(conn, cn_id) == sorted([
            (env["ar"], "0.00", "60.00", Sv),
            (env["revenue"], "60.00", "0.00", Sv),
            (env["stock_acct"], "60.00", "0.00", Sv),
            (env["cogs"], "0.00", "60.00", Sv)])

    def test_different_object_refused(self, conn, env):
        inv_id, _ = self._tagged_submitted_invoice(conn, env)
        before = _counts(conn, env["company_id"])
        res = call_action(mod.create_credit_note, conn, ns(
            against_invoice_id=inv_id,
            items=json.dumps([{"item_id": env["item2"], "qty": "2"}]),
            posting_date=POSTING, reason=None,
            dimensions='{"department": "Ops"}',
            dimension_key=None, dimension_value=None))
        assert is_error(res)
        assert res["message"] == (
            "Credit note dimensions must match the original invoice's "
            f"({Sv})")
        assert _counts(conn, env["company_id"]) == before

    def test_update_credit_note_refused(self, conn, env):
        inv_id, _ = self._tagged_submitted_invoice(conn, env)
        cn = call_action(mod.create_credit_note, conn, ns(
            against_invoice_id=inv_id,
            items=json.dumps([{"item_id": env["item2"], "qty": "2"}]),
            posting_date=POSTING, reason=None,
            dimensions=None, dimension_key=None, dimension_value=None))
        assert is_ok(cn), f"create credit note failed: {cn}"
        cn_id = cn["credit_note_id"]
        before = _counts(conn, env["company_id"])
        res = call_action(mod.update_sales_invoice, conn, ns(
            sales_invoice_id=cn_id, due_date=None, items=None,
            dimensions='{"department": "Ops"}',
            dimension_key=None, dimension_value=None))
        assert is_error(res)
        assert res["message"] == (
            "Credit note dimensions must match the original invoice's "
            f"({Sv})")
        assert _counts(conn, env["company_id"]) == before
        assert _doc_dimensions(conn, "sales_invoice", cn_id) == Sv

    def test_inactive_key_still_copies(self, conn, env):
        _register_fund(conn)
        both = '{"department": "Sales", "fund": "F1"}'
        inv_id, _ = _submit_standalone_invoice(
            conn, env, _items(env, ("item1", "1", "25.00")),
            dims='{"department": "Sales", "fund": "F1"}')
        assert _doc_dimensions(conn, "sales_invoice", inv_id) == both
        _set_fund_inactive(conn)
        cn = call_action(mod.create_credit_note, conn, ns(
            against_invoice_id=inv_id,
            items=json.dumps([{"item_id": env["item1"], "qty": "1"}]),
            posting_date=POSTING, reason=None,
            dimensions=None, dimension_key=None, dimension_value=None))
        assert is_ok(cn), f"create credit note failed: {cn}"
        assert _doc_dimensions(
            conn, "sales_invoice", cn["credit_note_id"]) == both


# ──────────────────────────────────────────────────────────────────────────────
# 6. Refusals, nothing written
# ──────────────────────────────────────────────────────────────────────────────

class TestRefusals:
    def test_unknown_key_on_quotation(self, conn, env):
        before = _counts(conn, env["company_id"])
        res = call_action(mod.add_quotation, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-03-01",
            items=_items(env, ("item1", "1", "25.00")),
            valid_till=None, tax_template_id=None,
            dimensions='{"nope": "X"}',
            dimension_key=None, dimension_value=None))
        assert is_error(res)
        assert res["message"] == (
            "Unknown or inactive dimension 'nope'; run list-dimensions")
        assert _counts(conn, env["company_id"]) == before

    def test_unpaired_lists_on_order(self, conn, env):
        before = _counts(conn, env["company_id"])
        res = call_action(mod.add_sales_order, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-03-01",
            items=_items(env, ("item1", "1", "25.00")),
            delivery_date=None, tax_template_id=None,
            dimensions=None,
            dimension_key=["department"],
            dimension_value=["Sales", "Extra"]))
        assert is_error(res)
        assert res["message"] == (
            "--dimension-key and --dimension-value must be given in pairs")
        assert _counts(conn, env["company_id"]) == before

    def test_conflicting_duplicate_on_invoice(self, conn, env):
        before = _counts(conn, env["company_id"])
        res = call_action(mod.create_sales_invoice, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date=POSTING,
            items=_items(env, ("item1", "1", "25.00")),
            sales_order_id=None, delivery_note_id=None,
            tax_template_id=None, due_date=None,
            dimensions='{"department": "Sales"}',
            dimension_key=["department"], dimension_value=["Ops"]))
        assert is_error(res)
        assert res["message"] == (
            "Dimension 'department' given twice with different values")
        assert _counts(conn, env["company_id"]) == before

    def test_inherited_inactive_key_refused(self, conn, env):
        _register_fund(conn)
        so = call_action(mod.add_sales_order, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-03-01",
            items=_items(env, ("item1", "1", "25.00")),
            delivery_date=None, tax_template_id=None,
            dimensions='{"fund": "F1"}',
            dimension_key=None, dimension_value=None))
        assert is_ok(so), f"add SO failed: {so}"
        sub = call_action(mod.submit_sales_order, conn, ns(
            sales_order_id=so["sales_order_id"]))
        assert is_ok(sub), f"submit SO failed: {sub}"
        _set_fund_inactive(conn)
        before = _counts(conn, env["company_id"])
        res = call_action(mod.create_sales_invoice, conn, ns(
            customer_id=None, company_id=None, posting_date=POSTING,
            items=None, sales_order_id=so["sales_order_id"],
            delivery_note_id=None, tax_template_id=None, due_date=None,
            dimensions=None, dimension_key=None, dimension_value=None))
        assert is_error(res)
        assert res["message"] == (
            "Unknown or inactive dimension 'fund'; run list-dimensions")
        assert _counts(conn, env["company_id"]) == before


# ──────────────────────────────────────────────────────────────────────────────
# 7. Update
# ──────────────────────────────────────────────────────────────────────────────

class TestUpdateInvoice:
    def test_replace_and_clear(self, conn, env):
        created = call_action(mod.create_sales_invoice, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date=POSTING,
            items=_items(env, ("item1", "1", "25.00")),
            sales_order_id=None, delivery_note_id=None,
            tax_template_id=None, due_date=None))
        assert is_ok(created)
        si_id = created["sales_invoice_id"]
        assert _doc_dimensions(conn, "sales_invoice", si_id) == "{}"

        upd = call_action(mod.update_sales_invoice, conn, ns(
            sales_invoice_id=si_id, due_date=None, items=None,
            dimensions='{"department": "Ops"}',
            dimension_key=None, dimension_value=None))
        assert is_ok(upd), f"update failed: {upd}"
        assert "dimensions_json" in upd["updated_fields"]
        assert _doc_dimensions(conn, "sales_invoice", si_id) == \
            '{"department": "Ops"}'

        t = Table("audit_log")
        aq = (Q.from_(t).select(Field("old_values"), Field("new_values"))
              .where(Field("entity_type") == P())
              .where(Field("entity_id") == P())
              .where(Field("action") == P())
              .orderby(Field("timestamp"), order=Order.desc))
        rows = conn.execute(
            aq.get_sql(), ("sales_invoice", si_id,
                           "update-sales-invoice")).fetchall()
        assert rows, "no audit row for update-sales-invoice"
        old = json.loads(rows[0]["old_values"] or "{}")
        new = json.loads(rows[0]["new_values"] or "{}")
        assert old["dimensions_json"] == "{}"
        assert new["dimensions_json"] == '{"department": "Ops"}'

        clear = call_action(mod.update_sales_invoice, conn, ns(
            sales_invoice_id=si_id, due_date=None, items=None,
            dimensions="{}",
            dimension_key=None, dimension_value=None))
        assert is_ok(clear), f"clear failed: {clear}"
        assert _doc_dimensions(conn, "sales_invoice", si_id) == "{}"


# ──────────────────────────────────────────────────────────────────────────────
# 8. Cancel nets to zero
# ──────────────────────────────────────────────────────────────────────────────

class TestCancelNetsToZero:
    def test_cancel_nets_to_zero(self, conn, env):
        inv_id, _ = _submit_standalone_invoice(
            conn, env, _items(env, ("item2", "2", "30.00")),
            dims=None, dkeys=["department"], dvals=["Sales"])
        assert _gl(conn, inv_id) == sorted([
            (env["ar"], "60.00", "0.00", Sv),
            (env["revenue"], "0.00", "60.00", Sv),
            (env["cogs"], "40.00", "0.00", Sv),
            (env["stock_acct"], "0.00", "40.00", Sv)])
        res = call_action(mod.cancel_sales_invoice, conn, ns(
            sales_invoice_id=inv_id))
        assert is_ok(res), f"cancel failed: {res}"
        nets = {}
        for account_id, debit, credit, dims in _gl_all(conn, inv_id):
            nets[(account_id, dims)] = (
                nets.get((account_id, dims), Decimal("0"))
                + Decimal(debit) - Decimal(credit))
        assert nets, "no gl rows for voucher"
        for key, net in nets.items():
            assert net == Decimal("0.00"), f"{key} nets to {net}"


# ──────────────────────────────────────────────────────────────────────────────
# 9. Untagged is unchanged
# ──────────────────────────────────────────────────────────────────────────────

class TestUntaggedUnchanged:
    def test_untagged_posts_plain(self, conn, env):
        inv_id, sub = _submit_standalone_invoice(
            conn, env, _items(env, ("item1", "1", "25.00")))
        assert sub["gl_entries_created"] == 4
        t = Table("gl_entry")
        q = (Q.from_(t).select(Field("dimensions_json"))
             .where(Field("voucher_id") == P())
             .where(Field("is_cancelled") == 0))
        rows = conn.execute(q.get_sql(), (inv_id,)).fetchall()
        assert rows, "no gl rows for voucher"
        assert all(r["dimensions_json"] == "{}" for r in rows)
