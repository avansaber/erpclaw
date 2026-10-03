"""Behaviour of gl-summary, payment-summary, tax-summary, list-journal-entries,
get-journal-entry and list-payments, read back against a fully known book.

The three summaries live in erpclaw-reports (this directory). The journal and
payment readers live in the sibling erpclaw-journals and erpclaw-payments
domains; those db_query.py files, and erpclaw-selling's, are loaded by path.
Every domain runs against the same full-schema database the `conn` fixture
builds, and every document below is posted through its real action.

Book of "Harbor Supply" (all dates fixed, service items only, no stock ledger):

  JE-1  2026-03-10  submitted  DR Office Expense 400.00 / CR Operating Bank 400.00
  JE-2  2026-03-15  CANCELLED  DR Office Expense 150.00 / CR Operating Bank 150.00
  JE-3  2026-03-20  draft      DR Office Expense  75.00 / CR Operating Bank  75.00
  SI-1  2026-04-01  submitted  10 x 100.00 + 8.25 % tax = 1082.50
  SI-2  2026-04-05  CANCELLED   2 x 100.00 + 8.25 % tax =  216.50
  PE-1  2026-04-10  submitted  receive 600.00 from Acme Corp, allocated to SI-1
  PE-2  2026-04-12  submitted  pay 250.00 to Gotham Steel, unallocated
  PE-3  2026-04-15  CANCELLED  receive 100.00 from Acme Corp, unallocated
  JE-4  2026-04-20  submitted  DR Sales Tax Payable 20.00 / CR Operating Bank 20.00
  PE-4  2026-04-18  draft      receive 45.00 from Acme Corp
  JE-5  2026-05-05  submitted  DR Office Expense 60.00 / CR Operating Bank 60.00

A second company, "Lakeside Traders", posts a journal entry to its own tax
account and receives a payment inside the same window, so every company
filter has something to exclude.

A cancellation marks both the original GL legs and their mirror legs
is_cancelled = 1, and sets the document status to 'cancelled'. The summaries
filter is_cancelled = 0 (gl-summary, tax-summary) and status = 'submitted'
(payment-summary), so a cancelled document contributes nothing to any of them;
the list readers show it with its cancelled status.
"""
import importlib.util
import json
import os
import uuid

import pytest

from payments_helpers import call_action, is_error, is_ok, ns

try:
    from erpclaw_lib.db import get_dialect
except ImportError:  # lib not on path in some minimal contexts
    def get_dialect():
        return os.environ.get("ERPCLAW_DB_DIALECT", "sqlite")

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(os.path.dirname(_TESTS_DIR))


def _load(name, domain):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_SCRIPTS_DIR, domain, "db_query.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


REP = _load("db_query_reports_m341", "erpclaw-reports")
JR = _load("db_query_journals_m341", "erpclaw-journals")
PAY = _load("db_query_payments_m341", "erpclaw-payments")
SEL = _load("db_query_selling_m341", "erpclaw-selling")

WINDOW = ("2026-03-01", "2026-04-30")


def _u():
    return str(uuid.uuid4())


def _msg(result):
    return result.get("message", "")


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


def _account(conn, company_id, name, number, root_type, account_type):
    aid = _u()
    direction = "debit_normal" if root_type in ("asset", "expense") else "credit_normal"
    conn.execute(
        "INSERT INTO account (id, name, account_number, root_type, account_type, "
        "balance_direction, company_id, depth) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
        (aid, name, number, root_type, account_type, direction, company_id))
    return aid


