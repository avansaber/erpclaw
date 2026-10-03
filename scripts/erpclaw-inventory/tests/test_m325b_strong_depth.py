"""M325B strong depth: 12 inventory actions made strong.

Each action below already had a behavioural test that the depth instrument
calls weak (content-only assertions, no money literal, no read-back through
the seam, no pinned refusal). This module deepens each one WITHOUT deleting
or weakening the original: every class names the weak test it deepens and
says which new assertion now carries the weight.

What "strong" means per test:
  1. Read back through the seam: after the action runs, the stored rows are
     re-read on a FRESH connection from ``erpclaw_lib.db.get_connection``
     with queries built by PyPika through ``erpclaw_lib.query``, and every
     value is compared exactly (never counts, never truthiness). Catalog
     visibility itself is proved through ``erpclaw_lib.seam.table_exists``.
  2. A money literal where money exists: the exact expected TEXT string is
     named in the test, computed by hand, never copied from the action output.
     Actions with no amount column say so explicitly; exact TEXT identity is
     then the whole check.
  3. A pinned refusal per action: the refusal is asserted, the message is
     asserted truthful (names what the caller typed), and the database is
     asserted unchanged afterwards.
  4. What should NOT have changed is snapshotted and asserted too.

Four of the twelve actions (list-batches, list-item-alternatives,
list-item-groups, list-stock-entries) have NO refusal branch in the action
code: any filter combination succeeds, and an unknown filter value yields an
empty ok-result. That is documented as a finding in CHANGES.md. For those
four the refusal leg is carried by the owning writer on the same rows
(add-batch / add-item-alternative / add-item-group / add-stock-entry), plus
an assertion that the list itself reports the empty set truthfully and
writes nothing.

Money discipline: Decimal in Python, TEXT columns, exact string comparisons.
Never float. No sqlite catalog or introspection statements appear here;
catalog questions go through ``erpclaw_lib.seam``.
"""
import json
from decimal import Decimal

import pytest

from inventory_helpers import (
    call_action, ns, is_error, is_ok, load_db_query, seed_item, _uuid,
)
from erpclaw_lib import seam
from erpclaw_lib.db import get_connection
from erpclaw_lib.query import Q, P, Table

mod = load_db_query()

_DEFAULTS = dict(
    name=None, company_id=None, company_name=None, warehouse_type=None,
    parent_id=None, account_id=None, is_group=None,
    item_id=None, item_code=None, item_name=None, item_group=None,
    item_type=None, stock_uom=None, valuation_method=None,
    has_batch=None, has_serial=None, standard_rate=None,
    reorder_level=None, reorder_qty=None, item_status=None,
    custom_fields=None, search=None,
    template_item_id=None, attribute_name=None, attribute_values=None,
    attributes=None,
    supplier_id=None, min_order_qty=None, lead_time_days=None, priority=None,
    alternative_item_id=None, conversion_factor=None, notes=None,
    qty=None, warehouse_id=None, warehouse=None, batch_id=None,
    active_only=False,
    batch_name=None, manufacturing_date=None, expiry_date=None,
    serial_no=None, sn_status=None,
    entry_type=None, posting_date=None, items=None, se_status=None,
    from_date=None, to_date=None, supplier_warehouse_id=None,
    work_order_id=None,
    new_rate=None, reason=None, revaluation_id=None,
    stock_entry_id=None, limit=None, offset=None, db_path=None,
)


def _ns(**kw):
    d = dict(_DEFAULTS)
    d.update(kw)
    return ns(**d)


_SNAPSHOT_TABLES = (
    "warehouse", "item", "item_group", "batch", "serial_number",
    "item_alternative", "item_supplier", "item_attribute",
    "stock_entry", "stock_entry_item", "stock_ledger_entry", "gl_entry",
    "stock_revaluation", "audit_log",
)

_LEDGER_TABLES = ("stock_ledger_entry", "gl_entry")


def _snapshot(conn):
    snap = {}
    for table in _SNAPSHOT_TABLES:
        snap[table] = [tuple(r) for r in
                       conn.execute("SELECT * FROM %s ORDER BY id" % table).fetchall()]
    return snap


def _counts(conn):
    return {t: len(v) for t, v in _snapshot(conn).items()}


@pytest.fixture(autouse=True)
def _dispose_seam_engines():
    yield
    seam.dispose_engines()


def _seed_template(conn, code, name, rate):
    iid = _uuid()
    conn.execute(
        """INSERT INTO item (id, item_code, item_name, stock_uom, is_stock_item,
           item_type, standard_rate, status, has_variants)
           VALUES (?, ?, ?, 'Each', 1, 'stock', ?, 'active', 0)""",
        (iid, code, name, rate),
    )
    conn.commit()
    return iid


def _seed_supplier(conn, company_id, name="Test Supplier"):
    sid = _uuid()
    conn.execute(
        """INSERT INTO supplier (id, name, company_id, status)
           VALUES (?, ?, ?, 'active')""",
        (sid, "%s %s" % (name, sid[:6]), company_id),
    )
    conn.commit()
    return sid


