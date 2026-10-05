"""Focused VALUE tests for the weekly-digest report (floor-o037).

Every pin invokes the REAL ``weekly-digest`` action against a fresh core DB
and asserts exact Decimal strings produced by PyPika queries through
``erpclaw_lib.query``. Seeds are direct INSERTs built with ``insert_row``
(bound parameters, TEXT money, never float).
"""
import importlib.util
import os
import uuid
from decimal import Decimal

from payments_helpers import call_action, is_error, is_ok, ns

from erpclaw_lib.query import P, insert_row

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(os.path.dirname(_TESTS_DIR))


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_SCRIPTS_DIR, "erpclaw-reports", "db_query.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


REP = _load("db_query_reports_weekly_digest")

D = Decimal
START = "2026-01-05"
END = "2026-01-11"


def _u():
    return str(uuid.uuid4())


def _insert(conn, table, row):
    sql, cols = insert_row(table, {key: P() for key in row})
    conn.execute(sql, [row[c] for c in cols])


def _company(conn, name="Digest Co"):
    cid = _u()
    _insert(conn, "company", {"id": cid, "name": "%s %s" % (name, cid[:6]),
                             "abbr": "DG%s" % cid[:4].upper()})
    conn.commit()
    return cid


def _env(conn, name="Digest Co"):
    cid = _company(conn, name)
    cust = _u()
    _insert(conn, "customer", {"id": cust, "name": "Cust %s" % cust[:6],
                              "company_id": cid})
    supp = _u()
    _insert(conn, "supplier", {"id": supp, "name": "Supp %s" % supp[:6],
                              "company_id": cid})

    def _acct(aname, number, root_type):
        aid = _u()
        _insert(conn, "account", {
            "id": aid, "name": aname, "account_number": number,
            "root_type": root_type,
            "balance_direction": ("debit_normal"
                                  if root_type in ("asset", "expense")
                                  else "credit_normal"),
            "company_id": cid, "depth": 0, "is_group": 0})
        return aid

    bank = _acct("Bank", "1010", "asset")
    ar = _acct("Receivable", "1100", "asset")
    conn.commit()
    return {"company_id": cid, "customer": cust, "supplier": supp,
            "bank": bank, "ar": ar}


def _sales_invoice(conn, env, posting_date, grand_total, outstanding=None,
                   due_date="2026-02-01", status="submitted"):
    si = _u()
    _insert(conn, "sales_invoice", {
        "id": si, "customer_id": env["customer"], "posting_date": posting_date,
        "due_date": due_date, "grand_total": str(grand_total),
        "outstanding_amount": str(outstanding if outstanding is not None
                                  else grand_total),
        "status": status, "company_id": env["company_id"]})
    conn.commit()
    return si


def _purchase_invoice(conn, env, posting_date, grand_total,
                      status="submitted"):
    pi = _u()
    _insert(conn, "purchase_invoice", {
        "id": pi, "supplier_id": env["supplier"], "posting_date": posting_date,
        "due_date": "2026-02-01", "grand_total": str(grand_total),
        "outstanding_amount": str(grand_total),
        "status": status, "company_id": env["company_id"]})
    conn.commit()
    return pi


def _payment(conn, env, posting_date, amount, payment_type="receive",
             status="submitted"):
    pe = _u()
    if payment_type == "receive":
        party = ("customer", env["customer"], env["ar"], env["bank"])
    else:
        party = ("supplier", env["supplier"], env["bank"], env["ar"])
    _insert(conn, "payment_entry", {
        "id": pe, "payment_type": payment_type, "posting_date": posting_date,
        "party_type": party[0], "party_id": party[1],
        "paid_from_account": party[2], "paid_to_account": party[3],
        "paid_amount": str(amount), "status": status,
        "company_id": env["company_id"]})
    conn.commit()
    return pe


def _sales_order(conn, env, status="confirmed"):
    so = _u()
    _insert(conn, "sales_order", {
        "id": so, "customer_id": env["customer"], "order_date": START,
        "status": status, "company_id": env["company_id"]})
    conn.commit()
    return so


def _purchase_order(conn, env, status="confirmed"):
    po = _u()
    _insert(conn, "purchase_order", {
        "id": po, "supplier_id": env["supplier"], "order_date": START,
        "status": status, "company_id": env["company_id"]})
    conn.commit()
    return po