def _company(conn, name, abbr):
    cid = _u()
    conn.execute("INSERT INTO company (id, name, abbr) VALUES (?, ?, ?)", (cid, name, abbr))
    conn.execute(
        "INSERT INTO fiscal_year (id, name, start_date, end_date, is_closed, company_id) "
        "VALUES (?, ?, '2026-01-01', '2026-12-31', 0, ?)", (_u(), f"FY-2026-{abbr}", cid))
    cc = _u()
    conn.execute("INSERT INTO cost_center (id, name, company_id, is_group) VALUES (?, ?, ?, 0)",
                 (cc, f"Main - {abbr}", cid))
    env = {
        "company_id": cid, "cc": cc,
        "bank": _account(conn, cid, "Operating Bank", "1010", "asset", "bank"),
        "ar": _account(conn, cid, "Accounts Receivable", "1100", "asset", "receivable"),
        "ap": _account(conn, cid, "Accounts Payable", "2100", "liability", "payable"),
        "sales_tax": _account(conn, cid, "Sales Tax Payable", "2200", "liability", "tax"),
        "input_tax": _account(conn, cid, "Input Tax", "1400", "asset", "tax"),
        "revenue": _account(conn, cid, "Service Revenue", "4000", "income", "revenue"),
        "expense": _account(conn, cid, "Office Expense", "5100", "expense", None),
    }
    conn.execute(
        "UPDATE company SET default_cost_center_id = ?, default_receivable_account_id = ?, "
        "default_income_account_id = ? WHERE id = ?", (cc, env["ar"], env["revenue"], cid))
    env["customer"] = _u()
    conn.execute(
        "INSERT INTO customer (id, name, company_id, customer_type, status, credit_limit) "
        "VALUES (?, ?, ?, 'company', 'active', '0')",
        (env["customer"], "Acme Corp" if abbr == "HS" else "Birch Ltd", cid))
    env["supplier"] = _u()
    conn.execute(
        "INSERT INTO supplier (id, name, supplier_type, status, company_id) "
        "VALUES (?, 'Gotham Steel', 'company', 'active', ?)", (env["supplier"], cid))
    env["item"] = _u()
    conn.execute(
        "INSERT INTO item (id, item_name, item_code, stock_uom, is_stock_item) "
        "VALUES (?, 'Consulting', ?, 'Hour', 0)", (env["item"], f"SVC-{abbr}"))
    env["tax_tpl"] = _u()
    conn.execute("INSERT INTO tax_template (id, name, tax_type, company_id) "
                 "VALUES (?, ?, 'sales', ?)", (env["tax_tpl"], f"Sales Tax 8.25 {abbr}", cid))
    conn.execute(
        "INSERT INTO tax_template_line (id, tax_template_id, tax_account_id, rate, "
        "charge_type, row_order, add_deduct) VALUES (?, ?, ?, '8.25', 'on_net_total', 0, 'add')",
        (_u(), env["tax_tpl"], env["sales_tax"]))
    conn.commit()
    return env


def _je(conn, env, date, debit_acct, credit_acct, amount, submit=True, remark=None):
    lines = [{"account_id": env[debit_acct], "debit": amount, "credit": "0",
              "cost_center_id": env["cc"], "remark": "debit leg"},
             {"account_id": env[credit_acct], "debit": "0", "credit": amount,
              "cost_center_id": env["cc"], "remark": "credit leg"}]
    r = call_action(JR.add_journal_entry, conn, ns(
        company_id=env["company_id"], posting_date=date, entry_type=None,
        remark=remark, lines=json.dumps(lines), cwip_asset_id=None))
    assert is_ok(r), r
    je_id = r["journal_entry_id"]
    if submit:
        r = call_action(JR.submit_journal_entry, conn, ns(journal_entry_id=je_id))
        assert is_ok(r), r
    return je_id


def _invoice(conn, env, date, qty):
    r = call_action(SEL.create_sales_invoice, conn, ns(
        company_id=env["company_id"], customer_id=env["customer"],
        tax_template_id=env["tax_tpl"], sales_order_id=None, delivery_note_id=None,
        posting_date=date, due_date="2026-06-30", payment_terms_id=None,
        items=json.dumps([{"item_id": env["item"], "qty": qty, "rate": "100.00"}])))
    assert is_ok(r), r
    si_id = r["sales_invoice_id"]
    r = call_action(SEL.submit_sales_invoice, conn, ns(sales_invoice_id=si_id))
    assert is_ok(r), r
    return si_id


def _payment(conn, env, date, payment_type, amount, allocations=None, submit=True):
    if payment_type == "receive":
        party = ("customer", env["customer"], env["ar"], env["bank"])
    else:
        party = ("supplier", env["supplier"], env["bank"], env["ap"])
    r = call_action(PAY.add_payment, conn, ns(
        company_id=env["company_id"], payment_type=payment_type, posting_date=date,
        party_type=party[0], party_id=party[1], paid_from_account=party[2],
        paid_to_account=party[3], paid_amount=amount, exchange_rate=None,
        payment_currency=None, reference_number=None, reference_date=None,
        allocations=json.dumps(allocations) if allocations else None, deductions=None))
    assert is_ok(r), r
    pe_id = r["payment_entry_id"]
    if submit:
        r = call_action(PAY.submit_payment, conn, ns(payment_entry_id=pe_id))
        assert is_ok(r), r
    return pe_id