def _supplier_name(conn, supplier_id):
    row = conn.execute("SELECT name FROM supplier WHERE id = ?",
                       (supplier_id,)).fetchone()
    return row["name"]


# ── 1. add-warehouse ─────────────────────────────────────────────────────────

class TestAddWarehouseStrong:
    """Deepens TestAddWarehouse.test_basic_create in
    test_items_warehouses.py (asserts only response shape). The weight is now
    on the stored-row readback below. No amount column exists on warehouse,
    so exact TEXT identity of every stored field is the money-grade check."""

    def test_create_writes_exact_row_and_nothing_else(self, conn, env, db_path):
        before = _counts(conn)
        r = call_action(mod.add_warehouse, conn, _ns(
            name="M325B Transit", company_id=env["company_id"],
            warehouse_type="transit", parent_id=None,
            account_id=env["stock_acct"], is_group=None))
        assert is_ok(r), r
        wid = r["warehouse_id"]
        assert r["name"] == "M325B Transit"

        assert seam.table_exists("warehouse", db_path)
        vconn = get_connection(db_path)
        try:
            t = Table("warehouse")
            q = Q.from_(t).select(t.star).where(t.id == P())
            row = vconn.execute(q.get_sql(), (wid,)).fetchone()
        finally:
            vconn.close()
        assert row["name"] == "M325B Transit"
        assert row["warehouse_type"] == "transit"
        assert row["company_id"] == env["company_id"]
        assert row["account_id"] == env["stock_acct"]
        assert row["is_group"] == 0
        assert row["parent_id"] is None

        after = _counts(conn)
        assert after["warehouse"] == before["warehouse"] + 1
        assert after["audit_log"] == before["audit_log"] + 1
        for table in ("item", "item_group", "batch", "serial_number",
                      "stock_entry", "stock_ledger_entry", "gl_entry",
                      "stock_revaluation"):
            assert after[table] == before[table], table

    def test_unknown_company_refused_truthfully_and_writes_nothing(self, conn, env):
        before = _snapshot(conn)
        ghost = "m325b-ghost-company"
        r = call_action(mod.add_warehouse, conn, _ns(
            name="M325B Ghost", company_id=ghost, warehouse_type=None,
            parent_id=None, account_id=None, is_group=None))
        assert is_error(r)
        assert r["message"] == "Company %s not found" % ghost
        assert _snapshot(conn) == before
        assert conn.execute(
            "SELECT COUNT(*) FROM warehouse WHERE name = 'M325B Ghost'"
        ).fetchone()[0] == 0


# ── 2. check-reorder ─────────────────────────────────────────────────────────

class TestCheckReorderStrong:
    """Deepens TestCheckReorder.test_item_below_reorder in
    test_reports_recon_reval.py (asserted only a count >= 1). The weight is
    now on the exact row literals below. Quantities are TEXT decimals;
    every literal is computed by hand in the comments."""

    def test_below_reorder_row_is_exact_and_read_is_clean(self, conn, env, db_path):
        conn.execute("UPDATE item SET reorder_level = '200', reorder_qty = '50'"
                     " WHERE id = ?", (env["item2"],))
        conn.commit()
        before = _snapshot(conn)

        r = call_action(mod.check_reorder, conn, _ns(
            company_id=env["company_id"], company_name=None))
        assert is_ok(r), r
        assert r["items_below_reorder"] == 1
        (row,) = r["items"]
        # Hand: item2 holds no SLE rows -> current 0.00 <= level 200.00;
        # shortfall 200.00 - 0.00 = 200.00. item1 has no reorder_level set,
        # so it must NOT appear.
        assert row["item_id"] == env["item2"]
        assert row["current_stock"] == "0.00"
        assert row["reorder_level"] == "200.00"
        assert row["reorder_qty"] == "50.00"
        assert row["shortfall"] == "200.00"

        assert seam.table_exists("item", db_path)
        vconn = get_connection(db_path)
        try:
            t = Table("item")
            q = Q.from_(t).select(t.id, t.reorder_level, t.reorder_qty
                                  ).where(t.id == P())
            stored = vconn.execute(q.get_sql(), (env["item2"],)).fetchone()
            sle = Table("stock_ledger_entry")
            sq = (Q.from_(sle).select(sle.actual_qty)
                  .where(sle.item_id == P()).where(sle.is_cancelled == 0))
            sle_rows = vconn.execute(sq.get_sql(), (env["item2"],)).fetchall()
        finally:
            vconn.close()
        assert stored["reorder_level"] == "200"
        assert stored["reorder_qty"] == "50"
        assert sum((Decimal(x["actual_qty"]) for x in sle_rows),
                   Decimal("0")) == Decimal("0")

        assert _snapshot(conn) == before, "a read must write nothing"

    def test_unknown_company_refused_truthfully_and_writes_nothing(self, conn, env):
        before = _snapshot(conn)
        r = call_action(mod.check_reorder, conn, _ns(
            company_id=None, company_name="M325B No Such Co"))
        assert "M325B No Such Co" in r.get("error", "")
        assert _snapshot(conn) == before


