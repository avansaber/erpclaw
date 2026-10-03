"""Behavioural depth for ten erpclaw-reports actions (task m475).

Each action below previously had only a shape test (asserts on the response
envelope) or a routability test (the contract suite's ``"Unknown action" not
in ...``). Neither observes the database, so an action could return a perfect
envelope while writing nothing — or the wrong thing — and stay green. Every
test here drives the REAL action against a fresh core DB, reads the stored
rows back with PyPika-built queries through ``erpclaw_lib.query`` on a
connection from ``erpclaw_lib.db.get_connection``, and compares exact values;
money is compared as exact ``Decimal`` strings, never float, never rounded.

Per-action depth (stored row vs ledger effect):

- cash-flow: ledger effect. Opening/closing/net/operating and the per-account
  details are pinned against the known GL legs of a hand-built book; the
  report is read-only so no ledger assertion can hold for it — the
  write-nothing proof is a full snapshot taken before and after the call.
- budget-vs-actual: stored row + ledger effect. The budget rows are read back
  with exact amounts, and each reported actual/variance pair is pinned
  against the GL the owner actions posted.
- budget-variance: alias of the same function. Parity with budget-vs-actual
  is pinned on the same book, plus its own refusal case.
- comparative-pl: ledger effect. Per-period account amounts and totals are
  pinned against the GL legs; read-only, so the snapshot is the no-write
  proof and no ledger assertion is made for the report itself.
- gl-summary: ledger effect. Group totals are pinned per voucher type, every
  group is asserted balanced (debit equals credit), and the invoice legs are
  read back individually; read-only, snapshot proves no write.
- tax-summary: ledger effect. Collected/paid/net and the per-account row are
  pinned against the tax legs; read-only, snapshot proves no write.
- payment-summary: stored row. The payment_entry rows are read back with
  exact amounts and statuses; the report reads them, so no ledger assertion
  can hold for it — the snapshot is the no-write proof.
- add-elimination-rule / list-elimination-rules / list-elimination-entries:
  retired. Each test pins the refusal (a truthful steer naming the action
  and every step of the replacement flow) and proves the legacy tables and
  the ledger are byte-identical afterwards. These actions reach no ledger,
  so no ledger assertion can hold; the nothing-lands proof is the snapshot.

Cash-flow rule (see TestCashFlowNullAccountType): an account with a NULL
account_type is classified by its root type and is never a cash account, so
operating reconciles to the net change.

No test in this file inspects catalog tables or sets connection options;
reads are PyPika-built and run on a connection from
``erpclaw_lib.db.get_connection``.
"""
import importlib.util
import json
import os
import sys
import uuid
from decimal import Decimal

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_MODULE_DIR = os.path.dirname(_TESTS_DIR)                 # erpclaw-reports/
_SCRIPTS_DIR = os.path.dirname(_MODULE_DIR)               # scripts/
_SETUP_DIR = os.path.join(_SCRIPTS_DIR, "erpclaw-setup")