@pytest.fixture
def book(conn):
    hs = _company(conn, "Harbor Supply", "HS")
    ids = {"hs": hs}
    ids["je1"] = _je(conn, hs, "2026-03-10", "expense", "bank", "400.00", remark="March rent")
    ids["je2"] = _je(conn, hs, "2026-03-15", "expense", "bank", "150.00")
    r = call_action(JR.cancel_journal_entry, conn, ns(journal_entry_id=ids["je2"]))
    assert is_ok(r), r
    ids["je3"] = _je(conn, hs, "2026-03-20", "expense", "bank", "75.00", submit=False)
    ids["si1"] = _invoice(conn, hs, "2026-04-01", "10")
    ids["si2"] = _invoice(conn, hs, "2026-04-05", "2")
    r = call_action(SEL.cancel_sales_invoice, conn, ns(sales_invoice_id=ids["si2"]))
    assert is_ok(r), r
    ids["pe1"] = _payment(conn, hs, "2026-04-10", "receive", "600.00", allocations=[
        {"voucher_type": "sales_invoice", "voucher_id": ids["si1"],
         "allocated_amount": "600.00"}])
    ids["pe2"] = _payment(conn, hs, "2026-04-12", "pay", "250.00")
    ids["pe3"] = _payment(conn, hs, "2026-04-15", "receive", "100.00")
    r = call_action(PAY.cancel_payment, conn, ns(payment_entry_id=ids["pe3"]))
    assert is_ok(r), r
    ids["pe4"] = _payment(conn, hs, "2026-04-18", "receive", "45.00", submit=False)
    ids["je4"] = _je(conn, hs, "2026-04-20", "sales_tax", "bank", "20.00")
    ids["je5"] = _je(conn, hs, "2026-05-05", "expense", "bank", "60.00")

    lt = _company(conn, "Lakeside Traders", "LT")
    ids["lt"] = lt
    ids["lt_je"] = _je(conn, lt, "2026-04-02", "bank", "sales_tax", "30.00")
    ids["lt_pe"] = _payment(conn, lt, "2026-04-03", "receive", "777.00")
    return ids


def _report(conn, fn, company_id=None, company_name=None, window=WINDOW):
    return call_action(fn, conn, ns(company_id=company_id, company_name=company_name,
                                    from_date=window[0], to_date=window[1]))


# ---------------------------------------------------------------------------
# The ledger the summaries read
# ---------------------------------------------------------------------------

def test_cancelled_documents_leave_fully_cancelled_gl(conn, book):
    hs = book["hs"]
    for vtype, vid, legs in (("journal_entry", book["je2"], 4),
                             ("sales_invoice", book["si2"], 6),
                             ("payment_entry", book["pe3"], 4)):
        rows = conn.execute(
            "SELECT is_cancelled FROM gl_entry WHERE voucher_type = ? AND voucher_id = ?",
            (vtype, vid)).fetchall()
        assert (len(rows), {r["is_cancelled"] for r in rows}) == (legs, {1})
    si1 = conn.execute(
        "SELECT account_id, debit, credit, is_cancelled FROM gl_entry "
        "WHERE voucher_type = 'sales_invoice' AND voucher_id = ?", (book["si1"],)).fetchall()
    assert sorted((g["account_id"], g["debit"], g["credit"], g["is_cancelled"]) for g in si1) \
        == sorted([(hs["ar"], "1082.50", "0.00", 0), (hs["revenue"], "0.00", "1000.00", 0),
                   (hs["sales_tax"], "0.00", "82.50", 0)])
    statuses = {r["id"]: r["status"] for r in conn.execute(
        "SELECT id, status FROM journal_entry").fetchall()}
    assert statuses[book["je2"]] == "cancelled"
    assert conn.execute("SELECT status FROM sales_invoice WHERE id = ?",
                        (book["si2"],)).fetchone()["status"] == "cancelled"
    assert conn.execute("SELECT status FROM payment_entry WHERE id = ?",
                        (book["pe3"],)).fetchone()["status"] == "cancelled"


