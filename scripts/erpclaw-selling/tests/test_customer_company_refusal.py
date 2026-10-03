"""A sales document refuses a customer of another company (refusal only).

Covers add-quotation, add-sales-order, create-sales-invoice (standalone,
from sales order, from delivery note) and add-recurring-template: a document
of company A naming a customer whose company_id differs is refused before
any write, with ``Customer <id> belongs to another company``. A foreign
party given BY NAME resolves only inside the document's company, so it
answers the existing not-found text instead.
"""
import json
import uuid

from selling_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
    build_selling_env, seed_customer,
)
from erpclaw_lib.query import P, Q, Table

mod = load_db_query()

STATE_TABLES = (
    "quotation", "quotation_item",
    "sales_order", "sales_order_item",
    "sales_invoice", "sales_invoice_item",
    "naming_series", "gl_entry", "payment_ledger_entry",
    "stock_ledger_entry", "audit_log",
)
TEMPLATE_TABLES = (
    "recurring_invoice_template", "recurring_invoice_template_item",
)


def _items(env, *specs):
    """Build items JSON. Each spec = (item_key, qty, rate)."""
    return json.dumps([
        {"item_id": env[k], "qty": q, "rate": r}
        for k, q, r in specs
    ])


def _two_company_env(conn):
    """Two full selling envs plus Bruce (Acme) and Alfred (Wayne)."""
    acme = build_selling_env(conn)
    wayne = build_selling_env(conn)
    bruce = seed_customer(conn, acme["company_id"], "Bruce Wayne")
    alfred = seed_customer(conn, wayne["company_id"], "Alfred Pennyworth")
    return acme, wayne, bruce, alfred


def _state(conn, extra=()):
    snap = {}
    for table in STATE_TABLES + tuple(extra):
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        snap[table] = sorted(
            tuple(None if v is None else str(v) for v in r) for r in rows)
    return snap


def _quotation_ns(customer_id, company_id, items):
    return ns(customer_id=customer_id, company_id=company_id,
              posting_date="2026-06-15", items=items,
              valid_till=None, tax_template_id=None)


def _so_ns(customer_id, company_id, items):
    return ns(customer_id=customer_id, company_id=company_id,
              posting_date="2026-06-15", items=items,
              delivery_date="2026-07-01", tax_template_id=None)


def _invoice_ns(customer_id=None, company_id=None, items=None,
                sales_order_id=None, delivery_note_id=None):
    return ns(sales_order_id=sales_order_id, delivery_note_id=delivery_note_id,
              customer_id=customer_id, company_id=company_id,
              posting_date="2026-06-20", due_date=None,
              items=items, tax_template_id=None, payment_terms_id=None)


def test_quotation_refuses_foreign_customer(conn):
    acme, wayne, bruce, alfred = _two_company_env(conn)
    before = _state(conn)
    result = call_action(mod.add_quotation, conn, _quotation_ns(
        alfred, acme["company_id"], _items(acme, ("item1", "1", "10.00"))))
    assert is_error(result)
    assert result["message"] == f"Customer {alfred} belongs to another company"
    assert _state(conn) == before


def test_sales_order_refuses_foreign_customer(conn):
    acme, wayne, bruce, alfred = _two_company_env(conn)
    before = _state(conn)
    result = call_action(mod.add_sales_order, conn, _so_ns(
        alfred, acme["company_id"], _items(acme, ("item1", "1", "10.00"))))
    assert is_error(result)
    assert result["message"] == f"Customer {alfred} belongs to another company"
    assert _state(conn) == before


def test_standalone_invoice_refuses_foreign_customer(conn):
    acme, wayne, bruce, alfred = _two_company_env(conn)
    before = _state(conn)
    result = call_action(mod.create_sales_invoice, conn, _invoice_ns(
        customer_id=alfred, company_id=acme["company_id"],
        items=_items(acme, ("item1", "1", "10.00"))))
    assert is_error(result)
    assert result["message"] == f"Customer {alfred} belongs to another company"
    assert _state(conn) == before


def _insert_mixed_sales_order(conn, acme, alfred):
    """Legacy shape: a confirmed Acme order naming Wayne's customer."""
    so_id = str(uuid.uuid4())
    so_t = Table("sales_order")
    q = (Q.into(so_t)
         .columns("id", "customer_id", "order_date", "total_amount",
                  "tax_amount", "grand_total", "status", "company_id")
         .insert(P(), P(), P(), P(), P(), P(), P(), P()))
    conn.execute(q.get_sql(), (so_id, alfred, "2026-06-15",
                               "10.00", "0.00", "10.00", "confirmed",
                               acme["company_id"]))
    soi_t = Table("sales_order_item")
    qi = (Q.into(soi_t)
          .columns("id", "sales_order_id", "item_id", "quantity",
                   "invoiced_qty", "uom", "rate", "amount",
                   "discount_percentage", "net_amount", "warehouse_id")
          .insert(P(), P(), P(), P(), P(), P(), P(), P(), P(), P(), P()))
    conn.execute(qi.get_sql(), (str(uuid.uuid4()), so_id, acme["item1"],
                                "1", "0", "Each", "10.00", "10.00",
                                "0", "10.00", acme["warehouse"]))
    conn.commit()
    return so_id


