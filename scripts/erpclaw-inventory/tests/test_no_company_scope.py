"""Stock reads answer for one company: ledger report, balance, projected qty.

A list or report that takes an optional company answers for exactly one
company through resolve_scope_company. A warehouse anchors the scope:
stock-ledger-report with --warehouse-id and no company keeps today's query,
while get-stock-balance and get-projected-qty always take a warehouse and
never refuse for a missing company. An explicitly given company must exist
and, for the two warehouse reads, must own the warehouse. Projected quantity
counts only the warehouse's own company orders.
"""
import json

from inventory_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
    seed_stock_entry_sle, _uuid,
)
from erpclaw_lib.query import Q, P, Table, insert_row

mod = load_db_query()

ZERO_ERROR = "No company found. Create one first."
ZERO_SUGGESTION = "Run 'tutorial' to create a demo company, or 'setup company' to create your own."
MULTI_ERROR = "Multiple companies found. Please specify the company by name."
MULTI_SUGGESTION = 'Pass the company name (e.g. --company "Acme"), or use --company-id with one of the IDs above.'
UNKNOWN_ID = "no-such-company"

_LEDGER_DEFAULTS = dict(
    item_id=None, warehouse_id=None, from_date=None, to_date=None,
    company_id=None, company_name=None, limit=None, offset=None,
)


def _lns(**kw):
    d = dict(_LEDGER_DEFAULTS)
    d.update(kw)
    return ns(**d)


_STATE_TABLES = (
    "stock_ledger_entry", "stock_entry",
    "purchase_order", "purchase_order_item",
    "sales_order", "sales_order_item",
    "stock_reservation_entry", "company", "audit_log",
)


def _norm(row):
    return {k: (None if v is None else str(v)) for k, v in dict(row).items()}


def _state(conn):
    out = {}
    for name in _STATE_TABLES:
        tbl = Table(name)
        rows = conn.execute(Q.from_(tbl).select(tbl.star).get_sql()).fetchall()
        out[name] = sorted(json.dumps(_norm(r), sort_keys=True) for r in rows)
    return out


def _insert(conn, table, **data):
    markers = {k: P() for k in data}
    sql, cols = insert_row(table, markers)
    conn.execute(sql, [data[c] for c in cols])


def _seed_two_companies(conn):
    acme = _uuid()
    wayne = _uuid()
    _insert(conn, "company", id=acme, name="Acme Widgets", abbr="ACME",
            default_currency="USD", country="United States",
            fiscal_year_start_month=1)
    _insert(conn, "company", id=wayne, name="Wayne Enterprises", abbr="WAYNE",
            default_currency="USD", country="United States",
            fiscal_year_start_month=1)
    item = _uuid()
    _insert(conn, "item", id=item, item_code="WIDGET", item_name="Widget",
            item_type="stock", stock_uom="Each", standard_rate="10.00",
            status="active", is_stock_item=1)
    wa = _uuid()
    ww = _uuid()
    _insert(conn, "warehouse", id=wa, name="Acme Main", company_id=acme)
    _insert(conn, "warehouse", id=ww, name="Wayne Main", company_id=wayne)
    conn.commit()
    seed_stock_entry_sle(conn, item, wa, "10", "10.00")
    seed_stock_entry_sle(conn, item, ww, "3", "20.00")
    return {"acme": acme, "wayne": wayne, "item": item,
            "wa": wa, "ww": ww}


def _seed_acme_only(conn):
    acme = _uuid()
    _insert(conn, "company", id=acme, name="Acme Widgets", abbr="ACME",
            default_currency="USD", country="United States",
            fiscal_year_start_month=1)
    item = _uuid()
    _insert(conn, "item", id=item, item_code="WIDGET", item_name="Widget",
            item_type="stock", stock_uom="Each", standard_rate="10.00",
            status="active", is_stock_item=1)
    wa = _uuid()
    _insert(conn, "warehouse", id=wa, name="Acme Main", company_id=acme)
    conn.commit()
    seed_stock_entry_sle(conn, item, wa, "10", "10.00")
    return {"acme": acme, "item": item, "wa": wa}


