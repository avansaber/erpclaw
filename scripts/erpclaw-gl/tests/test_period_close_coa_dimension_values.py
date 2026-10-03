"""Behaviour of validate-period-close, import-chart-of-accounts and
update-dimension, read back from the database.

validate-period-close
  Reports, for one fiscal year of one company, the income total (credit minus
  debit on income accounts), the expense total (debit minus credit on expense
  accounts) and their difference, over live GL legs dated inside the fiscal
  year, plus whether the company's whole live ledger balances. It writes
  nothing. Every GL leg below is posted through the module's own
  post-gl-entries / reverse-gl-entries actions, except one deliberately stray
  leg that models a damaged ledger.

  Book of company A, fiscal year 2026 (all dates fixed):
    V-1  2026-02-10  DR Cash 1000.30          / CR Sales Revenue 1000.30
    V-2  2026-03-15  DR Sales Revenue 0.10    / CR Cash 0.10       (allowance)
    V-3  2026-04-01  DR Office Supplies 0.30  / CR Cash 0.30
    V-4  2026-04-20  DR Cash 0.10             / CR Office Supplies 0.10 (refund)
    V-5  2026-12-20  DR Office Supplies 250.00 / CR Cash 250.00, REVERSED on
                     2027-01-05 (the original legs sit in FY 2026, the mirror
                     legs in FY 2027; all four are marked is_cancelled = 1)
    V-0  2025-12-31  DR Cash 999.00           / CR Sales Revenue 999.00 (FY 2025)
    JE   2026-06-01  draft journal entry DR Office Supplies 75.00 (posts nothing)
  Company B posts DR Cash 400.00 / CR Sales Revenue 400.00 on 2026-02-11.

  Income 1000.30 - 0.10 = 1000.20, expense 0.30 - 0.10 = 0.20, net 1000.00.
  The cancelled voucher, the prior-year voucher, the draft and company B
  contribute nothing.

import-chart-of-accounts
  Creates one account row per CSV row that does not already exist by name in
  the company, linking parent_name to an account of that name in the same
  company (an earlier row of the same file counts), with depth one below the
  parent. The whole file is validated before the first write with the same
  registry, root-type coherence and leaf-only checks add-account applies,
  plus a parent check against pre-existing accounts and earlier file rows;
  any problem refuses the whole file and nothing is written. All inserts
  and one audit row per created account commit in a single transaction, so
  a failed import leaves no partial rows.

update-dimension
  Rewrites only the dimension_registry columns it is given, for the key it is
  given, and records an audit row. Passing --is-active false applies the
  same recent-use guard deactivate-dimension applies (same --within-days
  handling and refusal text): while recent live GL carries the dimension
  the whole update is refused. Refusals change nothing.
"""
import importlib.util
import json
import os
from datetime import date, datetime, timedelta, timezone

import pytest
from erpclaw_lib.query import Field, P, Q, Table
from gl_helpers import (call_action, is_error, is_ok, load_db_query, ns,
                        seed_account, seed_company, seed_cost_center,
                        seed_fiscal_year, _uuid)

gl = load_db_query()

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(os.path.dirname(_TESTS_DIR))


def _load(name, domain):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_SCRIPTS_DIR, domain, "db_query.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


JR = _load("db_query_journals_m349", "erpclaw-journals")


def _count(conn, table, where="", params=()):
    sql = "SELECT COUNT(*) FROM " + table + (" WHERE " + where if where else "")
    return conn.execute(sql, params).fetchone()[0]


def _snapshot(conn):
    return {t: _count(conn, t) for t in (
        "gl_entry", "account", "journal_entry", "period_closing_voucher",
        "dimension_registry", "audit_log")}


# ── validate-period-close ────────────────────────────────────────────────────

def _post(conn, env, voucher_id, date, debit_key, credit_key, amount):
    entries = []
    for key, debit, credit in ((debit_key, amount, "0"), (credit_key, "0", amount)):
        entry = {"account_id": env[key], "debit": debit, "credit": credit}
        if key in ("revenue", "expense"):
            entry["cost_center_id"] = env["cc"]
        entries.append(entry)
    r = call_action(gl.post_gl_entries, conn, ns(
        voucher_type="journal_entry", voucher_id=voucher_id, posting_date=date,
        company_id=env["company_id"], entries=json.dumps(entries)))
    assert is_ok(r), r


