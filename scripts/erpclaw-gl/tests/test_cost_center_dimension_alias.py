"""A document tagged with a cost center posts its ledger rows to that cost center.

The ``cost_center`` dimension aliases ``gl_entry.cost_center_id``: the seam in
``validate_gl_entries`` copies a leg's ``dimensions["cost_center"]`` tag into an
empty ``cost_center_id`` (step 13 refuses a leg carrying two different ones),
and every document submit path uses the stored document tag in place of the
company default. A line's own cost center wins over a document tag.

Helpers below are copied from the neighbouring suites (never imported across
test files). Money is exact hand-written strings; ledger rows are read back
with PyPika and bound parameters.
"""
import argparse
import importlib.util
import io
import json
import os
import sys
import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_MODULE_DIR = os.path.dirname(_TESTS_DIR)
_SCRIPTS_DIR = os.path.dirname(_MODULE_DIR)
_SETUP_DIR = os.path.join(_SCRIPTS_DIR, "erpclaw-setup")

_IN_TREE_LIB = os.path.join(_SETUP_DIR, "lib")
if _IN_TREE_LIB not in sys.path:
    if importlib.util.find_spec("erpclaw_lib") is None:
        sys.path.insert(0, _IN_TREE_LIB)

from erpclaw_lib.db import setup_pragmas  # noqa: E402
from erpclaw_lib.gl_posting import (  # noqa: E402
    insert_gl_entries,
    validate_gl_entries,
)
from erpclaw_lib.query import Field, P, Q, Table  # noqa: E402


