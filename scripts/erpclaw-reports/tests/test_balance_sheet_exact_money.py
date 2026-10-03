"""Exact-money balance-sheet net income (task m630).

Money is text: Decimal in Python, TEXT columns. Year-to-date net income must
be built from exact-decimal text sums with the subtraction done in Python.
Casting the exact sums back to a numeric type so the database can subtract
them is float arithmetic on SQLite: at magnitudes where a binary float can
no longer hold cents, the rounded cent flips (here ...600.27 posts as
...600.28).

Book (fiscal year 2026-01-01 to 2026-12-31, as of 2026-06-30):
  income credits 1000.10, 0.20 and 100000000000000.07 against a 0.10 debit
    -> exact income 100000000001000.27
  expense debit 400.00 -> exact expense 400.00
  exact net income 100000000000600.27, with no other equity balances so total
  equity feeds the same figure.
A cancelled 999.99 income credit and a 50.00 credit dated after as-of pin the
cancelled-row exclusion and the as-of date, which must not change.
"""
import importlib.util
import json
import os
import uuid
from decimal import Decimal

import pytest

from payments_helpers import call_action, is_ok, ns

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(os.path.dirname(_TESTS_DIR))


def _load(name, domain):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_SCRIPTS_DIR, domain, "db_query.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


REP = _load("db_query_reports_m630", "erpclaw-reports")

AS_OF = "2026-06-30"


def _u():
    return str(uuid.uuid4())


def _account(conn, company_id, name, number, root_type, account_type):
    aid = _u()
    direction = "debit_normal" if root_type in ("asset", "expense") else "credit_normal"
    conn.execute(
        "INSERT INTO account (id, name, account_number, root_type, account_type, "
        "balance_direction, company_id, depth) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
        (aid, name, number, root_type, account_type, direction, company_id))
    return aid


def _gl(conn, account_id, posting_date, debit, credit, cancelled=0,
         project_id=None, dimensions_json="{}"):
    conn.execute(
        "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
        " debit_base, credit_base, currency, exchange_rate, voucher_type, "
        " voucher_id, entry_set, is_cancelled, project_id, dimensions_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'USD', '1', 'journal_entry', ?, "
        " 'primary', ?, ?, ?)",
        (_u(), posting_date, account_id, debit, credit, debit, credit,
         _u(), cancelled, project_id, dimensions_json))


@pytest.fixture
def book(conn):
    cid = _u()
    conn.execute("INSERT INTO company (id, name, abbr) VALUES (?, ?, ?)",
                 (cid, "Exact Money Co", "EM"))
    conn.execute(
        "INSERT INTO fiscal_year (id, name, start_date, end_date, is_closed, company_id) "
        "VALUES (?, 'FY-2026-EM', '2026-01-01', '2026-12-31', 0, ?)", (_u(), cid))
    revenue = _account(conn, cid, "Service Revenue", "4000", "income", "revenue")
    expense = _account(conn, cid, "Office Expense", "5100", "expense", "expense")
    _gl(conn, revenue, "2026-02-01", "0", "1000.10")
    _gl(conn, revenue, "2026-02-02", "0", "0.20")
    _gl(conn, revenue, "2026-02-03", "0.10", "0")
    _gl(conn, revenue, "2026-02-04", "0", "100000000000000.07")
    _gl(conn, revenue, "2026-02-05", "0", "999.99", cancelled=1)
    _gl(conn, revenue, "2026-09-01", "0", "50.00")
    _gl(conn, expense, "2026-02-05", "400.00", "0")
    conn.commit()
    return {"company_id": cid}


def test_net_income_ytd_is_the_exact_hand_computed_string(conn, book):
    res = call_action(REP.balance_sheet, conn, ns(
        company_id=book["company_id"], as_of_date=AS_OF))
    assert res["status"] == "ok", res

    income = Decimal("1000.10") + Decimal("0.20") + Decimal("100000000000000.07") \
        - Decimal("0.10")
    assert income == Decimal("100000000001000.27")
    net = income - Decimal("400.00")
    assert net == Decimal("100000000000600.27")

    # Exact strings: float arithmetic posts ...600.28 here.
    assert res["net_income_ytd"] == "100000000000600.27"
    assert res["total_equity"] == "100000000000600.27"