def _seed_wayne_orders(conn, env):
    sup = _uuid()
    _insert(conn, "supplier", id=sup, name="Wayne Supplier",
            company_id=env["wayne"])
    po = _uuid()
    _insert(conn, "purchase_order", id=po, supplier_id=sup,
            order_date="2026-01-15", status="confirmed",
            company_id=env["wayne"])
    _insert(conn, "purchase_order_item", id=_uuid(), purchase_order_id=po,
            item_id=env["item"], quantity="100", received_qty="0",
            warehouse_id=None, rate="10.00", amount="1000.00",
            net_amount="1000.00")
    cus = _uuid()
    _insert(conn, "customer", id=cus, name="Wayne Customer",
            company_id=env["wayne"])
    so = _uuid()
    _insert(conn, "sales_order", id=so, customer_id=cus,
            order_date="2026-01-15", status="confirmed",
            company_id=env["wayne"])
    _insert(conn, "sales_order_item", id=_uuid(), sales_order_id=so,
            item_id=env["item"], quantity="4", delivered_qty="0",
            warehouse_id=None, rate="20.00", amount="80.00",
            net_amount="80.00")
    conn.commit()


def test_ledger_zero_companies_refuses(conn):
    """No company in the install refuses with the zero-company dict."""
    before = _state(conn)
    r = call_action(mod.stock_ledger_report, conn, _lns())
    assert r == {"status": "error", "error": ZERO_ERROR,
                 "message": ZERO_ERROR, "suggestion": ZERO_SUGGESTION}
    assert _state(conn) == before


def test_ledger_two_companies_no_company_refuses(conn):
    """Two companies and no company argument refuses listing both."""
    env = _seed_two_companies(conn)
    before = _state(conn)
    r = call_action(mod.stock_ledger_report, conn, _lns())
    assert r == {
        "status": "error",
        "error": MULTI_ERROR,
        "message": MULTI_ERROR,
        "companies": [
            {"id": env["acme"], "name": "Acme Widgets"},
            {"id": env["wayne"], "name": "Wayne Enterprises"},
        ],
        "suggestion": MULTI_SUGGESTION,
    }
    assert _state(conn) == before


def test_ledger_unknown_company_refuses(conn):
    """An explicit company id that does not exist refuses exactly."""
    env = _seed_two_companies(conn)
    before = _state(conn)
    r = call_action(mod.stock_ledger_report, conn,
                    _lns(company_id=UNKNOWN_ID))
    assert r == {"status": "error",
                 "error": "Company not found: %s" % UNKNOWN_ID,
                 "message": "Company not found: %s" % UNKNOWN_ID}
    assert _state(conn) == before


def test_ledger_warehouse_anchor(conn):
    """Guard: warehouse with no company keeps today's query (no refusal)."""
    env = _seed_two_companies(conn)
    before = _state(conn)
    r = call_action(mod.stock_ledger_report, conn,
                    _lns(warehouse_id=env["ww"]))
    assert is_ok(r), r
    assert r["count"] == 1
    assert r["entries"][0]["warehouse_id"] == env["ww"]
    assert r["entries"][0]["actual_qty"] == "3"
    assert _state(conn) == before


def test_ledger_explicit_company(conn):
    """Guard: an explicit company scopes the ledger to that company."""
    env = _seed_two_companies(conn)
    before = _state(conn)
    r = call_action(mod.stock_ledger_report, conn,
                    _lns(company_id=env["wayne"]))
    assert is_ok(r), r
    assert r["count"] == 1
    assert r["entries"][0]["warehouse_id"] == env["ww"]
    assert r["entries"][0]["actual_qty"] == "3"
    assert _state(conn) == before


def test_ledger_one_company_uses_it(conn):
    """Guard: a sole company is used with or without the argument."""
    env = _seed_acme_only(conn)
    before = _state(conn)
    r_none = call_action(mod.stock_ledger_report, conn, _lns())
    assert is_ok(r_none), r_none
    r_id = call_action(mod.stock_ledger_report, conn,
                       _lns(company_id=env["acme"]))
    assert is_ok(r_id), r_id
    assert r_none == r_id
    assert r_none["count"] == 1
    assert r_none["entries"][0]["warehouse_id"] == env["wa"]
    assert _state(conn) == before


