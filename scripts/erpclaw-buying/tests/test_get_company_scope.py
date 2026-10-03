"""get-supplier / get-purchase-invoice honour the company scope (m796).

With no company given both reads behave as before. With a company given
(by --company-id or --company) the company must exist
(`Company not found: <id>` via resolve_scope_company) and the record must
belong to it, else the exact refusal below. get-supplier also accepts a
name in the id argument; with a company given the name half is scoped to
that company. Every action here writes nothing.
"""
import uuid

from buying_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
    build_buying_env,
)
from erpclaw_lib.query import Q, P, Table

mod = load_db_query()

ACME_NAME = "Acme Widgets"
WAYNE_NAME = "Wayne Enterprises"

STATE_TABLES = (
    "company",
    "customer",
    "supplier",
    "sales_invoice",
    "sales_invoice_item",
    "purchase_invoice",
    "purchase_invoice_item",
    "stock_entry",
    "stock_entry_item",
    "item",
    "payment_ledger_entry",
    "gl_entry",
    "stock_ledger_entry",
    "audit_log",
)


def _state(conn):
    snap = {}
    for table in STATE_TABLES:
        try:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        except Exception:
            snap[table] = []
            continue
        snap[table] = sorted(
            tuple(None if v is None else str(v) for v in r) for r in rows)
    return snap


def _rename_companies(conn, acme_id, wayne_id):
    t = Table("company")
    for cid, name, abbr in ((acme_id, ACME_NAME, "ACME"),
                            (wayne_id, WAYNE_NAME, "WAYNE")):
        uq = (Q.update(t).set("name", P()).set("abbr", P())
              .where(t.id == P()))
        conn.execute(uq.get_sql(), (name, abbr, cid))
    conn.commit()


def _seed_customer(conn, company_id, name):
    cid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO customer (id, name, company_id, customer_type, status, credit_limit)"
        " VALUES (?, ?, ?, 'company', 'active', '0')",
        (cid, name, company_id))
    conn.commit()
    return cid


def _seed_supplier(conn, company_id, name):
    sid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO supplier (id, name, company_id, supplier_type, status)"
        " VALUES (?, ?, ?, 'company', 'active')",
        (sid, name, company_id))
    conn.commit()
    return sid


def _insert_sales_invoice(conn, company_id, customer_id, amount, item_id):
    si = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO sales_invoice (id, customer_id, posting_date, total_amount,"
        " grand_total, outstanding_amount, status, company_id)"
        " VALUES (?, ?, '2026-06-20', ?, ?, ?, 'submitted', ?)",
        (si, customer_id, amount, amount, amount, company_id))
    conn.execute(
        "INSERT INTO sales_invoice_item (id, sales_invoice_id, item_id,"
        " quantity, rate, amount, net_amount) VALUES (?, ?, ?, '1', ?, ?, ?)",
        (str(uuid.uuid4()), si, item_id, amount, amount, amount))
    conn.commit()
    return si


def _insert_purchase_invoice(conn, company_id, supplier_id, amount, item_id):
    pi = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO purchase_invoice (id, supplier_id, posting_date, total_amount,"
        " grand_total, outstanding_amount, status, company_id)"
        " VALUES (?, ?, '2026-06-20', ?, ?, ?, 'submitted', ?)",
        (pi, supplier_id, amount, amount, amount, company_id))
    conn.execute(
        "INSERT INTO purchase_invoice_item (id, purchase_invoice_id, item_id,"
        " quantity, rate, amount) VALUES (?, ?, ?, '1', ?, ?)",
        (str(uuid.uuid4()), pi, item_id, amount, amount))
    conn.commit()
    return pi


def _insert_stock_entry(conn, company_id, warehouse_id, item_id):
    se = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO stock_entry (id, stock_entry_type, posting_date, status,"
        " company_id, to_warehouse_id) VALUES (?, 'material_receipt',"
        " '2026-06-20', 'submitted', ?, ?)",
        (se, company_id, warehouse_id))
    conn.execute(
        "INSERT INTO stock_entry_item (id, stock_entry_id, item_id, quantity,"
        " to_warehouse_id, valuation_rate, amount)"
        " VALUES (?, ?, ?, '5', ?, '10.00', '50.00')",
        (str(uuid.uuid4()), se, item_id, warehouse_id))
    conn.commit()
    return se


