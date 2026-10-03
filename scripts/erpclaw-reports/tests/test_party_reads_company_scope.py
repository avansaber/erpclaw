"""Company scoping for ar-aging / ap-aging / party-ledger (m794).

A report given a company answers from that company's books only; an
unknown company id is refused. ``payment_ledger_entry`` and ``gl_entry``
carry no company column -- a row belongs to the company of its
``account_id`` (``account.company_id``). Every read here writes nothing.
"""
import importlib.util
import json
import os
import uuid

import pytest

from payments_helpers import (call_action, is_error, is_ok, ns,
                              seed_purchase_invoice, seed_sales_invoice)

from erpclaw_lib.query import Q, P, Table, insert_row

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_MODULE_DIR = os.path.dirname(_TESTS_DIR)
_SCRIPTS_DIR = os.path.dirname(_MODULE_DIR)


def _load_reports():
    spec = importlib.util.spec_from_file_location(
        "db_query_reports_scope_m794",
        os.path.join(_SCRIPTS_DIR, "erpclaw-reports", "db_query.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


REP = _load_reports()

AS_OF = "2026-12-31"


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


def _mk_supplier(conn, company_id, name):
    pid = _uuid()
    _insert(conn, "supplier", id=pid, name=name, company_id=company_id)
    return pid


def _seed_two_company_ar(conn):
    """Acme + Bruce/100.00 and Wayne + Alfred/150.00, invoice + PLE + GL."""
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
    _mirror_gl(conn, acme_ar, "customer", bruce, si_acme, "100.00")
    _mirror_gl(conn, wayne_ar, "customer", alfred, si_wayne, "150.00")
    return {"acme": acme, "wayne": wayne, "acme_ar": acme_ar,
            "acme_ap": acme_ap, "wayne_ar": wayne_ar,
            "wayne_ap": wayne_ap, "bruce": bruce, "alfred": alfred,
            "si_acme": si_acme, "si_wayne": si_wayne}


def _mirror_gl(conn, account_id, party_type, party_id, voucher_id, amount,
               posting_date="2026-06-01"):
    _insert(conn, "gl_entry", id=_uuid(), posting_date=posting_date,
            account_id=account_id, party_type=party_type, party_id=party_id,
            debit=str(amount), credit="0",
            voucher_type="sales_invoice", voucher_id=voucher_id,
            is_cancelled=0)


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
    _insert(conn, "gl_entry", id=_uuid(), posting_date="2026-06-01",
            account_id=env["acme_ar"], party_type="customer",
            party_id=env["alfred"], debit="10.00", credit="0",
            voucher_type="sales_invoice", voucher_id=si, is_cancelled=0)
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


def _ar_aging(conn, company_id=None, company_name=None):
    return call_action(REP.ar_aging, conn, ns(
        company_id=company_id, company_name=company_name,
        as_of_date=AS_OF, aging_buckets=None))


def _ap_aging(conn, company_id=None, company_name=None):
    return call_action(REP.ap_aging, conn, ns(
        company_id=company_id, company_name=company_name,
        as_of_date=AS_OF, aging_buckets=None))


def _party_ledger(conn, party_type, party_id, company_id=None,
                  company_name=None):
    return call_action(REP.party_ledger, conn, ns(
        party_type=party_type, party_id=party_id,
        company_id=company_id, company_name=company_name,
        from_date=None, to_date=None))


# ── ar-aging ────────────────────────────────────────────────────────────────

def test_ar_aging_scoped_to_company(conn):
    env = _seed_two_company_ar(conn)
    before = _state(conn)
    wayne = _ar_aging(conn, company_id=env["wayne"])
    assert is_ok(wayne), wayne
    assert wayne["total_outstanding"] == "150.00"
    assert [(c["customer_id"], c["total"]) for c in wayne["customers"]] == [
        (env["alfred"], "150.00")]
    acme = _ar_aging(conn, company_id=env["acme"])
    assert is_ok(acme), acme
    assert acme["total_outstanding"] == "100.00"
    assert [(c["customer_id"], c["total"]) for c in acme["customers"]] == [
        (env["bruce"], "100.00")]
    assert _state(conn) == before


def test_ar_aging_unknown_company_refuses(conn):
    _seed_two_company_ar(conn)
    before = _state(conn)
    r = _ar_aging(conn, company_id="bogus")
    assert r == {"status": "error", "error": "Company not found: bogus",
                 "message": "Company not found: bogus"}
    assert _state(conn) == before


def test_ar_aging_no_company_two_companies_refuses(conn):
    _seed_two_company_ar(conn)
    before = _state(conn)
    r = _ar_aging(conn)
    assert is_error(r)
    assert r["message"] == r["error"] == (
        "Multiple companies found. Please specify the company by name.")
    assert sorted(c["name"] for c in r["companies"]) == [
        "Acme Widgets", "Wayne Enterprises"]
    assert _state(conn) == before


# ── ap-aging ────────────────────────────────────────────────────────────────

def test_ap_aging_scoped_to_company(conn):
    env = _seed_two_company_ar(conn)
    acme_supp = _mk_supplier(conn, env["acme"], "Acme Supplier")
    wayne_supp = _mk_supplier(conn, env["wayne"], "Wayne Supplier")
    seed_purchase_invoice(
        conn, {"supplier": acme_supp, "company_id": env["acme"],
               "ap": env["acme_ap"]}, "100.00")
    seed_purchase_invoice(
        conn, {"supplier": wayne_supp, "company_id": env["wayne"],
               "ap": env["wayne_ap"]}, "150.00")
    before = _state(conn)
    wayne = _ap_aging(conn, company_id=env["wayne"])
    assert is_ok(wayne), wayne
    assert wayne["total_outstanding"] == "150.00"
    assert [(s["supplier_id"], s["total"]) for s in wayne["suppliers"]] == [
        (wayne_supp, "150.00")]
    acme = _ap_aging(conn, company_id=env["acme"])
    assert is_ok(acme), acme
    assert acme["total_outstanding"] == "100.00"
    assert [(s["supplier_id"], s["total"]) for s in acme["suppliers"]] == [
        (acme_supp, "100.00")]
    assert _state(conn) == before


# ── party-ledger ────────────────────────────────────────────────────────────

def test_party_ledger_foreign_party_refuses(conn):
    env = _seed_two_company_ar(conn)
    before = _state(conn)
    r = _party_ledger(conn, "customer", env["bruce"],
                      company_id=env["wayne"])
    assert is_error(r)
    assert r["message"] == (
        "Customer %s belongs to another company" % env["bruce"])
    assert _state(conn) == before


def test_party_ledger_scoped_rows(conn):
    env = _seed_two_company_ar(conn)
    _seed_mixed_legacy_invoice(conn, env)
    before = _state(conn)
    scoped = _party_ledger(conn, "customer", env["alfred"],
                           company_id=env["wayne"])
    assert is_ok(scoped), scoped
    assert [e["voucher_id"] for e in scoped["entries"]] == [env["si_wayne"]]
    assert scoped["closing_balance"] == "150.00"
    unscoped = _party_ledger(conn, "customer", env["alfred"])
    assert is_ok(unscoped), unscoped
    assert [e["voucher_id"] for e in unscoped["entries"]] == [env["si_wayne"]]
    assert unscoped["closing_balance"] == "150.00"
    assert _state(conn) == before


def test_party_ledger_unknown_company_refuses(conn):
    env = _seed_two_company_ar(conn)
    before = _state(conn)
    r = _party_ledger(conn, "customer", env["alfred"], company_id="bogus")
    assert r == {"status": "error", "error": "Company not found: bogus",
                 "message": "Company not found: bogus"}
    assert _state(conn) == before