def _digest(conn, company_id, start=START, extra=None):
    base = {"company_id": company_id, "company_name": None,
            "start_date": start, "from_date": None, "to_date": None}
    if extra:
        base.update(extra)
    return call_action(REP.weekly_digest, conn, ns(**base))


def _counts(conn):
    out = {}
    tables = ["company", "sales_invoice", "sales_invoice_item",
              "payment_entry", "payment_allocation", "payment_deduction",
              "purchase_invoice", "purchase_invoice_item",
              "sales_order", "purchase_order"]
    for tbl in tables:
        try:
            out[tbl] = conn.execute(
                "SELECT COUNT(*) AS n FROM %s" % tbl).fetchone()["n"]
        except Exception:
            out[tbl] = None
    return out


def test_exact_money_and_derived_end_date(conn):
    env = _env(conn)
    _sales_invoice(conn, env, "2026-01-06", "100.10", due_date="2026-03-01")
    _sales_invoice(conn, env, "2026-01-08", "200.20", due_date="2026-03-01")
    _payment(conn, env, "2026-01-07", "50.05")
    _purchase_invoice(conn, env, "2026-01-09", "75.25")
    _sales_invoice(conn, env, "2025-12-20", "500.00",
                   due_date="2026-01-01", outstanding="33.33")

    r = _digest(conn, env["company_id"])
    assert is_ok(r), r
    assert r["week_start"] == START
    assert r["week_end"] == END
    assert r["sections"] == ["sales", "collections", "spending",
                             "overdue_receivables", "operations"]
    assert r["sales"]["status"] == "available"
    assert r["sales"]["invoice_count"] == 2
    assert D(r["sales"]["total"]) == D("300.30")
    assert r["sales"]["total"] == "300.30"
    assert r["collections"]["receipt_count"] == 1
    assert D(r["collections"]["total"]) == D("50.05")
    assert r["spending"]["bill_count"] == 1
    assert D(r["spending"]["total"]) == D("75.25")
    assert r["overdue_receivables"]["overdue_count"] == 1
    assert D(r["overdue_receivables"]["total_overdue"]) == D("33.33")


def test_company_isolation(conn):
    env_a = _env(conn, "Alpha Co")
    env_b = _env(conn, "Beta Co")
    _sales_invoice(conn, env_a, "2026-01-06", "111.11", due_date="2026-03-01")
    _sales_invoice(conn, env_b, "2026-01-06", "999.99", due_date="2026-03-01")
    _payment(conn, env_b, "2026-01-07", "888.88")
    _purchase_invoice(conn, env_b, "2026-01-07", "777.77")
    _sales_order(conn, env_b)
    _purchase_order(conn, env_b)

    r = _digest(conn, env_a["company_id"])
    assert is_ok(r), r
    assert D(r["sales"]["total"]) == D("111.11")
    assert D(r["collections"]["total"]) == D("0")
    assert D(r["spending"]["total"]) == D("0")
    assert r["overdue_receivables"]["overdue_count"] == 0
    assert r["operations"]["open_sales_orders"] == 0
    assert r["operations"]["open_purchase_orders"] == 0


def test_seven_day_bounds_are_inclusive(conn):
    env = _env(conn)
    _sales_invoice(conn, env, "2026-01-04", "10.00", due_date="2026-03-01")
    _sales_invoice(conn, env, START, "20.00", due_date="2026-03-01")
    _sales_invoice(conn, env, END, "30.00", due_date="2026-03-01")
    _sales_invoice(conn, env, "2026-01-12", "40.00", due_date="2026-03-01")
    _payment(conn, env, "2026-01-04", "1.00")
    _payment(conn, env, "2026-01-12", "2.00")
    _payment(conn, env, "2026-01-10", "3.00")

    r = _digest(conn, env["company_id"])
    assert is_ok(r), r
    assert r["sales"]["invoice_count"] == 2
    assert D(r["sales"]["total"]) == D("50.00")
    assert r["collections"]["receipt_count"] == 1
    assert D(r["collections"]["total"]) == D("3.00")


def test_draft_and_cancelled_documents_excluded(conn):
    env = _env(conn)
    _sales_invoice(conn, env, "2026-01-06", "100.00", due_date="2026-03-01",
                   status="draft")
    _sales_invoice(conn, env, "2026-01-06", "200.00", due_date="2026-03-01",
                   status="cancelled")
    _sales_invoice(conn, env, "2026-01-06", "300.00", due_date="2026-03-01")
    _payment(conn, env, "2026-01-07", "44.00", status="draft")
    _payment(conn, env, "2026-01-07", "55.00", status="cancelled")

    r = _digest(conn, env["company_id"])
    assert is_ok(r), r
    assert r["sales"]["invoice_count"] == 1
    assert D(r["sales"]["total"]) == D("300.00")
    assert r["collections"]["receipt_count"] == 0
    assert D(r["collections"]["total"]) == D("0")