def _load(domain, tag):
    path = os.path.join(_SCRIPTS_DIR, domain, "db_query.py")
    spec = importlib.util.spec_from_file_location("db_query_cc_%s" % tag, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GL = _load("erpclaw-gl", "gl")
SELLING = _load("erpclaw-selling", "selling")
BUYING = _load("erpclaw-buying", "buying")
INVENTORY = _load("erpclaw-inventory", "inventory")
PAYMENTS = _load("erpclaw-payments", "payments")
JOURNALS = _load("erpclaw-journals", "journals")
REPORTS = _load("erpclaw-reports", "reports")

POSTING = "2026-06-15"


def call_action(fn, conn, args):
    buf = io.StringIO()

    def _fake_exit(code=0):
        raise SystemExit(code)

    try:
        with patch("sys.stdout", buf), patch("sys.exit", side_effect=_fake_exit):
            fn(conn, args)
    except SystemExit:
        pass
    output = buf.getvalue().strip()
    if not output:
        return {"status": "error", "message": "no output captured"}
    return json.loads(output)


def ns(**kwargs):
    return argparse.Namespace(**kwargs)


def is_error(result):
    return result.get("status") == "error"


def is_ok(result):
    return result.get("status") == "ok"


def _uuid():
    return str(uuid.uuid4())


def seed_company(conn, name="Test Co", abbr="TC"):
    cid = _uuid()
    conn.execute(
        """INSERT INTO company (id, name, abbr, default_currency, country,
           fiscal_year_start_month)
           VALUES (?, ?, ?, 'USD', 'United States', 1)""",
        (cid, "%s %s" % (name, cid[:6]), "%s%s" % (abbr, cid[:4])),
    )
    conn.commit()
    return cid


def seed_account(conn, company_id, name, root_type, account_type, number):
    aid = _uuid()
    direction = "debit_normal" if root_type in ("asset", "expense") else "credit_normal"
    conn.execute(
        """INSERT INTO account (id, name, account_number, root_type, account_type,
           balance_direction, company_id, depth, is_group)
           VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)""",
        (aid, name, number, root_type, account_type, direction, company_id),
    )
    conn.commit()
    return aid


def seed_fiscal_year(conn, company_id, name=None, start="2026-01-01", end="2026-12-31"):
    fid = _uuid()
    conn.execute(
        """INSERT INTO fiscal_year (id, name, start_date, end_date, company_id)
           VALUES (?, ?, ?, ?, ?)""",
        (fid, name or "FY-%s" % fid[:6], start, end, company_id),
    )
    conn.commit()
    return fid


def seed_cost_center(conn, company_id, name="Main CC", is_group=0):
    ccid = _uuid()
    conn.execute(
        """INSERT INTO cost_center (id, name, company_id, is_group)
           VALUES (?, ?, ?, ?)""",
        (ccid, name, company_id, is_group),
    )
    conn.commit()
    return ccid


def seed_customer(conn, company_id, name="Test Customer"):
    cid = _uuid()
    conn.execute(
        """INSERT INTO customer (id, name, company_id, customer_type, status, credit_limit)
           VALUES (?, ?, ?, 'company', 'active', '0')""",
        (cid, name, company_id),
    )
    conn.commit()
    return cid


def seed_supplier(conn, company_id, name="Test Supplier"):
    sid = _uuid()
    conn.execute(
        """INSERT INTO supplier (id, name, supplier_type, status, company_id)
           VALUES (?, ?, 'company', 'active', ?)""",
        (sid, name, company_id),
    )
    conn.commit()
    return sid


def seed_item(conn, name="Test Item", stock_uom="Each"):
    iid = _uuid()
    conn.execute(
        """INSERT INTO item (id, item_name, item_code, stock_uom, is_stock_item)
           VALUES (?, ?, ?, ?, 1)""",
        (iid, name, "ITEM-%s" % iid[:6], stock_uom),
    )
    conn.commit()
    return iid


def seed_warehouse(conn, company_id, name="Main Warehouse", account_id=None):
    wid = _uuid()
    conn.execute(
        """INSERT INTO warehouse (id, name, company_id, account_id)
           VALUES (?, ?, ?, ?)""",
        (wid, name, company_id, account_id),
    )
    conn.commit()
    return wid


def seed_stock_entry(conn, item_id, warehouse_id, qty="100", valuation_rate="10.00"):
    sle_id = _uuid()
    stock_value = str(Decimal(qty) * Decimal(valuation_rate))
    conn.execute(
        """INSERT INTO stock_ledger_entry
           (id, item_id, warehouse_id, posting_date, actual_qty,
            qty_after_transaction, valuation_rate, stock_value,
            stock_value_difference, voucher_type, voucher_id, is_cancelled)
           VALUES (?, ?, ?, '2026-01-01', ?, ?, ?, ?, ?, 'stock_entry', ?, 0)""",
        (sle_id, item_id, warehouse_id, qty, qty,
         valuation_rate, stock_value, stock_value,
         "INIT-%s" % sle_id[:8]),
    )
    conn.commit()


@pytest.fixture
def env(conn):
    cid = seed_company(conn)
    fyid = seed_fiscal_year(conn, cid)
    main_cc = seed_cost_center(conn, cid, "Main CC")
    sales_cc = seed_cost_center(conn, cid, "Sales")
    prod_cc = seed_cost_center(conn, cid, "Production")

    cash = seed_account(conn, cid, "Cash", "asset", "cash", "1000")
    bank = seed_account(conn, cid, "Bank", "asset", "bank", "1010")
    ar = seed_account(conn, cid, "Accounts Receivable", "asset", "receivable", "1100")
    ap = seed_account(conn, cid, "Trade Payables", "liability", "payable", "2000")
    revenue = seed_account(conn, cid, "Sales Revenue", "income", "revenue", "4000")
    expense = seed_account(conn, cid, "Office Expenses", "expense", "expense", "5000")
    cogs = seed_account(conn, cid, "Cost of Goods Sold", "expense",
                        "cost_of_goods_sold", "5100")
    stock = seed_account(conn, cid, "Stock In Hand", "asset", "stock", "1200")
    srnb = seed_account(conn, cid, "Stock Received Not Billed", "liability",
                        "stock_received_not_billed", "2150")
    stock_adj = seed_account(conn, cid, "Stock Adjustment", "expense",
                             "stock_adjustment", "5200")
    bad_debt = seed_account(conn, cid, "Bad Debts", "expense", "expense", "5300")

    wh = seed_warehouse(conn, cid, "Main Warehouse", stock)

    conn.execute(
        """UPDATE company SET
           default_receivable_account_id = ?,
           default_income_account_id = ?,
           default_payable_account_id = ?,
           default_expense_account_id = ?,
           default_cost_center_id = ?,
           default_warehouse_id = ?
           WHERE id = ?""",
        (ar, revenue, ap, expense, main_cc, wh, cid),
    )
    conn.commit()

    item = seed_item(conn, "Widget")
    customer = seed_customer(conn, cid)
    supplier = seed_supplier(conn, cid)

    return {
        "company_id": cid, "fiscal_year_id": fyid,
        "main_cc": main_cc, "sales_cc": sales_cc, "prod_cc": prod_cc,
        "cash": cash, "bank": bank, "ar": ar, "ap": ap,
        "revenue": revenue, "expense": expense, "cogs": cogs,
        "stock": stock, "srnb": srnb, "stock_adj": stock_adj,
        "bad_debt": bad_debt, "warehouse": wh,
        "item": item, "customer": customer, "supplier": supplier,
    }


def _gl_rows(conn, voucher_id):
    t = Table("gl_entry")
    q = (Q.from_(t)
         .select(Field("account_id"), Field("debit"), Field("credit"),
                 Field("cost_center_id"), Field("dimensions_json"),
                 Field("entry_set"), Field("remarks"), Field("is_cancelled"))
         .where(Field("voucher_id") == P()))
    return conn.execute(q.get_sql(), (voucher_id,)).fetchall()


def _live_rows(conn, voucher_id):
    return [r for r in _gl_rows(conn, voucher_id) if not r["is_cancelled"]]


def _by_account(rows, account_id):
    return [r for r in rows if r["account_id"] == account_id]


def _tag(conn, cc_id):
    return json.dumps({"cost_center": cc_id})


def _budget_report(conn, env, account_id, cc_id):
    return call_action(REPORTS.budget_vs_actual, conn, ns(
        fiscal_year_id=env["fiscal_year_id"], company_id=env["company_id"],
        account_id=account_id, cost_center_id=cc_id))


def test_seam_copies_the_tag(conn, env):
    entries = [
        {"account_id": env["expense"], "debit": "250.00", "credit": "0",
         "dimensions": {"cost_center": env["sales_cc"]}},
        {"account_id": env["cash"], "debit": "0", "credit": "250.00",
         "dimensions": {"cost_center": env["sales_cc"]}},
    ]
    insert_gl_entries(
        conn, entries,
        voucher_type="journal_entry", voucher_id="SEAM-1",
        posting_date=POSTING, company_id=env["company_id"],
        remarks="seam copies the tag",
    )
    rows = _live_rows(conn, "SEAM-1")
    assert len(rows) == 2
    for row in rows:
        assert row["cost_center_id"] == env["sales_cc"]
        assert json.loads(row["dimensions_json"]) == {"cost_center": env["sales_cc"]}


def test_seam_refuses_two_cost_centers(conn, env):
    entries = [
        {"account_id": env["expense"], "debit": "250.00", "credit": "0",
         "cost_center_id": env["prod_cc"],
         "dimensions": {"cost_center": env["sales_cc"]}},
        {"account_id": env["cash"], "debit": "0", "credit": "250.00"},
    ]
    try:
        insert_gl_entries(
            conn, entries,
            voucher_type="journal_entry", voucher_id="SEAM-2",
            posting_date=POSTING, company_id=env["company_id"],
            remarks="seam refuses two cost centers",
        )
        raised = None
    except ValueError as exc:
        raised = str(exc)
    assert raised is not None, "conflicting leg posted without refusal"
    assert raised.startswith("GL Validation Step 13 Failed:"), raised
    assert env["sales_cc"] in raised and env["prod_cc"] in raised, raised
    assert "two cost centers" in raised, raised
    assert _gl_rows(conn, "SEAM-2") == []


def test_seam_leaves_untagged_legs_alone(conn, env):
    entries = [
        {"account_id": env["expense"], "debit": "75.00", "credit": "0",
         "cost_center_id": env["prod_cc"]},
        {"account_id": env["cash"], "debit": "0", "credit": "75.00"},
    ]
    insert_gl_entries(
        conn, entries,
        voucher_type="journal_entry", voucher_id="SEAM-3",
        posting_date=POSTING, company_id=env["company_id"],
        remarks="untagged legs alone",
    )
    rows = _live_rows(conn, "SEAM-3")
    assert len(rows) == 2
    exp = _by_account(rows, env["expense"])
    assert len(exp) == 1
    assert exp[0]["cost_center_id"] == env["prod_cc"]
    assert json.loads(exp[0]["dimensions_json"]) == {}


def test_payment_tag_reaches_the_ledger(conn, env):
    created = call_action(PAYMENTS.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="pay",
        posting_date=POSTING, party_type="supplier",
        party_id=env["supplier"],
        paid_from_account=env["bank"], paid_to_account=env["ap"],
        paid_amount="100.00", exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None,
        allocations=None, deductions=None,
        dimensions=_tag(conn, env["sales_cc"]),
        dimension_key=None, dimension_value=None))
    assert is_ok(created), created
    sub = call_action(PAYMENTS.submit_payment, conn, ns(
        payment_entry_id=created["payment_entry_id"]))
    assert is_ok(sub), sub

    rows = _live_rows(conn, created["payment_entry_id"])
    assert len(rows) == 2
    for row in rows:
        assert row["cost_center_id"] == env["sales_cc"], dict(row)
        assert json.loads(row["dimensions_json"]) == {"cost_center": env["sales_cc"]}

    budgeted = call_action(GL.add_budget, conn, ns(
        fiscal_year_id=env["fiscal_year_id"], account_id=env["ap"],
        cost_center_id=env["sales_cc"], budget_amount="1000.00",
        action_if_exceeded="warn"))
    assert is_ok(budgeted), budgeted
    report = _budget_report(conn, env, env["ap"], env["sales_cc"])
    assert is_ok(report), report
    assert len(report["items"]) == 1
    item = report["items"][0]
    assert item["budget"] == "1000.00"
    assert item["actual"] == "100.00"
    assert item["variance"] == "900.00"
    assert item["variance_pct"] == "90.00"