# ---------------------------------------------------------------------------
# gl-summary
# ---------------------------------------------------------------------------

def test_gl_summary_totals_by_voucher_type_without_cancelled_legs(conn, book):
    gl_before = _count(conn, "gl_entry")
    r = _report(conn, REP.gl_summary, company_id=book["hs"]["company_id"])
    assert is_ok(r), r
    assert r["by_voucher_type"] == [
        {"voucher_type": "journal_entry", "count": 4,
         "total_debit": "420.00", "total_credit": "420.00"},
        {"voucher_type": "payment_entry", "count": 4,
         "total_debit": "850.00", "total_credit": "850.00"},
        {"voucher_type": "sales_invoice", "count": 3,
         "total_debit": "1082.50", "total_credit": "1082.50"},
    ]
    assert _count(conn, "gl_entry") == gl_before


def test_gl_summary_date_bounds_are_inclusive_and_company_scoped(conn, book):
    r = _report(conn, REP.gl_summary, company_id=book["hs"]["company_id"],
                window=("2026-04-01", "2026-04-10"))
    assert is_ok(r), r
    assert r["by_voucher_type"] == [
        {"voucher_type": "payment_entry", "count": 2,
         "total_debit": "600.00", "total_credit": "600.00"},
        {"voucher_type": "sales_invoice", "count": 3,
         "total_debit": "1082.50", "total_credit": "1082.50"},
    ]
    r = _report(conn, REP.gl_summary, company_id=book["hs"]["company_id"],
                window=("2026-01-01", "2026-12-31"))
    assert r["by_voucher_type"][0] == {"voucher_type": "journal_entry", "count": 6,
                                       "total_debit": "480.00", "total_credit": "480.00"}
    r = _report(conn, REP.gl_summary, company_name="lakeside traders")
    assert is_ok(r), r
    assert r["by_voucher_type"] == [
        {"voucher_type": "journal_entry", "count": 2,
         "total_debit": "30.00", "total_credit": "30.00"},
        {"voucher_type": "payment_entry", "count": 2,
         "total_debit": "777.00", "total_credit": "777.00"},
    ]


def test_gl_summary_refusals(conn, book):
    cid = book["hs"]["company_id"]
    r = call_action(REP.gl_summary, conn, ns(company_id=cid, company_name=None,
                                             from_date=None, to_date="2026-04-30"))
    assert is_error(r) and _msg(r) == "--from-date is required"
    r = call_action(REP.gl_summary, conn, ns(company_id=cid, company_name=None,
                                             from_date="2026-03-01", to_date=None))
    assert is_error(r) and _msg(r) == "--to-date is required"
    r = _report(conn, REP.gl_summary, company_name="Nowhere Inc")
    assert r["error"] == "Company 'Nowhere Inc' not found."
    assert r["available_companies"] == ["Harbor Supply", "Lakeside Traders"]
    r = _report(conn, REP.gl_summary)
    assert r["error"] == "Multiple companies found. Please specify the company by name."
    assert "by_voucher_type" not in r


# ---------------------------------------------------------------------------
# tax-summary
# ---------------------------------------------------------------------------

def test_tax_summary_collected_paid_and_net_per_tax_account(conn, book):
    hs = book["hs"]
    gl_before = _count(conn, "gl_entry")
    r = _report(conn, REP.tax_summary, company_id=hs["company_id"])
    assert is_ok(r), r
    # SI-1 credits 82.50; SI-2's 16.50 and its mirror are both cancelled;
    # JE-4 debits 20.00. Input Tax has no rows and is left out.
    assert (r["collected"], r["paid"], r["net_liability"]) == ("82.50", "20.00", "62.50")
    assert r["by_account"] == [{"account_id": hs["sales_tax"],
                                "account_name": "Sales Tax Payable", "amount": "62.50"}]
    assert _count(conn, "gl_entry") == gl_before