def test_invoice_from_mixed_order_refuses(conn):
    acme, wayne, bruce, alfred = _two_company_env(conn)
    so_id = _insert_mixed_sales_order(conn, acme, alfred)
    before = _state(conn)
    result = call_action(mod.create_sales_invoice, conn, _invoice_ns(
        sales_order_id=so_id))
    assert is_error(result)
    assert result["message"] == f"Customer {alfred} belongs to another company"
    assert _state(conn) == before


def _insert_mixed_delivery_note(conn, acme, alfred):
    """Legacy shape: a submitted Acme delivery note naming Wayne's customer."""
    dn_id = str(uuid.uuid4())
    dn_t = Table("delivery_note")
    q = (Q.into(dn_t)
         .columns("id", "customer_id", "posting_date", "status", "company_id")
         .insert(P(), P(), P(), P(), P()))
    conn.execute(q.get_sql(), (dn_id, alfred, "2026-06-20", "submitted",
                               acme["company_id"]))
    dni_t = Table("delivery_note_item")
    qi = (Q.into(dni_t)
          .columns("id", "delivery_note_id", "item_id", "quantity", "uom",
                   "warehouse_id", "rate", "amount")
          .insert(P(), P(), P(), P(), P(), P(), P(), P()))
    conn.execute(qi.get_sql(), (str(uuid.uuid4()), dn_id, acme["item1"],
                                "1", "Each", acme["warehouse"],
                                "10.00", "10.00"))
    conn.commit()
    return dn_id


def test_invoice_from_mixed_delivery_note_refuses(conn):
    acme, wayne, bruce, alfred = _two_company_env(conn)
    dn_id = _insert_mixed_delivery_note(conn, acme, alfred)
    before = _state(conn)
    result = call_action(mod.create_sales_invoice, conn, _invoice_ns(
        delivery_note_id=dn_id))
    assert is_error(result)
    assert result["message"] == f"Customer {alfred} belongs to another company"
    assert _state(conn) == before


def test_name_resolves_within_company(conn):
    acme, wayne, bruce, alfred = _two_company_env(conn)
    # Only Wayne has an Alfred: the scoped name lookup finds nothing in Acme.
    before = _state(conn)
    result = call_action(mod.add_sales_order, conn, _so_ns(
        "Alfred Pennyworth", acme["company_id"],
        _items(acme, ("item1", "1", "10.00"))))
    assert is_error(result)
    assert result["message"] == "Active customer Alfred Pennyworth not found"
    assert _state(conn) == before
    # Acme gains its own Alfred: the same name now resolves to Acme's row.
    acme_alfred = seed_customer(conn, acme["company_id"], "Alfred Pennyworth")
    result = call_action(mod.add_sales_order, conn, _so_ns(
        "Alfred Pennyworth", acme["company_id"],
        _items(acme, ("item1", "1", "10.00"))))
    assert is_ok(result), result
    row = conn.execute("SELECT customer_id FROM sales_order WHERE id=?",
                       (result["sales_order_id"],)).fetchone()
    assert row["customer_id"] == acme_alfred


def test_unknown_company_on_standalone_invoice(conn):
    acme, wayne, bruce, alfred = _two_company_env(conn)
    result = call_action(mod.create_sales_invoice, conn, _invoice_ns(
        customer_id=bruce, company_id="no-such-company",
        items=_items(acme, ("item1", "1", "10.00"))))
    assert is_error(result)
    assert result["message"] == "Company no-such-company not found"


def test_own_customer_accepted(conn):
    acme, wayne, bruce, alfred = _two_company_env(conn)
    items = _items(acme, ("item1", "1", "10.00"))
    assert is_ok(call_action(mod.add_quotation, conn, _quotation_ns(
        bruce, acme["company_id"], items)))
    assert is_ok(call_action(mod.add_sales_order, conn, _so_ns(
        bruce, acme["company_id"], items)))
    result = call_action(mod.create_sales_invoice, conn, _invoice_ns(
        customer_id=bruce, company_id=acme["company_id"], items=items))
    assert is_ok(result), result
    assert result["grand_total"] == "10.00"


def test_recurring_template_refuses_foreign_customer(conn):
    acme, wayne, bruce, alfred = _two_company_env(conn)
    before = _state(conn, TEMPLATE_TABLES)
    result = call_action(mod.add_recurring_template, conn, ns(
        customer_id=alfred, company_id=acme["company_id"],
        items=_items(acme, ("item1", "1", "10.00")), frequency="monthly",
        start_date="2026-01-01", end_date=None,
        tax_template_id=None, payment_terms_id=None))
    assert is_error(result)
    assert result["message"] == f"Customer {alfred} belongs to another company"
    assert _state(conn, TEMPLATE_TABLES) == before
