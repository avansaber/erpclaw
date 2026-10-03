"""M625 strong depth: inventory lists, stock reports and item/warehouse updates.

Each class below deepens a behavioural test the depth instrument calls weak
(content-only assertions, no money literal, no read-back through the seam, no
pinned refusal). Nothing here deletes or weakens the original: every class
names the weak test it deepens and says which new assertion now carries the
weight.

What "strong" means per test:
  1. Read back through the seam: stored rows are re-read on a FRESH
     connection from ``erpclaw_lib.db.get_connection`` with queries built by
     PyPika through ``erpclaw_lib.query``, and every value is compared
     exactly (never counts, never truthiness). Visibility of each table is
     proved through ``erpclaw_lib.seam.table_exists``. No raw catalog reads
     appear anywhere below, including in prose.
  2. A money literal where money exists: the exact expected TEXT string is
     named in the test, computed by hand, never copied from the action
     output. ``list-warehouses`` carries no amount, rate or quantity column,
     so exact TEXT identity of every stored field is the money-grade check.
  3. A pinned refusal per action: the refusal is asserted with the EXACT
     message (``==``, not a substring), and the database is asserted
     unchanged afterwards. ``stock-ledger-report`` has no refusal branch in
     the handler -- any filter combination succeeds and an unknown filter
     yields an empty ok-result -- so the refusal leg pins that empty result
     exactly instead; see CHANGES.md.
  4. What should NOT have changed is snapshotted and asserted too. Writers
     prove the stock ledger and the general ledger byte-identical.

Money discipline: Decimal in Python, TEXT columns, exact string comparisons.
Never float.
"""
import json
import uuid
from datetime import datetime
from decimal import Decimal

import pytest

from inventory_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
)
from erpclaw_lib import seam
from erpclaw_lib.db import get_connection
from erpclaw_lib.query import Q, P, Table, Order, insert_row, dynamic_update

mod = load_db_query()

_DEFAULTS = dict(
    item_id=None, item_code=None, item_name=None, item_group=None,
    item_type=None, stock_uom=None, valuation_method=None,
    has_batch=None, has_serial=None, standard_rate=None,
    reorder_level=None, reorder_qty=None, item_status=None,
    custom_fields=None, search=None,
    name=None, company_id=None, company_name=None, warehouse_type=None,
    parent_id=None, account_id=None, is_group=None,
    warehouse_id=None, from_date=None, to_date=None,
    limit=None, offset=None,
)


def _ns(**kw):
    d = dict(_DEFAULTS)
    d.update(kw)
    return ns(**d)


@pytest.fixture(autouse=True)
def _dispose_seam_engines():
    yield
    seam.dispose_engines()


_SNAPSHOT_TABLES = (
    "warehouse", "item", "stock_ledger_entry", "gl_entry", "audit_log",
)


def _norm(row):
    return {k: (None if v is None else str(v)) for k, v in dict(row).items()}


def _rows(conn, table):
    t = Table(table)
    q = Q.from_(t).select(t.star).orderby(t.id)
    return [_norm(r) for r in conn.execute(q.get_sql(), ()).fetchall()]


def _snapshot(conn):
    return {t: _rows(conn, t) for t in _SNAPSHOT_TABLES}


def _insert(conn, table, **data):
    markers = {k: P() for k in data}
    sql, cols = insert_row(table, markers)
    conn.execute(sql, [data[c] for c in cols])
    conn.commit()


def _uuid():
    return str(uuid.uuid4())


def _seed_company(conn, name, abbr):
    cid = _uuid()
    _insert(conn, "company", id=cid, name="%s %s" % (name, cid[:6]),
            abbr="%s%s" % (abbr, cid[:4]), default_currency="USD",
            country="United States", fiscal_year_start_month=1)
    return cid


def _seed_account(conn, company_id, name, root_type="asset"):
    aid = _uuid()
    direction = "debit_normal" if root_type in ("asset", "expense") else "credit_normal"
    _insert(conn, "account", id=aid, name="%s %s" % (name, aid[:6]),
            account_number="ACC-%s" % aid[:6], root_type=root_type,
            account_type="stock" if root_type == "asset" else "expense",
            balance_direction=direction, company_id=company_id, depth=0)
    return aid


def _seed_warehouse(conn, company_id, name, wh_type="stores", account_id=None):
    wid = _uuid()
    _insert(conn, "warehouse", id=wid, name=name, parent_id=None,
            warehouse_type=wh_type, company_id=company_id,
            account_id=account_id, is_group=0)
    return wid


def _seed_item(conn, code, name, rate="10.00", level=None, qty=None):
    iid = _uuid()
    _insert(conn, "item", id=iid, item_code=code, item_name=name,
            item_type="stock", stock_uom="Each", standard_rate=rate,
            reorder_level=level, reorder_qty=qty, status="active")
    return iid


def _seed_sle(conn, item_id, warehouse_id, qty, rate,
              posting_date="2026-01-15", cancelled=0):
    sid = _uuid()
    value = str(Decimal(qty) * Decimal(rate))
    _insert(conn, "stock_ledger_entry", id=sid, posting_date=posting_date,
            item_id=item_id, warehouse_id=warehouse_id, actual_qty=qty,
            qty_after_transaction=qty, valuation_rate=rate,
            stock_value=value, stock_value_difference=value,
            voucher_type="stock_entry", voucher_id="INIT-%s" % sid[:8],
            is_cancelled=cancelled)
    return sid


def _seed_moving_avg_row(conn, item_id, warehouse_id, qty, qty_after,
                       valuation_rate, stock_value, diff, incoming_rate,
                       posting_date="2026-01-15", cancelled=0):
    sid = _uuid()
    _insert(conn, "stock_ledger_entry", id=sid, posting_date=posting_date,
            item_id=item_id, warehouse_id=warehouse_id, actual_qty=qty,
            qty_after_transaction=qty_after, valuation_rate=valuation_rate,
            stock_value=stock_value, stock_value_difference=diff,
            incoming_rate=incoming_rate,
            voucher_type="stock_entry", voucher_id="INIT-%s" % sid[:8],
            is_cancelled=cancelled)
    return sid