def _submit_tagged_sales_invoice(conn, env, qty, rate, cc_key="sales_cc"):
    seed_stock_entry(conn, env["item"], env["warehouse"], qty="10", valuation_rate=rate)
    created = call_action(SELLING.create_sales_invoice, conn, ns(
        customer_id=env["customer"], company_id=env["company_id"],
        posting_date=POSTING,
        items=json.dumps([{"item_id": env["item"], "qty": qty, "rate": rate,
                           "warehouse_id": env["warehouse"]}]),
        sales_order_id=None, delivery_note_id=None,
        tax_template_id=None, due_date=None,
        dimensions=_tag(conn, env[cc_key]),
        dimension_key=None, dimension_value=None))
    assert is_ok(created), created
    sub = call_action(SELLING.submit_sales_invoice, conn, ns(
        sales_invoice_id=created["sales_invoice_id"]))
    assert is_ok(sub), sub
    return created["sales_invoice_id"]


def test_sales_invoice_tag_reaches_the_ledger(conn, env):
    si_id = _submit_tagged_sales_invoice(conn, env, "1", "1000.00")

    rows = _live_rows(conn, si_id)
    rev = _by_account(rows, env["revenue"])
    rec = _by_account(rows, env["ar"])
    assert len(rev) == 1 and len(rec) == 1
    assert (rev[0]["debit"], rev[0]["credit"]) == ("0.00", "1000.00")
    assert (rec[0]["debit"], rec[0]["credit"]) == ("1000.00", "0.00")
    for row in (rev[0], rec[0]):
        assert row["cost_center_id"] == env["sales_cc"], dict(row)
        assert json.loads(row["dimensions_json"]) == {"cost_center": env["sales_cc"]}

    budgeted = call_action(GL.add_budget, conn, ns(
        fiscal_year_id=env["fiscal_year_id"], account_id=env["revenue"],
        cost_center_id=env["sales_cc"], budget_amount="1000.00",
        action_if_exceeded="warn"))
    assert is_ok(budgeted), budgeted
    report = _budget_report(conn, env, env["revenue"], env["sales_cc"])
    assert is_ok(report), report
    assert len(report["items"]) == 1
    item = report["items"][0]
    assert item["budget"] == "1000.00"
    assert item["actual"] == "-1000.00"
    assert item["variance"] == "2000.00"
    assert item["variance_pct"] == "200.00"