def test_tax_summary_date_window_and_company_filter(conn, book):
    hs, lt = book["hs"], book["lt"]
    r = _report(conn, REP.tax_summary, company_id=hs["company_id"],
                window=("2026-04-01", "2026-04-19"))
    assert (r["collected"], r["paid"], r["net_liability"]) == ("82.50", "0.00", "82.50")
    assert r["by_account"] == [{"account_id": hs["sales_tax"],
                                "account_name": "Sales Tax Payable", "amount": "82.50"}]
    r = _report(conn, REP.tax_summary, company_id=hs["company_id"],
                window=("2026-03-01", "2026-03-31"))
    assert (r["collected"], r["paid"], r["net_liability"], r["by_account"]) == \
        ("0.00", "0.00", "0.00", [])
    r = _report(conn, REP.tax_summary, company_id=lt["company_id"])
    assert (r["collected"], r["paid"], r["net_liability"]) == ("30.00", "0.00", "30.00")
    assert r["by_account"] == [{"account_id": lt["sales_tax"],
                                "account_name": "Sales Tax Payable", "amount": "30.00"}]


def test_tax_summary_refusals(conn, book):
    cid = book["hs"]["company_id"]
    r = call_action(REP.tax_summary, conn, ns(company_id=cid, company_name=None,
                                              from_date=None, to_date="2026-04-30"))
    assert is_error(r) and _msg(r) == "--from-date is required"
    r = call_action(REP.tax_summary, conn, ns(company_id=cid, company_name=None,
                                              from_date="2026-03-01", to_date=None))
    assert is_error(r) and _msg(r) == "--to-date is required"
    r = _report(conn, REP.tax_summary, company_name="Nowhere Inc")
    assert r["error"] == "Company 'Nowhere Inc' not found."
    assert "collected" not in r


# ---------------------------------------------------------------------------
# payment-summary
# ---------------------------------------------------------------------------

def _by_party(result):
    return sorted(result["by_party_type"], key=lambda p: p["party_type"])


def test_payment_summary_counts_only_submitted_payments(conn, book):
    pe_before = _count(conn, "payment_entry")
    r = _report(conn, REP.payment_summary, company_id=book["hs"]["company_id"])
    assert is_ok(r), r
    # PE-3 (cancelled, 100.00) and PE-4 (draft, 45.00) are excluded.
    assert (r["total_received"], r["total_paid"]) == ("600.00", "250.00")
    assert _by_party(r) == [
        {"party_type": "customer", "count": 1, "amount": "600.00"},
        {"party_type": "supplier", "count": 1, "amount": "250.00"},
    ]
    assert _count(conn, "payment_entry") == pe_before


def test_payment_summary_date_window_and_company_filter(conn, book):
    r = _report(conn, REP.payment_summary, company_id=book["hs"]["company_id"],
                window=("2026-04-11", "2026-04-30"))
    assert (r["total_received"], r["total_paid"]) == ("0.00", "250.00")
    assert _by_party(r) == [{"party_type": "supplier", "count": 1, "amount": "250.00"}]
    r = _report(conn, REP.payment_summary, company_id=book["hs"]["company_id"],
                window=("2026-03-01", "2026-03-31"))
    assert (r["total_received"], r["total_paid"], r["by_party_type"]) == \
        ("0.00", "0.00", [])
    r = _report(conn, REP.payment_summary, company_name="Lakeside Traders")
    assert (r["total_received"], r["total_paid"]) == ("777.00", "0.00")
    assert _by_party(r) == [{"party_type": "customer", "count": 1, "amount": "777.00"}]


def test_payment_summary_refusals(conn, book):
    cid = book["hs"]["company_id"]
    r = call_action(REP.payment_summary, conn, ns(company_id=cid, company_name=None,
                                                  from_date=None, to_date="2026-04-30"))
    assert is_error(r) and _msg(r) == "--from-date is required"
    r = call_action(REP.payment_summary, conn, ns(company_id=cid, company_name=None,
                                                  from_date="2026-03-01", to_date=None))
    assert is_error(r) and _msg(r) == "--to-date is required"
    r = _report(conn, REP.payment_summary)
    assert r["error"] == "Multiple companies found. Please specify the company by name."
    assert "total_received" not in r


# ---------------------------------------------------------------------------
# get-journal-entry
# ---------------------------------------------------------------------------