# ── 3. generate-item-variants ────────────────────────────────────────────────

class TestGenerateItemVariantsStrong:
    """Deepens TestGenerateItemVariants.test_generate_all_variants in
    test_sprint3_inventory_projections.py (asserted created/skipped counts
    only). The weight is now on the stored variant rows and the inherited
    money literal: template rate 42.50 copied verbatim onto each variant."""

    def test_generate_writes_exact_variant_rows(self, conn, env, db_path):
        tpl = _seed_template(conn, "M325B-TMPL", "M325B Shirt", "42.50")
        setup = call_action(mod.add_item_attribute, conn, _ns(
            item_id=tpl, attribute_name="Color",
            attribute_values='["Red", "Blue"]'))
        assert is_ok(setup), setup
        before = _counts(conn)

        r = call_action(mod.generate_item_variants, conn, _ns(
            template_item_id=tpl))
        assert is_ok(r), r
        assert r["created"] == 2
        assert r["skipped"] == 0
        assert sorted(v["item_code"] for v in r["variants"]) == [
            "M325B-TMPL-Blue", "M325B-TMPL-Red"]

        assert seam.table_exists("item", db_path)
        vconn = get_connection(db_path)
        try:
            t = Table("item")
            q = (Q.from_(t).select(t.id, t.item_code, t.item_name,
                                   t.standard_rate, t.variant_of)
                 .where(t.variant_of == P()).orderby(t.item_code))
            variants = vconn.execute(q.get_sql(), (tpl,)).fetchall()
            a = Table("item_attribute")
            aq = (Q.from_(a).select(a.attribute_name, a.attribute_values)
                  .where(a.item_id == P()))
            attr_rows = {}
            for v in variants:
                attr_rows[v["id"]] = vconn.execute(
                    aq.get_sql(), (v["id"],)).fetchall()
        finally:
            vconn.close()
        assert [v["item_code"] for v in variants] == [
            "M325B-TMPL-Blue", "M325B-TMPL-Red"]
        for v in variants:
            assert v["standard_rate"] == "42.50"
            assert v["variant_of"] == tpl
        assert json.loads(attr_rows[variants[0]["id"]][0]["attribute_values"]) == "Blue"
        assert json.loads(attr_rows[variants[1]["id"]][0]["attribute_values"]) == "Red"

        after = _counts(conn)
        assert after["item"] == before["item"] + 2
        assert after["item_attribute"] == before["item_attribute"] + 2
        assert after["audit_log"] == before["audit_log"]
        for table in _LEDGER_TABLES:
            assert after[table] == before[table], table

    def test_non_template_refused_truthfully_and_writes_nothing(self, conn, env):
        before = _snapshot(conn)
        r = call_action(mod.generate_item_variants, conn, _ns(
            template_item_id=env["item1"]))
        assert is_error(r)
        assert r["message"] == "Item is not a template (has_variants must be 1)"
        assert _snapshot(conn) == before
        assert conn.execute("SELECT COUNT(*) FROM item WHERE variant_of = ?",
                            (env["item1"],)).fetchone()[0] == 0


# ── 4. get-item ──────────────────────────────────────────────────────────────

class TestGetItemStrong:
    """Deepens TestGetItem.test_get in test_items_warehouses.py (asserted id
    match plus a key presence). The weight is now on the exact money
    literals: standard_rate 50.00, 100 units @ 50.00 = 5000.00, all by hand
    from the seeded env (Widget A @ 50.00, 100 units in Main Warehouse)."""

    def test_get_returns_exact_money_and_balances(self, conn, env, db_path):
        before = _snapshot(conn)
        r = call_action(mod.get_item, conn, _ns(item_id=env["item1"]))
        assert is_ok(r), r
        assert r["id"] == env["item1"]
        assert r["standard_rate"] == "50.00"
        assert r["total_qty"] == "100.00"
        assert r["total_stock_value"] == "5000.00"
        assert len(r["stock_balances"]) == 1
        bal = r["stock_balances"][0]
        assert bal["warehouse_id"] == env["warehouse"]
        assert bal["qty"] == "100.00"
        assert bal["valuation_rate"] == "50.00"
        assert bal["stock_value"] == "5000.00"

        assert seam.table_exists("stock_ledger_entry", db_path)
        vconn = get_connection(db_path)
        try:
            t = Table("item")
            q = Q.from_(t).select(t.id, t.standard_rate).where(t.id == P())
            stored = vconn.execute(q.get_sql(), (env["item1"],)).fetchone()
            sle = Table("stock_ledger_entry")
            sq = (Q.from_(sle).select(
                sle.actual_qty, sle.valuation_rate, sle.stock_value,
                sle.stock_value_difference, sle.is_cancelled)
                .where(sle.item_id == P())
                .where(sle.warehouse_id == P())
                .where(sle.is_cancelled == 0))
            legs = vconn.execute(
                sq.get_sql(), (env["item1"], env["warehouse"])).fetchall()
        finally:
            vconn.close()
        assert stored["standard_rate"] == "50.00"
        assert len(legs) == 1
        assert legs[0]["actual_qty"] == "100"
        assert legs[0]["valuation_rate"] == "50.00"
        assert legs[0]["stock_value"] == "5000.00"
        assert legs[0]["stock_value_difference"] == "5000.00"

        assert _snapshot(conn) == before, "a get must write nothing"

    def test_unknown_item_refused_truthfully_and_writes_nothing(self, conn, env):
        before = _snapshot(conn)
        ghost = "m325b-ghost-item"
        r = call_action(mod.get_item, conn, _ns(item_id=ghost))
        assert is_error(r)
        assert r["message"] == "Item %s not found" % ghost
        assert _snapshot(conn) == before