def _company_env(conn):
    cid = seed_company(conn)
    env = {
        "company_id": cid,
        "cc": seed_cost_center(conn, cid),
        "cash": seed_account(conn, cid, "Cash", "asset", "cash"),
        "revenue": seed_account(conn, cid, "Sales Revenue", "income", "revenue"),
        "expense": seed_account(conn, cid, "Office Supplies", "expense", "expense"),
    }
    env["fy2025"] = seed_fiscal_year(conn, cid, "FY 2025 " + cid[:6],
                                     "2025-01-01", "2025-12-31")
    env["fy2026"] = seed_fiscal_year(conn, cid, "FY 2026 " + cid[:6],
                                     "2026-01-01", "2026-12-31")
    return env


@pytest.fixture
def close_book(conn):
    a = _company_env(conn)
    a["fy2027"] = seed_fiscal_year(conn, a["company_id"], "FY 2027 " + a["company_id"][:6],
                                   "2027-01-01", "2027-12-31")
    _post(conn, a, "V-0", "2025-12-31", "cash", "revenue", "999.00")
    _post(conn, a, "V-1", "2026-02-10", "cash", "revenue", "1000.30")
    _post(conn, a, "V-2", "2026-03-15", "revenue", "cash", "0.10")
    _post(conn, a, "V-3", "2026-04-01", "expense", "cash", "0.30")
    _post(conn, a, "V-4", "2026-04-20", "cash", "expense", "0.10")
    _post(conn, a, "V-5", "2026-12-20", "expense", "cash", "250.00")
    r = call_action(gl.reverse_gl_entries_action, conn, ns(
        voucher_type="journal_entry", voucher_id="V-5", posting_date="2027-01-05"))
    assert is_ok(r), r
    lines = [{"account_id": a["expense"], "debit": "75.00", "credit": "0",
              "cost_center_id": a["cc"]},
             {"account_id": a["cash"], "debit": "0", "credit": "75.00",
              "cost_center_id": a["cc"]}]
    r = call_action(JR.add_journal_entry, conn, ns(
        company_id=a["company_id"], posting_date="2026-06-01", entry_type=None,
        remark="Not yet submitted", lines=json.dumps(lines), cwip_asset_id=None))
    assert is_ok(r), r
    a["draft_je"] = r["journal_entry_id"]

    b = _company_env(conn)
    _post(conn, b, "V-B1", "2026-02-11", "cash", "revenue", "400.00")
    return {"a": a, "b": b}


def _validate(conn, fiscal_year_id):
    return call_action(gl.validate_period_close, conn, ns(fiscal_year_id=fiscal_year_id))


def _fy_name(conn, fy_id):
    return conn.execute("SELECT name FROM fiscal_year WHERE id = ?",
                        (fy_id,)).fetchone()["name"]


def test_validate_period_close_exact_pl_figures_for_the_fiscal_year(conn, close_book):
    a = close_book["a"]
    before = _snapshot(conn)
    r = _validate(conn, a["fy2026"])
    assert is_ok(r), r
    assert r["fiscal_year"] == _fy_name(conn, a["fy2026"])
    assert (r["income_total"], r["expense_total"], r["net_income"]) == (
        "1000.20", "0.20", "1000.00")
    assert r["trial_balance_balanced"] is True
    assert _snapshot(conn) == before
    assert conn.execute("SELECT is_closed FROM fiscal_year WHERE id = ?",
                        (a["fy2026"],)).fetchone()["is_closed"] == 0


def test_validate_period_close_ignores_cancelled_draft_other_year_and_company(
        conn, close_book):
    a, b = close_book["a"], close_book["b"]
    legs = conn.execute(
        "SELECT posting_date, account_id, debit, credit, is_cancelled FROM gl_entry "
        "WHERE voucher_id = 'V-5'").fetchall()
    assert sorted((x["posting_date"], x["account_id"], x["debit"], x["credit"],
                   x["is_cancelled"]) for x in legs) == sorted([
        ("2026-12-20", a["expense"], "250.00", "0.00", 1),
        ("2026-12-20", a["cash"], "0.00", "250.00", 1),
        ("2027-01-05", a["expense"], "0.00", "250.00", 1),
        ("2027-01-05", a["cash"], "250.00", "0.00", 1)])
    assert _count(conn, "gl_entry", "voucher_id = ?", (a["draft_je"],)) == 0
    assert conn.execute("SELECT status FROM journal_entry WHERE id = ?",
                        (a["draft_je"],)).fetchone()["status"] == "draft"

    r = _validate(conn, a["fy2025"])
    assert (r["income_total"], r["expense_total"], r["net_income"],
            r["trial_balance_balanced"]) == ("999.00", "0.00", "999.00", True)
    r = _validate(conn, a["fy2027"])
    assert (r["income_total"], r["expense_total"], r["net_income"],
            r["trial_balance_balanced"]) == ("0.00", "0.00", "0.00", True)
    r = _validate(conn, b["fy2026"])
    assert (r["income_total"], r["expense_total"], r["net_income"],
            r["trial_balance_balanced"]) == ("400.00", "0.00", "400.00", True)
    r = _validate(conn, b["fy2025"])
    assert (r["income_total"], r["expense_total"], r["net_income"],
            r["trial_balance_balanced"]) == ("0.00", "0.00", "0.00", True)


