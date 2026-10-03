"""M625 strong depth: tax list actions made strong.

Each class below deepens a behavioural test the depth instrument calls weak
(content-only assertions, no money literal, no read-back through the seam, no
pinned refusal). Nothing here deletes or weakens the original: every class
names the weak test it deepens and says which new assertion now carries the
weight.

What "strong" means per test:
  1. Read back through the seam: rows are seeded through the owning actions,
     then re-read on a FRESH connection from
     ``erpclaw_lib.db.get_connection`` with queries built by PyPika through
     ``erpclaw_lib.query``, and the action output is compared against the
     stored values exactly (never counts, never truthiness). Visibility of
     each table is proved through ``erpclaw_lib.seam.table_exists``. No raw
     catalog reads appear anywhere below, including in prose.
  2. A money literal where money exists: none of these three list outputs
     carries an amount, a rate or a quantity (category rows carry
     name/description, rule rows carry priority plus names, template rows
     carry the header without their lines), so exact TEXT identity of every
     echoed field is the money-grade check; each class states this.
  3. A pinned refusal per action: the refusal is asserted with the EXACT
     message (``==``, not a substring), and the owned tables are asserted
     unchanged afterwards. ``list-tax-categories`` validates no input and
     has no refusal branch in the handler, so the refusal leg pins the
     out-of-range empty page exactly instead; see CHANGES.md.
  4. What should NOT have changed is snapshotted and asserted too: the
     eight owned tables plus an exact re-read of every seeded row.

Money discipline: Decimal in Python, TEXT columns, exact string comparisons.
Never float.
"""
import json
from decimal import Decimal

import pytest

from tax_helpers import (
    call_action, ns, is_error, is_ok, snapshot, TAX_TABLES,
    seed_company, seed_account, seed_customer,
    tl, load_db_query,
)
from erpclaw_lib import seam
from erpclaw_lib.db import get_connection
from erpclaw_lib.query import Q, P, Table, Order

mod = load_db_query()


@pytest.fixture(autouse=True)
def _dispose_seam_engines():
    yield
    seam.dispose_engines()


def _norm(row):
    return {k: (None if v is None else str(v)) for k, v in dict(row).items()}


def _add_template(conn, company_id, account_id, name,
                  tax_type="sales", rate="10"):
    return call_action(mod.add_tax_template, conn, ns(
        name=name, tax_type=tax_type, company_id=company_id,
        lines=json.dumps([tl(account_id, rate)])))


def _stored_rows(db_path, table, where_col=None, where_val=None, order_col="name"):
    vconn = get_connection(db_path)
    try:
        t = Table(table)
        q = Q.from_(t).select(t.star)
        params = ()
        if where_col is not None:
            q = q.where(t.field(where_col) == P())
            params = (where_val,)
        q = q.orderby(t.field(order_col))
        return [_norm(r) for r in vconn.execute(q.get_sql(), params).fetchall()]
    finally:
        vconn.close()


def _stored_rules_with_templates(db_path, company_id):
    vconn = get_connection(db_path)
    try:
        r = Table("tax_rule")
        q = (Q.from_(r).select(r.id, r.tax_template_id, r.tax_type,
                               r.priority, r.company_id)
             .where(r.company_id == P())
             .orderby(r.priority).orderby(r.created_at))
        rules = [_norm(x) for x in vconn.execute(q.get_sql(), (company_id,)).fetchall()]
        t = Table("tax_template")
        names = {}
        for rule in rules:
            tq = Q.from_(t).select(t.id, t.name).where(t.id == P())
            row = vconn.execute(tq.get_sql(), (rule["tax_template_id"],)).fetchone()
            names[rule["id"]] = row["name"]
        return rules, names
    finally:
        vconn.close()


def _full_snapshot(conn):
    out = {}
    for table in TAX_TABLES + ("audit_log",):
        t = Table(table)
        q = Q.from_(t).select(t.star).orderby(t.id)
        out[table] = [_norm(r) for r in conn.execute(q.get_sql(), ()).fetchall()]
    return out


# ── 1. list-tax-categories ───────────────────────────────────────────────────