# ── 5. list-batches ──────────────────────────────────────────────────────────

class TestListBatchesStrong:
    """Deepens TestListBatches.test_list_by_item in
    test_pricing_batch_serial.py (asserted only total_count >= 1). The weight
    is now on the stored-row readback. Batch rows carry no amount column;
    exact TEXT identity of every stored field is the check."""

    def test_list_returns_the_stored_batch_exactly(self, conn, env, db_path):
        created = call_action(mod.add_batch, conn, _ns(
            item_id=env["item1"], batch_name="M325B-B1",
            manufacturing_date="2026-01-15", expiry_date="2027-01-15"))
        assert is_ok(created), created
        before = _snapshot(conn)

        r = call_action(mod.list_batches, conn, _ns(
            item_id=env["item1"], warehouse_id=None,
            limit=None, offset=None))
        assert is_ok(r), r
        assert r["total_count"] == 1
        (row,) = r["batches"]
        assert row["batch_name"] == "M325B-B1"
        assert row["item_id"] == env["item1"]
        assert row["manufacturing_date"] == "2026-01-15"
        assert row["expiry_date"] == "2027-01-15"

        assert seam.table_exists("batch", db_path)
        vconn = get_connection(db_path)
        try:
            t = Table("batch")
            q = Q.from_(t).select(t.star).where(t.id == P())
            stored = vconn.execute(
                q.get_sql(), (created["batch_id"],)).fetchone()
        finally:
            vconn.close()
        assert stored["batch_name"] == "M325B-B1"
        assert stored["item_id"] == env["item1"]
        assert stored["manufacturing_date"] == "2026-01-15"
        assert stored["expiry_date"] == "2027-01-15"

        empty = call_action(mod.list_batches, conn, _ns(
            item_id=env["item2"], warehouse_id=None,
            limit=None, offset=None))
        assert is_ok(empty), empty
        assert empty["total_count"] == 0
        assert empty["batches"] == []

        assert _snapshot(conn) == before, "lists must write nothing"

    def test_no_refusal_path_probe_and_writer_refusal(self, conn, env):
        """FINDING (CHANGES.md): list-batches has no refusal branch -- an
        unknown filter value yields an empty ok-result, not an error. The
        refusal leg is therefore carried by the owning writer on the same
        rows, with the list proved unchanged afterwards."""
        before = _snapshot(conn)
        r = call_action(mod.list_batches, conn, _ns(
            item_id="m325b-ghost-item", warehouse_id=None,
            limit=None, offset=None))
        assert is_ok(r)
        assert r["total_count"] == 0
        assert r["batches"] == []

        bad = call_action(mod.add_batch, conn, _ns(
            item_id="m325b-ghost-item", batch_name="M325B-X",
            manufacturing_date=None, expiry_date=None))
        assert is_error(bad)
        assert bad["message"] == "Item m325b-ghost-item not found"
        assert _snapshot(conn) == before


# ── 6. list-item-alternatives ────────────────────────────────────────────────