def _fresh_rows(db_path, table):
    vconn = get_connection(db_path)
    try:
        return _rows(vconn, table)
    finally:
        vconn.close()


def _audit_new_ids(conn):
    a = Table("audit_log")
    q = Q.from_(a).select(a.id)
    return {r["id"] for r in conn.execute(q.get_sql(), ()).fetchall()}


def _audit_row(db_path, audit_id):
    vconn = get_connection(db_path)
    try:
        a = Table("audit_log")
        q = Q.from_(a).select(a.star).where(a.id == P())
        return _norm(vconn.execute(q.get_sql(), (audit_id,)).fetchone())
    finally:
        vconn.close()


# ── 1. list-warehouses ───────────────────────────────────────────────────────

class TestListWarehousesStrong:
    """Deepens TestListWarehouses.test_list in test_items_warehouses.py
    (asserted only total_count >= 2). The weight is now on the exact ordered
    name list, the company-scoping decoy and the type-filter decoy below."""

    def test_lists_exact_company_rows_with_filters_and_decoys(self, conn, db_path):
        ca = _seed_company(conn, "Harbor", "HB")
        cb = _seed_company(conn, "Lakeside", "LK")
        # Seeded out of alphabetical order on purpose: the name-ordering
        # assertions below only pass when the action orders by name.
        _seed_warehouse(conn, ca, "Beta Transit", "transit")
        _seed_warehouse(conn, ca, "Alpha Stores", "stores")
        _seed_warehouse(conn, cb, "Gamma Rejected", "rejected")
        _seed_warehouse(conn, cb, "Alpha Stores", "stores")
        before = _snapshot(conn)

        assert seam.table_exists("warehouse", db_path)
        r = call_action(mod.list_warehouses, conn, _ns(company_id=ca))
        assert is_ok(r), r
        # Derived 1: alphabetical ordering decided by the action.
        assert [w["name"] for w in r["warehouses"]] == ["Alpha Stores", "Beta Transit"]
        # Derived 2: the count under no filter.
        assert r["total_count"] == 2
        assert r["has_more"] is False

        # The Lakeside "Alpha Stores" shares its name exactly: if the action
        # read across companies it would surface here. total_count == 2 and
        # the name list above prove it did not.
        vconn = get_connection(db_path)
        try:
            w = Table("warehouse")
            q = (Q.from_(w).select(w.id, w.name, w.warehouse_type, w.company_id)
                 .where(w.company_id == P()).orderby(w.name))
            stored = [_norm(x) for x in vconn.execute(q.get_sql(), (ca,)).fetchall()]
        finally:
            vconn.close()
        assert [(s["name"], s["warehouse_type"]) for s in stored] == [
            ("Alpha Stores", "stores"), ("Beta Transit", "transit")]
        assert [s["company_id"] for s in stored] == [ca, ca]
        assert [w["name"] for w in r["warehouses"]] == [s["name"] for s in stored]

        # Type filter: "Alpha Stores" differs in exactly the filtered column,
        # so it is the decoy that proves the filter ran.
        f = call_action(mod.list_warehouses, conn,
                        _ns(company_id=ca, warehouse_type="transit"))
        assert is_ok(f), f
        assert f["total_count"] == 1
        assert [w["name"] for w in f["warehouses"]] == ["Beta Transit"]

        # Scoping holds the other way too.
        b = call_action(mod.list_warehouses, conn, _ns(company_id=cb))
        assert is_ok(b), b
        assert [w["name"] for w in b["warehouses"]] == ["Alpha Stores", "Gamma Rejected"]

        assert _snapshot(conn) == before, "a read must write nothing"

    def test_unknown_company_refused_exactly_and_writes_nothing(self, conn, db_path):
        _seed_company(conn, "Harbor", "HB")
        before = _snapshot(conn)
        r = call_action(mod.list_warehouses, conn,
                        _ns(company_name="No Such Company"))
        assert r.get("status") != "ok"
        assert r.get("error") == "Company 'No Such Company' not found."
        assert _snapshot(conn) == before

    def test_bad_limit_refused_exactly_and_writes_nothing(self, conn, db_path):
        ca = _seed_company(conn, "Harbor", "HB")
        _seed_warehouse(conn, ca, "Alpha Stores", "stores")
        before = _snapshot(conn)
        for bad in ("abc", "-1", "0"):
            r = call_action(mod.list_warehouses, conn,
                            _ns(company_id=ca, limit=bad, offset="0"))
            assert is_error(r), bad
            assert r["message"] == "--limit must be a positive integer", bad
        assert _snapshot(conn) == before

    def test_bad_offset_refused_exactly_and_writes_nothing(self, conn, db_path):
        ca = _seed_company(conn, "Harbor", "HB")
        _seed_warehouse(conn, ca, "Alpha Stores", "stores")
        before = _snapshot(conn)
        for bad in ("xyz", "-2"):
            r = call_action(mod.list_warehouses, conn,
                            _ns(company_id=ca, limit="20", offset=bad))
            assert is_error(r), bad
            assert r["message"] == "--offset must be a non-negative integer", bad
        assert _snapshot(conn) == before


# ── 1b. list-items paging ────────────────────────────────────────────────────

class TestListItemsPagingStrong:
    """Pins paging validation on list-items: a non-integer, zero or
    negative limit and a negative or non-integer offset are refused with
    the exact messages below, and the database is unchanged."""

    def test_bad_paging_refused_exactly_and_writes_nothing(self, conn, db_path):
        _seed_item(conn, "M625-PAGE", "Paged Widget", rate="10.00")
        before = _snapshot(conn)
        assert seam.table_exists("item", db_path)
        for bad in ("many", "0", "-1"):
            r = call_action(mod.list_items, conn, _ns(limit=bad, offset="0"))
            assert is_error(r), bad
            assert r["message"] == "--limit must be a positive integer", bad
        for bad in ("-1", "x"):
            r = call_action(mod.list_items, conn, _ns(limit="20", offset=bad))
            assert is_error(r), bad
            assert r["message"] == "--offset must be a non-negative integer", bad
        assert _snapshot(conn) == before