def _fixture(conn):
    acme = build_buying_env(conn)
    wayne = build_buying_env(conn)
    _rename_companies(conn, acme["company_id"], wayne["company_id"])
    bruce = _seed_customer(conn, acme["company_id"], "Bruce Wayne")
    alfred_cust = _seed_customer(conn, wayne["company_id"], "Alfred Pennyworth")
    acme_sup = _seed_supplier(conn, acme["company_id"], "Acme Supplier")
    wayne_sup = _seed_supplier(conn, wayne["company_id"], "Wayne Supplier")
    acme_si = _insert_sales_invoice(conn, acme["company_id"], bruce, "100.00", acme["item1"])
    wayne_si = _insert_sales_invoice(conn, wayne["company_id"], alfred_cust, "150.00", wayne["item1"])
    acme_pi = _insert_purchase_invoice(conn, acme["company_id"], acme_sup, "50.00", acme["item1"])
    wayne_pi = _insert_purchase_invoice(conn, wayne["company_id"], wayne_sup, "70.00", wayne["item1"])
    _insert_stock_entry(conn, acme["company_id"], acme["warehouse"], acme["item1"])
    _insert_stock_entry(conn, wayne["company_id"], wayne["warehouse"], wayne["item1"])
    return {
        "acme": acme, "wayne": wayne,
        "acme_sup": acme_sup, "wayne_sup": wayne_sup,
        "acme_pi": acme_pi, "wayne_pi": wayne_pi,
    }


def _get_supplier_args(supplier_id, company_id=None):
    return ns(supplier_id=supplier_id, company_id=company_id, company_name=None)


def _get_pi_args(pi_id, company_id=None):
    return ns(purchase_invoice_id=pi_id, company_id=company_id, company_name=None)


def test_get_supplier_foreign_record_refuses(conn):
    f = _fixture(conn)
    before = _state(conn)
    result = call_action(mod.get_supplier, conn,
                         _get_supplier_args(f["acme_sup"], f["wayne"]["company_id"]))
    assert is_error(result)
    assert result["message"] == f"Supplier {f['acme_sup']} belongs to another company"
    assert _state(conn) == before


def test_get_supplier_unknown_company_refuses(conn):
    f = _fixture(conn)
    before = _state(conn)
    result = call_action(mod.get_supplier, conn,
                         _get_supplier_args(f["acme_sup"], "bogus"))
    assert result == {"status": "error", "error": "Company not found: bogus",
                      "message": "Company not found: bogus"}
    assert _state(conn) == before


def test_get_supplier_own_company_unchanged(conn):
    f = _fixture(conn)
    scoped = call_action(mod.get_supplier, conn,
                         _get_supplier_args(f["acme_sup"], f["acme"]["company_id"]))
    unscoped = call_action(mod.get_supplier, conn,
                           _get_supplier_args(f["acme_sup"], None))
    assert is_ok(scoped), scoped
    assert scoped == unscoped


def test_get_supplier_name_scoped_to_company(conn):
    f = _fixture(conn)
    acme_wayne = _seed_supplier(conn, f["acme"]["company_id"], "Wayne Supplier")
    got_acme = call_action(mod.get_supplier, conn,
                           _get_supplier_args("Wayne Supplier", f["acme"]["company_id"]))
    assert is_ok(got_acme), got_acme
    assert got_acme["id"] == acme_wayne
    got_wayne = call_action(mod.get_supplier, conn,
                            _get_supplier_args("Wayne Supplier", f["wayne"]["company_id"]))
    assert is_ok(got_wayne), got_wayne
    assert got_wayne["id"] == f["wayne_sup"]


def test_get_purchase_invoice_foreign_record_refuses(conn):
    f = _fixture(conn)
    before = _state(conn)
    result = call_action(mod.get_purchase_invoice, conn,
                         _get_pi_args(f["acme_pi"], f["wayne"]["company_id"]))
    assert is_error(result)
    assert result["message"] == f"Purchase invoice {f['acme_pi']} belongs to another company"
    assert _state(conn) == before


def test_get_purchase_invoice_unknown_company_refuses(conn):
    f = _fixture(conn)
    before = _state(conn)
    result = call_action(mod.get_purchase_invoice, conn,
                         _get_pi_args(f["acme_pi"], "bogus"))
    assert result == {"status": "error", "error": "Company not found: bogus",
                      "message": "Company not found: bogus"}
    assert _state(conn) == before


def test_get_purchase_invoice_own_company_unchanged(conn):
    f = _fixture(conn)
    scoped = call_action(mod.get_purchase_invoice, conn,
                         _get_pi_args(f["acme_pi"], f["acme"]["company_id"]))
    unscoped = call_action(mod.get_purchase_invoice, conn,
                           _get_pi_args(f["acme_pi"], None))
    assert is_ok(scoped), scoped
    assert scoped == unscoped