@pytest.fixture
def filter_book(conn):
    """Project/dimension filter book (fiscal year 2026-01-01 to 2026-12-31).

    gl_entry.project_id is plain text with no reference, so any two
    distinct ids serve as projects A and B.
    """
    cid = _u()
    conn.execute("INSERT INTO company (id, name, abbr) VALUES (?, ?, ?)",
                 (cid, "Filter Co", "FB"))
    conn.execute(
        "INSERT INTO fiscal_year (id, name, start_date, end_date, is_closed, company_id) "
        "VALUES (?, 'FY-2026-FB', '2026-01-01', '2026-12-31', 0, ?)", (_u(), cid))
    revenue = _account(conn, cid, "Service Revenue", "4000", "income", "revenue")
    expense = _account(conn, cid, "Office Expense", "5100", "expense", "expense")
    proj_a = _u()
    proj_b = _u()
    _gl(conn, revenue, "2026-02-01", "0", "1000.10",
        project_id=proj_a,
        dimensions_json=json.dumps({"department": "sales"}))
    _gl(conn, revenue, "2026-02-02", "0", "0.20",
        project_id=proj_b,
        dimensions_json=json.dumps({"department": "ops"}))
    _gl(conn, expense, "2026-02-03", "400.00", "0",
        project_id=proj_a,
        dimensions_json=json.dumps({"department": "ops"}))
    _gl(conn, expense, "2026-02-04", "0.05", "0",
        dimensions_json=json.dumps({}))
    conn.commit()
    return {"company_id": cid, "proj_a": proj_a, "proj_b": proj_b}


def test_net_income_ytd_without_filters(conn, filter_book):
    res = call_action(REP.balance_sheet, conn, ns(
        company_id=filter_book["company_id"], as_of_date=AS_OF))
    assert res["status"] == "ok", res
    # 1000.10 + 0.20 - 400.00 - 0.05 = 600.25.
    assert res["net_income_ytd"] == "600.25"
    assert res["total_equity"] == "600.25"


def test_net_income_ytd_filtered_to_project_a(conn, filter_book):
    res = call_action(REP.balance_sheet, conn, ns(
        company_id=filter_book["company_id"], as_of_date=AS_OF,
        project_id=filter_book["proj_a"]))
    assert res["status"] == "ok", res
    # 1000.10 - 400.00 = 600.10.
    assert res["net_income_ytd"] == "600.10"
    assert res["total_equity"] == "600.10"


def test_net_income_ytd_filtered_to_department_ops(conn, filter_book):
    res = call_action(REP.balance_sheet, conn, ns(
        company_id=filter_book["company_id"], as_of_date=AS_OF,
        dimension_key=["department"], dimension_value=["ops"]))
    assert res["status"] == "ok", res
    # 0.20 - 400.00 = -399.80.
    assert res["net_income_ytd"] == "-399.80"
    assert res["total_equity"] == "-399.80"


def test_net_income_ytd_filtered_to_project_a_and_ops(conn, filter_book):
    res = call_action(REP.balance_sheet, conn, ns(
        company_id=filter_book["company_id"], as_of_date=AS_OF,
        project_id=filter_book["proj_a"],
        dimension_key=["department"], dimension_value=["ops"]))
    assert res["status"] == "ok", res
    # 0 - 400.00 = -400.00.
    assert res["net_income_ytd"] == "-400.00"
    assert res["total_equity"] == "-400.00"


def test_section_lists_only_nonzero_asset_accounts(conn):
    """Zero-amount pin: the Python loop drops net-zero accounts, so no
    database-side zero-row filter is needed and output must not change."""
    cid = _u()
    conn.execute("INSERT INTO company (id, name, abbr) VALUES (?, ?, ?)",
                 (cid, "Zero Pin Co", "ZP"))
    conn.execute(
        "INSERT INTO fiscal_year (id, name, start_date, end_date, is_closed, company_id) "
        "VALUES (?, 'FY-2026-ZP', '2026-01-01', '2026-12-31', 0, ?)", (_u(), cid))
    washed = _account(conn, cid, "Washed Account", "1010", "asset", "cash")
    empty = _account(conn, cid, "Empty Account", "1020", "asset", "cash")
    dust = _account(conn, cid, "Dust Account", "1030", "asset", "cash")
    _gl(conn, washed, "2026-02-01", "250.00", "0")
    _gl(conn, washed, "2026-02-02", "0", "250.00")
    _gl(conn, dust, "2026-02-01", "0.10", "0")
    _gl(conn, dust, "2026-02-02", "0.20", "0")
    conn.commit()
    res = call_action(REP.balance_sheet, conn, ns(
        company_id=cid, as_of_date=AS_OF))
    assert res["status"] == "ok", res
    assert res["assets"] == [
        {"account": "Dust Account", "account_id": dust, "amount": "0.30"}
    ]
    assert res["total_assets"] == "0.30"