# ── 2. stock-balance (alias dispatch key) ────────────────────────────────────

class TestStockBalanceStrong:
    """Deepens TestStockBalanceReport.test_report in
    test_reports_recon_reval.py (asserted only row_count >= 1 and a positive
    total). The weight is now on the hand-computed money literals below.
    Covers the literal action name ``stock-balance`` through the dispatch
    table, and pins that it is the same handler as
    ``stock-balance-report``."""

    def test_company_totals_match_hand_computed_money(self, conn, db_path):
        ca = _seed_company(conn, "Harbor", "HB")
        cb = _seed_company(conn, "Lakeside", "LK")
        iid = _seed_item(conn, "M625-BAL", "Balance Widget", rate="25.00")
        w1 = _seed_warehouse(conn, ca, "Abaco Main")
        w2 = _seed_warehouse(conn, ca, "Zulu Overflow")
        wb = _seed_warehouse(conn, cb, "Lakeside Main")
        _seed_sle(conn, iid, w1, "60", "25.00")
        _seed_sle(conn, iid, w1, "40", "25.00")
        _seed_sle(conn, iid, w2, "10", "25.00")
        # Decoys: a cancelled row (differs in exactly the liveness column)
        # and a second-company row (differs in exactly the company column).
        _seed_sle(conn, iid, w1, "999", "25.00", cancelled=1)
        _seed_sle(conn, iid, wb, "5", "25.00")
        before = _snapshot(conn)

        assert mod.ACTIONS["stock-balance"] is mod.ACTIONS["stock-balance-report"]
        assert seam.table_exists("stock_ledger_entry", db_path)
        r = call_action(mod.ACTIONS["stock-balance"], conn, _ns(company_id=ca))
        assert is_ok(r), r
        # Hand: Abaco 60 + 40 = 100 @ 25.00 = 2500.00;
        # Zulu 10 @ 25.00 = 250.00; total 2750.00. Cancelled 999 and
        # Lakeside 5 must not move any of these.
        assert r["row_count"] == 2
        assert [x["warehouse_name"] for x in r["report"]] == ["Abaco Main", "Zulu Overflow"]
        abaco, zulu = r["report"]
        assert abaco["qty"] == "100.00"
        assert abaco["valuation_rate"] == "25.00"
        assert abaco["stock_value"] == "2500.00"
        assert zulu["qty"] == "10.00"
        assert zulu["valuation_rate"] == "25.00"
        assert zulu["stock_value"] == "250.00"
        assert r["total_stock_value"] == "2750.00"

        # Independent recompute from the seam on a fresh connection.
        vconn = get_connection(db_path)
        try:
            s = Table("stock_ledger_entry")
            q = (Q.from_(s).select(s.warehouse_id, s.actual_qty)
                 .where(s.item_id == P()).where(s.is_cancelled == 0))
            live = vconn.execute(q.get_sql(), (iid,)).fetchall()
            w = Table("warehouse")
            wq = Q.from_(w).select(w.id, w.company_id).where(w.id == P())
            live_ca = [x for x in live
                       if vconn.execute(wq.get_sql(), (x["warehouse_id"],)).fetchone()["company_id"] == ca]
        finally:
            vconn.close()
        totals = {}
        for x in live_ca:
            totals[x["warehouse_id"]] = totals.get(x["warehouse_id"], Decimal("0")) + Decimal(str(x["actual_qty"]))
        assert totals[w1] == Decimal("100")
        assert totals[w2] == Decimal("10")
        assert sum(totals.values(), Decimal("0")) * Decimal("25.00") == Decimal("2750.00")

        assert _snapshot(conn) == before, "a read must write nothing"

    def test_unknown_company_refused_exactly_and_writes_nothing(self, conn, db_path):
        _seed_company(conn, "Harbor", "HB")
        before = _snapshot(conn)
        r = call_action(mod.ACTIONS["stock-balance"], conn,
                        _ns(company_name="No Such Company"))
        assert r.get("status") != "ok"
        assert r.get("error") == "Company 'No Such Company' not found."
        assert _snapshot(conn) == before

    def test_differing_rates_use_running_average(self, conn, db_path):
        # A moving-average ledger the way the product writes one: each row's
        # valuation_rate is the running average AFTER that row, not the
        # incoming rate. Receive 60 @ 20.00 (qty after 60, rate 20.00), then
        # 40 @ 25.00 (qty after 100, rate 22.00, since
        # (1200.00 + 1000.00) / 100), then a later CANCELLED receipt of
        # 10 @ 99.00 (rate 29.00) that must not move the balance.
        ca = _seed_company(conn, "Harbor", "HB")
        iid = _seed_item(conn, "M625-AVG", "Average Widget", rate="22.00")
        w1 = _seed_warehouse(conn, ca, "Average WH")
        _seed_moving_avg_row(conn, iid, w1, "60", "60", "20.00",
                             "1200.00", "1200.00", "20.00",
                             posting_date="2026-01-10")
        _seed_moving_avg_row(conn, iid, w1, "40", "100", "22.00",
                             "2200.00", "1000.00", "25.00",
                             posting_date="2026-01-15")
        _seed_moving_avg_row(conn, iid, w1, "10", "110", "29.00",
                             "3190.00", "990.00", "99.00",
                             posting_date="2026-01-20", cancelled=1)
        before = _snapshot(conn)

        assert seam.table_exists("stock_ledger_entry", db_path)
        r = call_action(mod.ACTIONS["stock-balance"], conn, _ns(company_id=ca))
        assert is_ok(r), r
        assert r["row_count"] == 1
        (row,) = r["report"]
        assert row["qty"] == "100.00"
        assert row["valuation_rate"] == "22.00"
        assert row["stock_value"] == "2200.00"
        assert r["total_stock_value"] == "2200.00"
        # The first row's rate would give 2000.00, the cancelled row's rate
        # 2900.00, and the mean of the incoming rates (22.50) 2250.00: each
        # of those literals must fail this test.
        assert row["valuation_rate"] not in ("20.00", "29.00", "22.50")
        assert row["stock_value"] not in ("2000.00", "2900.00", "2250.00")

        assert _snapshot(conn) == before, "a read must write nothing"


