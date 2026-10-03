"""A purchase document refuses a supplier of another company (refusal only).

Covers add-purchase-order, create-purchase-invoice (standalone, from purchase
order, from purchase receipt) and add-recurring-bill-template: a document of
company A naming a supplier whose company_id differs is refused before any
write, with ``Supplier <id> belongs to another company``.
"""
import json
import uuid

from buying_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
    build_buying_env, seed_supplier,
)
from erpclaw_lib.query import P, Q, Table

mod = load_db_query()

STATE_TABLES = (
    "purchase_order", "purchase_order_item",
    "purchase_invoice", "purchase_invoice_item",
    "naming_series", "gl_entry", "payment_ledger_entry",
    "stock_ledger_entry", "audit_log",
)
TEMPLATE_TABLES = (
    "recurring_bill_template", "recurring_bill_template_item",
)


def _items(env, *specs):
    """Build items JSON. Each spec = (item_key, qty, rate)."""
    return json.dumps([
        {"item_id": env[k], "qty": q, "rate": r,
         "warehouse_id": env["warehouse"]}
        for k, q, r in specs
    ])


def _two_company_env(conn):
    """Two full buying envs plus an Acme and a Wayne supplier."""
    acme = build_buying_env(conn)
    wayne = build_buying_env(conn)
    own = seed_supplier(conn, acme["company_id"], "Acme Supplier")
    foreign = seed_supplier(conn, wayne["company_id"], "Wayne Supplier")
    return acme, wayne, own, foreign


def _state(conn, extra=()):
    snap = {}
    for table in STATE_TABLES + tuple(extra):
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        snap[table] = sorted(
            tuple(None if v is None else str(v) for v in r) for r in rows)
    return snap


def _po_ns(supplier_id, company_id, items):
    return ns(supplier_id=supplier_id, company_id=company_id,
              posting_date="2026-06-15", items=items,
              tax_template_id=None, name=None)


def _invoice_ns(supplier_id=None, company_id=None, items=None,
                purchase_order_id=None, purchase_receipt_id=None):
    return ns(purchase_order_id=purchase_order_id,
              purchase_receipt_id=purchase_receipt_id,
              supplier_id=supplier_id, company_id=company_id,
              posting_date="2026-06-20", due_date=None,
              items=items, tax_template_id=None)


def test_purchase_order_refuses_foreign_supplier(conn):
    acme, wayne, own, foreign = _two_company_env(conn)
    before = _state(conn)
    result = call_action(mod.add_purchase_order, conn, _po_ns(
        foreign, acme["company_id"], _items(acme, ("item1", "1", "10.00"))))
    assert is_error(result)
    assert result["message"] == f"Supplier {foreign} belongs to another company"
    assert _state(conn) == before


def test_standalone_purchase_invoice_refuses_foreign_supplier(conn):
    acme, wayne, own, foreign = _two_company_env(conn)
    before = _state(conn)
    result = call_action(mod.create_purchase_invoice, conn, _invoice_ns(
        supplier_id=foreign, company_id=acme["company_id"],
        items=_items(acme, ("item1", "1", "10.00"))))
    assert is_error(result)
    assert result["message"] == f"Supplier {foreign} belongs to another company"
    assert _state(conn) == before


def _insert_mixed_purchase_order(conn, acme, foreign):
    """Legacy shape: a confirmed Acme order naming Wayne's supplier."""
    po_id = str(uuid.uuid4())
    po_t = Table("purchase_order")
    q = (Q.into(po_t)
         .columns("id", "supplier_id", "order_date", "total_amount",
                  "tax_amount", "grand_total", "status", "company_id")
         .insert(P(), P(), P(), P(), P(), P(), P(), P()))
    conn.execute(q.get_sql(), (po_id, foreign, "2026-06-15",
                               "10.00", "0.00", "10.00", "confirmed",
                               acme["company_id"]))
    poi_t = Table("purchase_order_item")
    qi = (Q.into(poi_t)
          .columns("id", "purchase_order_id", "item_id", "quantity",
                   "invoiced_qty", "uom", "rate", "amount",
                   "discount_percentage", "net_amount", "warehouse_id")
          .insert(P(), P(), P(), P(), P(), P(), P(), P(), P(), P(), P()))
    conn.execute(qi.get_sql(), (str(uuid.uuid4()), po_id, acme["item1"],
                                "1", "0", "Each", "10.00", "10.00",
                                "0", "10.00", acme["warehouse"]))
    conn.commit()
    return po_id


def test_purchase_invoice_from_mixed_order_refuses(conn):
    acme, wayne, own, foreign = _two_company_env(conn)
    po_id = _insert_mixed_purchase_order(conn, acme, foreign)
    before = _state(conn)
    result = call_action(mod.create_purchase_invoice, conn, _invoice_ns(
        purchase_order_id=po_id))
    assert is_error(result)
    assert result["message"] == f"Supplier {foreign} belongs to another company"
    assert _state(conn) == before


def _insert_mixed_purchase_receipt(conn, acme, foreign):
    """Legacy shape: a submitted Acme receipt naming Wayne's supplier."""
    pr_id = str(uuid.uuid4())
    pr_t = Table("purchase_receipt")
    q = (Q.into(pr_t)
         .columns("id", "supplier_id", "posting_date", "status", "company_id")
         .insert(P(), P(), P(), P(), P()))
    conn.execute(q.get_sql(), (pr_id, foreign, "2026-06-20", "submitted",
                               acme["company_id"]))
    pri_t = Table("purchase_receipt_item")
    qi = (Q.into(pri_t)
          .columns("id", "purchase_receipt_id", "item_id", "quantity", "uom",
                   "warehouse_id", "rate", "amount")
          .insert(P(), P(), P(), P(), P(), P(), P(), P()))
    conn.execute(qi.get_sql(), (str(uuid.uuid4()), pr_id, acme["item1"],
                                "1", "Each", acme["warehouse"],
                                "10.00", "10.00"))
    conn.commit()
    return pr_id


def test_purchase_invoice_from_mixed_receipt_refuses(conn):
    acme, wayne, own, foreign = _two_company_env(conn)
    pr_id = _insert_mixed_purchase_receipt(conn, acme, foreign)
    before = _state(conn)
    result = call_action(mod.create_purchase_invoice, conn, _invoice_ns(
        purchase_receipt_id=pr_id))
    assert is_error(result)
    assert result["message"] == f"Supplier {foreign} belongs to another company"
    assert _state(conn) == before


def test_own_supplier_accepted(conn):
    acme, wayne, own, foreign = _two_company_env(conn)
    items = _items(acme, ("item1", "1", "10.00"))
    assert is_ok(call_action(mod.add_purchase_order, conn, _po_ns(
        own, acme["company_id"], items)))
    result = call_action(mod.create_purchase_invoice, conn, _invoice_ns(
        supplier_id=own, company_id=acme["company_id"], items=items))
    assert is_ok(result), result
    assert result["grand_total"] == "10.00"


def test_recurring_bill_template_refuses_foreign_supplier(conn):
    acme, wayne, own, foreign = _two_company_env(conn)
    before = _state(conn, TEMPLATE_TABLES)
    result = call_action(mod.add_recurring_bill_template, conn, ns(
        supplier_id=foreign, company_id=acme["company_id"],
        items=_items(acme, ("item1", "1", "10.00")), frequency="monthly",
        start_date="2026-01-01", end_date=None,
        tax_template_id=None, auto_submit=False))
    assert is_error(result)
    assert result["message"] == f"Supplier {foreign} belongs to another company"
    assert _state(conn, TEMPLATE_TABLES) == before