# Bind erpclaw_lib to THIS TREE's lib, not the deployed ~/.openclaw symlink:
# the symlink can point at another worktree/branch, which would make these
# tests exercise foreign lib code.
_IN_TREE_LIB = os.path.join(_SETUP_DIR, "lib")
ERPCLAW_LIB = (_IN_TREE_LIB if os.path.isdir(os.path.join(_IN_TREE_LIB, "erpclaw_lib"))
               else os.path.join(os.path.expanduser(
                   os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))
if ERPCLAW_LIB not in sys.path:
    if importlib.util.find_spec("erpclaw_lib") is None:
        sys.path.insert(0, ERPCLAW_LIB)

_PAY_TESTS = os.path.join(_SCRIPTS_DIR, "erpclaw-payments", "tests")
if _PAY_TESTS not in sys.path:
    sys.path.append(_PAY_TESTS)

from payments_helpers import (call_action, get_conn, is_error, is_ok,  # noqa: E402
                                ns)

from erpclaw_lib import seam  # noqa: E402
from erpclaw_lib.query import Field, P, Q, Table, fn, insert_row  # noqa: E402


def _load(name, domain):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_SCRIPTS_DIR, domain, "db_query.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


REP = _load("db_query_reports_m475", "erpclaw-reports")
JR = _load("db_query_journals_m475", "erpclaw-journals")
PAY = _load("db_query_payments_m475", "erpclaw-payments")
SEL = _load("db_query_selling_m475", "erpclaw-selling")
GL = _load("db_query_gl_m475", "erpclaw-gl")

WINDOW = ("2026-03-01", "2026-04-30")

ADVACCT_FLOW = [
    "add-consolidation-group",
    "add-group-entity",
    "add-ic-transaction",
    "approve-ic-transaction",
    "post-ic-transaction",
    "generate-elimination-entries",
]


@pytest.fixture
def conn(db_path):
    connection = get_conn(db_path)
    yield connection
    connection.close()


def _u():
    return str(uuid.uuid4())


def _msg(result):
    return result.get("message", "")


def _row(conn, table, row_id):
    t = Table(table)
    q = Q.from_(t).select(t.star).where(t.id == P())
    found = conn.execute(q.get_sql(), (row_id,)).fetchone()
    assert found is not None, "%s %s not found" % (table, row_id)
    return dict(found)


def _where(conn, table, **filters):
    t = Table(table)
    q = Q.from_(t).select(t.star)
    params = []
    for column, value in filters.items():
        q = q.where(Field(column) == P())
        params.append(value)
    return [dict(r) for r in conn.execute(q.get_sql(), params).fetchall()]


def _all(conn, table):
    t = Table(table)
    q = Q.from_(t).select(t.star).orderby(t.id)
    return [dict(r) for r in conn.execute(q.get_sql()).fetchall()]


def _count(conn, table):
    t = Table(table)
    q = Q.from_(t).select(fn.Count("*").as_("n"))
    return conn.execute(q.get_sql()).fetchone()["n"]


def _snapshot(conn, tables):
    return {name: _all(conn, name) for name in tables}


def _insert(conn, table, row):
    sql, _cols = insert_row(table, {key: P() for key in row})
    conn.execute(sql, tuple(row.values()))


def _gl_legs(conn, voucher_type, voucher_id):
    t = Table("gl_entry")
    q = (Q.from_(t)
         .select(t.account_id, t.debit, t.credit, t.posting_date,
                 t.voucher_type, t.voucher_id, t.is_cancelled)
         .where(t.voucher_type == P())
         .where(t.voucher_id == P()))
    return [dict(r) for r in
            conn.execute(q.get_sql(), (voucher_type, voucher_id)).fetchall()]


_LEDGERS = ("gl_entry", "payment_ledger_entry", "stock_ledger_entry")

BOOK_TABLES = (
    "company", "fiscal_year", "cost_center", "account", "customer",
    "supplier", "item", "tax_template", "tax_template_line",
    "journal_entry", "journal_entry_line", "sales_invoice",
    "sales_invoice_item", "payment_entry", "payment_allocation",
    "gl_entry", "payment_ledger_entry", "stock_ledger_entry", "budget",
    "naming_series", "audit_log",
)

LEGACY_TABLES = ("company", "account", "fiscal_year", "gl_entry",
                 "elimination_rule", "elimination_entry", "audit_log")


def _account(conn, company_id, name, number, root_type, account_type):
    aid = _u()
    direction = "debit_normal" if root_type in ("asset", "expense") else "credit_normal"
    # The expense account carries its registered type on purpose: cash-flow
    # builds its bank/cash population from account_type, so a NULL here would
    # conflate two filters. The NULL-type shape is pinned separately in
    # TestCashFlowNullAccountType.
    _insert(conn, "account", {
        "id": aid, "name": name, "account_number": number,
        "root_type": root_type, "account_type": account_type,
        "balance_direction": direction, "company_id": company_id,
        "depth": 0})
    return aid


def _company(conn, name, abbr):
    cid = _u()
    _insert(conn, "company", {"id": cid, "name": name, "abbr": abbr})
    _insert(conn, "fiscal_year", {
        "id": _u(), "name": "FY-2026-%s" % abbr, "start_date": "2026-01-01",
        "end_date": "2026-12-31", "is_closed": 0, "company_id": cid})
    cc = _u()
    _insert(conn, "cost_center", {
        "id": cc, "name": "Main - %s" % abbr, "company_id": cid,
        "is_group": 0})
    env = {
        "company_id": cid, "cc": cc,
        "bank": _account(conn, cid, "Operating Bank", "1010", "asset", "bank"),
        "ar": _account(conn, cid, "Accounts Receivable", "1100", "asset", "receivable"),
        "ap": _account(conn, cid, "Accounts Payable", "2100", "liability", "payable"),
        "sales_tax": _account(conn, cid, "Sales Tax Payable", "2200", "liability", "tax"),
        "input_tax": _account(conn, cid, "Input Tax", "1400", "asset", "tax"),
        "revenue": _account(conn, cid, "Service Revenue", "4000", "income", "revenue"),
        "expense": _account(conn, cid, "Office Expense", "5100", "expense", "expense"),
    }
    conn.execute(
        "UPDATE company SET default_cost_center_id = ?, "
        "default_receivable_account_id = ?, default_income_account_id = ? "
        "WHERE id = ?", (cc, env["ar"], env["revenue"], cid))
    env["customer"] = _u()
    _insert(conn, "customer", {
        "id": env["customer"],
        "name": "Acme Corp" if abbr == "HS" else "Birch Ltd",
        "company_id": cid, "customer_type": "company", "status": "active",
        "credit_limit": "0"})
    env["supplier"] = _u()
    _insert(conn, "supplier", {
        "id": env["supplier"], "name": "Gotham Steel",
        "supplier_type": "company", "status": "active", "company_id": cid})
    env["item"] = _u()
    _insert(conn, "item", {
        "id": env["item"], "item_name": "Consulting",
        "item_code": "SVC-%s" % abbr, "stock_uom": "Hour",
        "is_stock_item": 0})
    env["tax_tpl"] = _u()
    _insert(conn, "tax_template", {
        "id": env["tax_tpl"], "name": "Sales Tax 8.25 %s" % abbr,
        "tax_type": "sales", "company_id": cid})
    _insert(conn, "tax_template_line", {
        "id": _u(), "tax_template_id": env["tax_tpl"],
        "tax_account_id": env["sales_tax"], "rate": "8.25",
        "charge_type": "on_net_total", "row_order": 0, "add_deduct": "add"})
    conn.commit()
    return env


def _je(conn, env, date, debit_acct, credit_acct, amount, submit=True):
    lines = [
        {"account_id": env[debit_acct], "debit": amount, "credit": "0",
         "cost_center_id": env["cc"], "remark": "debit leg"},
        {"account_id": env[credit_acct], "debit": "0", "credit": amount,
         "cost_center_id": env["cc"], "remark": "credit leg"}]
    r = call_action(JR.add_journal_entry, conn, ns(
        company_id=env["company_id"], posting_date=date, entry_type=None,
        remark=None, lines=json.dumps(lines), cwip_asset_id=None))
    assert is_ok(r), r
    je_id = r["journal_entry_id"]
    if submit:
        r = call_action(JR.submit_journal_entry, conn, ns(journal_entry_id=je_id))
        assert is_ok(r), r
    return je_id


def _invoice(conn, env, date, qty):
    r = call_action(SEL.create_sales_invoice, conn, ns(
        company_id=env["company_id"], customer_id=env["customer"],
        tax_template_id=env["tax_tpl"], sales_order_id=None,
        delivery_note_id=None, posting_date=date, due_date="2026-06-30",
        payment_terms_id=None,
        items=json.dumps([{"item_id": env["item"], "qty": qty,
                           "rate": "100.00"}])))
    assert is_ok(r), r
    si_id = r["sales_invoice_id"]
    r = call_action(SEL.submit_sales_invoice, conn, ns(sales_invoice_id=si_id))
    assert is_ok(r), r
    return si_id


def _payment(conn, env, date, payment_type, amount, allocations=None,
             submit=True):
    if payment_type == "receive":
        party = ("customer", env["customer"], env["ar"], env["bank"])
    else:
        party = ("supplier", env["supplier"], env["bank"], env["ap"])
    r = call_action(PAY.add_payment, conn, ns(
        company_id=env["company_id"], payment_type=payment_type,
        posting_date=date, party_type=party[0], party_id=party[1],
        paid_from_account=party[2], paid_to_account=party[3],
        paid_amount=amount, exchange_rate=None, payment_currency=None,
        reference_number=None, reference_date=None,
        allocations=json.dumps(allocations) if allocations else None,
        deductions=None))
    assert is_ok(r), r
    pe_id = r["payment_entry_id"]
    if submit:
        r = call_action(PAY.submit_payment, conn, ns(payment_entry_id=pe_id))
        assert is_ok(r), r
    return pe_id


@pytest.fixture
def book(conn):
    """Harbor Supply + Lakeside Traders, every document posted through its
    real action (amounts mirror the sibling value suite, plus two budgets):

      JE-1  2026-03-10  submitted  DR Office Expense 400.00 / CR Bank 400.00
      JE-2  2026-03-15  CANCELLED  DR Office Expense 150.00 / CR Bank 150.00
      JE-3  2026-03-20  draft      DR Office Expense  75.00 / CR Bank  75.00
      SI-1  2026-04-01  submitted  10 x 100.00 + 8.25 % tax = 1082.50
      SI-2  2026-04-05  CANCELLED   2 x 100.00 + 8.25 % tax =  216.50
      PE-1  2026-04-10  submitted  receive 600.00, allocated to SI-1
      PE-2  2026-04-12  submitted  pay 250.00, unallocated
      PE-3  2026-04-15  CANCELLED  receive 100.00, unallocated
      PE-4  2026-04-18  draft      receive 45.00
      JE-4  2026-04-20  submitted  DR Sales Tax Payable 20.00 / CR Bank 20.00
      JE-5  2026-05-05  submitted  DR Office Expense 60.00 / CR Bank 60.00
      LT    2026-04-02  submitted  DR Bank 30.00 / CR Sales Tax Payable 30.00
      LT    2026-04-03  submitted  receive 777.00
      budgets (FY-2026-HS, via the owning erpclaw-gl action):
        Office Expense 1000.00, Service Revenue 1500.00
    """
    hs = _company(conn, "Harbor Supply", "HS")
    ids = {"hs": hs}
    ids["je1"] = _je(conn, hs, "2026-03-10", "expense", "bank", "400.00")
    ids["je2"] = _je(conn, hs, "2026-03-15", "expense", "bank", "150.00")
    r = call_action(JR.cancel_journal_entry, conn,
                    ns(journal_entry_id=ids["je2"]))
    assert is_ok(r), r
    ids["je3"] = _je(conn, hs, "2026-03-20", "expense", "bank", "75.00",
                     submit=False)
    ids["si1"] = _invoice(conn, hs, "2026-04-01", "10")
    ids["si2"] = _invoice(conn, hs, "2026-04-05", "2")
    r = call_action(SEL.cancel_sales_invoice, conn,
                    ns(sales_invoice_id=ids["si2"]))
    assert is_ok(r), r
    ids["pe1"] = _payment(conn, hs, "2026-04-10", "receive", "600.00",
                          allocations=[
                              {"voucher_type": "sales_invoice",
                               "voucher_id": ids["si1"],
                               "allocated_amount": "600.00"}])
    ids["pe2"] = _payment(conn, hs, "2026-04-12", "pay", "250.00")
    ids["pe3"] = _payment(conn, hs, "2026-04-15", "receive", "100.00")
    r = call_action(PAY.cancel_payment, conn,
                    ns(payment_entry_id=ids["pe3"]))
    assert is_ok(r), r
    ids["pe4"] = _payment(conn, hs, "2026-04-18", "receive", "45.00",
                          submit=False)
    ids["je4"] = _je(conn, hs, "2026-04-20", "sales_tax", "bank", "20.00")
    ids["je5"] = _je(conn, hs, "2026-05-05", "expense", "bank", "60.00")

    lt = _company(conn, "Lakeside Traders", "LT")
    ids["lt"] = lt
    ids["lt_je"] = _je(conn, lt, "2026-04-02", "bank", "sales_tax", "30.00")
    ids["lt_pe"] = _payment(conn, lt, "2026-04-03", "receive", "777.00")

    fy = _where(conn, "fiscal_year",
                company_id=hs["company_id"])[0]["id"]
    ids["fy"] = fy
    for account_key, amount in (("expense", "1000.00"),
                                ("revenue", "1500.00")):
        r = call_action(GL.add_budget, conn, ns(
            fiscal_year_id=fy, budget_amount=amount,
            account_id=hs[account_key], cost_center_id=None,
            action_if_exceeded=None))
        assert is_ok(r), r
    return ids


def _legacy_metadata():
    """The retired pair, in the shape erpclaw-growth's init_db used to create.

    Declared, never hand-written: ``seam.provision`` emits the
    dialect-correct DDL.
    """
    sa = seam._sqlalchemy()
    md = sa.MetaData()
    sa.Table(
        "elimination_rule", md,
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("source_company_id", sa.Text, nullable=False),
        sa.Column("target_company_id", sa.Text, nullable=False),
        sa.Column("source_account_id", sa.Text, nullable=False),
        sa.Column("target_account_id", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False,
                  server_default="active"),
        sa.Column("created_at", sa.Text),
        sa.Column("updated_at", sa.Text),
    )
    sa.Table(
        "elimination_entry", md,
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("elimination_rule_id", sa.Text,
                  sa.ForeignKey("elimination_rule.id",
                                ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("fiscal_year_id", sa.Text),
        sa.Column("posting_date", sa.Text, nullable=False),
        sa.Column("amount", sa.Text, nullable=False, server_default="0"),
        sa.Column("source_gl_entry_id", sa.Text),
        sa.Column("target_gl_entry_id", sa.Text),
        sa.Column("status", sa.Text, nullable=False,
                  server_default="posted"),
        sa.Column("created_at", sa.Text),
    )
    return md


@pytest.fixture
def legacy_env(conn, db_path):
    """A PRE-migration install: legacy tables present and seeded with a live
    rule, so "the table is gone" cannot be what makes a retirement test pass.
    """
    seam.provision(_legacy_metadata(), db_path)
    co_a, co_b = _u(), _u()
    _insert(conn, "company", {"id": co_a, "name": "Parent Co",
                              "abbr": "PC"})
    _insert(conn, "company", {"id": co_b, "name": "Sub Co", "abbr": "SC"})
    inc, exp = _u(), _u()
    _insert(conn, "account", {
        "id": inc, "name": "IC Revenue", "root_type": "income",
        "account_type": "revenue", "company_id": co_a})
    _insert(conn, "account", {
        "id": exp, "name": "IC Expense", "root_type": "expense",
        "account_type": "expense", "company_id": co_b})
    fy = _u()
    _insert(conn, "fiscal_year", {
        "id": fy, "name": "FY2026", "start_date": "2026-01-01",
        "end_date": "2026-12-31", "company_id": co_a})
    _insert(conn, "gl_entry", {
        "id": _u(), "posting_date": "2026-06-30", "account_id": inc,
        "debit": "0", "credit": "1000.00", "voucher_type": "sales_invoice",
        "voucher_id": _u(), "remarks": "IC sale", "fiscal_year": "FY2026",
        "is_cancelled": 0})
    rule = _u()
    _insert(conn, "elimination_rule", {
        "id": rule, "name": "IC sales elimination",
        "source_company_id": co_a, "target_company_id": co_b,
        "source_account_id": inc, "target_account_id": exp,
        "status": "active"})
    conn.commit()
    return {"company_id": co_a, "target_company_id": co_b,
            "source_account_id": inc, "target_account_id": exp,
            "fiscal_year_id": fy, "rule_id": rule}


def _legacy_args(env):
    """Every legacy flag at once — the old invocation, replayed verbatim."""
    return ns(name="IC sales elimination",
              company_id=env["company_id"],
              target_company_id=env["target_company_id"],
              source_account_id=env["source_account_id"],
              target_account_id=env["target_account_id"],
              fiscal_year_id=env["fiscal_year_id"],
              posting_date="2026-12-31",
              as_of_date=None)


# ---------------------------------------------------------------------------
# cash-flow — ledger effect (read-only: the snapshot is the no-write proof;
# no ledger assertion can hold for the report itself).
# ---------------------------------------------------------------------------

class TestCashFlowDepth:
    def test_cash_flow_pins_balances_and_details_against_the_ledger(
            self, conn, book):
        hs = book["hs"]
        before = _snapshot(conn, BOOK_TABLES)
        r = call_action(REP.cash_flow, conn, ns(
            company_id=hs["company_id"], company_name=None,
            from_date=WINDOW[0], to_date=WINDOW[1],
            dimension_key=None, dimension_value=None))
        assert is_ok(r), r
        # Bank legs in window: JE-1 CR 400.00, PE-1 DR 600.00,
        # PE-2 CR 250.00, JE-4 CR 20.00. JE-2 is cancelled, JE-3 is a
        # draft with no GL, JE-5 falls after the window.
        assert (r["opening_balance"], r["closing_balance"],
                r["net_change"]) == ("0.00", "-70.00", "-70.00")
        assert (r["operating"], r["investing"],
                r["financing"]) == ("-70.00", "0.00", "0.00")
        assert r["details"] == [
            {"account": "Accounts Receivable", "amount": "-482.50",
             "category": "operating"},
            {"account": "Office Expense", "amount": "-400.00",
             "category": "operating"},
            {"account": "Service Revenue", "amount": "1000.00",
             "category": "operating"},
            {"account": "Accounts Payable", "amount": "-250.00",
             "category": "operating"},
            {"account": "Sales Tax Payable", "amount": "62.50",
             "category": "operating"},
        ]
        # Both legs of the anchor journal entry are really there and
        # balance exactly.
        legs = _gl_legs(conn, "journal_entry", book["je1"])
        assert sorted((leg["account_id"], leg["debit"], leg["credit"],
                       leg["is_cancelled"]) for leg in legs) == sorted([
            (hs["expense"], "400.00", "0.00", 0),
            (hs["bank"], "0.00", "400.00", 0)])
        assert (Decimal(legs[0]["debit"]) + Decimal(legs[1]["debit"]),
                Decimal(legs[0]["credit"]) + Decimal(legs[1]["credit"]),
                ) == (Decimal("400.00"), Decimal("400.00"))
        assert _snapshot(conn, BOOK_TABLES) == before

    def test_cash_flow_refusals_leave_the_database_identical(
            self, conn, book):
        cid = book["hs"]["company_id"]
        snapshot = _snapshot(conn, BOOK_TABLES)
        r = call_action(REP.cash_flow, conn, ns(
            company_id=cid, company_name=None, from_date=None,
            to_date=WINDOW[1], dimension_key=None, dimension_value=None))
        assert is_error(r) and _msg(r) == "--from-date is required"
        r = call_action(REP.cash_flow, conn, ns(
            company_id=cid, company_name=None, from_date=WINDOW[0],
            to_date=None, dimension_key=None, dimension_value=None))
        assert is_error(r) and _msg(r) == "--to-date is required"
        r = call_action(REP.cash_flow, conn, ns(
            company_id=None, company_name="Nowhere Inc",
            from_date=WINDOW[0], to_date=WINDOW[1],
            dimension_key=None, dimension_value=None))
        assert r["error"] == "Company 'Nowhere Inc' not found."
        assert "operating" not in r
        assert _snapshot(conn, BOOK_TABLES) == snapshot


class TestCashFlowNullAccountType:
    """An account with no account type is classified by its root type; it is
    never a cash account, so operating reconciles to the net change."""

    def _null_co(self, conn):
        cid = _u()
        _insert(conn, "company", {"id": cid, "name": "Null Co",
                                  "abbr": "NC"})
        _insert(conn, "fiscal_year", {
            "id": _u(), "name": "FY-2026-NC",
            "start_date": "2026-01-01", "end_date": "2026-12-31",
            "is_closed": 0, "company_id": cid})
        cc = _u()
        _insert(conn, "cost_center", {
            "id": cc, "name": "Main - NC", "company_id": cid,
            "is_group": 0})
        bank = _u()
        _insert(conn, "account", {
            "id": bank, "name": "Bank", "account_number": "1010",
            "root_type": "asset", "account_type": "bank",
            "balance_direction": "debit_normal", "company_id": cid,
            "depth": 0})
        exp = _u()
        _insert(conn, "account", {
            "id": exp, "name": "Untyped Expense", "account_number": "5100",
            "root_type": "expense", "account_type": None,
            "balance_direction": "debit_normal", "company_id": cid,
            "depth": 0})
        conn.commit()
        assert _where(conn, "account", id=exp)[0]["account_type"] is None
        return {"company_id": cid, "cc": cc, "bank": bank, "exp": exp}

    def test_null_type_expense_is_classified_as_operating(self, conn):
        env = self._null_co(conn)
        cid, cc, bank, exp = (env["company_id"], env["cc"], env["bank"],
                              env["exp"])
        r = call_action(JR.add_journal_entry, conn, ns(
            company_id=cid, posting_date="2026-03-10", entry_type=None,
            remark=None, lines=json.dumps([
                {"account_id": exp, "debit": "400.00", "credit": "0",
                 "cost_center_id": cc},
                {"account_id": bank, "debit": "0", "credit": "400.00",
                 "cost_center_id": cc}]),
            cwip_asset_id=None))
        assert is_ok(r), r
        r = call_action(JR.submit_journal_entry, conn,
                        ns(journal_entry_id=r["journal_entry_id"]))
        assert is_ok(r), r
        before = _snapshot(conn, BOOK_TABLES)
        r = call_action(REP.cash_flow, conn, ns(
            company_id=cid, company_name=None, from_date="2026-03-01",
            to_date="2026-03-31", dimension_key=None,
            dimension_value=None))
        assert is_ok(r), r
        assert (r["closing_balance"], r["net_change"]) == (
            "-400.00", "-400.00")
        assert r["operating"] == "-400.00"
        assert (r["investing"], r["financing"]) == ("0.00", "0.00")
        assert r["details"] == [
            {"account": "Untyped Expense", "amount": "-400.00",
             "category": "operating"}]
        # The legs exist and balance — the report now sees the expense side.
        t = Table("gl_entry")
        q = (Q.from_(t)
             .select(fn.Coalesce(t.debit, "0").as_("d"),
                     fn.Coalesce(t.credit, "0").as_("c"))
             .where(t.account_id == P()))
        rows = conn.execute(q.get_sql(), (exp,)).fetchall()
        assert [(row["d"], row["c"]) for row in rows] == [("400.00", "0.00")]
        assert _snapshot(conn, BOOK_TABLES) == before

    def test_null_type_income_and_expense_reconcile_to_net_change(
            self, conn):
        env = self._null_co(conn)
        cid, cc, bank, exp = (env["company_id"], env["cc"], env["bank"],
                              env["exp"])
        inc = _u()
        _insert(conn, "account", {
            "id": inc, "name": "Untyped Income", "account_number": "4100",
            "root_type": "income", "account_type": None,
            "balance_direction": "credit_normal", "company_id": cid,
            "depth": 0})
        conn.commit()
        assert _where(conn, "account", id=inc)[0]["account_type"] is None
        r = call_action(JR.add_journal_entry, conn, ns(
            company_id=cid, posting_date="2026-03-05", entry_type=None,
            remark=None, lines=json.dumps([
                {"account_id": bank, "debit": "1000.00", "credit": "0",
                 "cost_center_id": cc},
                {"account_id": inc, "debit": "0", "credit": "1000.00",
                 "cost_center_id": cc}]),
            cwip_asset_id=None))
        assert is_ok(r), r
        r = call_action(JR.submit_journal_entry, conn,
                        ns(journal_entry_id=r["journal_entry_id"]))
        assert is_ok(r), r
        r = call_action(JR.add_journal_entry, conn, ns(
            company_id=cid, posting_date="2026-03-10", entry_type=None,
            remark=None, lines=json.dumps([
                {"account_id": exp, "debit": "400.00", "credit": "0",
                 "cost_center_id": cc},
                {"account_id": bank, "debit": "0", "credit": "400.00",
                 "cost_center_id": cc}]),
            cwip_asset_id=None))
        assert is_ok(r), r
        r = call_action(JR.submit_journal_entry, conn,
                        ns(journal_entry_id=r["journal_entry_id"]))
        assert is_ok(r), r
        before = _snapshot(conn, BOOK_TABLES)
        r = call_action(REP.cash_flow, conn, ns(
            company_id=cid, company_name=None, from_date="2026-03-01",
            to_date="2026-03-31", dimension_key=None,
            dimension_value=None))
        assert is_ok(r), r
        assert r["opening_balance"] == "0.00"
        assert (r["closing_balance"], r["net_change"]) == (
            "600.00", "600.00")
        assert r["operating"] == "600.00"
        assert (r["investing"], r["financing"]) == ("0.00", "0.00")
        assert r["details"] == [
            {"account": "Untyped Expense", "amount": "-400.00",
             "category": "operating"},
            {"account": "Untyped Income", "amount": "1000.00",
             "category": "operating"}]
        assert Decimal(r["operating"]) == Decimal(r["net_change"])
        assert _snapshot(conn, BOOK_TABLES) == before


# ---------------------------------------------------------------------------
# budget-vs-actual / budget-variance — stored rows (the budget rows read back
# exact) plus the ledger-derived actuals. Read-only: the snapshot is the
# no-write proof; no ledger assertion can hold for the report itself.
# ---------------------------------------------------------------------------

_EXPECTED_BUDGET_ITEMS = [
    {"account_or_cc": "Office Expense", "budget": "1000.00",
     "actual": "460.00", "variance": "540.00", "variance_pct": "54.00",
     "action_if_exceeded": "warn"},
    {"account_or_cc": "Service Revenue", "budget": "1500.00",
     "actual": "-1000.00", "variance": "2500.00",
     "variance_pct": "166.67", "action_if_exceeded": "warn"},
]


class TestBudgetVsActualDepth:
    def test_budget_rows_and_variance_match_the_posted_ledger(
            self, conn, book):
        hs = book["hs"]
        before = _snapshot(conn, BOOK_TABLES)
        # The stored budget rows, read back exact: JE-1 (400.00) and JE-5
        # (60.00) are the FY's only expense postings — JE-2 is cancelled
        # and JE-3 is a draft with no GL — so actual is 460.00. Revenue
        # actual is debit-minus-credit, hence negative for a credit-normal
        # account; SI-2 is cancelled and contributes nothing.
        stored = sorted(
            (row["account_id"], row["budget_amount"],
             row["action_if_exceeded"])
            for row in _where(conn, "budget", fiscal_year_id=book["fy"]))
        assert stored == sorted([
            (hs["expense"], "1000.00", "warn"),
            (hs["revenue"], "1500.00", "warn")])
        r = call_action(REP.budget_vs_actual, conn, ns(
            fiscal_year_id=book["fy"], company_id=hs["company_id"],
            company_name=None, account_id=None, cost_center_id=None))
        assert is_ok(r), r
        assert r["items"] == _EXPECTED_BUDGET_ITEMS
        assert (Decimal(r["items"][0]["budget"])
                - Decimal(r["items"][0]["actual"])) == Decimal(
                    r["items"][0]["variance"])
        assert (Decimal(r["items"][1]["budget"])
                - Decimal(r["items"][1]["actual"])) == Decimal(
                    r["items"][1]["variance"])
        assert _snapshot(conn, BOOK_TABLES) == before

    def test_budget_vs_actual_refusals_leave_the_database_identical(
            self, conn, book):
        hs = book["hs"]
        snapshot = _snapshot(conn, BOOK_TABLES)
        r = call_action(REP.budget_vs_actual, conn, ns(
            fiscal_year_id=None, company_id=hs["company_id"],
            company_name=None, account_id=None, cost_center_id=None))
        assert is_error(r) and _msg(r) == "--fiscal-year-id is required"
        r = call_action(REP.budget_vs_actual, conn, ns(
            fiscal_year_id="no-such-fy", company_id=hs["company_id"],
            company_name=None, account_id=None, cost_center_id=None))
        assert is_error(r) and _msg(r) == "Fiscal year not found: no-such-fy"
        assert "items" not in r
        assert _snapshot(conn, BOOK_TABLES) == snapshot


class TestBudgetVarianceAliasDepth:
    def test_budget_variance_matches_budget_vs_actual_on_the_same_book(
            self, conn, book):
        hs = book["hs"]
        before = _snapshot(conn, BOOK_TABLES)
        r = call_action(REP.ACTIONS["budget-variance"], conn, ns(
            fiscal_year_id=book["fy"], company_id=hs["company_id"],
            company_name=None, account_id=None, cost_center_id=None))
        assert is_ok(r), r
        assert r["items"] == _EXPECTED_BUDGET_ITEMS
        stored = _where(conn, "budget", fiscal_year_id=book["fy"],
                        account_id=hs["expense"])
        assert len(stored) == 1
        assert (stored[0]["budget_amount"],
                stored[0]["company_id"]) == ("1000.00", hs["company_id"])
        assert _snapshot(conn, BOOK_TABLES) == before

    def test_budget_variance_refusals_leave_the_database_identical(
            self, conn, book):
        hs = book["hs"]
        snapshot = _snapshot(conn, BOOK_TABLES)
        r = call_action(REP.ACTIONS["budget-variance"], conn, ns(
            fiscal_year_id=None, company_id=hs["company_id"],
            company_name=None, account_id=None, cost_center_id=None))
        assert is_error(r) and _msg(r) == "--fiscal-year-id is required"
        assert _snapshot(conn, BOOK_TABLES) == snapshot


# ---------------------------------------------------------------------------
# comparative-pl — ledger effect (read-only: the snapshot is the no-write
# proof; no ledger assertion can hold for the report itself).
# ---------------------------------------------------------------------------

class TestComparativePlDepth:
    def test_period_amounts_and_totals_match_the_posted_ledger(
            self, conn, book):
        before = _snapshot(conn, BOOK_TABLES)
        r = call_action(REP.comparative_pl, conn, ns(
            company_id=book["hs"]["company_id"], company_name=None,
            periods=json.dumps([
                {"from_date": "2026-03-01", "to_date": "2026-03-31",
                 "label": "Mar"},
                {"from_date": "2026-04-01", "to_date": "2026-04-30",
                 "label": "Apr"}])))
        assert is_ok(r), r
        # March carries JE-1's 400.00 expense (JE-2 is cancelled, JE-3 a
        # draft with no GL); April carries SI-1's 1000.00 revenue. JE-4
        # hits a liability account, so April expenses stay 0.00.
        by_name = {a["account"]: a for a in r["accounts"]}
        assert sorted(by_name) == ["Office Expense", "Service Revenue"]
        assert by_name["Office Expense"]["root_type"] == "expense"
        assert by_name["Office Expense"]["periods"] == [
            {"label": "Mar", "amount": "400.00"},
            {"label": "Apr", "amount": "0.00"}]
        assert by_name["Service Revenue"]["root_type"] == "income"
        assert by_name["Service Revenue"]["periods"] == [
            {"label": "Mar", "amount": "0.00"},
            {"label": "Apr", "amount": "1000.00"}]
        assert r["totals"] == [
            {"label": "Mar", "income": "0.00", "expenses": "400.00",
             "net": "-400.00"},
            {"label": "Apr", "income": "1000.00", "expenses": "0.00",
             "net": "1000.00"}]
        for total in r["totals"]:
            assert Decimal(total["income"]) - Decimal(
                total["expenses"]) == Decimal(total["net"])
        assert _snapshot(conn, BOOK_TABLES) == before

    def test_comparative_pl_refusals_leave_the_database_identical(
            self, conn, book):
        cid = book["hs"]["company_id"]
        snapshot = _snapshot(conn, BOOK_TABLES)
        r = call_action(REP.comparative_pl, conn, ns(
            company_id=cid, company_name=None, periods=None))
        assert is_error(r)
        assert _msg(r) == ("--periods must be a non-empty JSON array of "
                           "{from_date, to_date, label}")
        r = call_action(REP.comparative_pl, conn, ns(
            company_id=cid, company_name=None, periods="{}"))
        assert is_error(r) and _msg(r) == (
            "--periods must be a non-empty JSON array of "
            "{from_date, to_date, label}")
        assert "totals" not in r
        assert _snapshot(conn, BOOK_TABLES) == snapshot


# ---------------------------------------------------------------------------
# gl-summary — ledger effect: group totals pinned per voucher type, every
# group asserted balanced, invoice legs read back individually. Read-only:
# the snapshot is the no-write proof.
# ---------------------------------------------------------------------------

class TestGlSummaryDepth:
    def test_group_totals_match_balanced_posted_legs(self, conn, book):
        hs = book["hs"]
        before = _snapshot(conn, BOOK_TABLES)
        r = call_action(REP.gl_summary, conn, ns(
            company_id=hs["company_id"], company_name=None,
            from_date=WINDOW[0], to_date=WINDOW[1]))
        assert is_ok(r), r
        # JE-1 (400.00) + JE-4 (20.00); JE-2 cancelled, JE-3 draft, JE-5
        # outside the window. PE-1 (600.00) + PE-2 (250.00); PE-3
        # cancelled, PE-4 draft. SI-1 (1082.50); SI-2 cancelled.
        assert r["by_voucher_type"] == [
            {"voucher_type": "journal_entry", "count": 4,
             "total_debit": "420.00", "total_credit": "420.00"},
            {"voucher_type": "payment_entry", "count": 4,
             "total_debit": "850.00", "total_credit": "850.00"},
            {"voucher_type": "sales_invoice", "count": 3,
             "total_debit": "1082.50", "total_credit": "1082.50"},
        ]
        for group in r["by_voucher_type"]:
            assert Decimal(group["total_debit"]) == Decimal(
                group["total_credit"]), (
                "group %s does not balance" % group["voucher_type"])
        legs = _gl_legs(conn, "sales_invoice", book["si1"])
        assert sorted((leg["account_id"], leg["debit"], leg["credit"],
                       leg["is_cancelled"]) for leg in legs) == sorted([
            (hs["ar"], "1082.50", "0.00", 0),
            (hs["revenue"], "0.00", "1000.00", 0),
            (hs["sales_tax"], "0.00", "82.50", 0)])
        assert sum((Decimal(leg["debit"]) - Decimal(leg["credit"])
                    for leg in legs), Decimal("0")) == Decimal("0")
        cancelled = _gl_legs(conn, "sales_invoice", book["si2"])
        assert len(cancelled) > 0
        assert {leg["is_cancelled"] for leg in cancelled} == {1}
        assert _snapshot(conn, BOOK_TABLES) == before

    def test_gl_summary_refusals_leave_the_database_identical(
            self, conn, book):
        cid = book["hs"]["company_id"]
        snapshot = _snapshot(conn, BOOK_TABLES)
        r = call_action(REP.gl_summary, conn, ns(
            company_id=cid, company_name=None, from_date=None,
            to_date=WINDOW[1]))
        assert is_error(r) and _msg(r) == "--from-date is required"
        r = call_action(REP.gl_summary, conn, ns(
            company_id=cid, company_name=None, from_date=WINDOW[0],
            to_date=None))
        assert is_error(r) and _msg(r) == "--to-date is required"
        r = call_action(REP.gl_summary, conn, ns(
            company_id=None, company_name="Nowhere Inc",
            from_date=WINDOW[0], to_date=WINDOW[1]))
        assert r["error"] == "Company 'Nowhere Inc' not found."
        assert "by_voucher_type" not in r
        assert _snapshot(conn, BOOK_TABLES) == snapshot


# ---------------------------------------------------------------------------
# tax-summary — ledger effect: collected/paid/net pinned against the tax
# legs. Read-only: the snapshot is the no-write proof.
# ---------------------------------------------------------------------------

class TestTaxSummaryDepth:
    def test_collected_paid_and_net_match_the_tax_legs(self, conn, book):
        hs = book["hs"]
        before = _snapshot(conn, BOOK_TABLES)
        r = call_action(REP.tax_summary, conn, ns(
            company_id=hs["company_id"], company_name=None,
            from_date=WINDOW[0], to_date=WINDOW[1]))
        assert is_ok(r), r
        # SI-1 credits 82.50 (SI-2's 16.50 and its mirror are cancelled);
        # JE-4 debits 20.00. Input Tax has no rows and is left out.
        assert (r["collected"], r["paid"],
                r["net_liability"]) == ("82.50", "20.00", "62.50")
        assert r["by_account"] == [
            {"account_id": hs["sales_tax"],
             "account_name": "Sales Tax Payable", "amount": "62.50"}]
        assert Decimal(r["collected"]) - Decimal(r["paid"]) == Decimal(
            r["net_liability"])
        t = Table("gl_entry")
        q = (Q.from_(t)
             .select(t.debit, t.credit, t.voucher_type, t.is_cancelled)
             .where(t.account_id == P())
             .where(t.posting_date >= P())
             .where(t.posting_date <= P()))
        rows = conn.execute(q.get_sql(),
                            (hs["sales_tax"], WINDOW[0], WINDOW[1])).fetchall()
        live = [dict(row) for row in rows if row["is_cancelled"] == 0]
        assert sorted((row["voucher_type"], row["debit"], row["credit"])
                      for row in live) == sorted([
            ("sales_invoice", "0.00", "82.50"),
            ("journal_entry", "20.00", "0.00")])
        assert _where(conn, "gl_entry",
                      account_id=hs["input_tax"]) == []
        assert _snapshot(conn, BOOK_TABLES) == before

    def test_tax_summary_refusals_leave_the_database_identical(
            self, conn, book):
        cid = book["hs"]["company_id"]
        snapshot = _snapshot(conn, BOOK_TABLES)
        r = call_action(REP.tax_summary, conn, ns(
            company_id=cid, company_name=None, from_date=None,
            to_date=WINDOW[1]))
        assert is_error(r) and _msg(r) == "--from-date is required"
        r = call_action(REP.tax_summary, conn, ns(
            company_id=cid, company_name=None, from_date=WINDOW[0],
            to_date=None))
        assert is_error(r) and _msg(r) == "--to-date is required"
        assert _snapshot(conn, BOOK_TABLES) == snapshot


# ---------------------------------------------------------------------------
# payment-summary — stored rows: the payment_entry rows read back exact.
# The report reads them, so no ledger assertion can hold for it; the
# snapshot is the no-write proof.
# ---------------------------------------------------------------------------

class TestPaymentSummaryDepth:
    def test_totals_match_the_stored_submitted_payments(self, conn, book):
        hs = book["hs"]
        before = _snapshot(conn, BOOK_TABLES)
        r = call_action(REP.payment_summary, conn, ns(
            company_id=hs["company_id"], company_name=None,
            from_date=WINDOW[0], to_date=WINDOW[1]))
        assert is_ok(r), r
        # PE-3 (cancelled, 100.00) and PE-4 (draft, 45.00) are excluded.
        assert (r["total_received"], r["total_paid"]) == ("600.00",
                                                          "250.00")
        assert sorted(r["by_party_type"],
                      key=lambda p: p["party_type"]) == [
            {"party_type": "customer", "count": 1, "amount": "600.00"},
            {"party_type": "supplier", "count": 1, "amount": "250.00"},
        ]
        stored = {row["id"]: row for row in _where(
            conn, "payment_entry", company_id=hs["company_id"])}
        assert (stored[book["pe1"]]["payment_type"],
                stored[book["pe1"]]["party_type"],
                stored[book["pe1"]]["paid_amount"],
                stored[book["pe1"]]["status"],
                stored[book["pe1"]]["unallocated_amount"]) == (
            "receive", "customer", "600.00", "submitted", "0.00")
        assert (stored[book["pe2"]]["payment_type"],
                stored[book["pe2"]]["party_type"],
                stored[book["pe2"]]["paid_amount"],
                stored[book["pe2"]]["status"],
                stored[book["pe2"]]["unallocated_amount"]) == (
            "pay", "supplier", "250.00", "submitted", "250.00")
        assert stored[book["pe3"]]["status"] == "cancelled"
        assert stored[book["pe4"]]["status"] == "draft"
        assert _snapshot(conn, BOOK_TABLES) == before

    def test_payment_summary_refusals_leave_the_database_identical(
            self, conn, book):
        cid = book["hs"]["company_id"]
        snapshot = _snapshot(conn, BOOK_TABLES)
        r = call_action(REP.payment_summary, conn, ns(
            company_id=cid, company_name=None, from_date=None,
            to_date=WINDOW[1]))
        assert is_error(r) and _msg(r) == "--from-date is required"
        r = call_action(REP.payment_summary, conn, ns(
            company_id=cid, company_name=None, from_date=WINDOW[0],
            to_date=None))
        assert is_error(r) and _msg(r) == "--to-date is required"
        r = call_action(REP.payment_summary, conn, ns(
            company_id=None, company_name=None, from_date=WINDOW[0],
            to_date=WINDOW[1]))
        assert r["error"] == ("Multiple companies found. Please specify "
                              "the company by name.")
        assert "total_received" not in r
        assert _snapshot(conn, BOOK_TABLES) == snapshot


# ---------------------------------------------------------------------------
# Company scoping across the read actions.
# ---------------------------------------------------------------------------

class TestCompanyScopingDepth:
    def test_lakeside_rows_never_leak_into_harbor_reports(self, conn, book):
        hs, lt = book["hs"], book["lt"]
        before = _snapshot(conn, BOOK_TABLES)
        r = call_action(REP.cash_flow, conn, ns(
            company_id=hs["company_id"], company_name=None,
            from_date=WINDOW[0], to_date=WINDOW[1],
            dimension_key=None, dimension_value=None))
        assert is_ok(r), r
        assert r["closing_balance"] == "-70.00"
        r = call_action(REP.tax_summary, conn, ns(
            company_id=hs["company_id"], company_name=None,
            from_date=WINDOW[0], to_date=WINDOW[1]))
        assert is_ok(r), r
        assert r["net_liability"] == "62.50"
        r = call_action(REP.payment_summary, conn, ns(
            company_id=hs["company_id"], company_name=None,
            from_date=WINDOW[0], to_date=WINDOW[1]))
        assert is_ok(r), r
        assert r["total_received"] == "600.00"
        r = call_action(REP.gl_summary, conn, ns(
            company_id=lt["company_id"], company_name=None,
            from_date=WINDOW[0], to_date=WINDOW[1]))
        assert is_ok(r), r
        assert r["by_voucher_type"] == [
            {"voucher_type": "journal_entry", "count": 2,
             "total_debit": "30.00", "total_credit": "30.00"},
            {"voucher_type": "payment_entry", "count": 2,
             "total_debit": "777.00", "total_credit": "777.00"},
        ]
        r = call_action(REP.budget_vs_actual, conn, ns(
            fiscal_year_id=book["fy"], company_id=lt["company_id"],
            company_name=None, account_id=None, cost_center_id=None))
        assert is_ok(r), r
        assert r["items"] == []
        r = call_action(REP.comparative_pl, conn, ns(
            company_id=lt["company_id"], company_name=None,
            periods=json.dumps([
                {"from_date": "2026-03-01", "to_date": "2026-03-31",
                 "label": "Mar"},
                {"from_date": "2026-04-01", "to_date": "2026-04-30",
                 "label": "Apr"}])))
        assert is_ok(r), r
        assert [(t["income"], t["expenses"], t["net"])
                for t in r["totals"]] == [("0.00", "0.00", "0.00"),
                                          ("0.00", "0.00", "0.00")]
        assert _snapshot(conn, BOOK_TABLES) == before


# ---------------------------------------------------------------------------
# add-elimination-rule / list-elimination-rules / list-elimination-entries —
# retired. The refusal (a truthful steer) IS the behaviour; the legacy
# tables and the ledger must be byte-identical afterwards. These actions
# reach no ledger, so no ledger assertion can hold.
# ---------------------------------------------------------------------------

def _assert_retired(conn, action, env, snapshot):
    r = call_action(REP.ACTIONS[action], conn, _legacy_args(env))
    assert r["status"] == "error", (
        "%s still returned a result: %s" % (action, json.dumps(r)[:200]))
    assert "retired" in r["message"].lower()
    assert action in r["message"], (
        "the message must name the action the caller typed")
    for replacement in ADVACCT_FLOW:
        assert replacement in r.get("suggestion", ""), (
            "the steer for %s does not name %s" % (action, replacement))
    assert _snapshot(conn, LEGACY_TABLES) == snapshot
    t = Table("gl_entry")
    q = (Q.from_(t).select(fn.Count("*").as_("n"))
         .where(t.voucher_type == P()))
    assert conn.execute(q.get_sql(),
                        ("elimination_entry",)).fetchone()["n"] == 0


class TestAddEliminationRuleDepth:
    def test_refusal_names_the_flow_and_writes_nothing(
            self, conn, legacy_env):
        snapshot = _snapshot(conn, LEGACY_TABLES)
        assert _count(conn, "elimination_rule") == 1
        assert _count(conn, "elimination_entry") == 0
        assert _count(conn, "gl_entry") == 1
        _assert_retired(conn, "add-elimination-rule", legacy_env, snapshot)


class TestListEliminationRulesDepth:
    def test_refusal_names_the_flow_and_writes_nothing(
            self, conn, legacy_env):
        snapshot = _snapshot(conn, LEGACY_TABLES)
        stored = _row(conn, "elimination_rule", legacy_env["rule_id"])
        assert (stored["name"], stored["status"]) == ("IC sales elimination",
                                                      "active")
        _assert_retired(conn, "list-elimination-rules", legacy_env,
                        snapshot)


class TestListEliminationEntriesDepth:
    def test_refusal_names_the_flow_and_writes_nothing(
            self, conn, legacy_env):
        snapshot = _snapshot(conn, LEGACY_TABLES)
        _assert_retired(conn, "list-elimination-entries", legacy_env,
                        snapshot)