class TestListItemAlternativesStrong:
    """Deepens test_list_item_alternatives_filtered_and_ordered in
    test_s7_item_alternatives.py (asserted order + count). The weight is now
    on the stored-row readback with hand-named conversion literals: "2"
    stores as "2.00", "1.5" as "1.50"."""

    def test_list_returns_stored_rows_in_priority_order(self, conn, env, db_path):
        alt_c = seed_item(conn, "Widget C", "Each", "stock", "40.00")
        first = call_action(mod.add_item_alternative, conn, _ns(
            item_id=env["item1"], alternative_item_id=alt_c, priority=2,
            conversion_factor="1.5", notes="second"))
        assert is_ok(first), first
        second = call_action(mod.add_item_alternative, conn, _ns(
            item_id=env["item1"], alternative_item_id=env["item2"],
            priority=1, conversion_factor="2", notes="first"))
        assert is_ok(second), second
        before = _snapshot(conn)

        r = call_action(mod.list_item_alternatives, conn, _ns(
            item_id=env["item1"], active_only=False))
        assert is_ok(r), r
        assert r["count"] == 2
        assert [x["alternative_item_id"] for x in r["item_alternatives"]] == [
            env["item2"], alt_c]
        assert r["item_alternatives"][0]["conversion_factor"] == "2.00"
        assert r["item_alternatives"][1]["conversion_factor"] == "1.50"

        assert seam.table_exists("item_alternative", db_path)
        vconn = get_connection(db_path)
        try:
            t = Table("item_alternative")
            q = (Q.from_(t).select(
                t.item_id, t.alternative_item_id, t.priority,
                t.conversion_factor, t.notes, t.is_active)
                .where(t.item_id == P()).orderby(t.priority))
            stored = vconn.execute(q.get_sql(), (env["item1"],)).fetchall()
        finally:
            vconn.close()
        assert [(x["alternative_item_id"], x["priority"],
                 x["conversion_factor"], x["is_active"]) for x in stored] == [
            (env["item2"], 1, "2.00", 1), (alt_c, 2, "1.50", 1)]

        assert _snapshot(conn) == before, "lists must write nothing"

    def test_no_refusal_path_probe_and_writer_refusal(self, conn, env):
        """FINDING (CHANGES.md): list-item-alternatives has no refusal branch
        -- an unknown filter value yields an empty ok-result. The refusal leg
        is carried by the owning writer (self-reference) on the same rows."""
        before = _snapshot(conn)
        r = call_action(mod.list_item_alternatives, conn, _ns(
            item_id="m325b-ghost-item", active_only=False))
        assert is_ok(r)
        assert r["count"] == 0
        assert r["item_alternatives"] == []

        bad = call_action(mod.add_item_alternative, conn, _ns(
            item_id=env["item1"], alternative_item_id=env["item1"],
            priority=None, conversion_factor=None, notes=None))
        assert is_error(bad)
        assert bad["message"] == ("An item cannot be its own alternative "
                                  "(--item and --alternative must differ)")
        assert _snapshot(conn) == before


# ── 7. list-item-groups ──────────────────────────────────────────────────────

class TestListItemGroupsStrong:
    """Deepens TestListItemGroups.test_list in test_items_warehouses.py
    (asserted only total_count >= 1). The weight is now on the stored-row
    readback including the parent linkage. Group rows carry no amount."""

    def test_list_returns_stored_groups_with_parent_link(self, conn, env, db_path):
        parent = call_action(mod.add_item_group, conn, _ns(
            name="M325B Parent", company_id=env["company_id"],
            parent_id=None))
        assert is_ok(parent), parent
        child = call_action(mod.add_item_group, conn, _ns(
            name="M325B Child", company_id=env["company_id"],
            parent_id=parent["item_group_id"]))
        assert is_ok(child), child
        before = _snapshot(conn)

        r = call_action(mod.list_item_groups, conn, _ns(
            company_id=env["company_id"], parent_id=None,
            limit=None, offset=None))
        assert is_ok(r), r
        assert r["total_count"] == 2
        assert [g["name"] for g in r["item_groups"]] == [
            "M325B Child", "M325B Parent"]

        assert seam.table_exists("item_group", db_path)
        vconn = get_connection(db_path)
        try:
            t = Table("item_group")
            q = (Q.from_(t).select(t.id, t.name, t.company_id, t.parent_id)
                 .where(t.company_id == P()).orderby(t.name))
            stored = vconn.execute(q.get_sql(), (env["company_id"],)).fetchall()
        finally:
            vconn.close()
        assert [(x["name"], x["parent_id"]) for x in stored] == [
            ("M325B Child", parent["item_group_id"]),
            ("M325B Parent", None)]
        assert all(x["company_id"] == env["company_id"] for x in stored)

        only_child = call_action(mod.list_item_groups, conn, _ns(
            company_id=env["company_id"],
            parent_id=parent["item_group_id"], limit=None, offset=None))
        assert is_ok(only_child), only_child
        assert only_child["total_count"] == 1
        assert only_child["item_groups"][0]["name"] == "M325B Child"

        assert _snapshot(conn) == before, "lists must write nothing"

    def test_no_refusal_path_probe_and_writer_refusal(self, conn, env):
        """FINDING (CHANGES.md): list-item-groups has no refusal branch --
        an unknown parent filter yields an empty ok-result. The refusal leg
        is carried by the owning writer (unknown parent) on the same rows."""
        before = _snapshot(conn)
        r = call_action(mod.list_item_groups, conn, _ns(
            company_id=env["company_id"], parent_id="m325b-ghost-parent",
            limit=None, offset=None))
        assert is_ok(r)
        assert r["total_count"] == 0
        assert r["item_groups"] == []

        bad = call_action(mod.add_item_group, conn, _ns(
            name="M325B Orphan", company_id=env["company_id"],
            parent_id="m325b-ghost-parent"))
        assert is_error(bad)
        assert bad["message"] == "Parent item group m325b-ghost-parent not found"
        assert _snapshot(conn) == before


