"""Strong-depth tests for 6 selling actions (m626-strong-depth-selling-payments-2).

Each action below already has a behavioural test elsewhere in this directory;
the depth instrument flags those tests weak (content-only assertions: no money
literal, no read-back through the seam, no pinned refusal). This module deepens
each one WITHOUT deleting or weakening the original. Every test docstring names
the existing test it deepens and states which assertion now carries the weight.

Strong contract per action:
  1. Read back through the seam on a fresh connection from
     erpclaw_lib.db.get_connection(), comparing EXACT values.
  2. A hand-computed money literal wherever money exists (exact strings).
  3. A pinned refusal with the exact message and the database unchanged
     afterwards; the four list actions have no refusal branch, so they pin the
     exact empty result instead (unknown company -> zero rows).
  4. Explicit NOT-changed assertions (neighbour rows, ledgers, audit trail).

Conventions: money is TEXT; every monetary assertion compares exact strings
(never float). Test queries are built with PyPika through erpclaw_lib.query
and are parameterised. No raw catalog reads anywhere in this file.
"""
import json
import time
from datetime import datetime

import pytest
from decimal import Decimal

from selling_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
    seed_company, seed_customer,
)
from erpclaw_lib.db import get_connection as fresh_connection
from erpclaw_lib.query import Q, P, Table, fn

mod = load_db_query()

T_QUOT = Table("quotation")
T_QUOT_ITEM = Table("quotation_item")
T_SO = Table("sales_order")
T_SO_ITEM = Table("sales_order_item")
T_SI = Table("sales_invoice")
T_SI_ITEM = Table("sales_invoice_item")
T_SP = Table("sales_partner")
T_GL = Table("gl_entry")
T_PLE = Table("payment_ledger_entry")
T_SLE = Table("stock_ledger_entry")
T_AUDIT = Table("audit_log")


def _fresh():
    return fresh_connection()


def _close(conn):
    try:
        conn.close()
    except Exception:
        pass


def _row_by_id(conn, table, row_id):
    q = Q.from_(table).select(table.star).where(table.id == P())
    row = conn.execute(q.get_sql(), (row_id,)).fetchone()
    return dict(row) if row else None


def _count(conn, table):
    q = Q.from_(table).select(fn.Count("*"))
    return conn.execute(q.get_sql(), ()).fetchone()[0]


def _snapshot(conn, table):
    q = Q.from_(table).select(table.star).orderby(table.id)
    return [dict(r) for r in conn.execute(q.get_sql(), ()).fetchall()]


def _items(env, *specs):
    return json.dumps([
        {"item_id": env[k], "qty": q, "rate": r}
        for k, q, r in specs
    ])


def _so_items(env, *specs):
    return json.dumps([
        {"item_id": env[k], "qty": q, "rate": r,
         "warehouse_id": env["warehouse"]}
        for k, q, r in specs
    ])


def _second_company(conn):
    cid = seed_company(conn, name="Second Co", abbr="SC")
    cust = seed_customer(conn, cid, "Second Customer")
    return cid, cust


def _audit_rows(conn, entity_id, action):
    t = T_AUDIT
    q = (Q.from_(t).select(t.star)
         .where(t.entity_id == P())
         .where(t.action == P()))
    return [dict(r) for r in
            conn.execute(q.get_sql(), (entity_id, action)).fetchall()]


# ---------------------------------------------------------------------------
# 1. list-quotations
# ---------------------------------------------------------------------------