def test_projected_ignores_other_company_orders(conn):
    """Acme Main counts only Acme orders: 10.00/0.00/0.00/10.00.

    Before the fix the same call answered ordered 100.00, reserved 4.00
    and projected 106.00 from Wayne's confirmed orders.
    """
    env = _seed_two_companies(conn)
    _seed_wayne_orders(conn, env)
    before = _state(conn)
    r = call_action(mod.get_projected_qty, conn,
                    ns(item_id=env["item"], warehouse_id=env["wa"]))
    assert is_ok(r), r
    assert r["actual_qty"] == "10.00"
    assert r["ordered_qty"] == "0.00"
    assert r["reserved_qty"] == "0.00"
    assert r["projected_qty"] == "10.00"
    assert _state(conn) == before


def test_projected_counts_own_company_orders(conn):
    """Guard: Wayne Main counts Wayne's own orders: 3.00/100.00/4.00/99.00."""
    env = _seed_two_companies(conn)
    _seed_wayne_orders(conn, env)
    before = _state(conn)
    r = call_action(mod.get_projected_qty, conn,
                    ns(item_id=env["item"], warehouse_id=env["ww"]))
    assert is_ok(r), r
    assert r["actual_qty"] == "3.00"
    assert r["ordered_qty"] == "100.00"
    assert r["reserved_qty"] == "4.00"
    assert r["projected_qty"] == "99.00"
    assert _state(conn) == before


def test_projected_company_must_own_warehouse(conn):
    """A company that does not own the warehouse is refused exactly."""
    env = _seed_two_companies(conn)
    _seed_wayne_orders(conn, env)
    before = _state(conn)
    r = call_action(mod.get_projected_qty, conn,
                    ns(item_id=env["item"], warehouse_id=env["wa"],
                       company_id=env["wayne"]))
    assert is_error(r), r
    assert r["message"] == "Warehouse %s belongs to another company" % env["wa"]
    assert _state(conn) == before
    r = call_action(mod.get_projected_qty, conn,
                    ns(item_id=env["item"], warehouse_id=env["wa"],
                       company_id=UNKNOWN_ID))
    assert r == {"status": "error",
                 "error": "Company not found: %s" % UNKNOWN_ID,
                 "message": "Company not found: %s" % UNKNOWN_ID}
    assert _state(conn) == before
    r_none = call_action(mod.get_projected_qty, conn,
                         ns(item_id=env["item"], warehouse_id=env["wa"]))
    assert is_ok(r_none), r_none
    r_own = call_action(mod.get_projected_qty, conn,
                        ns(item_id=env["item"], warehouse_id=env["wa"],
                           company_id=env["acme"]))
    assert r_own == r_none
    assert r_none["projected_qty"] == "10.00"
    assert _state(conn) == before


def test_balance_company_must_own_warehouse(conn):
    """Balance pins today's 10-on-hand dict and the ownership refusal."""
    env = _seed_two_companies(conn)
    before = _state(conn)
    r_none = call_action(mod.get_stock_balance_action, conn,
                         ns(item_id=env["item"], warehouse_id=env["wa"]))
    assert is_ok(r_none), r_none
    assert r_none["qty"] == "10.00"
    assert r_none["valuation_rate"] == "10.00"
    assert r_none["stock_value"] == "100.00"
    assert _state(conn) == before
    r_own = call_action(mod.get_stock_balance_action, conn,
                        ns(item_id=env["item"], warehouse_id=env["wa"],
                           company_id=env["acme"]))
    assert r_own == r_none
    assert _state(conn) == before
    r = call_action(mod.get_stock_balance_action, conn,
                    ns(item_id=env["item"], warehouse_id=env["wa"],
                       company_id=env["wayne"]))
    assert is_error(r), r
    assert r["message"] == "Warehouse %s belongs to another company" % env["wa"]
    assert _state(conn) == before
    r = call_action(mod.get_stock_balance_action, conn,
                    ns(item_id=env["item"], warehouse_id=env["wa"],
                       company_id=UNKNOWN_ID))
    assert r == {"status": "error",
                 "error": "Company not found: %s" % UNKNOWN_ID,
                 "message": "Company not found: %s" % UNKNOWN_ID}
    assert _state(conn) == before
