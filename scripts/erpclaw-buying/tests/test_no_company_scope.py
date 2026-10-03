"""Company scope for buying lists and status (task m791)."""
import io
import json
import os
import sys
import uuid
from unittest.mock import patch

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from buying_helpers import call_action, is_ok, is_error, load_db_query, ns  # noqa: E402
from erpclaw_lib.query import Q, Table  # noqa: E402

B = load_db_query()

STATE_TABLES = (
    "supplier",
    "material_request",
    "request_for_quotation",
    "purchase_order",
    "purchase_receipt",
    "purchase_invoice",
    "blanket_order",
    "recurring_bill_template",
    "company",
    "audit_log",
)

ZERO_REFUSAL = {
    "status": "error",
    "error": "No company found. Create one first.",
    "message": "No company found. Create one first.",
    "suggestion": "Run 'tutorial' to create a demo company, or 'setup company' to create your own.",
}

UNKNOWN_REFUSAL = {
    "status": "error",
    "error": "Company not found: no-such-company",
    "message": "Company not found: no-such-company",
}

MULTI_ERROR = "Multiple companies found. Please specify the company by name."
MULTI_SUGGESTION = "Pass the company name (e.g. --company \"Acme\"), or use --company-id with one of the IDs above."
NAME_MISS_SUGGESTION = "Use one of the available company names exactly, or run 'list-companies' to see them."

ACTIONS = [
    "list-suppliers",
    "list-material-requests",
    "list-rfqs",
    "list-purchase-orders",
    "list-purchase-receipts",
    "list-purchase-invoices",
    "list-blanket-pos",
    "list-recurring-bill-templates",
    "status",
]

_FN = {
    "list-suppliers": "list_suppliers",
    "list-material-requests": "list_material_requests",
    "list-rfqs": "list_rfqs",
    "list-purchase-orders": "list_purchase_orders",
    "list-purchase-receipts": "list_purchase_receipts",
    "list-purchase-invoices": "list_purchase_invoices",
    "list-blanket-pos": "list_blanket_pos",
    "list-recurring-bill-templates": "list_recurring_bill_templates",
    "status": "status_action",
}


def _insert_company(conn, name, abbr):
    cid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO company (id, name, abbr, default_currency, country, fiscal_year_start_month)"
        " VALUES (?, ?, ?, 'USD', 'United States', 1)",
        (cid, name, abbr),
    )
    conn.commit()
    return cid


def _insert_supplier(conn, company_id, name):
    sid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO supplier (id, name, company_id, supplier_type, status)"
        " VALUES (?, ?, ?, 'company', 'active')",
        (sid, name, company_id),
    )
    conn.commit()
    return sid


def _insert_item(conn, name="Test Item"):
    iid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO item (id, item_name, item_code, stock_uom, is_stock_item)"
        " VALUES (?, ?, ?, 'Each', 1)",
        (iid, name, "ITEM-%s" % iid[:6]),
    )
    conn.commit()
    return iid


def _seed_two(conn):
    acme = _insert_company(conn, "Acme Widgets", "ACME")
    wayne = _insert_company(conn, "Wayne Enterprises", "WAYNE")
    acme_sup = _insert_supplier(conn, acme, "Acme Supplier")
    wayne_sup = _insert_supplier(conn, wayne, "Downtown Properties LLC")
    item = _insert_item(conn)
    r1 = call_action(
        B.add_purchase_order, conn, ns(
            supplier_id=acme_sup, company_id=acme, posting_date="2026-06-15",
            items=json.dumps([{"item_id": item, "qty": "1", "rate": "50.00"}]),
            tax_template_id=None, name=None))
    assert is_ok(r1), r1
    r2 = call_action(
        B.add_purchase_order, conn, ns(
            supplier_id=wayne_sup, company_id=wayne, posting_date="2026-06-15",
            items=json.dumps([{"item_id": item, "qty": "1", "rate": "70.00"}]),
            tax_template_id=None, name=None))
    assert is_ok(r2), r2
    return {"acme": acme, "wayne": wayne, "acme_supplier": acme_sup,
            "wayne_supplier": wayne_sup, "item": item}


def _seed_one(conn):
    acme = _insert_company(conn, "Acme Widgets", "ACME")
    acme_sup = _insert_supplier(conn, acme, "Acme Supplier")
    item = _insert_item(conn)
    r1 = call_action(
        B.add_purchase_order, conn, ns(
            supplier_id=acme_sup, company_id=acme, posting_date="2026-06-15",
            items=json.dumps([{"item_id": item, "qty": "1", "rate": "50.00"}]),
            tax_template_id=None, name=None))
    assert is_ok(r1), r1
    return {"acme": acme, "acme_supplier": acme_sup, "item": item}