# ── 8. list-item-suppliers ───────────────────────────────────────────────────

class TestListItemSuppliersStrong:
    """Deepens TestListItemSuppliers.test_list_by_item in
    test_sprint3_inventory_projections.py (asserted count + first priority).
    The weight is now on the stored-row readback with the hand-named
    quantity literal: min_order_qty "50" stores as "50.00"."""

    def test_list_returns_stored_link_exactly(self, conn, env, db_path):
        sup = _seed_supplier(conn, env["company_id"], "M325B Parts")
        expected_supplier_name = _supplier_name(conn, sup)
        link = call_action(mod.add_item_supplier, conn, _ns(
            item_id=env["item1"], supplier_id=sup,
            min_order_qty="50", lead_time_days="5", priority=1))
        assert is_ok(link), link
        assert link["min_order_qty"] == "50.00"
        before = _snapshot(conn)

        r = call_action(mod.list_item_suppliers, conn, _ns(
            item_id=env["item1"], supplier_id=None))
        assert is_ok(r), r
        assert r["count"] == 1
        (row,) = r["item_suppliers"]
        assert row["min_order_qty"] == "50.00"
        assert row["lead_time_days"] == 5
        assert row["priority"] == 1
        assert row["supplier_name"] == expected_supplier_name

        assert seam.table_exists("item_supplier", db_path)
        vconn = get_connection(db_path)
        try:
            t = Table("item_supplier")
            q = (Q.from_(t).select(
                t.item_id, t.supplier_id, t.min_order_qty,
                t.lead_time_days, t.priority).where(t.item_id == P()))
            stored = vconn.execute(q.get_sql(), (env["item1"],)).fetchall()
        finally:
            vconn.close()
        assert len(stored) == 1
        assert stored[0]["min_order_qty"] == "50.00"
        assert stored[0]["lead_time_days"] == 5
        assert stored[0]["priority"] == 1
        assert stored[0]["supplier_id"] == sup

        assert _snapshot(conn) == before, "lists must write nothing"

    def test_missing_filters_refused_truthfully_and_writes_nothing(self, conn, env):
        before = _snapshot(conn)
        r = call_action(mod.list_item_suppliers, conn, _ns(
            item_id=None, supplier_id=None))
        assert is_error(r)
        assert r["message"] == ("At least one of --item-id or --supplier-id "
                                "is required")
        assert _snapshot(conn) == before


# ── 9. list-item-variants ────────────────────────────────────────────────────

class TestListItemVariantsStrong:
    """Deepens TestListItemVariants.test_list_variants in
    test_sprint3_inventory_projections.py (asserted count + substring match
    on codes). The weight is now on exact codes plus the inherited money
    literal: template rate 33.75 on every variant, and exact attribute maps."""

    def test_list_returns_exact_variants_with_money(self, conn, env, db_path):
        tpl = _seed_template(conn, "M325B-JKT", "M325B Jacket", "33.75")
        setup = call_action(mod.add_item_attribute, conn, _ns(
            item_id=tpl, attribute_name="Size",
            attribute_values='["S", "M"]'))
        assert is_ok(setup), setup
        gen = call_action(mod.generate_item_variants, conn, _ns(
            template_item_id=tpl))
        assert is_ok(gen), gen
        before = _snapshot(conn)

        r = call_action(mod.list_item_variants, conn, _ns(
            template_item_id=tpl))
        assert is_ok(r), r
        assert r["count"] == 2
        assert [v["item_code"] for v in r["variants"]] == [
            "M325B-JKT-M", "M325B-JKT-S"]
        for v in r["variants"]:
            assert v["standard_rate"] == "33.75"
        assert [v["attributes"] for v in r["variants"]] == [
            {"Size": "M"}, {"Size": "S"}]

        assert seam.table_exists("item", db_path)
        vconn = get_connection(db_path)
        try:
            t = Table("item")
            q = (Q.from_(t).select(t.id, t.item_code, t.standard_rate,
                                   t.variant_of)
                 .where(t.variant_of == P()).orderby(t.item_code))
            stored = vconn.execute(q.get_sql(), (tpl,)).fetchall()
        finally:
            vconn.close()
        assert [x["item_code"] for x in stored] == [
            "M325B-JKT-M", "M325B-JKT-S"]
        assert all(x["standard_rate"] == "33.75" for x in stored)
        assert all(x["variant_of"] == tpl for x in stored)

        assert _snapshot(conn) == before, "lists must write nothing"

    def test_unknown_template_refused_truthfully_and_writes_nothing(self, conn, env):
        before = _snapshot(conn)
        ghost = "m325b-ghost-template"
        r = call_action(mod.list_item_variants, conn, _ns(
            template_item_id=ghost))
        assert is_error(r)
        assert r["message"] == "Template item %s not found" % ghost
        assert _snapshot(conn) == before