def test_stock_issue_tag_reaches_the_ledger(conn, env):
    received = call_action(INVENTORY.add_stock_entry, conn, ns(
        entry_type="receive", company_id=env["company_id"],
        posting_date=POSTING,
        items=json.dumps([{"item_id": env["item"], "qty": "100",
                           "rate": "10.00",
                           "to_warehouse_id": env["warehouse"]}]),
        supplier_warehouse_id=None, work_order_id=None,
        dimensions=None, dimension_key=None, dimension_value=None))
    assert is_ok(received), received
    sub_rec = call_action(INVENTORY.submit_stock_entry, conn, ns(
        stock_entry_id=received["stock_entry_id"]))
    assert is_ok(sub_rec), sub_rec

    issued = call_action(INVENTORY.add_stock_entry, conn, ns(
        entry_type="issue", company_id=env["company_id"],
        posting_date=POSTING,
        items=json.dumps([{"item_id": env["item"], "qty": "50",
                           "rate": "10.00",
                           "from_warehouse_id": env["warehouse"]}]),
        supplier_warehouse_id=None, work_order_id=None,
        dimensions=_tag(conn, env["prod_cc"]),
        dimension_key=None, dimension_value=None))
    assert is_ok(issued), issued
    sub_issue = call_action(INVENTORY.submit_stock_entry, conn, ns(
        stock_entry_id=issued["stock_entry_id"]))
    assert is_ok(sub_issue), sub_issue

    rows = _live_rows(conn, issued["stock_entry_id"])
    exp = _by_account(rows, env["cogs"])
    stk = _by_account(rows, env["stock"])
    assert len(exp) == 1 and len(stk) == 1
    assert (exp[0]["debit"], exp[0]["credit"]) == ("500.00", "0.00")
    assert (stk[0]["debit"], stk[0]["credit"]) == ("0.00", "500.00")
    for row in (exp[0], stk[0]):
        assert row["cost_center_id"] == env["prod_cc"], dict(row)
        assert json.loads(row["dimensions_json"]) == {"cost_center": env["prod_cc"]}