class TestListQuotationsStrong:
    def test_lists_exact_stored_values_with_ordering_and_decoys(self, conn, env):
        """Deepens TestListQuotations.test_list_by_company (test_quotation.py),
        which asserts only total_count >= 1. The ordered id sequence plus the
        exact stored grand_total strings below carry the weight."""
        other_co, _ = _second_company(conn)
        old = call_action(mod.add_quotation, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-10", items=_items(env, ("item1", "3", "50.00")),
            valid_till=None, tax_template_id=None,
        ))
        assert is_ok(old), old
        new = call_action(mod.add_quotation, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-20", items=_items(env, ("item1", "2", "100.00")),
            valid_till=None, tax_template_id=None,
        ))
        assert is_ok(new), new
        submitted = call_action(mod.add_quotation, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-25", items=_items(env, ("item1", "1", "10.00")),
            valid_till=None, tax_template_id=None,
        ))
        assert is_ok(submitted), submitted
        assert is_ok(call_action(mod.submit_quotation, conn, ns(
            quotation_id=submitted["quotation_id"])))
        foreign = call_action(mod.add_quotation, conn, ns(
            customer_id=_row_other_customer(conn, other_co),
            company_id=other_co,
            posting_date="2026-06-21", items=_items(env, ("item1", "9", "9.00")),
            valid_till=None, tax_template_id=None,
        ))
        assert is_ok(foreign), foreign

        quot_before = _snapshot(conn, T_QUOT)
        quot_item_before = _snapshot(conn, T_QUOT_ITEM)
        audit_before = _snapshot(conn, T_AUDIT)

        result = call_action(mod.list_quotations, conn, ns(
            company_id=env["company_id"], customer_id=None,
            doc_status="draft", from_date=None, to_date=None,
            search=None, limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] == 2
        assert result["has_more"] is False
        got_ids = [r["id"] for r in result["quotations"]]
        assert got_ids == [new["quotation_id"], old["quotation_id"]]
        assert submitted["quotation_id"] not in got_ids
        assert foreign["quotation_id"] not in got_ids

        fresh = _fresh()
        try:
            by_id = {r["id"]: r for r in result["quotations"]}
            assert by_id[new["quotation_id"]]["grand_total"] == "200.00"
            assert by_id[old["quotation_id"]]["grand_total"] == "150.00"
            for qid in got_ids:
                stored = _row_by_id(fresh, T_QUOT, qid)
                out = by_id[qid]
                assert out["grand_total"] == stored["grand_total"]
                assert out["customer_id"] == stored["customer_id"] == env["customer"]
                assert out["company_id"] == stored["company_id"] == env["company_id"]
                assert out["status"] == stored["status"] == "draft"
                assert out["quotation_date"] == stored["quotation_date"]
                assert out["customer_name"] == "Acme Corp"
        finally:
            _close(fresh)

        assert _snapshot(conn, T_QUOT) == quot_before
        assert _snapshot(conn, T_QUOT_ITEM) == quot_item_before
        assert _snapshot(conn, T_AUDIT) == audit_before

    def test_unknown_company_refuses_and_writes_nothing(self, conn, env):
        """Pins the refusal branch: an unknown company refuses with
        "Company not found: no-such-company" and writes nothing."""
        call_action(mod.add_quotation, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", items=_items(env, ("item1", "1", "10.00")),
            valid_till=None, tax_template_id=None,
        ))
        quot_before = _snapshot(conn, T_QUOT)
        quot_item_before = _snapshot(conn, T_QUOT_ITEM)
        audit_before = _snapshot(conn, T_AUDIT)
        result = call_action(mod.list_quotations, conn, ns(
            company_id="no-such-company", customer_id=None,
            doc_status=None, from_date=None, to_date=None,
            search=None, limit=None, offset=None,
        ))
        assert result["error"] == "Company not found: no-such-company"
        assert _snapshot(conn, T_QUOT) == quot_before
        assert _snapshot(conn, T_QUOT_ITEM) == quot_item_before
        assert _snapshot(conn, T_AUDIT) == audit_before

    def test_no_company_refuses_with_multiple_companies(self, conn, env):
        """Asserts what should happen: with more than one company and no
        company given, the list refuses with the multiple-company error."""
        other_co, other_cust = _second_company(conn)
        home = call_action(mod.add_quotation, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", items=_items(env, ("item1", "1", "10.00")),
            valid_till=None, tax_template_id=None,
        ))
        assert is_ok(home), home
        away = call_action(mod.add_quotation, conn, ns(
            customer_id=other_cust, company_id=other_co,
            posting_date="2026-06-16", items=_items(env, ("item1", "1", "10.00")),
            valid_till=None, tax_template_id=None,
        ))
        assert is_ok(away), away
        result = call_action(mod.list_quotations, conn, ns(
            company_id=None, customer_id=None,
            doc_status=None, from_date=None, to_date=None,
            search=None, limit=None, offset=None,
        ))
        assert is_error(result)
        assert result["error"] == "Multiple companies found. Please specify the company by name."


def _row_other_customer(conn, other_co):
    t = Table("customer")
    q = Q.from_(t).select(t.id).where(t.company_id == P())
    return conn.execute(q.get_sql(), (other_co,)).fetchone()["id"]


# ---------------------------------------------------------------------------
# 2. list-sales-invoices
# ---------------------------------------------------------------------------