def _all(conn, table):
    t = Table(table)
    q = Q.from_(t).select(t.star).orderby(t.id)
    return [dict(r) for r in conn.execute(q.get_sql()).fetchall()]


def _state(conn):
    return {name: _all(conn, name) for name in STATE_TABLES}


def _call(conn, action, company_id=None, company_name=None):
    fn = getattr(B, _FN[action])
    args = ns(
        company_id=company_id, company_name=company_name,
        supplier_group=None, search=None, request_type=None, mr_status=None,
        rfq_status=None, supplier_id=None, po_status=None, pr_status=None,
        pi_status=None, blanket_status=None, template_status=None,
        from_date=None, to_date=None, limit=None, offset=None,
    )
    return call_action(fn, conn, args)


def _multi_refusal(acme, wayne):
    return {
        "status": "error",
        "error": MULTI_ERROR,
        "message": MULTI_ERROR,
        "companies": [
            {"id": acme, "name": "Acme Widgets"},
            {"id": wayne, "name": "Wayne Enterprises"},
        ],
        "suggestion": MULTI_SUGGESTION,
    }


@pytest.mark.parametrize("action", ACTIONS)
def test_zero_companies_refuses(conn, action):
    before = _state(conn)
    r = _call(conn, action, company_id=None, company_name=None)
    assert r == ZERO_REFUSAL, r
    assert _state(conn) == before


@pytest.mark.parametrize("action", ACTIONS)
def test_two_companies_no_company_refuses(conn, action):
    ids = _seed_two(conn)
    before = _state(conn)
    r = _call(conn, action, company_id=None, company_name=None)
    assert r == _multi_refusal(ids["acme"], ids["wayne"]), r
    assert _state(conn) == before


@pytest.mark.parametrize("action", ACTIONS)
def test_unknown_company_refuses(conn, action):
    _seed_two(conn)
    before = _state(conn)
    r = _call(conn, action, company_id="no-such-company", company_name=None)
    assert r == UNKNOWN_REFUSAL, r
    assert _state(conn) == before


def test_explicit_second_company_scopes(conn):
    ids = _seed_two(conn)
    r = call_action(B.list_suppliers, conn, ns(
        company_id=ids["wayne"], company_name=None, supplier_group=None,
        search=None, limit=None, offset=None))
    assert is_ok(r), r
    assert [s["name"] for s in r["suppliers"]] == ["Downtown Properties LLC"]
    p = call_action(B.list_purchase_orders, conn, ns(
        company_id=ids["wayne"], company_name=None, supplier_id=None,
        po_status=None, from_date=None, to_date=None, limit=None, offset=None))
    assert is_ok(p), p
    assert p["total_count"] == 1
    assert p["purchase_orders"][0]["grand_total"] == "70.00"


def test_one_company_uses_it(conn):
    ids = _seed_one(conn)
    acme = ids["acme"]
    for action in ACTIONS:
        bare = _call(conn, action, company_id=None, company_name=None)
        scoped = _call(conn, action, company_id=acme, company_name=None)
        assert is_ok(bare), (action, bare)
        assert bare == scoped, action


def test_list_suppliers_by_company_name(conn):
    ids = _seed_two(conn)
    r = call_action(B.list_suppliers, conn, ns(
        company_id=None, company_name="Wayne Enterprises",
        supplier_group=None, search=None, limit=None, offset=None))
    assert is_ok(r), r
    assert [s["name"] for s in r["suppliers"]] == ["Downtown Properties LLC"]


def test_company_flag_resolves_name(conn):
    ids = _seed_two(conn)
    acme = ids["acme"]
    wayne = ids["wayne"]
    args = ns(company_id=None, company_name="wayne enterprises")
    B._resolve_company_flag(conn, args)
    assert args.company_id == wayne
    args2 = ns(company_id=None, company_name=wayne)
    B._resolve_company_flag(conn, args2)
    assert args2.company_id == wayne
    args3 = ns(company_id=None, company_name=acme)
    B._resolve_company_flag(conn, args3)
    assert args3.company_id == acme
    buf = io.StringIO()

    def _fake_exit(code=0):
        raise SystemExit(code)

    with patch("sys.stdout", buf), patch("sys.exit", side_effect=_fake_exit):
        try:
            B._resolve_company_flag(
                conn, ns(company_id=None, company_name="Wayne"))
        except SystemExit:
            pass
    out = buf.getvalue().strip()
    got = json.loads(out)
    assert got == {
        "status": "error",
        "error": "Company 'Wayne' not found.",
        "message": "Company 'Wayne' not found.",
        "available_companies": ["Acme Widgets", "Wayne Enterprises"],
        "suggestion": NAME_MISS_SUGGESTION,
    }, got