# ── 3. stock-balance-report (warehouse filter) ───────────────────────────────

class TestStockBalanceReportStrong:
    """Deepens TestStockBalanceReport.test_report_by_warehouse in
    test_reports_recon_reval.py (asserted only row_count >= 1). The weight is
    now on the filtered money literal and the warehouse decoy below."""

    def test_warehouse_filter_carries_exact_money(self, conn, db_path):
        ca = _seed_company(conn, "Harbor", "HB")
        iid = _seed_item(conn, "M625-FLT", "Filter Widget", rate="12.50")
        wa = _seed_warehouse(conn, ca, "Filter A")
        wb = _seed_warehouse(conn, ca, "Filter B")
        _seed_sle(conn, iid, wa, "30", "12.50")
        _seed_sle(conn, iid, wb, "20", "12.50")
        before = _snapshot(conn)

        assert seam.table_exists("stock_ledger_entry", db_path)
        r = call_action(mod.ACTIONS["stock-balance-report"], conn,
                        _ns(company_id=ca, warehouse_id=wa))
        assert is_ok(r), r
        # Hand: 30 @ 12.50 = 375.00. Filter B holds 20 @ 12.50 = 250.00 and
        # differs in exactly the filtered column, so row_count == 1 proves
        # the filter ran.
        assert r["row_count"] == 1
        (row,) = r["report"]
        assert row["warehouse_id"] == wa
        assert row["item_name"] == "Filter Widget"
        assert row["qty"] == "30.00"
        assert row["valuation_rate"] == "12.50"
        assert row["stock_value"] == "375.00"
        assert r["total_stock_value"] == "375.00"

        vconn = get_connection(db_path)
        try:
            s = Table("stock_ledger_entry")
            q = (Q.from_(s).select(s.actual_qty, s.valuation_rate)
                 .where(s.item_id == P()).where(s.warehouse_id == P())
                 .where(s.is_cancelled == 0))
            rows = vconn.execute(q.get_sql(), (iid, wa)).fetchall()
        finally:
            vconn.close()
        assert sum((Decimal(str(x["actual_qty"])) for x in rows), Decimal("0")) == Decimal("30")
        assert rows[0]["valuation_rate"] == "12.50"

        assert _snapshot(conn) == before, "a read must write nothing"

    def test_differing_rates_use_running_average(self, conn, db_path):
        # Same moving-average ledger as the stock-balance probe above: the
        # row rate is the running average after that row, so the live balance
        # is 100 @ 22.00 = 2200.00 and the later cancelled 10 @ 99.00
        # (rate 29.00) must not move it.
        ca = _seed_company(conn, "Harbor", "HB")
        iid = _seed_item(conn, "M625-AVGR", "Average R Widget", rate="22.00")
        w1 = _seed_warehouse(conn, ca, "Average R WH")
        _seed_moving_avg_row(conn, iid, w1, "60", "60", "20.00",
                             "1200.00", "1200.00", "20.00",
                             posting_date="2026-01-10")
        _seed_moving_avg_row(conn, iid, w1, "40", "100", "22.00",
                             "2200.00", "1000.00", "25.00",
                             posting_date="2026-01-15")
        _seed_moving_avg_row(conn, iid, w1, "10", "110", "29.00",
                             "3190.00", "990.00", "99.00",
                             posting_date="2026-01-20", cancelled=1)
        before = _snapshot(conn)

        assert seam.table_exists("stock_ledger_entry", db_path)
        r = call_action(mod.ACTIONS["stock-balance-report"], conn,
                        _ns(company_id=ca, warehouse_id=w1))
        assert is_ok(r), r
        assert r["row_count"] == 1
        (row,) = r["report"]
        assert row["qty"] == "100.00"
        assert row["valuation_rate"] == "22.00"
        assert row["stock_value"] == "2200.00"
        assert r["total_stock_value"] == "2200.00"
        assert row["valuation_rate"] not in ("20.00", "29.00", "22.50")
        assert row["stock_value"] not in ("2000.00", "2900.00", "2250.00")

        assert _snapshot(conn) == before, "a read must write nothing"

    def test_unknown_company_refused_exactly_and_writes_nothing(self, conn, db_path):
        _seed_company(conn, "Harbor", "HB")
        before = _snapshot(conn)
        r = call_action(mod.ACTIONS["stock-balance-report"], conn,
                        _ns(company_name="No Such Company"))
        assert r.get("status") != "ok"
        assert r.get("error") == "Company 'No Such Company' not found."
        assert _snapshot(conn) == before


# ── 4. stock-ledger-report ───────────────────────────────────────────────────