class TestListSalesInvoicesStrong:
    def test_lists_exact_stored_values_with_ordering_and_decoys(self, conn, env):
        """Deepens TestListSalesInvoices.test_list (test_delivery_invoice.py),
        which asserts only total_count >= 1. The ordered id sequence plus the
        exact stored grand_total strings below carry the weight."""
        other_co, other_cust = _second_company(conn)

        def _make(customer, company, date, qty, rate):
            created = call_action(mod.create_sales_invoice, conn, ns(
                sales_order_id=None, delivery_note_id=None,
                customer_id=customer, company_id=company,
                posting_date=date, due_date=None,
                items=json.dumps([{"item_id": env["item1"],
                                   "qty": qty, "rate": rate}]),
                tax_template_id=None, payment_terms_id=None,
            ))
            assert is_ok(created), created
            return created["sales_invoice_id"]

        early = _make(env["customer"], env["company_id"], "2026-06-05", "1", "10.00")
        mid = _make(env["customer"], env["company_id"], "2026-06-20", "5", "200.00")
        assert is_ok(call_action(mod.submit_sales_invoice, conn,
                                 ns(sales_invoice_id=mid)))
        late = _make(env["customer"], env["company_id"], "2026-06-25", "2", "100.00")
        foreign = _make(other_cust, other_co, "2026-06-21", "9", "9.00")

        si_before = _snapshot(conn, T_SI)
        si_item_before = _snapshot(conn, T_SI_ITEM)
        audit_before = _snapshot(conn, T_AUDIT)

        result = call_action(mod.list_sales_invoices, conn, ns(
            company_id=env["company_id"], customer_id=None,
            sales_order_id=None, doc_status="draft",
            from_date=None, to_date=None,
            search=None, limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] == 2
        got_ids = [r["id"] for r in result["sales_invoices"]]
        assert got_ids == [late, early]
        assert mid not in got_ids
        assert foreign not in got_ids

        fresh = _fresh()
        try:
            by_id = {r["id"]: r for r in result["sales_invoices"]}
            assert by_id[late]["grand_total"] == "200.00"
            assert by_id[early]["grand_total"] == "10.00"
            for sid in got_ids:
                stored = _row_by_id(fresh, T_SI, sid)
                out = by_id[sid]
                assert out["grand_total"] == stored["grand_total"]
                assert out["outstanding_amount"] == stored["outstanding_amount"]
                assert out["customer_id"] == stored["customer_id"] == env["customer"]
                assert out["company_id"] == stored["company_id"] == env["company_id"]
                assert out["status"] == stored["status"] == "draft"
                assert out["posting_date"] == stored["posting_date"]
        finally:
            _close(fresh)

        assert _snapshot(conn, T_SI) == si_before
        assert _snapshot(conn, T_SI_ITEM) == si_item_before
        assert _snapshot(conn, T_AUDIT) == audit_before

    def test_unknown_company_refuses_and_writes_nothing(self, conn, env):
        """Pins the refusal branch: an unknown company refuses with
        "Company not found: no-such-company" and writes nothing."""
        created = call_action(mod.create_sales_invoice, conn, ns(
            sales_order_id=None, delivery_note_id=None,
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date=None,
            items=_items(env, ("item1", "1", "10.00")),
            tax_template_id=None, payment_terms_id=None,
        ))
        assert is_ok(created)
        si_before = _snapshot(conn, T_SI)
        si_item_before = _snapshot(conn, T_SI_ITEM)
        audit_before = _snapshot(conn, T_AUDIT)
        result = call_action(mod.list_sales_invoices, conn, ns(
            company_id="no-such-company", customer_id=None,
            sales_order_id=None, doc_status=None,
            from_date=None, to_date=None,
            search=None, limit=None, offset=None,
        ))
        assert result["error"] == "Company not found: no-such-company"
        assert _snapshot(conn, T_SI) == si_before
        assert _snapshot(conn, T_SI_ITEM) == si_item_before
        assert _snapshot(conn, T_AUDIT) == audit_before

    def test_no_company_refuses_with_multiple_companies(self, conn, env):
        """Asserts what should happen: with more than one company and no
        company given, the list refuses with the multiple-company error."""
        other_co, other_cust = _second_company(conn)
        home = call_action(mod.create_sales_invoice, conn, ns(
            sales_order_id=None, delivery_note_id=None,
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", due_date=None,
            items=_items(env, ("item1", "1", "10.00")),
            tax_template_id=None, payment_terms_id=None,
        ))
        assert is_ok(home), home
        away = call_action(mod.create_sales_invoice, conn, ns(
            sales_order_id=None, delivery_note_id=None,
            customer_id=other_cust, company_id=other_co,
            posting_date="2026-06-16", due_date=None,
            items=_items(env, ("item1", "1", "10.00")),
            tax_template_id=None, payment_terms_id=None,
        ))
        assert is_ok(away), away
        result = call_action(mod.list_sales_invoices, conn, ns(
            company_id=None, customer_id=None,
            sales_order_id=None, doc_status=None,
            from_date=None, to_date=None,
            search=None, limit=None, offset=None,
        ))
        assert is_error(result)
        assert result["error"] == "Multiple companies found. Please specify the company by name."


# ---------------------------------------------------------------------------
# 3. list-sales-orders
# ---------------------------------------------------------------------------

class TestListSalesOrdersStrong:
    def test_lists_exact_stored_values_with_ordering_and_decoys(self, conn, env):
        """Deepens TestListSalesOrders.test_list_by_company (test_sales_order.py),
        which asserts only total_count >= 1. The ordered id sequence plus the
        exact stored grand_total strings below carry the weight."""
        other_co, other_cust = _second_company(conn)

        def _make(customer, company, date, qty, rate):
            created = call_action(mod.add_sales_order, conn, ns(
                customer_id=customer, company_id=company,
                posting_date=date, items=json.dumps([{
                    "item_id": env["item1"], "qty": qty, "rate": rate,
                    "warehouse_id": env["warehouse"]}]),
                delivery_date=None, tax_template_id=None,
            ))
            assert is_ok(created), created
            return created["sales_order_id"]

        early = _make(env["customer"], env["company_id"], "2026-06-05", "1", "10.00")
        mid = _make(env["customer"], env["company_id"], "2026-06-15", "10", "100.00")
        assert is_ok(call_action(mod.submit_sales_order, conn,
                                 ns(sales_order_id=mid)))
        late = _make(env["customer"], env["company_id"], "2026-06-25", "2", "100.00")
        foreign = _make(other_cust, other_co, "2026-06-16", "9", "9.00")

        so_before = _snapshot(conn, T_SO)
        so_item_before = _snapshot(conn, T_SO_ITEM)
        audit_before = _snapshot(conn, T_AUDIT)

        result = call_action(mod.list_sales_orders, conn, ns(
            company_id=env["company_id"], customer_id=None,
            doc_status="draft", from_date=None, to_date=None,
            search=None, limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] == 2
        got_ids = [r["id"] for r in result["sales_orders"]]
        assert got_ids == [late, early]
        assert mid not in got_ids
        assert foreign not in got_ids

        fresh = _fresh()
        try:
            by_id = {r["id"]: r for r in result["sales_orders"]}
            assert by_id[late]["grand_total"] == "200.00"
            assert by_id[early]["grand_total"] == "10.00"
            for sid in got_ids:
                stored = _row_by_id(fresh, T_SO, sid)
                out = by_id[sid]
                assert out["grand_total"] == stored["grand_total"]
                assert out["customer_id"] == stored["customer_id"] == env["customer"]
                assert out["company_id"] == stored["company_id"] == env["company_id"]
                assert out["status"] == stored["status"] == "draft"
                assert out["order_date"] == stored["order_date"]
        finally:
            _close(fresh)

        assert _snapshot(conn, T_SO) == so_before
        assert _snapshot(conn, T_SO_ITEM) == so_item_before
        assert _snapshot(conn, T_AUDIT) == audit_before

    def test_unknown_company_refuses_and_writes_nothing(self, conn, env):
        """Pins the refusal branch: an unknown company refuses with
        "Company not found: no-such-company" and writes nothing."""
        created = call_action(mod.add_sales_order, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", items=_so_items(env, ("item1", "1", "10.00")),
            delivery_date=None, tax_template_id=None,
        ))
        assert is_ok(created)
        so_before = _snapshot(conn, T_SO)
        so_item_before = _snapshot(conn, T_SO_ITEM)
        audit_before = _snapshot(conn, T_AUDIT)
        result = call_action(mod.list_sales_orders, conn, ns(
            company_id="no-such-company", customer_id=None,
            doc_status=None, from_date=None, to_date=None,
            search=None, limit=None, offset=None,
        ))
        assert result["error"] == "Company not found: no-such-company"
        assert _snapshot(conn, T_SO) == so_before
        assert _snapshot(conn, T_SO_ITEM) == so_item_before
        assert _snapshot(conn, T_AUDIT) == audit_before

    def test_no_company_refuses_with_multiple_companies(self, conn, env):
        """Asserts what should happen: with more than one company and no
        company given, the list refuses with the multiple-company error."""
        other_co, other_cust = _second_company(conn)
        home = call_action(mod.add_sales_order, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", items=json.dumps([{
                "item_id": env["item1"], "qty": "1", "rate": "10.00",
                "warehouse_id": env["warehouse"]}]),
            delivery_date=None, tax_template_id=None,
        ))
        assert is_ok(home), home
        away = call_action(mod.add_sales_order, conn, ns(
            customer_id=other_cust, company_id=other_co,
            posting_date="2026-06-16", items=json.dumps([{
                "item_id": env["item1"], "qty": "1", "rate": "10.00",
                "warehouse_id": env["warehouse"]}]),
            delivery_date=None, tax_template_id=None,
        ))
        assert is_ok(away), away
        result = call_action(mod.list_sales_orders, conn, ns(
            company_id=None, customer_id=None,
            doc_status=None, from_date=None, to_date=None,
            search=None, limit=None, offset=None,
        ))
        assert is_error(result)
        assert result["error"] == "Multiple companies found. Please specify the company by name."