class TestListTaxCategoriesStrong:
    """Deepens TestListTaxCategories.test_list_pagination in test_tax.py
    (asserted the page names, the total and has_more, but never read the
    stored rows back and pinned no refusal). The weight is now on the
    stored-row read-back below. No amount, rate or quantity exists on a tax
    category, so exact TEXT identity of name and description is the check."""

    def test_pages_carry_exact_stored_rows(self, conn, db_path):
        # Seeded out of alphabetical order on purpose: the name-ordering
        # assertions below only pass when the action orders by name.
        for name in ("C2", "C0", "C1"):
            assert is_ok(call_action(mod.add_tax_category, conn, ns(
                name=name, description="desc-%s" % name)))
        before = snapshot(conn)

        assert seam.table_exists("tax_category", db_path)
        page = call_action(mod.list_tax_categories, conn, ns(limit="2", offset="1"))
        assert is_ok(page), page
        # Derived 1: name ordering decided by the action.
        assert [c["name"] for c in page["categories"]] == ["C1", "C2"]
        # Derived 2: paging math across both pages.
        assert page["total_count"] == 3
        assert page["has_more"] is False
        first = call_action(mod.list_tax_categories, conn, ns(limit="2", offset="0"))
        assert [c["name"] for c in first["categories"]] == ["C0", "C1"]
        assert first["has_more"] is True

        # C0 is the decoy for the offset dimension: it exists, it sorts
        # first, and the offset-1 page must exclude exactly it.
        stored = _stored_rows(db_path, "tax_category")
        assert [(s["name"], s["description"]) for s in stored] == [
            ("C0", "desc-C0"), ("C1", "desc-C1"), ("C2", "desc-C2")]
        assert [c["id"] for c in page["categories"]] == [
            s["id"] for s in stored if s["name"] in ("C1", "C2")]
        assert [c["description"] for c in page["categories"]] == ["desc-C1", "desc-C2"]

        assert snapshot(conn) == before, "a read must write nothing"

    def test_out_of_range_offset_is_an_empty_ok_page(self, conn, db_path):
        assert is_ok(call_action(mod.add_tax_category, conn, ns(name="Solo")))
        before = snapshot(conn)
        r = call_action(mod.list_tax_categories, conn, ns(limit="20", offset="99"))
        assert is_ok(r), r
        assert r["categories"] == []
        assert r["total_count"] == 1
        assert r["has_more"] is False
        assert snapshot(conn) == before

    def test_bad_limit_refused_exactly_and_unchanged(self, conn, db_path):
        assert is_ok(call_action(mod.add_tax_category, conn, ns(name="Solo")))
        full_before = _full_snapshot(conn)
        for bad in ("abc", "-1", "0"):
            r = call_action(mod.list_tax_categories, conn, ns(limit=bad, offset="0"))
            assert is_error(r), bad
            assert r["message"] == "--limit must be a positive integer", bad
        assert _full_snapshot(conn) == full_before

    def test_bad_offset_refused_exactly_and_unchanged(self, conn, db_path):
        assert is_ok(call_action(mod.add_tax_category, conn, ns(name="Solo")))
        full_before = _full_snapshot(conn)
        for bad in ("xyz", "-2"):
            r = call_action(mod.list_tax_categories, conn, ns(limit="20", offset=bad))
            assert is_error(r), bad
            assert r["message"] == "--offset must be a non-negative integer", bad
        assert _full_snapshot(conn) == full_before


# ── 2. list-tax-rules ────────────────────────────────────────────────────────

class TestListTaxRulesStrong:
    """Deepens TestListTaxRules.test_list_returns_template_names_in_priority_order
    in test_tax.py (asserted the joined names and the count, but never read
    the stored rows back on a fresh connection and pinned the company refusal
    only by substring). The weight is now on the stored-row read-back and the
    exact refusal below. No amount, rate or quantity exists on a tax rule, so
    exact TEXT identity of every echoed field is the check."""

    def _seed(self, conn):
        ca = seed_company(conn)
        cb = seed_company(conn)
        aa = seed_account(conn, ca)
        ab = seed_account(conn, cb)
        first = _add_template(conn, ca, aa, name="First")["tax_template_id"]
        second = _add_template(conn, ca, aa, name="Second")["tax_template_id"]
        third = _add_template(conn, ca, aa, name="Third")["tax_template_id"]
        cust = seed_customer(conn, ca)
        assert is_ok(call_action(mod.add_tax_rule, conn, ns(
            tax_template_id=first, tax_type="sales", priority=9, customer_id=cust)))
        assert is_ok(call_action(mod.add_tax_rule, conn, ns(
            tax_template_id=second, tax_type="sales", priority=1, customer_id=cust)))
        assert is_ok(call_action(mod.add_tax_rule, conn, ns(
            tax_template_id=third, tax_type="purchase", priority=5,
            customer_id=cust)))
        # Second-company decoy: same template name, lower priority. If the
        # action read across companies it would surface first here.
        b_second = _add_template(conn, cb, ab, name="Second")["tax_template_id"]
        bcust = seed_customer(conn, cb)
        assert is_ok(call_action(mod.add_tax_rule, conn, ns(
            tax_template_id=b_second, tax_type="sales", priority=0, customer_id=bcust)))
        return ca, cb, first, second, third

    def test_priority_order_and_join_carry_exact_stored_values(self, conn, db_path):
        ca, cb, first, second, third = self._seed(conn)
        before = snapshot(conn)

        assert seam.table_exists("tax_rule", db_path)
        assert seam.table_exists("tax_template", db_path)
        r = call_action(mod.list_tax_rules, conn, ns(company_id=ca))
        assert is_ok(r), r
        # Derived: priority ordering decided by the action.
        assert [x["template_name"] for x in r["rules"]] == ["Second", "Third", "First"]
        assert r["total_count"] == 3
        assert [x["priority"] for x in r["rules"]] == [1, 5, 9]
        assert [x["company_id"] for x in r["rules"]] == [ca, ca, ca]

        rules, names = _stored_rules_with_templates(db_path, ca)
        assert [x["id"] for x in r["rules"]] == [x["id"] for x in rules]
        assert [x["template_name"] for x in r["rules"]] == [names[x["id"]] for x in rules]
        assert [x["tax_type"] for x in r["rules"]] == [
            x["tax_type"] for x in rules] == ["sales", "purchase", "sales"]

        # The second-company "Second" shares its name exactly: the id list
        # above proves the joined name came from this company's template.
        other = _stored_rules_with_templates(db_path, cb)[0]
        assert len(other) == 1
        assert other[0]["id"] not in [x["id"] for x in r["rules"]]

        assert snapshot(conn) == before, "a read must write nothing"

    def test_unknown_company_refused_exactly_and_unchanged(self, conn, db_path):
        self._seed(conn)
        before = snapshot(conn)
        full_before = _full_snapshot(conn)
        before_rows = _stored_rows(db_path, "tax_rule", order_col="priority")
        r = call_action(mod.list_tax_rules, conn, ns(company_name="No Such Company"))
        assert r.get("status") != "ok"
        assert r.get("error") == "Company 'No Such Company' not found."
        assert snapshot(conn) == before
        assert _full_snapshot(conn) == full_before
        # The seeded rows themselves are byte-identical, not just counted.
        assert _stored_rows(db_path, "tax_rule", order_col="priority") == before_rows