def test_line_cost_center_wins_over_the_document_tag(conn, env):
    created = call_action(BUYING.create_purchase_invoice, conn, ns(
        supplier_id=env["supplier"], company_id=env["company_id"],
        posting_date=POSTING, due_date=None,
        items=json.dumps([{"item_id": env["item"], "qty": "1",
                           "rate": "150.00",
                           "cost_center_id": env["prod_cc"]}]),
        purchase_order_id=None, purchase_receipt_id=None,
        tax_template_id=None, cwip_asset_id=None,
        dimensions=_tag(conn, env["sales_cc"]),
        dimension_key=None, dimension_value=None))
    assert is_ok(created), created
    sub = call_action(BUYING.submit_purchase_invoice, conn, ns(
        purchase_invoice_id=created["purchase_invoice_id"]))
    assert is_ok(sub), sub

    rows = _live_rows(conn, created["purchase_invoice_id"])
    exp = _by_account(rows, env["expense"])
    assert len(exp) == 1
    assert (exp[0]["debit"], exp[0]["credit"]) == ("150.00", "0.00")
    assert exp[0]["cost_center_id"] == env["prod_cc"], dict(exp[0])
    assert json.loads(exp[0]["dimensions_json"]) == {"cost_center": env["prod_cc"]}

    je = call_action(JOURNALS.add_journal_entry, conn, ns(
        company_id=env["company_id"], company_name=None,
        posting_date=POSTING, entry_type="journal", remark="line wins",
        lines=json.dumps([
            {"account_id": env["expense"], "debit": "200.00", "credit": "0",
             "cost_center_id": env["prod_cc"]},
            {"account_id": env["cash"], "debit": "0", "credit": "200.00"},
        ]),
        cwip_asset_id=None,
        dimensions=_tag(conn, env["sales_cc"]),
        dimension_key=None, dimension_value=None))
    assert is_ok(je), je
    sub_je = call_action(JOURNALS.submit_journal_entry, conn, ns(
        journal_entry_id=je["journal_entry_id"]))
    assert is_ok(sub_je), sub_je

    je_rows = _live_rows(conn, je["journal_entry_id"])
    je_exp = _by_account(je_rows, env["expense"])
    je_cash = _by_account(je_rows, env["cash"])
    assert len(je_exp) == 1 and len(je_cash) == 1
    assert je_exp[0]["cost_center_id"] == env["prod_cc"], dict(je_exp[0])
    assert json.loads(je_exp[0]["dimensions_json"]) == {"cost_center": env["prod_cc"]}
    assert je_cash[0]["cost_center_id"] == env["sales_cc"], dict(je_cash[0])
    assert json.loads(je_cash[0]["dimensions_json"]) == {"cost_center": env["sales_cc"]}