def test_validate_period_close_reports_an_unbalanced_ledger(conn, close_book):
    a, b = close_book["a"], close_book["b"]
    # A stray leg already marked cancelled does not unbalance the ledger.
    conn.execute(
        "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
        "voucher_type, voucher_id, is_cancelled) VALUES (?, '2026-06-29', ?, '55.00', "
        "'0', 'journal_entry', 'STRAY-0', 1)", (_uuid(), a["cash"]))
    conn.commit()
    assert _validate(conn, a["fy2026"])["trial_balance_balanced"] is True
    # A live stray debit with no credit: a damaged ledger that post-gl-entries
    # itself would refuse to create.
    conn.execute(
        "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
        "voucher_type, voucher_id) VALUES (?, '2026-06-30', ?, '12.34', '0', "
        "'journal_entry', 'STRAY-1')", (_uuid(), a["cash"]))
    conn.commit()
    before = _snapshot(conn)
    r = _validate(conn, a["fy2026"])
    assert is_ok(r), r
    assert (r["income_total"], r["expense_total"], r["net_income"]) == (
        "1000.20", "0.20", "1000.00")
    assert r["trial_balance_balanced"] is False
    assert _validate(conn, b["fy2026"])["trial_balance_balanced"] is True
    assert _snapshot(conn) == before


def test_validate_period_close_refusals_write_nothing(conn, close_book):
    b = close_book["b"]
    closed = _uuid()
    conn.execute(
        "INSERT INTO fiscal_year (id, name, start_date, end_date, is_closed, company_id) "
        "VALUES (?, 'FY 2024 closed', '2024-01-01', '2024-12-31', 1, ?)",
        (closed, b["company_id"]))
    conn.commit()
    before = _snapshot(conn)
    for fy_id, message in ((None, "--fiscal-year-id is required"),
                           ("no-such-fy", "Fiscal year no-such-fy not found"),
                           (closed, "Fiscal year 'FY 2024 closed' is already closed")):
        r = _validate(conn, fy_id)
        assert is_error(r), r
        assert r["message"] == message
        assert "income_total" not in r
    assert _snapshot(conn) == before


# ── import-chart-of-accounts ─────────────────────────────────────────────────