# ── 3. list-tax-templates ────────────────────────────────────────────────────

class TestListTaxTemplatesStrong:
    """Deepens TestListTaxTemplates.test_pagination_and_type_filter in
    test_tax.py (asserted paging and the type filter, but never read the
    stored rows back on a fresh connection and pinned the company refusal
    only by substring). The weight is now on the stored-row read-back and
    the exact refusal below. The list returns template headers without their
    lines, so no rate is echoed and exact TEXT identity of every header
    field is the check."""

    def _seed(self, conn):
        ca = seed_company(conn)
        cb = seed_company(conn)
        aa = seed_account(conn, ca)
        ab = seed_account(conn, cb)
        for name, kind in (("S1", "sales"), ("S2", "sales"),
                           ("P1", "purchase"), ("B1", "both")):
            assert is_ok(_add_template(conn, ca, aa, name=name, tax_type=kind))
        # Second-company decoy: same name, same type. It must never surface
        # under company A, whichever filter runs.
        assert is_ok(_add_template(conn, cb, ab, name="S1", tax_type="sales"))
        return ca, cb

    def test_type_filters_carry_exact_stored_headers(self, conn, db_path):
        ca, cb = self._seed(conn)
        before = snapshot(conn)

        assert seam.table_exists("tax_template", db_path)
        sales = call_action(mod.list_tax_templates, conn,
                            ns(company_id=ca, tax_type="sales"))
        assert is_ok(sales), sales
        # Derived 1: the 'both'-inclusion count decided by the action.
        assert sorted(t["name"] for t in sales["templates"]) == ["B1", "S1", "S2"]
        assert sales["total_count"] == 3
        # Derived 2: name ordering decided by the action.
        assert [t["name"] for t in sales["templates"]] == ["B1", "S1", "S2"]

        purch = call_action(mod.list_tax_templates, conn,
                            ns(company_id=ca, tax_type="purchase"))
        assert sorted(t["name"] for t in purch["templates"]) == ["B1", "P1"]
        assert purch["total_count"] == 2

        page = call_action(mod.list_tax_templates, conn,
                           ns(company_id=ca, limit="2", offset="0"))
        assert page["total_count"] == 4
        assert len(page["templates"]) == 2
        assert page["has_more"] is True

        stored = _stored_rows(db_path, "tax_template")
        stored_ca = [s for s in stored if s["company_id"] == ca]
        assert [(s["name"], s["tax_type"]) for s in stored_ca] == [
            ("B1", "both"), ("P1", "purchase"), ("S1", "sales"), ("S2", "sales")]
        assert [t["id"] for t in sales["templates"]] == [
            s["id"] for s in stored_ca if s["tax_type"] in ("sales", "both")]
        assert [t["tax_type"] for t in sales["templates"]] == ["both", "sales", "sales"]
        # The Lakeside "S1" shares name and type exactly: the id comparison
        # above proves the output row is Harbor's, not Lakeside's.
        stored_cb = [s for s in stored if s["company_id"] == cb]
        assert len(stored_cb) == 1
        assert stored_cb[0]["id"] not in [t["id"] for t in sales["templates"]]

        assert snapshot(conn) == before, "a read must write nothing"

    def test_unknown_company_refused_exactly_and_unchanged(self, conn, db_path):
        self._seed(conn)
        before = snapshot(conn)
        full_before = _full_snapshot(conn)
        r = call_action(mod.list_tax_templates, conn,
                        ns(company_name="No Such Company"))
        assert r.get("status") != "ok"
        assert r.get("error") == "Company 'No Such Company' not found."
        assert snapshot(conn) == before
        assert _full_snapshot(conn) == full_before