def test_missing_tables_are_unavailable_not_zero(conn):
    env = _env(conn)
    _sales_invoice(conn, env, "2026-01-06", "100.00", due_date="2026-03-01")
    conn.execute("DROP TABLE IF EXISTS sales_invoice_item")
    conn.execute("DROP TABLE IF EXISTS sales_invoice")
    conn.commit()

    r = _digest(conn, env["company_id"])
    assert is_ok(r), r
    assert r["sales"]["status"] == "unavailable"
    assert "sales_invoice" in r["sales"]["reason"]
    assert r["overdue_receivables"]["status"] == "unavailable"
    assert r["collections"]["status"] == "available"
    assert r["spending"]["status"] == "available"


def test_missing_payment_table_marks_collections_unavailable(conn):
    env = _env(conn)
    _purchase_invoice(conn, env, "2026-01-06", "60.00")
    conn.execute("DROP TABLE IF EXISTS payment_allocation")
    conn.execute("DROP TABLE IF EXISTS payment_deduction")
    conn.execute("DROP TABLE IF EXISTS payment_entry")
    conn.commit()

    r = _digest(conn, env["company_id"])
    assert is_ok(r), r
    assert r["collections"]["status"] == "unavailable"
    assert "payment_entry" in r["collections"]["reason"]
    assert r["spending"]["status"] == "available"
    assert D(r["spending"]["total"]) == D("60.00")


def test_open_operational_counts(conn):
    env = _env(conn)
    _sales_order(conn, env, "confirmed")
    _sales_order(conn, env, "partially_delivered")
    _sales_order(conn, env, "cancelled")
    _sales_order(conn, env, "closed")
    _purchase_order(conn, env, "confirmed")
    _purchase_order(conn, env, "cancelled")

    r = _digest(conn, env["company_id"])
    assert is_ok(r), r
    assert r["operations"]["status"] == "available"
    assert r["operations"]["open_sales_orders"] == 2
    assert r["operations"]["open_purchase_orders"] == 1


def test_two_identical_calls_are_deterministic(conn):
    env = _env(conn)
    _sales_invoice(conn, env, "2026-01-06", "123.45", due_date="2026-01-02",
                   outstanding="123.45")
    _payment(conn, env, "2026-01-07", "10.00")
    _sales_order(conn, env)

    first = _digest(conn, env["company_id"])
    second = _digest(conn, env["company_id"])
    assert is_ok(first), first
    assert is_ok(second), second
    assert first == second
    assert list(first.keys()) == ["company_id", "week_start",
                                  "week_end", "sections", "sales",
                                  "collections", "spending",
                                  "overdue_receivables", "operations",
                                  "status"]


def test_report_performs_no_writes(conn):
    env = _env(conn)
    _sales_invoice(conn, env, "2026-01-06", "50.00", due_date="2026-03-01")
    _payment(conn, env, "2026-01-07", "5.00")
    _purchase_invoice(conn, env, "2026-01-08", "6.00")
    _sales_order(conn, env)
    _purchase_order(conn, env)

    before = _counts(conn)
    r = _digest(conn, env["company_id"])
    assert is_ok(r), r
    assert _counts(conn) == before


def test_invalid_input_is_refused(conn):
    env = _env(conn)

    r = call_action(REP.weekly_digest, conn,
                    ns(company_id=None, company_name=None,
                       start_date=START, from_date=None, to_date=None))
    assert is_error(r), r

    r = call_action(REP.weekly_digest, conn,
                    ns(company_id=env["company_id"], company_name=None,
                       start_date=None, from_date=None, to_date=None))
    assert is_error(r), r

    for bad in ("2026-13-01", "2026-01-32", "01/05/2026", "2026-1-5",
                "2026-01-05T00:00:00", "not-a-date"):
        r = call_action(REP.weekly_digest, conn,
                        ns(company_id=env["company_id"], company_name=None,
                           start_date=bad, from_date=None, to_date=None))
        assert is_error(r), bad

    r = call_action(REP.weekly_digest, conn,
                    ns(company_id=_u(), company_name=None,
                       start_date=START, from_date=None, to_date=None))
    assert is_error(r), r