class TestStockLedgerReportStrong:
    """Deepens TestStockLedgerReport.test_report_by_item in
    test_reports_recon_reval.py (asserted only count >= 1). The weight is now
    on the exact echoed money strings, the date-descending order and the
    three decoys below. The handler has no refusal branch: an unknown filter
    is a successful empty page, pinned exactly."""

    def test_item_and_date_filters_carry_exact_stored_values(self, conn, db_path):
        ca = _seed_company(conn, "Harbor", "HB")
        i1 = _seed_item(conn, "M625-LDG1", "Ledger One", rate="50.00")
        i2 = _seed_item(conn, "M625-LDG2", "Ledger Two", rate="9.00")
        w = _seed_warehouse(conn, ca, "Ledger WH")
        _seed_sle(conn, i1, w, "100", "50.00", posting_date="2026-01-10")
        _seed_sle(conn, i1, w, "-20", "50.00", posting_date="2026-02-15")
        _seed_sle(conn, i2, w, "7", "9.00", posting_date="2026-01-12")
        _seed_sle(conn, i1, w, "999", "50.00", posting_date="2026-01-20", cancelled=1)
        _seed_sle(conn, i1, w, "3", "50.00", posting_date="2026-05-01")
        before = _snapshot(conn)

        assert seam.table_exists("stock_ledger_entry", db_path)
        r = call_action(mod.stock_ledger_report, conn, _ns(
            item_id=i1, from_date="2026-01-01", to_date="2026-03-31"))
        assert is_ok(r), r
        # Derived: count under the item+date filter, and newest-first order
        # decided by the action.
        assert r["count"] == 2
        assert [e["posting_date"] for e in r["entries"]] == ["2026-02-15", "2026-01-10"]
        first, second = r["entries"]
        assert first["actual_qty"] == "-20"
        assert first["valuation_rate"] == "50.00"
        assert second["actual_qty"] == "100"
        assert second["stock_value"] == "5000.00"
        # The item decoy (Ledger Two, differs in item_id), the cancelled row
        # (differs in liveness) and the May row (differs in posting_date)
        # must all be absent.
        assert {e["item_id"] for e in r["entries"]} == {i1}
        assert all(e["actual_qty"] != "999" for e in r["entries"])
        assert all(e["posting_date"] != "2026-05-01" for e in r["entries"])

        vconn = get_connection(db_path)
        try:
            s = Table("stock_ledger_entry")
            q = (Q.from_(s).select(s.actual_qty, s.valuation_rate, s.stock_value)
                 .where(s.item_id == P()).where(s.posting_date >= P())
                 .where(s.posting_date <= P()).where(s.is_cancelled == 0)
                 .orderby(s.posting_date, order=Order.desc))
            stored = [_norm(x) for x in
                      vconn.execute(q.get_sql(), (i1, "2026-01-01", "2026-03-31")).fetchall()]
        finally:
            vconn.close()
        assert [(x["actual_qty"], x["valuation_rate"]) for x in stored] == [
            ("-20", "50.00"), ("100", "50.00")]
        assert [e["actual_qty"] for e in r["entries"]] == [x["actual_qty"] for x in stored]

        assert _snapshot(conn) == before, "a read must write nothing"

    def test_company_id_scopes_ledger_to_one_company(self, conn, db_path):
        ca = _seed_company(conn, "Harbor", "HB")
        cb = _seed_company(conn, "Lakeside", "LK")
        i1 = _seed_item(conn, "M625-SCP1", "Scoped One", rate="10.00")
        wa = _seed_warehouse(conn, ca, "Harbor WH")
        wb = _seed_warehouse(conn, cb, "Lakeside WH")
        _seed_sle(conn, i1, wa, "5", "10.00", posting_date="2026-01-10")
        _seed_sle(conn, i1, wb, "7", "10.00", posting_date="2026-01-11")
        r = call_action(mod.stock_ledger_report, conn, _ns(company_id=ca))
        assert is_ok(r), r
        assert r["count"] == 1
        assert [e["warehouse_id"] for e in r["entries"]] == [wa]
        r2 = call_action(mod.stock_ledger_report, conn, _ns(company_id=cb))
        assert is_ok(r2), r2
        assert r2["count"] == 1
        assert [e["warehouse_id"] for e in r2["entries"]] == [wb]
        before = _snapshot(conn)
        rall = call_action(mod.stock_ledger_report, conn, _ns())
        assert rall["status"] == "error"
        assert rall["error"] == "Multiple companies found. Please specify the company by name."
        assert rall["message"] == "Multiple companies found. Please specify the company by name."
        assert rall["suggestion"] == "Pass the company name (e.g. --company \"Acme\"), or use --company-id with one of the IDs above."
        assert {c["id"] for c in rall["companies"]} == {ca, cb}
        assert _snapshot(conn) == before, "a read must write nothing"

    def test_unknown_item_is_an_empty_ok_page_not_a_refusal(self, conn, db_path):
        ca = _seed_company(conn, "Harbor", "HB")
        i1 = _seed_item(conn, "M625-LDG1", "Ledger One", rate="50.00")
        w = _seed_warehouse(conn, ca, "Ledger WH")
        _seed_sle(conn, i1, w, "100", "50.00")
        before = _snapshot(conn)
        r = call_action(mod.stock_ledger_report, conn, _ns(item_id="nothing-there"))
        assert is_ok(r), r
        assert r["entries"] == []
        assert r["count"] == 0
        assert _snapshot(conn) == before


# ── 5. update-item ───────────────────────────────────────────────────────────