# ── 10. list-serial-numbers ──────────────────────────────────────────────────

class TestListSerialNumbersStrong:
    """Deepens TestListSerialNumbers.test_list_by_item in
    test_pricing_batch_serial.py (asserted only total_count >= 1). The weight
    is now on the stored-row readback with the joined item identity.
    Serial rows carry no amount column."""

    def test_list_returns_stored_serial_exactly(self, conn, env, db_path):
        created = call_action(mod.add_serial_number, conn, _ns(
            item_id=env["item1"], serial_no="M325B-SN-1",
            warehouse_id=env["warehouse"], batch_id=None))
        assert is_ok(created), created
        before = _snapshot(conn)

        r = call_action(mod.list_serial_numbers, conn, _ns(
            item_id=env["item1"], warehouse_id=None,
            sn_status=None, limit=None, offset=None))
        assert is_ok(r), r
        assert r["total_count"] == 1
        (row,) = r["serial_numbers"]
        assert row["serial_no"] == "M325B-SN-1"
        assert row["status"] == "active"
        assert row["item_id"] == env["item1"]
        assert row["warehouse_id"] == env["warehouse"]

        assert seam.table_exists("serial_number", db_path)
        vconn = get_connection(db_path)
        try:
            t = Table("serial_number")
            q = Q.from_(t).select(t.star).where(t.id == P())
            stored = vconn.execute(
                q.get_sql(), (created["serial_number_id"],)).fetchone()
            i = Table("item")
            iq = Q.from_(i).select(i.item_code, i.item_name
                                   ).where(i.id == P())
            item_row = vconn.execute(
                iq.get_sql(), (env["item1"],)).fetchone()
        finally:
            vconn.close()
        assert stored["serial_no"] == "M325B-SN-1"
        assert stored["status"] == "active"
        assert stored["item_id"] == env["item1"]
        assert stored["warehouse_id"] == env["warehouse"]
        assert stored["batch_id"] is None
        assert row["item_code"] == item_row["item_code"]
        assert row["item_name"] == item_row["item_name"]

        assert _snapshot(conn) == before, "lists must write nothing"

    def test_bad_status_refused_truthfully_and_writes_nothing(self, conn, env):
        before = _snapshot(conn)
        r = call_action(mod.list_serial_numbers, conn, _ns(
            item_id=None, warehouse_id=None, sn_status="m325b-bogus",
            limit=None, offset=None))
        assert is_error(r)
        assert r["message"] == ("--status must be one of: active, delivered, "
                                "returned, scrapped")
        assert _snapshot(conn) == before


# ── 11. list-stock-entries ───────────────────────────────────────────────────

class TestListStockEntriesStrong:
    """Deepens TestListStockEntries.test_list in test_stock_entries.py
    (asserted only ok-status). The weight is now on the exact money
    literals of the listed draft: hand 10 units x 25.00 = incoming 250.00,
    outgoing 0.00, difference 250.00."""

    def _receive(self, conn, env):
        items = json.dumps([{"item_id": env["item1"], "qty": "10",
                             "rate": "25.00",
                             "to_warehouse_id": env["warehouse"]}])
        r = call_action(mod.add_stock_entry, conn, _ns(
            entry_type="receive", company_id=env["company_id"],
            posting_date="2026-06-15", items=items))
        assert is_ok(r), r
        assert r["total_incoming_value"] == "250.00"
        return r["stock_entry_id"]

    def test_list_returns_entry_with_exact_money(self, conn, env, db_path):
        se_id = self._receive(conn, env)
        before = _snapshot(conn)

        r = call_action(mod.list_stock_entries, conn, _ns(
            company_id=env["company_id"], entry_type=None, se_status=None,
            from_date=None, to_date=None, limit=None, offset=None))
        assert is_ok(r), r
        got = {e["id"]: e for e in r["stock_entries"]}
        assert se_id in got
        row = got[se_id]
        assert row["total_incoming_value"] == "250.00"
        assert row["total_outgoing_value"] == "0.00"
        assert row["value_difference"] == "250.00"
        assert row["status"] == "draft"
        assert row["stock_entry_type"] == "material_receipt"

        assert seam.table_exists("stock_entry", db_path)
        vconn = get_connection(db_path)
        try:
            t = Table("stock_entry")
            q = (Q.from_(t).select(
                t.total_incoming_value, t.total_outgoing_value,
                t.value_difference, t.status, t.stock_entry_type)
                .where(t.id == P()))
            stored = vconn.execute(q.get_sql(), (se_id,)).fetchone()
            li = Table("stock_entry_item")
            lq = (Q.from_(li).select(li.quantity, li.valuation_rate,
                                     li.amount).where(li.stock_entry_id == P()))
            lines = vconn.execute(lq.get_sql(), (se_id,)).fetchall()
        finally:
            vconn.close()
        assert stored["total_incoming_value"] == "250.00"
        assert stored["total_outgoing_value"] == "0.00"
        assert stored["value_difference"] == "250.00"
        assert stored["status"] == "draft"
        assert len(lines) == 1
        assert lines[0]["quantity"] == "10.00"
        assert lines[0]["valuation_rate"] == "25.00"
        assert lines[0]["amount"] == "250.00"

        assert _snapshot(conn) == before, "lists must write nothing"

    def test_no_refusal_path_probe_and_writer_refusal(self, conn, env):
        """FINDING (CHANGES.md): list-stock-entries has no refusal branch --
        an unknown entry-type filter yields an empty ok-result. The refusal
        leg is carried by the owning writer (invalid entry-type) on the same
        rows, with the list proved unchanged afterwards."""
        before = _snapshot(conn)
        r = call_action(mod.list_stock_entries, conn, _ns(
            company_id=env["company_id"], entry_type="m325b-bogus",
            se_status=None, from_date=None, to_date=None,
            limit=None, offset=None))
        assert is_ok(r)
        assert r["total_count"] == 0
        assert r["stock_entries"] == []

        items = json.dumps([{"item_id": env["item1"], "qty": "10",
                             "rate": "25.00",
                             "to_warehouse_id": env["warehouse"]}])
        bad = call_action(mod.add_stock_entry, conn, _ns(
            entry_type="m325b-bogus", company_id=env["company_id"],
            posting_date="2026-06-15", items=items))
        assert is_error(bad)
        assert bad["message"] == (
            "Invalid --entry-type 'm325b-bogus'. Valid: receive, issue, "
            "transfer, manufacture, repack, subcontract, consume")
        assert _snapshot(conn) == before


