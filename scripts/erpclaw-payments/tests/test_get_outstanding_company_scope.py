"""Company scoping for get-outstanding (m794).

The party anchors the scope: with no company the read covers the party's
own company's rows; a given company must exist and a customer or supplier
of another company is refused. The read writes nothing.
"""
import importlib.util
import json
import os
import uuid

import pytest

from payments_helpers import (call_action, is_error, is_ok, load_db_query,
                              ns, seed_sales_invoice)

from erpclaw_lib.query import Q, P, Table, insert_row

PAY = load_db_query()


def _uuid():
    return str(uuid.uuid4())


def _insert(conn, table, **data):
    markers = {k: P() for k in data}
    sql, cols = insert_row(table, markers)
    conn.execute(sql, [data[c] for c in cols])
    conn.commit()


def _mk_company(conn, name, abbr):
    cid = _uuid()
    _insert(conn, "company", id=cid, name=name, abbr=abbr,
            default_currency="USD", country="United States",
            fiscal_year_start_month=1)
    return cid


def _mk_account(conn, company_id, name, root_type, account_type=None):
    aid = _uuid()
    direction = ("debit_normal" if root_type in ("asset", "expense")
                 else "credit_normal")
    row = {"id": aid, "name": "%s %s" % (name, aid[:6]),
           "account_number": "ACC-%s" % aid[:6], "root_type": root_type,
           "balance_direction": direction, "company_id": company_id,
           "depth": 0, "is_group": 0}
    if account_type is not None:
        row["account_type"] = account_type
    _insert(conn, "account", **row)
    return aid


def _mk_customer(conn, company_id, name):
    pid = _uuid()
    _insert(conn, "customer", id=pid, name=name, company_id=company_id)
    return pid


def _seed_two_company_ar(conn):
    acme = _mk_company(conn, "Acme Widgets", "ACME")
    wayne = _mk_company(conn, "Wayne Enterprises", "WAYNE")
    acme_ar = _mk_account(conn, acme, "Receivable", "asset", "receivable")
    acme_ap = _mk_account(conn, acme, "Payable", "liability", "payable")
    wayne_ar = _mk_account(conn, wayne, "Receivable", "asset", "receivable")
    wayne_ap = _mk_account(conn, wayne, "Payable", "liability", "payable")
    bruce = _mk_customer(conn, acme, "Bruce Wayne")
    alfred = _mk_customer(conn, wayne, "Alfred Pennyworth")
    si_acme = seed_sales_invoice(
        conn, {"customer": bruce, "company_id": acme, "ar": acme_ar},
        "100.00")
    si_wayne = seed_sales_invoice(
        conn, {"customer": alfred, "company_id": wayne, "ar": wayne_ar},
        "150.00")
    return {"acme": acme, "wayne": wayne, "acme_ar": acme_ar,
            "acme_ap": acme_ap, "wayne_ar": wayne_ar,
            "wayne_ap": wayne_ap, "bruce": bruce, "alfred": alfred,
            "si_acme": si_acme, "si_wayne": si_wayne}


def _seed_mixed_legacy_invoice(conn, env):
    """Legacy mixed shape, built directly with PyPika: an Acme invoice for
    Alfred (a Wayne customer) of 10.00 whose ledger rows use Acme's
    receivable account."""
    si = _uuid()
    _insert(conn, "sales_invoice", id=si, customer_id=env["alfred"],
            posting_date="2026-06-01", grand_total="10.00",
            total_amount="10.00", tax_amount="0", rounding_adjustment="0",
            outstanding_amount="10.00", status="submitted",
            company_id=env["acme"])
    _insert(conn, "sales_invoice_item", id=_uuid(), sales_invoice_id=si,
            item_id="ITEM-1", quantity="1", rate="10.00", amount="10.00",
            net_amount="10.00")
    _insert(conn, "payment_ledger_entry", id=_uuid(),
            posting_date="2026-06-01", account_id=env["acme_ar"],
            party_type="customer", party_id=env["alfred"],
            voucher_type="sales_invoice", voucher_id=si, amount="10.00",
            amount_in_account_currency="10.00", currency="USD", delinked=0)
    return si


def _state(conn):
    out = {}
    for table in ("gl_entry", "payment_ledger_entry", "audit_log"):
        t = Table(table)
        q = Q.from_(t).select(t.star)
        rows = conn.execute(q.get_sql(), ()).fetchall()
        out[table] = sorted(
            json.dumps({k: (None if v is None else str(v))
                        for k, v in dict(r).items()}, sort_keys=True)
            for r in rows)
    return out


def _outstanding(conn, party_type, party_id, company_id=None,
                 company_name=None):
    return call_action(PAY.get_outstanding, conn, ns(
        party_type=party_type, party_id=party_id,
        voucher_type=None, voucher_id=None,
        company_id=company_id, company_name=company_name))


def test_outstanding_foreign_party_refuses(conn):
    env = _seed_two_company_ar(conn)
    before = _state(conn)
    r = _outstanding(conn, "customer", env["bruce"], company_id=env["wayne"])
    assert is_error(r)
    assert r["message"] == (
        "Customer %s belongs to another company" % env["bruce"])
    assert _state(conn) == before


def test_outstanding_unknown_company_refuses(conn):
    env = _seed_two_company_ar(conn)
    before = _state(conn)
    r = _outstanding(conn, "customer", env["alfred"], company_id="bogus")
    assert r == {"status": "error", "error": "Company not found: bogus",
                 "message": "Company not found: bogus"}
    assert _state(conn) == before


def test_outstanding_scoped_rows(conn):
    env = _seed_two_company_ar(conn)
    _seed_mixed_legacy_invoice(conn, env)
    before = _state(conn)
    plain = _outstanding(conn, "customer", env["alfred"])
    assert is_ok(plain), plain
    assert plain["outstanding"] == "150.00"
    assert [(v["voucher_id"], v["outstanding_amount"])
            for v in plain["vouchers"]] == [(env["si_wayne"], "150.00")]
    scoped = _outstanding(conn, "customer", env["alfred"],
                          company_id=env["wayne"])
    assert is_ok(scoped), scoped
    assert scoped["outstanding"] == "150.00"
    assert [(v["voucher_id"], v["outstanding_amount"])
            for v in scoped["vouchers"]] == [(env["si_wayne"], "150.00")]
    assert _state(conn) == before


def test_outstanding_single_company_unchanged(conn):
    env = _seed_two_company_ar(conn)
    before = _state(conn)
    plain = _outstanding(conn, "customer", env["bruce"])
    assert is_ok(plain), plain
    assert plain["outstanding"] == "100.00"
    scoped = _outstanding(conn, "customer", env["bruce"],
                          company_id=env["acme"])
    assert is_ok(scoped), scoped
    assert scoped["outstanding"] == "100.00"
    assert _state(conn) == before