def test_get_journal_entry_reads_header_and_lines(conn, book):
    hs = book["hs"]
    r = call_action(JR.get_journal_entry, conn, ns(journal_entry_id=book["je1"]))
    assert is_ok(r), r
    header = conn.execute("SELECT naming_series FROM journal_entry WHERE id = ?",
                          (book["je1"],)).fetchone()
    assert {k: r[k] for k in ("id", "naming_series", "posting_date", "entry_type",
                              "document_status", "total_debit", "total_credit",
                              "remark", "amended_from", "company_id")} == {
        "id": book["je1"], "naming_series": header["naming_series"],
        "posting_date": "2026-03-10", "entry_type": "journal",
        "document_status": "submitted", "total_debit": "400.00",
        "total_credit": "400.00", "remark": "March rent", "amended_from": None,
        "company_id": hs["company_id"]}
    line_ids = {row["id"] for row in conn.execute(
        "SELECT id FROM journal_entry_line WHERE journal_entry_id = ?", (book["je1"],))}
    assert {ln["id"] for ln in r["lines"]} == line_ids
    expected = [
        {"account_id": hs["expense"], "account_name": "Office Expense",
         "debit": "400.00", "credit": "0.00", "party_type": None, "party_id": None,
         "cost_center_id": hs["cc"], "project_id": None, "remark": "debit leg",
         "dimensions_json": "{}"},
        {"account_id": hs["bank"], "account_name": "Operating Bank",
         "debit": "0.00", "credit": "400.00", "party_type": None, "party_id": None,
         "cost_center_id": hs["cc"], "project_id": None, "remark": "credit leg",
         "dimensions_json": "{}"},
    ]
    stripped = [{k: v for k, v in ln.items() if k != "id"} for ln in r["lines"]]
    if get_dialect() == "postgresql":
        # line_order() falls back to id (random UUID) order on PostgreSQL, so sort by remark.
        assert sorted(stripped, key=lambda ln: ln["remark"]) == sorted(
            expected, key=lambda ln: ln["remark"])
    else:
        assert stripped == expected


def test_get_journal_entry_shows_cancelled_status_and_refuses(conn, book):
    r = call_action(JR.get_journal_entry, conn, ns(journal_entry_id=book["je2"]))
    assert (r["document_status"], r["total_debit"], len(r["lines"])) == \
        ("cancelled", "150.00", 2)
    r = call_action(JR.get_journal_entry, conn, ns(journal_entry_id=None))
    assert is_error(r) and _msg(r) == "--journal-entry-id is required"
    missing = _u()
    r = call_action(JR.get_journal_entry, conn, ns(journal_entry_id=missing))
    assert is_error(r) and _msg(r) == f"Journal entry {missing} not found"


# ---------------------------------------------------------------------------
# list-journal-entries
# ---------------------------------------------------------------------------

def _list_je(conn, company_id, **kw):
    args = dict(company_id=company_id, company_name=None, je_status=None, entry_type=None,
                from_date=None, to_date=None, account_id=None, limit=None, offset=None)
    args.update(kw)
    r = call_action(JR.list_journal_entries, conn, ns(**args))
    assert is_ok(r), r
    return r


def test_list_journal_entries_rows_order_and_filters(conn, book):
    cid = book["hs"]["company_id"]
    r = _list_je(conn, cid)
    assert (r["total_count"], r["limit"], r["offset"], r["has_more"]) == (5, 20, 0, False)
    assert [(e["id"], e["posting_date"], e["status"], e["total_debit"], e["total_credit"])
            for e in r["entries"]] == [
        (book["je5"], "2026-05-05", "submitted", "60.00", "60.00"),
        (book["je4"], "2026-04-20", "submitted", "20.00", "20.00"),
        (book["je3"], "2026-03-20", "draft", "75.00", "75.00"),
        (book["je2"], "2026-03-15", "cancelled", "150.00", "150.00"),
        (book["je1"], "2026-03-10", "submitted", "400.00", "400.00"),
    ]
    assert {e["entry_type"] for e in r["entries"]} == {"journal"}

    assert [e["id"] for e in _list_je(conn, cid, je_status="cancelled")["entries"]] == \
        [book["je2"]]
    assert [e["id"] for e in _list_je(conn, cid, je_status="submitted")["entries"]] == \
        [book["je5"], book["je4"], book["je1"]]
    windowed = _list_je(conn, cid, from_date="2026-03-15", to_date="2026-04-20")
    assert (windowed["total_count"], [e["id"] for e in windowed["entries"]]) == \
        (3, [book["je4"], book["je3"], book["je2"]])
    by_tax = _list_je(conn, cid, account_id=book["hs"]["sales_tax"])
    assert [e["id"] for e in by_tax["entries"]] == [book["je4"]]
    assert _list_je(conn, cid, entry_type="opening")["total_count"] == 0
    lt = _list_je(conn, book["lt"]["company_id"])
    assert [e["id"] for e in lt["entries"]] == [book["lt_je"]]