class TestUpdateItemStrong:
    """Deepens TestUpdateItem tests in test_items_warehouses.py (asserted
    only response shape and error status). The weight is now on the
    whole-row before/after comparison and the audit content below."""

    def _seed_for_update(self, conn):
        iid = _seed_item(conn, "M625-UPD", "Before Name", rate="10.00",
                         level="5", qty="2")
        sql, params = dynamic_update("item", {"updated_at": "2020-01-01 00:00:00"},
                                     where={"id": iid})
        conn.execute(sql, params)
        conn.commit()
        return iid

    def test_update_rewrites_exact_columns_and_audits(self, conn, db_path):
        iid = self._seed_for_update(conn)
        assert seam.table_exists("item", db_path)
        assert seam.table_exists("audit_log", db_path)
        vconn = get_connection(db_path)
        try:
            t = Table("item")
            q = Q.from_(t).select(t.star).where(t.id == P())
            before = _norm(vconn.execute(q.get_sql(), (iid,)).fetchone())
        finally:
            vconn.close()
        assert before["updated_at"] == "2020-01-01 00:00:00"
        audit_before = _audit_new_ids(conn)
        ledger_before = _rows(conn, "stock_ledger_entry")
        gl_before = _rows(conn, "gl_entry")
        wh_before = _rows(conn, "warehouse")

        r = call_action(mod.update_item, conn, _ns(
            item_id=iid, item_name="After Name",
            standard_rate="42.50", reorder_level="200"))
        assert is_ok(r), r
        assert r["updated_fields"] == ["item_name", "reorder_level", "standard_rate"]

        vconn = get_connection(db_path)
        try:
            after = _norm(vconn.execute(q.get_sql(), (iid,)).fetchone())
        finally:
            vconn.close()
        allowed = {"item_name", "standard_rate", "reorder_level", "updated_at"}
        assert {k: v for k, v in after.items() if k not in allowed} == \
               {k: v for k, v in before.items() if k not in allowed}
        assert after["item_name"] == "After Name"
        assert after["standard_rate"] == "42.50"
        assert after["reorder_level"] == "200.00"
        assert after["updated_at"] != before["updated_at"]
        datetime.fromisoformat(after["updated_at"])
        assert after["created_at"] == before["created_at"]

        new_ids = _audit_new_ids(conn) - audit_before
        assert len(new_ids) == 1
        (audit_id,) = new_ids
        audit = _audit_row(db_path, audit_id)
        assert audit["skill"] == "erpclaw-inventory"
        assert audit["action"] == "update-item"
        assert audit["entity_type"] == "item"
        assert audit["entity_id"] == iid
        assert json.loads(audit["old_values"]) == {
            "item_name": "Before Name", "standard_rate": "10.00",
            "reorder_level": "5"}
        assert json.loads(audit["new_values"]) == {
            "item_name": "After Name", "standard_rate": "42.50",
            "reorder_level": "200.00"}

        assert _rows(conn, "stock_ledger_entry") == ledger_before
        assert _rows(conn, "gl_entry") == gl_before
        assert _rows(conn, "warehouse") == wh_before

    def test_unknown_id_refused_exactly_and_writes_nothing(self, conn, db_path):
        self._seed_for_update(conn)
        before = _snapshot(conn)
        ghost = "m625-ghost-item"
        r = call_action(mod.update_item, conn, _ns(item_id=ghost, item_name="Nope"))
        assert is_error(r)
        assert r["message"] == "Item %s not found" % ghost
        assert _snapshot(conn) == before

    def test_disabled_item_refused_exactly_and_writes_nothing(self, conn, db_path):
        iid = self._seed_for_update(conn)
        assert is_ok(call_action(mod.update_item, conn,
                                 _ns(item_id=iid, item_status="disabled")))
        before = _snapshot(conn)
        r = call_action(mod.update_item, conn, _ns(item_id=iid, item_name="Blocked"))
        assert is_error(r)
        assert r["message"] == "Cannot update a disabled item (set --status active first)"
        assert _snapshot(conn) == before

    def test_bad_status_refused_exactly_and_writes_nothing(self, conn, db_path):
        iid = self._seed_for_update(conn)
        before = _snapshot(conn)
        r = call_action(mod.update_item, conn, _ns(item_id=iid, item_status="bogus"))
        assert is_error(r)
        assert r["message"] == "--status must be 'active' or 'disabled'"
        assert _snapshot(conn) == before

    def test_no_fields_refused_exactly_and_writes_nothing(self, conn, db_path):
        iid = self._seed_for_update(conn)
        before = _snapshot(conn)
        r = call_action(mod.update_item, conn, _ns(item_id=iid))
        assert is_error(r)
        assert r["message"] == "No fields to update"
        assert _snapshot(conn) == before

    def test_malformed_standard_rate_should_refuse(self, conn, db_path):
        iid = self._seed_for_update(conn)
        before = _snapshot(conn)
        for bad in ("not-a-number", "-1"):
            r = call_action(mod.update_item, conn,
                            _ns(item_id=iid, standard_rate=bad))
            assert is_error(r), bad
            assert r["message"] == "--standard-rate must be a non-negative number", bad
            assert _snapshot(conn) == before, bad

    def test_standard_rate_rounds_half_up_to_two_places(self, conn, db_path):
        iid = self._seed_for_update(conn)
        r = call_action(mod.update_item, conn,
                        _ns(item_id=iid, standard_rate="42.5"))
        assert is_ok(r), r
        vconn = get_connection(db_path)
        try:
            t = Table("item")
            q = Q.from_(t).select(t.standard_rate).where(t.id == P())
            first = _norm(vconn.execute(q.get_sql(), (iid,)).fetchone())
        finally:
            vconn.close()
        assert first["standard_rate"] == "42.50"
        r = call_action(mod.update_item, conn,
                        _ns(item_id=iid, standard_rate="42.505"))
        assert is_ok(r), r
        vconn = get_connection(db_path)
        try:
            second = _norm(vconn.execute(q.get_sql(), (iid,)).fetchone())
        finally:
            vconn.close()
        assert second["standard_rate"] == "42.51"

    def test_malformed_reorder_level_should_refuse(self, conn, db_path):
        iid = self._seed_for_update(conn)
        before = _snapshot(conn)
        for bad in ("lots", "-3"):
            r = call_action(mod.update_item, conn,
                            _ns(item_id=iid, reorder_level=bad))
            assert is_error(r), bad
            assert r["message"] == "--reorder-level must be a non-negative number", bad
            assert _snapshot(conn) == before, bad

    def test_reorder_level_quantizes_to_two_places(self, conn, db_path):
        iid = self._seed_for_update(conn)
        r = call_action(mod.update_item, conn,
                        _ns(item_id=iid, reorder_level="42.5"))
        assert is_ok(r), r
        vconn = get_connection(db_path)
        try:
            t = Table("item")
            q = Q.from_(t).select(t.reorder_level).where(t.id == P())
            first = _norm(vconn.execute(q.get_sql(), (iid,)).fetchone())
        finally:
            vconn.close()
        assert first["reorder_level"] == "42.50"
        r = call_action(mod.update_item, conn,
                        _ns(item_id=iid, reorder_level="42.505"))
        assert is_ok(r), r
        vconn = get_connection(db_path)
        try:
            second = _norm(vconn.execute(q.get_sql(), (iid,)).fetchone())
        finally:
            vconn.close()
        assert second["reorder_level"] == "42.51"

    def test_bad_reorder_qty_refused_exactly_and_writes_nothing(self, conn, db_path):
        iid = self._seed_for_update(conn)
        before = _snapshot(conn)
        for bad in ("many", "-4"):
            r = call_action(mod.update_item, conn,
                            _ns(item_id=iid, reorder_qty=bad))
            assert is_error(r), bad
            assert r["message"] == "--reorder-qty must be a non-negative number", bad
            assert _snapshot(conn) == before, bad

    def test_reorder_qty_quantizes_to_two_places(self, conn, db_path):
        iid = self._seed_for_update(conn)
        r = call_action(mod.update_item, conn,
                        _ns(item_id=iid, reorder_qty="42.5"))
        assert is_ok(r), r
        vconn = get_connection(db_path)
        try:
            t = Table("item")
            q = Q.from_(t).select(t.reorder_qty).where(t.id == P())
            row = _norm(vconn.execute(q.get_sql(), (iid,)).fetchone())
        finally:
            vconn.close()
        assert row["reorder_qty"] == "42.50"

    def test_non_finite_standard_rate_refused_exactly_and_writes_nothing(
            self, conn, db_path):
        iid = self._seed_for_update(conn)
        before = _snapshot(conn)
        for bad in ("NaN", "sNaN", "Infinity", "1e40"):
            r = call_action(mod.update_item, conn,
                            _ns(item_id=iid, standard_rate=bad))
            assert is_error(r), bad
            assert r["message"] == "--standard-rate must be a non-negative number", bad
            assert _snapshot(conn) == before, bad

    def test_non_finite_reorder_level_refused_exactly_and_writes_nothing(
            self, conn, db_path):
        iid = self._seed_for_update(conn)
        before = _snapshot(conn)
        for bad in ("NaN", "sNaN", "Infinity", "1e40"):
            r = call_action(mod.update_item, conn,
                            _ns(item_id=iid, reorder_level=bad))
            assert is_error(r), bad
            assert r["message"] == "--reorder-level must be a non-negative number", bad
            assert _snapshot(conn) == before, bad

    def test_non_finite_reorder_qty_refused_exactly_and_writes_nothing(
            self, conn, db_path):
        iid = self._seed_for_update(conn)
        before = _snapshot(conn)
        for bad in ("NaN", "sNaN", "Infinity", "1e40"):
            r = call_action(mod.update_item, conn,
                            _ns(item_id=iid, reorder_qty=bad))
            assert is_error(r), bad
            assert r["message"] == "--reorder-qty must be a non-negative number", bad
            assert _snapshot(conn) == before, bad