# ── 12. list-stock-revaluations ──────────────────────────────────────────────

class TestListStockRevaluationsStrong:
    """Deepens TestListStockRevaluations.test_list in
    test_reports_recon_reval.py (asserted only total_count >= 1). The weight
    is now on the exact money literals: hand 100 units, 50.00 -> 60.00, so
    current_qty 100.00, old 50.00, new 60.00, adjustment 1000.00, with both
    GL legs proved balanced at 1000.00."""

    def test_list_returns_revaluation_with_exact_money(self, conn, env, db_path):
        rv = call_action(mod.revalue_stock, conn, _ns(
            item_id=env["item1"], warehouse_id=env["warehouse"],
            new_rate="60.00", posting_date="2026-06-15",
            company_id=env["company_id"], reason="M325B check"))
        assert is_ok(rv), rv
        assert rv["current_qty"] == "100.00"
        assert rv["old_rate"] == "50.00"
        assert rv["new_rate"] == "60.00"
        assert rv["adjustment_amount"] == "1000.00"
        before = _snapshot(conn)

        r = call_action(mod.list_stock_revaluations, conn, _ns(
            company_id=env["company_id"], company_name=None,
            limit=None, offset=None))
        assert is_ok(r), r
        assert r["total_count"] == 1
        (row,) = r["revaluations"]
        assert row["id"] == rv["revaluation_id"]
        assert row["current_qty"] == "100.00"
        assert row["old_rate"] == "50.00"
        assert row["new_rate"] == "60.00"
        assert row["adjustment_amount"] == "1000.00"

        assert seam.table_exists("stock_revaluation", db_path)
        vconn = get_connection(db_path)
        try:
            t = Table("stock_revaluation")
            q = (Q.from_(t).select(
                t.current_qty, t.old_rate, t.new_rate,
                t.adjustment_amount, t.status, t.item_id, t.warehouse_id)
                .where(t.id == P()))
            stored = vconn.execute(
                q.get_sql(), (rv["revaluation_id"],)).fetchone()
            g = Table("gl_entry")
            gq = (Q.from_(g).select(g.debit, g.credit)
                  .where(g.voucher_type == "stock_revaluation")
                  .where(g.voucher_id == P()))
            legs = vconn.execute(
                gq.get_sql(), (rv["revaluation_id"],)).fetchall()
        finally:
            vconn.close()
        assert stored["current_qty"] == "100.00"
        assert stored["old_rate"] == "50.00"
        assert stored["new_rate"] == "60.00"
        assert stored["adjustment_amount"] == "1000.00"
        assert stored["status"] == "submitted"
        assert len(legs) == 2
        assert sum((Decimal(x["debit"]) for x in legs),
                   Decimal("0")) == Decimal("1000.00")
        assert sum((Decimal(x["credit"]) for x in legs),
                   Decimal("0")) == Decimal("1000.00")

        assert _snapshot(conn) == before, "lists must write nothing"

    def test_unknown_company_refused_truthfully_and_writes_nothing(self, conn, env):
        before = _snapshot(conn)
        r = call_action(mod.list_stock_revaluations, conn, _ns(
            company_id=None, company_name="M325B No Such Co",
            limit=None, offset=None))
        assert "M325B No Such Co" in r.get("error", "")
        assert _snapshot(conn) == before