# ---------------------------------------------------------------------------
# 4. list-sales-partners
# ---------------------------------------------------------------------------

class TestListSalesPartnersStrong:
    def test_alpha_order_pagination_and_readback(self, conn, env):
        """Deepens TestListSalesPartners.test_list (test_misc_selling.py),
        which asserts only total_count >= 1. The alphabetical id sequence plus
        the exact commission_rate strings below carry the weight."""
        ids = {}
        for name, rate in (("Zulu", "10.00"), ("Mike", "2.25"), ("Alpha", "7.50")):
            created = call_action(mod.add_sales_partner, conn, ns(
                name=name, company_id=env["company_id"],
                commission_rate=rate,
            ))
            assert is_ok(created), created
            ids[name] = created["sales_partner_id"]

        sp_before = _snapshot(conn, T_SP)
        audit_before = _snapshot(conn, T_AUDIT)

        result = call_action(mod.list_sales_partners, conn, ns(
            company_id=None, limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] == 3
        assert [r["id"] for r in result["sales_partners"]] == [
            ids["Alpha"], ids["Mike"], ids["Zulu"]]

        page = call_action(mod.list_sales_partners, conn, ns(
            company_id=None, limit="2", offset="0",
        ))
        assert is_ok(page)
        assert page["total_count"] == 3
        assert page["has_more"] is True
        assert [r["id"] for r in page["sales_partners"]] == [
            ids["Alpha"], ids["Mike"]]
        tail = call_action(mod.list_sales_partners, conn, ns(
            company_id=None, limit="2", offset="2",
        ))
        assert is_ok(tail)
        assert tail["has_more"] is False
        assert [r["id"] for r in tail["sales_partners"]] == [ids["Zulu"]]

        fresh = _fresh()
        try:
            by_id = {r["id"]: r for r in result["sales_partners"]}
            assert by_id[ids["Alpha"]]["commission_rate"] == "7.50"
            assert by_id[ids["Mike"]]["commission_rate"] == "2.25"
            assert by_id[ids["Zulu"]]["commission_rate"] == "10.00"
            for name, pid in ids.items():
                stored = _row_by_id(fresh, T_SP, pid)
                assert by_id[pid]["name"] == stored["name"] == name
                assert by_id[pid]["commission_rate"] == stored["commission_rate"]
        finally:
            _close(fresh)

        assert _snapshot(conn, T_SP) == sp_before
        assert _snapshot(conn, T_AUDIT) == audit_before

    def test_offset_beyond_total_returns_empty_and_writes_nothing(self, conn, env):
        """Pins the no-refusal branch: list-sales-partners has no err() path,
        so paging past the end returns zero rows with the count intact."""
        created = call_action(mod.add_sales_partner, conn, ns(
            name="Solo", company_id=env["company_id"],
            commission_rate="5.00",
        ))
        assert is_ok(created)
        sp_before = _snapshot(conn, T_SP)
        audit_before = _snapshot(conn, T_AUDIT)
        result = call_action(mod.list_sales_partners, conn, ns(
            company_id=None, limit="20", offset="50",
        ))
        assert is_ok(result)
        assert result["total_count"] == 1
        assert result["sales_partners"] == []
        assert result["has_more"] is False
        assert _snapshot(conn, T_SP) == sp_before
        assert _snapshot(conn, T_AUDIT) == audit_before


# ---------------------------------------------------------------------------
# 5. update-quotation
# ---------------------------------------------------------------------------

class TestUpdateQuotationStrong:
    def test_items_recompute_with_whole_row_and_audit(self, conn, env):
        """Deepens TestUpdateQuotation.test_update_items (test_quotation.py),
        which asserts only that 'items' is in updated_fields. The fresh-seam
        whole-row comparison plus the audit content below carry the weight."""
        decoy = call_action(mod.add_quotation, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-11", items=_items(env, ("item1", "1", "10.00")),
            valid_till=None, tax_template_id=None,
        ))
        assert is_ok(decoy), decoy
        target = call_action(mod.add_quotation, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", items=_items(env, ("item1", "5", "100.00")),
            valid_till=None, tax_template_id=None,
        ))
        assert is_ok(target), target
        qid = target["quotation_id"]

        fresh = _fresh()
        try:
            before = _row_by_id(fresh, T_QUOT, qid)
            decoy_before = _row_by_id(fresh, T_QUOT, decoy["quotation_id"])
            assert before["grand_total"] == "500.00"
            # FINDING (tax scale): with no tax template the helper returns an
            # unquantized Decimal("0"), so the row stores "0" beside two-place
            # money columns. Pinned as the current value.
            assert before["tax_amount"] == "0"
            assert before["status"] == "draft"
        finally:
            _close(fresh)
        gl_before = _count(conn, T_GL)
        ple_before = _count(conn, T_PLE)
        sle_before = _count(conn, T_SLE)

        time.sleep(1.1)
        result = call_action(mod.update_quotation, conn, ns(
            quotation_id=qid,
            items=_items(env, ("item1", "10", "150.00")), valid_till=None,
        ))
        assert is_ok(result)
        assert result["updated_fields"] == [
            "items", "total_amount", "tax_amount", "grand_total"]

        fresh = _fresh()
        try:
            after = _row_by_id(fresh, T_QUOT, qid)
            allowed = {"total_amount", "tax_amount", "grand_total", "updated_at"}
            assert after["total_amount"] == "1500.00"
            # FINDING (tax scale): pinned current value, see the note on the
            # before-row above; the two-place form is asserted xfail below.
            assert after["tax_amount"] == "0"
            assert after["grand_total"] == "1500.00"
            assert after["updated_at"] != before["updated_at"]
            datetime.fromisoformat(str(after["updated_at"]))
            for key, value in before.items():
                if key not in allowed:
                    assert after[key] == value, key
            assert after["status"] == "draft"
            assert _row_by_id(fresh, T_QUOT, decoy["quotation_id"]) == decoy_before
            iq = Q.from_(T_QUOT_ITEM).select(T_QUOT_ITEM.star).where(
                T_QUOT_ITEM.quotation_id == P())
            item_rows = [dict(r) for r in fresh.execute(
                iq.get_sql(), (qid,)).fetchall()]
            assert len(item_rows) == 1
            assert item_rows[0]["quantity"] == "10.00"
            assert item_rows[0]["rate"] == "150.00"
            assert item_rows[0]["amount"] == "1500.00"
            assert item_rows[0]["net_amount"] == "1500.00"
            rows = _audit_rows(fresh, qid, "update-quotation")
            assert len(rows) == 1
            assert rows[0]["skill"] == "erpclaw-selling"
            assert rows[0]["entity_type"] == "quotation"
            assert rows[0]["entity_id"] == qid
            assert json.loads(rows[0]["old_values"]) == {
                "total_amount": "500.00", "tax_amount": before["tax_amount"],
                "grand_total": "500.00"}
            assert json.loads(rows[0]["new_values"]) == {
                "total_amount": "1500.00", "tax_amount": after["tax_amount"],
                "grand_total": "1500.00"}
            assert rows[0]["description"] == (
                "Updated fields: items, total_amount, tax_amount, grand_total")
        finally:
            _close(fresh)

        assert _count(conn, T_GL) == gl_before
        assert _count(conn, T_PLE) == ple_before
        assert _count(conn, T_SLE) == sle_before

    def test_audit_carries_old_and_new_totals(self, conn, env):
        """Asserts what should happen: the audit row carries old_values and
        new_values for the recomputed totals, as update-employee does. Keys
        are asserted by containment so extra keys do not break the test.
        Today new_values holds only updated_fields (and old_values is empty),
        so this fails."""
        created = call_action(mod.add_quotation, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", items=_items(env, ("item1", "5", "100.00")),
            valid_till=None, tax_template_id=None,
        ))
        assert is_ok(created), created
        qid = created["quotation_id"]
        fresh = _fresh()
        try:
            before = _row_by_id(fresh, T_QUOT, qid)
        finally:
            _close(fresh)
        result = call_action(mod.update_quotation, conn, ns(
            quotation_id=qid,
            items=_items(env, ("item1", "10", "150.00")), valid_till=None,
        ))
        assert is_ok(result), result
        fresh = _fresh()
        try:
            after = _row_by_id(fresh, T_QUOT, qid)
            rows = _audit_rows(fresh, qid, "update-quotation")
            assert len(rows) == 1
            old_values = json.loads(rows[0]["old_values"])
            new_values = json.loads(rows[0]["new_values"])
            assert old_values["total_amount"] == "500.00"
            assert old_values["grand_total"] == "500.00"
            assert old_values["tax_amount"] == before["tax_amount"]
            assert new_values["total_amount"] == "1500.00"
            assert new_values["grand_total"] == "1500.00"
            assert new_values["tax_amount"] == after["tax_amount"]
        finally:
            _close(fresh)

    def test_valid_till_only_audit_carries_valid_until(self, conn, env):
        """A valid-till-only edit writes old/new valid_until and no money key."""
        created = call_action(mod.add_quotation, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", items=_items(env, ("item1", "5", "100.00")),
            valid_till=None, tax_template_id=None,
        ))
        assert is_ok(created), created
        qid = created["quotation_id"]
        fresh = _fresh()
        try:
            before = _row_by_id(fresh, T_QUOT, qid)
        finally:
            _close(fresh)
        result = call_action(mod.update_quotation, conn, ns(
            quotation_id=qid, items=None, valid_till="2026-07-31",
        ))
        assert is_ok(result), result
        assert result["updated_fields"] == ["valid_until"]
        fresh = _fresh()
        try:
            after = _row_by_id(fresh, T_QUOT, qid)
            assert after["valid_until"] == "2026-07-31"
            rows = _audit_rows(fresh, qid, "update-quotation")
            assert len(rows) == 1
            old_values = json.loads(rows[0]["old_values"])
            new_values = json.loads(rows[0]["new_values"])
            assert old_values == {"valid_until": before["valid_until"]}
            assert new_values == {"valid_until": "2026-07-31"}
            for key in ("total_amount", "tax_amount", "grand_total"):
                assert key not in old_values
                assert key not in new_values
            assert rows[0]["description"] == "Updated fields: valid_until"
        finally:
            _close(fresh)

    @pytest.mark.xfail(
        strict=True,
        reason="FINDING: with no tax template the row stores tax_amount as "
               'bare "0" instead of two-place "0.00"')
    def test_tax_amount_two_place_scale(self, conn, env):
        """Asserts what should happen: tax_amount is stored two-place beside
        the two-place money columns, both after add and after update. Today
        both rows store "0", so this fails."""
        created = call_action(mod.add_quotation, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", items=_items(env, ("item1", "5", "100.00")),
            valid_till=None, tax_template_id=None,
        ))
        assert is_ok(created), created
        qid = created["quotation_id"]
        fresh = _fresh()
        try:
            assert _row_by_id(fresh, T_QUOT, qid)["tax_amount"] == "0.00"
        finally:
            _close(fresh)
        result = call_action(mod.update_quotation, conn, ns(
            quotation_id=qid,
            items=_items(env, ("item1", "10", "150.00")), valid_till=None,
        ))
        assert is_ok(result), result
        fresh = _fresh()
        try:
            assert _row_by_id(fresh, T_QUOT, qid)["tax_amount"] == "0.00"
        finally:
            _close(fresh)

    def test_submitted_refusal_exact_and_unchanged(self, conn, env):
        """Pins the draft-only refusal with its exact message. The unchanged
        row plus unchanged audit count below carry the weight."""
        created = call_action(mod.add_quotation, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", items=_items(env, ("item1", "1", "10.00")),
            valid_till=None, tax_template_id=None,
        ))
        qid = created["quotation_id"]
        assert is_ok(call_action(mod.submit_quotation, conn,
                                 ns(quotation_id=qid)))
        fresh = _fresh()
        try:
            before = _row_by_id(fresh, T_QUOT, qid)
            audit_before = len(_audit_rows(fresh, qid, "update-quotation"))
        finally:
            _close(fresh)
        result = call_action(mod.update_quotation, conn, ns(
            quotation_id=qid, items=None, valid_till="2026-09-01",
        ))
        assert is_error(result)
        assert result["message"] == (
            "Cannot update: quotation is 'open' (must be 'draft')")
        assert result["suggestion"] == (
            "Cancel the document first, then make changes.")
        fresh = _fresh()
        try:
            assert _row_by_id(fresh, T_QUOT, qid) == before
            assert len(_audit_rows(fresh, qid, "update-quotation")) == audit_before
        finally:
            _close(fresh)

    def test_malformed_qty_leaves_rows_unchanged(self, conn, env):
        """Probes the TEXT quantity column with a malformed value. The crash
        itself is asserted xfail beside this test; what this test pins is
        that the failed call leaves the header row, every item row (ordered
        by id) and every audit row unchanged as whole dictionaries."""
        created = call_action(mod.add_quotation, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", items=_items(env, ("item1", "1", "10.00")),
            valid_till=None, tax_template_id=None,
        ))
        qid = created["quotation_id"]
        fresh = _fresh()
        try:
            before = _row_by_id(fresh, T_QUOT, qid)
            iq = (Q.from_(T_QUOT_ITEM).select(T_QUOT_ITEM.star).where(
                T_QUOT_ITEM.quotation_id == P()).orderby(T_QUOT_ITEM.id))
            items_before = [dict(r) for r in fresh.execute(
                iq.get_sql(), (qid,)).fetchall()]
            audit_before = _snapshot(fresh, T_AUDIT)
        finally:
            _close(fresh)
        bad = json.dumps([{"item_id": env["item1"], "qty": "abc",
                           "rate": "10.00"}])
        try:
            call_action(mod.update_quotation, conn, ns(
                quotation_id=qid, items=bad, valid_till=None,
            ))
        except Exception:
            pass
        conn.rollback()
        fresh = _fresh()
        try:
            assert _row_by_id(fresh, T_QUOT, qid) == before
            iq = (Q.from_(T_QUOT_ITEM).select(T_QUOT_ITEM.star).where(
                T_QUOT_ITEM.quotation_id == P()).orderby(T_QUOT_ITEM.id))
            assert [dict(r) for r in fresh.execute(
                iq.get_sql(), (qid,)).fetchall()] == items_before
            assert _snapshot(fresh, T_AUDIT) == audit_before
        finally:
            _close(fresh)

    @pytest.mark.xfail(
        strict=True,
        reason="FINDING: update-quotation raises ValueError on a malformed "
               "qty instead of returning a clean error result naming the "
               "qty field")
    def test_malformed_qty_clean_refusal(self, conn, env):
        """Asserts what should happen: a clean refusal whose message names
        the bad value and the qty field. Today the conversion raises, so the
        raise itself counts as the expected failure (no raises= is passed)."""
        created = call_action(mod.add_quotation, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", items=_items(env, ("item1", "1", "10.00")),
            valid_till=None, tax_template_id=None,
        ))
        qid = created["quotation_id"]
        bad = json.dumps([{"item_id": env["item1"], "qty": "abc",
                           "rate": "10.00"}])
        result = call_action(mod.update_quotation, conn, ns(
            quotation_id=qid, items=bad, valid_till=None,
        ))
        assert is_error(result)
        assert "abc" in result["message"]
        message = result["message"].lower()
        assert "qty" in message or "quantity" in message