# ── 6. update-warehouse ──────────────────────────────────────────────────────

class TestUpdateWarehouseStrong:
    """Deepens TestUpdateWarehouse.test_update_name in
    test_items_warehouses.py (asserted only the updated_fields shape). The
    weight is now on the whole-row before/after comparison, the audit
    content and the cross-company account probe below."""

    def _seed_for_update(self, conn):
        ca = _seed_company(conn, "Harbor", "HB")
        cb = _seed_company(conn, "Lakeside", "LK")
        a1 = _seed_account(conn, ca, "Stock A")
        a2 = _seed_account(conn, ca, "Stock A2")
        b1 = _seed_account(conn, cb, "Stock B")
        wid = _seed_warehouse(conn, ca, "M625 Depot", "stores", a1)
        sql, params = dynamic_update("warehouse", {"updated_at": "2020-01-01 00:00:00"},
                                     where={"id": wid})
        conn.execute(sql, params)
        conn.commit()
        return ca, cb, a1, a2, b1, wid

    def test_update_rewrites_exact_columns_and_audits(self, conn, db_path):
        ca, cb, a1, a2, b1, wid = self._seed_for_update(conn)
        assert seam.table_exists("warehouse", db_path)
        assert seam.table_exists("audit_log", db_path)
        vconn = get_connection(db_path)
        try:
            t = Table("warehouse")
            q = Q.from_(t).select(t.star).where(t.id == P())
            before = _norm(vconn.execute(q.get_sql(), (wid,)).fetchone())
        finally:
            vconn.close()
        assert before["updated_at"] == "2020-01-01 00:00:00"
        audit_before = _audit_new_ids(conn)
        ledger_before = _rows(conn, "stock_ledger_entry")
        gl_before = _rows(conn, "gl_entry")
        item_before = _rows(conn, "item")

        r = call_action(mod.update_warehouse, conn, _ns(
            warehouse_id=wid, name="M625 Depot Renamed", account_id=a2))
        assert is_ok(r), r
        assert r["warehouse_id"] == wid
        assert r["updated_fields"] == ["name", "account_id"]

        vconn = get_connection(db_path)
        try:
            after = _norm(vconn.execute(q.get_sql(), (wid,)).fetchone())
        finally:
            vconn.close()
        allowed = {"name", "account_id", "updated_at"}
        assert {k: v for k, v in after.items() if k not in allowed} == \
               {k: v for k, v in before.items() if k not in allowed}
        assert after["name"] == "M625 Depot Renamed"
        assert after["account_id"] == a2
        assert after["company_id"] == ca
        assert after["updated_at"] != before["updated_at"]
        datetime.fromisoformat(after["updated_at"])

        new_ids = _audit_new_ids(conn) - audit_before
        assert len(new_ids) == 1
        (audit_id,) = new_ids
        audit = _audit_row(db_path, audit_id)
        assert audit["skill"] == "erpclaw-inventory"
        assert audit["action"] == "update-warehouse"
        assert audit["entity_type"] == "warehouse"
        assert audit["entity_id"] == wid
        assert json.loads(audit["old_values"]) == {
            "name": "M625 Depot", "account_id": a1}
        assert json.loads(audit["new_values"]) == {
            "name": "M625 Depot Renamed", "account_id": a2}

        assert _rows(conn, "stock_ledger_entry") == ledger_before
        assert _rows(conn, "gl_entry") == gl_before
        assert _rows(conn, "item") == item_before

    def test_cross_company_account_refused_exactly_and_writes_nothing(self, conn, db_path):
        ca, cb, a1, a2, b1, wid = self._seed_for_update(conn)
        before = _snapshot(conn)
        r = call_action(mod.update_warehouse, conn, _ns(warehouse_id=wid, account_id=b1))
        assert is_error(r)
        assert r["message"] == (
            "Account %s belongs to company %s, not to the "
            "warehouse's company %s" % (b1, cb, ca))
        assert _snapshot(conn) == before

    def test_add_warehouse_cross_company_account_refused_and_writes_nothing(
            self, conn, db_path):
        ca, cb, a1, a2, b1, wid = self._seed_for_update(conn)
        before = _snapshot(conn)
        r = call_action(mod.add_warehouse, conn, _ns(
            name="Foreign Account WH", company_id=ca, account_id=b1))
        assert is_error(r)
        assert r["message"] == (
            "Account %s belongs to company %s, not to the "
            "warehouse's company %s" % (b1, cb, ca))
        assert _snapshot(conn) == before

    def test_ambiguous_name_refused_exactly_and_writes_nothing(self, conn, db_path):
        ca = _seed_company(conn, "Harbor", "HB")
        cb = _seed_company(conn, "Lakeside", "LK")
        a1 = _seed_account(conn, ca, "Stock A")
        b1 = _seed_account(conn, cb, "Stock B")
        _seed_warehouse(conn, cb, "Main", "stores", b1)
        _seed_warehouse(conn, ca, "Main", "stores", a1)
        before = _snapshot(conn)
        r = call_action(mod.update_warehouse, conn,
                        _ns(warehouse_id="Main", name="Renamed"))
        assert is_error(r)
        assert r["message"] == ("Warehouse name 'Main' matches more than one "
                                "warehouse; pass the warehouse id or --company-id")
        assert _snapshot(conn) == before

    def test_ambiguous_name_with_company_id_refused_exactly_and_writes_nothing(
            self, conn, db_path):
        ca = _seed_company(conn, "Harbor", "HB")
        cb = _seed_company(conn, "Lakeside", "LK")
        a1 = _seed_account(conn, ca, "Stock A")
        b1 = _seed_account(conn, cb, "Stock B")
        _seed_warehouse(conn, ca, "Main", "stores", a1)
        _seed_warehouse(conn, ca, "Main", "stores", a1)
        _seed_warehouse(conn, cb, "Main", "stores", b1)
        before = _snapshot(conn)
        r = call_action(mod.update_warehouse, conn,
                        _ns(warehouse_id="Main", company_id=ca, name="Renamed"))
        assert is_error(r)
        assert r["message"] == ("Warehouse name 'Main' matches more than one "
                                "warehouse in company %s; pass the warehouse id" % ca)
        assert _snapshot(conn) == before

    def test_name_lookup_scoped_by_company_id(self, conn, db_path):
        ca = _seed_company(conn, "Harbor", "HB")
        cb = _seed_company(conn, "Lakeside", "LK")
        a1 = _seed_account(conn, ca, "Stock A")
        b1 = _seed_account(conn, cb, "Stock B")
        other = _seed_warehouse(conn, ca, "Main", "stores", a1)
        target = _seed_warehouse(conn, cb, "Main", "stores", b1)
        vconn = get_connection(db_path)
        try:
            other_before = _fresh_rows(db_path, "warehouse")
        finally:
            vconn.close()
        r = call_action(mod.update_warehouse, conn,
                        _ns(warehouse_id="Main", company_id=cb, name="Second Main"))
        assert is_ok(r), r
        assert r["warehouse_id"] == target
        vconn = get_connection(db_path)
        try:
            t = Table("warehouse")
            q = Q.from_(t).select(t.star).where(t.id == P())
            after_target = _norm(vconn.execute(q.get_sql(), (target,)).fetchone())
            after_other = _norm(vconn.execute(q.get_sql(), (other,)).fetchone())
        finally:
            vconn.close()
        assert after_target["name"] == "Second Main"
        before_other = [w for w in other_before if w["id"] == other][0]
        assert after_other == before_other

    def test_id_of_other_company_refused_exactly_and_writes_nothing(
            self, conn, db_path):
        ca = _seed_company(conn, "Harbor", "HB")
        cb = _seed_company(conn, "Lakeside", "LK")
        a1 = _seed_account(conn, ca, "Stock A")
        b1 = _seed_account(conn, cb, "Stock B")
        other = _seed_warehouse(conn, ca, "Main", "stores", a1)
        _seed_warehouse(conn, cb, "Main", "stores", b1)
        before = _snapshot(conn)
        r = call_action(mod.update_warehouse, conn,
                        _ns(warehouse_id=other, company_id=cb, name="Sneaky"))
        assert is_error(r)
        assert r["message"] == "Warehouse %s not found" % other
        assert _snapshot(conn) == before

    def test_unknown_id_refused_exactly_and_writes_nothing(self, conn, db_path):
        self._seed_for_update(conn)
        before = _snapshot(conn)
        ghost = "m625-ghost-warehouse"
        r = call_action(mod.update_warehouse, conn, _ns(warehouse_id=ghost, name="Nope"))
        assert is_error(r)
        assert r["message"] == "Warehouse %s not found" % ghost
        assert _snapshot(conn) == before

    def test_unknown_account_refused_exactly_and_writes_nothing(self, conn, db_path):
        ca, cb, a1, a2, b1, wid = self._seed_for_update(conn)
        before = _snapshot(conn)
        ghost = "m625-ghost-account"
        r = call_action(mod.update_warehouse, conn, _ns(warehouse_id=wid, account_id=ghost))
        assert is_error(r)
        assert r["message"] == "Account %s not found" % ghost
        assert _snapshot(conn) == before

    def test_no_fields_refused_exactly_and_writes_nothing(self, conn, db_path):
        ca, cb, a1, a2, b1, wid = self._seed_for_update(conn)
        before = _snapshot(conn)
        r = call_action(mod.update_warehouse, conn, _ns(warehouse_id=wid))
        assert is_error(r)
        assert r["message"] == "No fields to update"
        assert _snapshot(conn) == before