def test_list_journal_entries_pagination(conn, book):
    cid = book["hs"]["company_id"]
    page = _list_je(conn, cid, limit="2", offset="1")
    assert (page["total_count"], page["limit"], page["offset"], page["has_more"]) == \
        (5, 2, 1, True)
    assert [e["id"] for e in page["entries"]] == [book["je4"], book["je3"]]
    last = _list_je(conn, cid, limit="2", offset="4")
    assert ([e["id"] for e in last["entries"]], last["has_more"]) == ([book["je1"]], False)


# ---------------------------------------------------------------------------
# list-payments
# ---------------------------------------------------------------------------

def _list_pe(conn, company_id, **kw):
    args = dict(company_id=company_id, company_name=None, payment_type=None,
                party_type=None, party_id=None, pe_status=None, from_date=None,
                to_date=None, limit=None, offset=None)
    args.update(kw)
    r = call_action(PAY.list_payments, conn, ns(**args))
    assert is_ok(r), r
    return r


def test_list_payments_rows_amounts_and_party_names(conn, book):
    hs = book["hs"]
    r = _list_pe(conn, hs["company_id"])
    assert (r["total_count"], r["limit"], r["offset"], r["has_more"]) == (4, 20, 0, False)
    assert [(p["id"], p["payment_type"], p["posting_date"], p["party_type"], p["party_id"],
             p["party_name"], p["paid_amount"], p["status"], p["unallocated_amount"])
            for p in r["payments"]] == [
        (book["pe4"], "receive", "2026-04-18", "customer", hs["customer"], "Acme Corp",
         "45.00", "draft", "45.00"),
        (book["pe3"], "receive", "2026-04-15", "customer", hs["customer"], "Acme Corp",
         "100.00", "cancelled", "100.00"),
        (book["pe2"], "pay", "2026-04-12", "supplier", hs["supplier"], "Gotham Steel",
         "250.00", "submitted", "250.00"),
        (book["pe1"], "receive", "2026-04-10", "customer", hs["customer"], "Acme Corp",
         "600.00", "submitted", "0.00"),
    ]


def test_list_payments_filters_and_pagination(conn, book):
    hs = book["hs"]
    cid = hs["company_id"]
    ids = lambda r: [p["id"] for p in r["payments"]]  # noqa: E731
    assert ids(_list_pe(conn, cid, payment_type="pay")) == [book["pe2"]]
    assert ids(_list_pe(conn, cid, party_type="customer")) == \
        [book["pe4"], book["pe3"], book["pe1"]]
    assert ids(_list_pe(conn, cid, party_id=hs["supplier"])) == [book["pe2"]]
    assert ids(_list_pe(conn, cid, pe_status="cancelled")) == [book["pe3"]]
    windowed = _list_pe(conn, cid, from_date="2026-04-12", to_date="2026-04-15")
    assert (windowed["total_count"], ids(windowed)) == (2, [book["pe3"], book["pe2"]])
    page = _list_pe(conn, cid, limit="2", offset="1")
    assert (page["total_count"], page["has_more"], ids(page)) == \
        (4, True, [book["pe3"], book["pe2"]])
    assert ids(_list_pe(conn, book["lt"]["company_id"])) == [book["lt_pe"]]
    r = call_action(PAY.list_payments, conn, ns(
        company_id=None, company_name="Nowhere Inc", payment_type=None, party_type=None,
        party_id=None, pe_status=None, from_date=None, to_date=None, limit=None, offset=None))
    assert r["error"] == "Company 'Nowhere Inc' not found."
    assert "payments" not in r