def test_untagged_documents_unchanged(conn):
    cid = seed_company(conn, "Second Co", "SC")
    fyid = seed_fiscal_year(conn, cid)
    default_cc = seed_cost_center(conn, cid, "Main CC")
    ar = seed_account(conn, cid, "Accounts Receivable", "asset", "receivable", "1100")
    revenue = seed_account(conn, cid, "Sales Revenue", "income", "revenue", "4000")
    stock = seed_account(conn, cid, "Stock In Hand", "asset", "stock", "1200")
    cogs = seed_account(conn, cid, "Cost of Goods Sold", "expense",
                        "cost_of_goods_sold", "5100")
    wh = seed_warehouse(conn, cid, "Second Warehouse", stock)
    conn.execute(
        """UPDATE company SET
           default_receivable_account_id = ?,
           default_income_account_id = ?,
           default_cost_center_id = ?,
           default_warehouse_id = ?
           WHERE id = ?""",
        (ar, revenue, default_cc, wh, cid),
    )
    conn.commit()
    item = seed_item(conn, "Second Widget")
    customer = seed_customer(conn, cid)
    seed_stock_entry(conn, item, wh, qty="10", valuation_rate="100.00")

    created = call_action(SELLING.create_sales_invoice, conn, ns(
        customer_id=customer, company_id=cid,
        posting_date=POSTING,
        items=json.dumps([{"item_id": item, "qty": "1", "rate": "100.00",
                           "warehouse_id": wh}]),
        sales_order_id=None, delivery_note_id=None,
        tax_template_id=None, due_date=None,
        dimensions=None, dimension_key=None, dimension_value=None))
    assert is_ok(created), created
    sub = call_action(SELLING.submit_sales_invoice, conn, ns(
        sales_invoice_id=created["sales_invoice_id"]))
    assert is_ok(sub), sub

    rows = _live_rows(conn, created["sales_invoice_id"])
    rev = _by_account(rows, revenue)
    rec = _by_account(rows, ar)
    assert len(rev) == 1 and len(rec) == 1
    assert rev[0]["cost_center_id"] == default_cc, dict(rev[0])
    assert rec[0]["cost_center_id"] is None, dict(rec[0])


def test_cancel_mirrors_the_cost_center(conn, env):
    si_id = _submit_tagged_sales_invoice(conn, env, "1", "1000.00")
    cancelled = call_action(SELLING.cancel_sales_invoice, conn, ns(
        sales_invoice_id=si_id))
    assert is_ok(cancelled), cancelled

    rows = _gl_rows(conn, si_id)
    reversals = [r for r in rows
                 if (r["remarks"] or "").startswith("Reversal of")]
    assert len(reversals) >= 2
    for row in reversals:
        assert row["cost_center_id"] == env["sales_cc"], dict(row)
        assert json.loads(row["dimensions_json"]) == {"cost_center": env["sales_cc"]}


def test_payment_deduction_and_write_off_follow_the_tag(conn, env):
    created = call_action(PAYMENTS.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="pay",
        posting_date=POSTING, party_type="supplier",
        party_id=env["supplier"],
        paid_from_account=env["bank"], paid_to_account=env["ap"],
        paid_amount="100.00", exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None,
        allocations=None,
        deductions=json.dumps([{"account_id": env["expense"],
                                "amount": "20.00", "type": "commission"}]),
        dimensions=_tag(conn, env["sales_cc"]),
        dimension_key=None, dimension_value=None))
    assert is_ok(created), created
    sub = call_action(PAYMENTS.submit_payment, conn, ns(
        payment_entry_id=created["payment_entry_id"]))
    assert is_ok(sub), sub

    rows = _live_rows(conn, created["payment_entry_id"])
    ded = _by_account(rows, env["expense"])
    assert len(ded) == 1
    assert (ded[0]["debit"], ded[0]["credit"]) == ("0.00", "20.00")
    assert ded[0]["cost_center_id"] == env["sales_cc"], dict(ded[0])

    si_id = _submit_tagged_sales_invoice(conn, env, "1", "1000.00")

    refused = call_action(PAYMENTS.write_off_invoice, conn, ns(
        voucher_type="sales_invoice", voucher_id=si_id,
        write_off_amount="200.00", write_off_account_id=env["bad_debt"],
        reason="bad debt review", posting_date="2026-06-20",
        cost_center_id=env["prod_cc"]))
    assert is_error(refused), refused
    text = refused.get("message", "") + refused.get("error", "")
    assert env["prod_cc"] in text and env["sales_cc"] in text, text
    rows = _gl_rows(conn, si_id)
    assert [r for r in rows if r["entry_set"] == "write_off"] == []

    written = call_action(PAYMENTS.write_off_invoice, conn, ns(
        voucher_type="sales_invoice", voucher_id=si_id,
        write_off_amount="200.00", write_off_account_id=env["bad_debt"],
        reason="bad debt review", posting_date="2026-06-20",
        cost_center_id=None))
    assert is_ok(written), written
    rows = _gl_rows(conn, si_id)
    wo = [r for r in rows if r["entry_set"] == "write_off"]
    assert len(wo) == 2
    legs = _by_account(wo, env["bad_debt"])
    assert len(legs) == 1
    assert (legs[0]["debit"], legs[0]["credit"]) == ("200.00", "0.00")
    assert legs[0]["cost_center_id"] == env["sales_cc"], dict(legs[0])