def _csv(tmp_path, text, name="chart.csv"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _import(conn, csv_path, company_id):
    return call_action(gl.import_chart_of_accounts, conn, ns(
        csv_path=csv_path, company_id=company_id))


def _accounts(conn, company_id):
    rows = conn.execute(
        "SELECT a.name, a.account_number, a.root_type, a.account_type, a.currency, "
        "a.is_group, a.balance_direction, a.depth, p.name AS parent "
        "FROM account a LEFT JOIN account p ON p.id = a.parent_id "
        "WHERE a.company_id = ?", (company_id,)).fetchall()
    return {r["name"]: (r["account_number"], r["root_type"], r["account_type"],
                        r["currency"], r["is_group"], r["balance_direction"],
                        r["depth"], r["parent"]) for r in rows}


CHART = (
    "name,root_type,account_number,account_type,parent_name,currency,is_group\n"
    "Current Assets,asset,1000,,,,1\n"
    "Operating Bank,asset,1010,bank,Current Assets,,0\n"
    "Euro Bank,asset,1020,bank,Current Assets,EUR,false\n"
    "Card Clearing,asset,1030,,Operating Bank,,0\n"
    "Payables,liability,2000,,,,true\n"
    "Trade Creditors,liability,2010,payable,Payables,,\n"
    "Owner Capital,equity,3000,equity,,,0\n"
    "Service Income,income,4000,revenue,,,0\n"
    "Rent,expense,5000,,Legacy Expenses,,0\n"
)


def test_import_chart_of_accounts_creates_exact_rows_and_parent_links(conn, tmp_path):
    cid = seed_company(conn)
    legacy = seed_account(conn, cid, "Legacy Expenses", "expense",
                          account_number="5999", is_group=1)
    conn.execute("UPDATE account SET depth = 1 WHERE id = ?", (legacy,))
    conn.commit()
    other = seed_company(conn)

    r = _import(conn, _csv(tmp_path, CHART), cid)
    assert is_ok(r), r
    assert (r["imported"], r["skipped"], r["total_rows"]) == (9, 0, 9)
    got = _accounts(conn, cid)
    assert {name: (row[7], row[6]) for name, row in got.items()} == {
        "Legacy Expenses": (None, 1), "Current Assets": (None, 0),
        "Operating Bank": ("Current Assets", 1), "Euro Bank": ("Current Assets", 1),
        "Card Clearing": ("Operating Bank", 2), "Payables": (None, 0),
        "Trade Creditors": ("Payables", 1), "Owner Capital": (None, 0),
        "Service Income": (None, 0), "Rent": ("Legacy Expenses", 2)}
    assert got == {
        "Legacy Expenses": ("5999", "expense", None, "USD", 1, "debit_normal", 1, None),
        "Current Assets": ("1000", "asset", None, "USD", 1, "debit_normal", 0, None),
        "Operating Bank": ("1010", "asset", "bank", "USD", 0, "debit_normal", 1,
                           "Current Assets"),
        "Euro Bank": ("1020", "asset", "bank", "EUR", 0, "debit_normal", 1,
                      "Current Assets"),
        "Card Clearing": ("1030", "asset", None, "USD", 0, "debit_normal", 2,
                          "Operating Bank"),
        "Payables": ("2000", "liability", None, "USD", 1, "credit_normal", 0, None),
        "Trade Creditors": ("2010", "liability", "payable", "USD", 0, "credit_normal",
                            1, "Payables"),
        "Owner Capital": ("3000", "equity", "equity", "USD", 0, "credit_normal", 0, None),
        "Service Income": ("4000", "income", "revenue", "USD", 0, "credit_normal", 0,
                           None),
        "Rent": ("5000", "expense", None, "USD", 0, "debit_normal", 2,
                 "Legacy Expenses"),
    }
    assert _count(conn, "account", "company_id = ?", (other,)) == 0
    rent = conn.execute(
        "SELECT parent_id, depth FROM account WHERE company_id = ? AND name = 'Rent'",
        (cid,)).fetchone()
    assert (rent["parent_id"], rent["depth"]) == (legacy, 2)


def test_import_chart_of_accounts_skips_existing_names_per_company(conn, tmp_path):
    cid = seed_company(conn)
    other = seed_company(conn)
    seed_account(conn, cid, "Operating Bank", "asset", "cash", account_number="1999")
    seed_account(conn, other, "Service Income", "income", "revenue",
                 account_number="4000")
    text = ("name,root_type,account_number,account_type\n"
            "Operating Bank,asset,1010,bank\n"
            "Service Income,income,4000,revenue\n"
            "Service Income,income,4001,revenue\n")
    r = _import(conn, _csv(tmp_path, text), cid)
    assert is_ok(r), r
    assert (r["imported"], r["skipped"], r["total_rows"]) == (1, 2, 3)
    mine = _accounts(conn, cid)
    assert set(mine) == {"Operating Bank", "Service Income"}
    assert mine["Operating Bank"][:3] == ("1999", "asset", "cash")
    assert mine["Service Income"][:3] == ("4000", "income", "revenue")
    assert _accounts(conn, other)["Service Income"][:3] == ("4000", "income", "revenue")
    assert _count(conn, "account") == 3


def test_import_chart_of_accounts_refusals_write_nothing(conn, tmp_path):
    cid = seed_company(conn)
    seed_account(conn, cid, "Cash", "asset", "cash", account_number="1000")
    good = _csv(tmp_path, CHART)
    cases = [
        (dict(csv_path=None, company_id=cid), "--csv-path is required"),
        (dict(csv_path=good, company_id=None), "--company-id is required"),
        (dict(csv_path=_csv(tmp_path, CHART, "chart.txt"), company_id=cid),
         "--csv-path must point to a .csv file"),
        (dict(csv_path=str(tmp_path / "missing.csv"), company_id=cid),
         "File not found: " + str(tmp_path / "missing.csv")),
        (dict(csv_path=_csv(tmp_path, "name,account_number\nBank,1010\n", "nocol.csv"),
              company_id=cid),
         "CSV validation failed: Missing required column: root_type"),
        (dict(csv_path=_csv(tmp_path, "name,root_type\nBank,asset\nLoan,\n,income\n",
                            "blank.csv"), company_id=cid),
         "CSV validation failed: Row 3: missing required value for 'root_type'; "
         "Row 4: missing required value for 'name'"),
        (dict(csv_path=_csv(tmp_path, "name,root_type\n", "empty.csv"), company_id=cid),
         "CSV file is empty"),
    ]
    before = _snapshot(conn)
    for args, message in cases:
        r = call_action(gl.import_chart_of_accounts, conn, ns(**args))
        assert is_error(r), r
        assert r["message"] == message
        assert "imported" not in r
    assert _snapshot(conn) == before
    assert [row["name"] for row in conn.execute(
        "SELECT name FROM account WHERE company_id = ?", (cid,)).fetchall()] == ["Cash"]


# ── update-dimension ─────────────────────────────────────────────────────────

def _dim_args(**kw):
    args = dict(key=None, label=None, dimension_type=None, refers_to=None,
                allowed_values=None, required_on_account_types=None, is_active=None)
    args.update(kw)
    return ns(**args)


def _dimension(conn, key):
    row = conn.execute(
        "SELECT id, key, label, data_type, referenced_table, allowed_values_json, "
        "is_required_on_account_types_json, is_active FROM dimension_registry "
        "WHERE key = ?", (key,)).fetchone()
    return dict(row) if row else None


@pytest.fixture
def dimensions(conn):
    for kw in (dict(key="region", label="Region", dimension_type="enum",
                    allowed_values="north,south", refers_to=None,
                    required_on_account_types="revenue"),
               dict(key="channel", label="Sales Channel", dimension_type="text",
                    allowed_values=None, refers_to=None,
                    required_on_account_types=None)):
        r = call_action(gl.add_dimension, conn, ns(**kw))
        assert is_ok(r), r
    return {"region": _dimension(conn, "region"),
            "channel": _dimension(conn, "channel"),
            "department": _dimension(conn, "department")}


def test_update_dimension_rewrites_only_the_given_columns(conn, dimensions):
    region = dimensions["region"]
    audits = _count(conn, "audit_log")
    r = call_action(gl.update_dimension, conn, _dim_args(
        key=" region ", label=" Sales Region ", allowed_values="north, south ,east,",
        required_on_account_types=""))
    assert is_ok(r), r
    assert (r["key"], r["document_status"]) == ("region", "updated")
    assert _dimension(conn, "region") == {
        "id": region["id"], "key": "region", "label": "Sales Region",
        "data_type": "enum", "referenced_table": None,
        "allowed_values_json": '["north", "south", "east"]',
        "is_required_on_account_types_json": None, "is_active": 1}
    assert _dimension(conn, "channel") == dimensions["channel"]

    assert _count(conn, "audit_log") == audits + 1
    row = conn.execute(
        "SELECT skill, action, entity_type, new_values FROM audit_log "
        "WHERE entity_type = 'dimension_registry' AND entity_id = ? AND action = 'update'",
        (region["id"],)).fetchone()
    assert (row["skill"], row["action"], row["entity_type"]) == (
        "erpclaw-gl", "update", "dimension_registry")
    assert json.loads(row["new_values"]) == {"key": "region"}


def test_update_dimension_retypes_links_and_toggles_active(conn, dimensions):
    channel = dimensions["channel"]
    r = call_action(gl.update_dimension, conn, _dim_args(
        key="channel", dimension_type="uuid_fk", refers_to="cost_center",
        is_active=False))
    assert is_ok(r), r
    assert _dimension(conn, "channel") == {
        "id": channel["id"], "key": "channel", "label": "Sales Channel",
        "data_type": "uuid_fk", "referenced_table": "cost_center",
        "allowed_values_json": None, "is_required_on_account_types_json": None,
        "is_active": 0}
    r = call_action(gl.update_dimension, conn, _dim_args(
        key="channel", dimension_type="text", refers_to="  ", is_active=True))
    assert is_ok(r), r
    got = _dimension(conn, "channel")
    assert (got["data_type"], got["referenced_table"], got["is_active"]) == (
        "text", None, 1)
    assert _dimension(conn, "region") == dimensions["region"]
    assert _dimension(conn, "department") == dimensions["department"]


def test_update_dimension_refusals_write_nothing(conn, dimensions):
    before = _snapshot(conn)
    cases = [
        (_dim_args(key=None, label="X"), "--key is required"),
        (_dim_args(key="   ", label="X"), "--key is required"),
        (_dim_args(key="territory", label="Territory"),
         "Dimension 'territory' does not exist"),
        (_dim_args(key="region", label="Renamed", dimension_type="number"),
         "--type must be one of text, uuid_fk, enum"),
        (_dim_args(key="region"),
         "Nothing to update — pass at least one field to change"),
    ]
    for args, message in cases:
        r = call_action(gl.update_dimension, conn, args)
        assert is_error(r), r
        assert r["message"] == message
    assert _snapshot(conn) == before
    assert _dimension(conn, "region") == dimensions["region"]
    assert _dimension(conn, "channel") == dimensions["channel"]


def test_import_chart_of_accounts_refuses_the_whole_file_listing_every_bad_row(
        conn, tmp_path):
    cid = seed_company(conn)
    seed_account(conn, cid, "Cash", "asset", "cash", account_number="1000")
    text = (
        "name,root_type,account_number,account_type,parent_name,currency,is_group\n"
        "Good Asset,asset,1100,,,,0\n"
        "Weird Receivable,income,4100,receivable,,,0\n"
        "Group Bank,asset,1200,bank,,,1\n"
        "Odd Root,revenue,4200,,,,0\n"
        "Widget Account,asset,1300,widget,,,0\n"
        "Orphan,expense,5100,,Nowhere,,0\n"
        "Late Child,expense,5200,,Late Parent,,0\n"
        "Late Parent,expense,5300,,,,1\n"
    )
    before = _snapshot(conn)
    r = _import(conn, _csv(tmp_path, text), cid)
    assert is_error(r), r
    assert r["message"] == (
        "Chart import refused, nothing was written: "
        "Row 3: account_type 'receivable' belongs on a root_type of asset, "
        "but 'Weird Receivable' has root_type 'income'. That combination leaves "
        "the account on the income side of the books while every report that "
        "filters on account_type reads it as receivable.; "
        "Row 4: account_type 'bank' must be a posting (leaf) account, not a group; "
        "Row 5: root_type 'revenue' must be one of asset, liability, equity, "
        "income, expense; "
        "Row 6: account_type 'widget' is not a registered, active type. Register it "
        "with add-account-type.; "
        "Row 7: parent_name 'Nowhere' is not an account of this company or an "
        "earlier row of this file; "
        "Row 8: parent_name 'Late Parent' is not an account of this company or "
        "an earlier row of this file")
    assert "imported" not in r
    assert _snapshot(conn) == before
    conn.commit()
    assert sorted(_accounts(conn, cid).keys()) == ["Cash"]


def test_import_chart_of_accounts_bad_root_type_after_good_rows_leaves_nothing(
        conn, tmp_path):
    cid = seed_company(conn)
    seed_account(conn, cid, "Cash", "asset", "cash", account_number="1000")
    audits = _count(conn, "audit_log")
    text = "name,root_type\nAlpha,asset\nBeta,asset\nGamma,revenue\n"
    before = _snapshot(conn)
    r = _import(conn, _csv(tmp_path, text), cid)
    assert is_error(r), r
    assert r["message"] == (
        "Chart import refused, nothing was written: "
        "Row 4: root_type 'revenue' must be one of asset, liability, equity, "
        "income, expense")
    assert _snapshot(conn) == before
    conn.commit()
    assert sorted(_accounts(conn, cid).keys()) == ["Cash"]
    assert _count(conn, "audit_log") == audits


def test_import_chart_of_accounts_writes_one_audit_row_per_created_account(
        conn, tmp_path):
    cid = seed_company(conn)
    legacy = seed_account(conn, cid, "Legacy Expenses", "expense",
                          account_number="5999", is_group=1)
    t_depth = Table("account")
    q_depth = Q.update(t_depth).set(Field("depth"), 1).where(t_depth.id == P())
    conn.execute(q_depth.get_sql(), (legacy,))
    conn.commit()
    audits = _count(conn, "audit_log")
    r = _import(conn, _csv(tmp_path, CHART), cid)
    assert is_ok(r), r
    assert (r["imported"], r["skipped"], r["total_rows"]) == (9, 0, 9)
    assert _count(conn, "audit_log") == audits + 9
    t_account = Table("account")
    q_accounts = (Q.from_(t_account).select(t_account.id, t_account.name)
                  .where(t_account.company_id == P()))
    id_by_name = {row["name"]: row["id"] for row in
                  conn.execute(q_accounts.get_sql(), (cid,)).fetchall()}
    assert "Legacy Expenses" in id_by_name
    created_ids = {name: aid for name, aid in id_by_name.items()
                   if name != "Legacy Expenses"}
    assert len(created_ids) == 9
    t_log = Table("audit_log")
    q_logs = (Q.from_(t_log)
              .select(t_log.entity_id, t_log.skill, t_log.action,
                      t_log.entity_type, t_log.new_values)
              .where(t_log.skill == P())
              .where(t_log.action == P())
              .where(t_log.entity_type == P()))
    log_rows = conn.execute(
        q_logs.get_sql(), ("erpclaw-gl", "create", "account")).fetchall()
    by_entity = {row["entity_id"]: row for row in log_rows
                 if row["entity_id"] in created_ids.values()}
    assert len(by_entity) == 9
    for row in by_entity.values():
        assert (row["skill"], row["action"], row["entity_type"]) == (
            "erpclaw-gl", "create", "account")
    assert json.loads(by_entity[id_by_name["Operating Bank"]]["new_values"]) == {
        "name": "Operating Bank", "root_type": "asset", "account_type": "bank"}
    assert json.loads(by_entity[id_by_name["Current Assets"]]["new_values"]) == {
        "name": "Current Assets", "root_type": "asset", "account_type": None}
    fresh = seed_company(conn)
    seed_account(conn, fresh, "Operating Bank", "asset", "cash",
                 account_number="1999")
    skip_text = ("name,root_type,account_number,account_type\n"
                 "Operating Bank,asset,1010,bank\n"
                 "Service Income,income,4000,revenue\n"
                 "Service Income,income,4001,revenue\n")
    skip_audits = _count(conn, "audit_log")
    r = _import(conn, _csv(tmp_path, skip_text, "skip.csv"), fresh)
    assert is_ok(r), r
    assert (r["imported"], r["skipped"], r["total_rows"]) == (1, 2, 3)
    assert _count(conn, "audit_log") == skip_audits + 1


def test_update_dimension_is_active_false_refuses_while_referenced(
        conn, dimensions):
    env = _company_env(conn)
    entries = [
        {"account_id": env["cash"], "debit": "10.00", "credit": "0"},
        {"account_id": env["revenue"], "debit": "0", "credit": "10.00",
         "cost_center_id": env["cc"], "dimensions": {"region": "north"}},
    ]
    r = call_action(gl.post_gl_entries, conn, ns(
        voucher_type="journal_entry", voucher_id="V-REGION-1",
        posting_date="2026-02-10", company_id=env["company_id"],
        entries=json.dumps(entries)))
    assert is_ok(r), r
    today = datetime.now(timezone.utc).date()
    age = (today - date.fromisoformat("2026-02-10")).days
    within = age + 30
    cutoff = (today - timedelta(days=within)).isoformat()
    before = _snapshot(conn)
    region_before = _dimension(conn, "region")
    assert region_before == dimensions["region"]
    r = call_action(gl.update_dimension, conn, _dim_args(
        key="region", label="Zone", is_active=False, within_days=str(within)))
    assert is_error(r), r
    assert r["message"] == (
        f"Dimension 'region' is referenced by 1 live GL entry since {cutoff} "
        f"(within {within} days); cannot deactivate")
    assert _snapshot(conn) == before
    assert _dimension(conn, "region") == dimensions["region"]
    assert _dimension(conn, "region")["label"] == "Region"
    assert _dimension(conn, "region")["is_active"] == 1
    r = call_action(gl.update_dimension, conn, _dim_args(
        key="region", is_active=False, within_days=str(age - 1)))
    assert is_ok(r), r
    assert _dimension(conn, "region")["is_active"] == 0
    assert _count(conn, "audit_log") == before["audit_log"] + 1