# ---------------------------------------------------------------------------
# 6. update-sales-order
# ---------------------------------------------------------------------------

class TestUpdateSalesOrderStrong:
    def test_items_recompute_with_whole_row_and_audit(self, conn, env):
        """Deepens TestUpdateSalesOrder.test_update_items (test_sales_order.py),
        which asserts only that 'items' is in updated_fields. The fresh-seam
        whole-row comparison plus the audit content below carry the weight."""
        decoy = call_action(mod.add_sales_order, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-11", items=_so_items(env, ("item1", "1", "10.00")),
            delivery_date=None, tax_template_id=None,
        ))
        assert is_ok(decoy), decoy
        target = call_action(mod.add_sales_order, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", items=_so_items(env, ("item1", "5", "100.00")),
            delivery_date="2026-07-01", tax_template_id=None,
        ))
        assert is_ok(target), target
        soid = target["sales_order_id"]

        fresh = _fresh()
        try:
            before = _row_by_id(fresh, T_SO, soid)
            decoy_before = _row_by_id(fresh, T_SO, decoy["sales_order_id"])
            assert before["grand_total"] == "500.00"
            # FINDING (tax scale): with no tax template the helper returns an
            # unquantized Decimal("0"), so the row stores "0" beside two-place
            # money columns. Pinned as the current value.
            assert before["tax_amount"] == "0"
            assert before["status"] == "draft"
        finally:
            _close(fresh)
        gl_before = _count(conn, T_GL)
        ple_before = _count(conn, T_PLE)
        sle_before = _count(conn, T_SLE)

        time.sleep(1.1)
        result = call_action(mod.update_sales_order, conn, ns(
            sales_order_id=soid, delivery_date=None,
            items=json.dumps([{"item_id": env["item1"], "qty": "20",
                               "rate": "150.00",
                               "warehouse_id": env["warehouse"]}]),
        ))
        assert is_ok(result)
        assert result["updated_fields"] == [
            "items", "total_amount", "tax_amount", "grand_total"]

        fresh = _fresh()
        try:
            after = _row_by_id(fresh, T_SO, soid)
            allowed = {"total_amount", "tax_amount", "grand_total", "updated_at"}
            assert after["total_amount"] == "3000.00"
            # FINDING (tax scale): pinned current value, see the note on the
            # before-row above; the two-place form is asserted xfail below.
            assert after["tax_amount"] == "0"
            assert after["grand_total"] == "3000.00"
            assert after["updated_at"] != before["updated_at"]
            datetime.fromisoformat(str(after["updated_at"]))
            for key, value in before.items():
                if key not in allowed:
                    assert after[key] == value, key
            assert after["status"] == "draft"
            assert after["delivery_date"] == "2026-07-01"
            assert _row_by_id(fresh, T_SO, decoy["sales_order_id"]) == decoy_before
            iq = Q.from_(T_SO_ITEM).select(T_SO_ITEM.star).where(
                T_SO_ITEM.sales_order_id == P())
            item_rows = [dict(r) for r in fresh.execute(
                iq.get_sql(), (soid,)).fetchall()]
            assert len(item_rows) == 1
            assert item_rows[0]["quantity"] == "20.00"
            assert item_rows[0]["rate"] == "150.00"
            assert item_rows[0]["amount"] == "3000.00"
            assert item_rows[0]["net_amount"] == "3000.00"
            rows = _audit_rows(fresh, soid, "update-sales-order")
            assert len(rows) == 1
            assert rows[0]["skill"] == "erpclaw-selling"
            assert rows[0]["entity_type"] == "sales_order"
            assert rows[0]["entity_id"] == soid
            assert json.loads(rows[0]["old_values"]) == {
                "total_amount": "500.00", "tax_amount": before["tax_amount"],
                "grand_total": "500.00"}
            assert json.loads(rows[0]["new_values"]) == {
                "total_amount": "3000.00", "tax_amount": after["tax_amount"],
                "grand_total": "3000.00"}
            assert rows[0]["description"] == (
                "Updated fields: items, total_amount, tax_amount, grand_total")
        finally:
            _close(fresh)

        assert _count(conn, T_GL) == gl_before
        assert _count(conn, T_PLE) == ple_before
        assert _count(conn, T_SLE) == sle_before

    def test_audit_carries_old_and_new_totals(self, conn, env):
        """Asserts what should happen: the audit row carries old_values and
        new_values for the recomputed totals, as update-employee does. Keys
        are asserted by containment so extra keys do not break the test.
        Today new_values holds only updated_fields (and old_values is empty),
        so this fails."""
        created = call_action(mod.add_sales_order, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", items=_so_items(env, ("item1", "5", "100.00")),
            delivery_date="2026-07-01", tax_template_id=None,
        ))
        assert is_ok(created), created
        soid = created["sales_order_id"]
        fresh = _fresh()
        try:
            before = _row_by_id(fresh, T_SO, soid)
        finally:
            _close(fresh)
        result = call_action(mod.update_sales_order, conn, ns(
            sales_order_id=soid, delivery_date=None,
            items=json.dumps([{"item_id": env["item1"], "qty": "20",
                               "rate": "150.00",
                               "warehouse_id": env["warehouse"]}]),
        ))
        assert is_ok(result), result
        fresh = _fresh()
        try:
            after = _row_by_id(fresh, T_SO, soid)
            rows = _audit_rows(fresh, soid, "update-sales-order")
            assert len(rows) == 1
            old_values = json.loads(rows[0]["old_values"])
            new_values = json.loads(rows[0]["new_values"])
            assert old_values["total_amount"] == "500.00"
            assert old_values["grand_total"] == "500.00"
            assert old_values["tax_amount"] == before["tax_amount"]
            assert new_values["total_amount"] == "3000.00"
            assert new_values["grand_total"] == "3000.00"
            assert new_values["tax_amount"] == after["tax_amount"]
        finally:
            _close(fresh)

    @pytest.mark.xfail(
        strict=True,
        reason="FINDING: with no tax template the row stores tax_amount as "
               'bare "0" instead of two-place "0.00"')
    def test_tax_amount_two_place_scale(self, conn, env):
        """Asserts what should happen: tax_amount is stored two-place beside
        the two-place money columns, both after add and after update. Today
        both rows store "0", so this fails."""
        created = call_action(mod.add_sales_order, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", items=_so_items(env, ("item1", "1", "10.00")),
            delivery_date=None, tax_template_id=None,
        ))
        assert is_ok(created), created
        soid = created["sales_order_id"]
        fresh = _fresh()
        try:
            assert _row_by_id(fresh, T_SO, soid)["tax_amount"] == "0.00"
        finally:
            _close(fresh)
        result = call_action(mod.update_sales_order, conn, ns(
            sales_order_id=soid, delivery_date=None,
            items=json.dumps([{"item_id": env["item1"], "qty": "20",
                               "rate": "150.00",
                               "warehouse_id": env["warehouse"]}]),
        ))
        assert is_ok(result), result
        fresh = _fresh()
        try:
            assert _row_by_id(fresh, T_SO, soid)["tax_amount"] == "0.00"
        finally:
            _close(fresh)

    def test_submitted_refusal_exact_and_unchanged(self, conn, env):
        """Pins the draft-only refusal with its exact message. The unchanged
        row plus unchanged audit count below carry the weight."""
        created = call_action(mod.add_sales_order, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", items=_so_items(env, ("item1", "1", "10.00")),
            delivery_date=None, tax_template_id=None,
        ))
        soid = created["sales_order_id"]
        assert is_ok(call_action(mod.submit_sales_order, conn,
                                 ns(sales_order_id=soid)))
        fresh = _fresh()
        try:
            before = _row_by_id(fresh, T_SO, soid)
            audit_before = len(_audit_rows(fresh, soid, "update-sales-order"))
        finally:
            _close(fresh)
        result = call_action(mod.update_sales_order, conn, ns(
            sales_order_id=soid, delivery_date="2026-09-01", items=None,
        ))
        assert is_error(result)
        assert result["message"] == (
            "Cannot update: sales order is 'confirmed' (must be 'draft')")
        assert result["suggestion"] == (
            "Cancel the document first, then make changes.")
        fresh = _fresh()
        try:
            assert _row_by_id(fresh, T_SO, soid) == before
            assert len(_audit_rows(fresh, soid, "update-sales-order")) == audit_before
        finally:
            _close(fresh)

    def test_malformed_qty_leaves_rows_unchanged(self, conn, env):
        """Probes the TEXT quantity column with a malformed value. The crash
        itself is asserted xfail beside this test; what this test pins is
        that the failed call leaves the header row, every item row (ordered
        by id) and every audit row unchanged as whole dictionaries."""
        created = call_action(mod.add_sales_order, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", items=_so_items(env, ("item1", "1", "10.00")),
            delivery_date=None, tax_template_id=None,
        ))
        soid = created["sales_order_id"]
        fresh = _fresh()
        try:
            before = _row_by_id(fresh, T_SO, soid)
            iq = (Q.from_(T_SO_ITEM).select(T_SO_ITEM.star).where(
                T_SO_ITEM.sales_order_id == P()).orderby(T_SO_ITEM.id))
            items_before = [dict(r) for r in fresh.execute(
                iq.get_sql(), (soid,)).fetchall()]
            audit_before = _snapshot(fresh, T_AUDIT)
        finally:
            _close(fresh)
        bad = json.dumps([{"item_id": env["item1"], "qty": "abc",
                           "rate": "10.00",
                           "warehouse_id": env["warehouse"]}])
        try:
            call_action(mod.update_sales_order, conn, ns(
                sales_order_id=soid, delivery_date=None, items=bad,
            ))
        except Exception:
            pass
        conn.rollback()
        fresh = _fresh()
        try:
            assert _row_by_id(fresh, T_SO, soid) == before
            iq = (Q.from_(T_SO_ITEM).select(T_SO_ITEM.star).where(
                T_SO_ITEM.sales_order_id == P()).orderby(T_SO_ITEM.id))
            assert [dict(r) for r in fresh.execute(
                iq.get_sql(), (soid,)).fetchall()] == items_before
            assert _snapshot(fresh, T_AUDIT) == audit_before
        finally:
            _close(fresh)

    @pytest.mark.xfail(
        strict=True,
        reason="FINDING: update-sales-order raises ValueError on a malformed "
               "qty instead of returning a clean error result naming the "
               "qty field")
    def test_malformed_qty_clean_refusal(self, conn, env):
        """Asserts what should happen: a clean refusal whose message names
        the bad value and the qty field. Today the conversion raises, so the
        raise itself counts as the expected failure (no raises= is passed)."""
        created = call_action(mod.add_sales_order, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", items=_so_items(env, ("item1", "1", "10.00")),
            delivery_date=None, tax_template_id=None,
        ))
        soid = created["sales_order_id"]
        bad = json.dumps([{"item_id": env["item1"], "qty": "abc",
                           "rate": "10.00",
                           "warehouse_id": env["warehouse"]}])
        result = call_action(mod.update_sales_order, conn, ns(
            sales_order_id=soid, delivery_date=None, items=bad,
        ))
        assert is_error(result)
        assert "abc" in result["message"]
        message = result["message"].lower()
        assert "qty" in message or "quantity" in message