def test_delivery_note_cogs_follows_the_tag(conn, env):
    seed_stock_entry(conn, env["item"], env["warehouse"], qty="100", valuation_rate="10.00")
    so = call_action(SELLING.add_sales_order, conn, ns(
        customer_id=env["customer"], company_id=env["company_id"],
        posting_date=POSTING,
        items=json.dumps([{"item_id": env["item"], "qty": "5",
                           "rate": "10.00",
                           "warehouse_id": env["warehouse"]}]),
        delivery_date="2026-07-01", tax_template_id=None,
        dimensions=None, dimension_key=None, dimension_value=None))
    assert is_ok(so), so
    sub_so = call_action(SELLING.submit_sales_order, conn, ns(
        sales_order_id=so["sales_order_id"]))
    assert is_ok(sub_so), sub_so

    dn = call_action(SELLING.create_delivery_note, conn, ns(
        sales_order_id=so["sales_order_id"], posting_date=POSTING,
        items=None,
        dimensions=_tag(conn, env["prod_cc"]),
        dimension_key=None, dimension_value=None))
    assert is_ok(dn), dn
    sub_dn = call_action(SELLING.submit_delivery_note, conn, ns(
        delivery_note_id=dn["delivery_note_id"]))
    assert is_ok(sub_dn), sub_dn

    rows = _live_rows(conn, dn["delivery_note_id"])
    cogs = _by_account(rows, env["cogs"])
    assert len(cogs) == 1
    assert (cogs[0]["debit"], cogs[0]["credit"]) == ("50.00", "0.00")
    assert cogs[0]["cost_center_id"] == env["prod_cc"], dict(cogs[0])
    assert json.loads(cogs[0]["dimensions_json"]) == {"cost_center": env["prod_cc"]}


def test_group_cost_center_tag_refused_at_submit(conn, env):
    group_cc = seed_cost_center(conn, env["company_id"], "Group CC", is_group=1)
    seed_stock_entry(conn, env["item"], env["warehouse"], qty="10", valuation_rate="100.00")
    created = call_action(SELLING.create_sales_invoice, conn, ns(
        customer_id=env["customer"], company_id=env["company_id"],
        posting_date=POSTING,
        items=json.dumps([{"item_id": env["item"], "qty": "1",
                           "rate": "100.00",
                           "warehouse_id": env["warehouse"]}]),
        sales_order_id=None, delivery_note_id=None,
        tax_template_id=None, due_date=None,
        dimensions=_tag(conn, group_cc),
        dimension_key=None, dimension_value=None))
    assert is_ok(created), created

    sub = call_action(SELLING.submit_sales_invoice, conn, ns(
        sales_invoice_id=created["sales_invoice_id"]))
    assert is_error(sub), sub
    text = sub.get("message", "") + sub.get("error", "")
    assert "GL Validation Step 8 Failed:" in text, text

    t = Table("sales_invoice")
    status = conn.execute(
        Q.from_(t).select(Field("status")).where(Field("id") == P()).get_sql(),
        (created["sales_invoice_id"],)).fetchone()["status"]
    assert status == "draft"
    assert _gl_rows(conn, created["sales_invoice_id"]) == []


def test_leg_conflict_refuses(conn, env):
    entries = [
        {"account_id": env["expense"], "debit": "60.00", "credit": "0",
         "cost_center_id": env["prod_cc"],
         "dimensions": {"cost_center": env["sales_cc"]}},
        {"account_id": env["cash"], "debit": "0", "credit": "60.00"},
    ]
    try:
        validate_gl_entries(
            conn, entries, env["company_id"], POSTING,
            voucher_type="journal_entry",
        )
        raised = None
    except ValueError as exc:
        raised = str(exc)
    assert raised is not None, "conflicting leg validated without refusal"
    assert raised.startswith("GL Validation Step 13 Failed:"), raised
    assert env["sales_cc"] in raised and env["prod_cc"] in raised, raised
