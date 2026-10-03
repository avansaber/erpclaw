"""Closed-year P&L still reports the year's income and expenses (task m722).

Closing a fiscal year posts one ``period_closing`` voucher that zeroes every
income/expense account into retained earnings. That voucher is a bookkeeping
transfer, not activity, so the P&L-shaped reports must exclude it; the balance
sheet and trial balance keep counting it (it is what moves the result into
retained earnings) and are pinned unchanged here.

Book (fiscal year 2026-01-01 to 2026-12-31):
  revenue 1000.00 (Dr receivable / Cr sales revenue, 2026-03-15)
  expense  300.00 (Dr office expense / Cr bank, 2026-04-10)
  close 2026-12-31 via the lib's ``insert_gl_entries`` with
  ``voucher_type="period_closing"``, exactly the legs ``close_fiscal_year``
  builds: Dr revenue 1000.00 / Cr retained 1000.00 and Cr expense 300.00 /
  Dr retained 300.00 (net Cr retained 700.00). The real ``close_fiscal_year``
  action is not used because driving it from the reports suite would mean
  editing shared helpers; the voucher is byte-identical to what it posts.

Money is text: exact ``Decimal`` strings, never float. All parameters bound.
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
_IN_TREE_LIB = os.path.join(_SETUP_DIR, "lib")
if _IN_TREE_LIB not in sys.path:
    if importlib.util.find_spec("erpclaw_lib") is None:
        sys.path.insert(0, _IN_TREE_LIB)

_PAY_TESTS = os.path.join(_SCRIPTS_DIR, "erpclaw-payments", "tests")
if _PAY_TESTS not in sys.path:
    sys.path.append(_PAY_TESTS)

from payments_helpers import call_action, is_ok, ns  # noqa: E402

from erpclaw_lib.gl_posting import insert_gl_entries  # noqa: E402


def _load(name, domain):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_SCRIPTS_DIR, domain, "db_query.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


REP = _load("db_query_reports_m722", "erpclaw-reports")

FROM = "2026-01-01"
TO = "2026-12-31"
AS_OF = "2026-12-31"


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


@pytest.fixture
def env(conn):
    cid = _u()
    conn.execute("INSERT INTO company (id, name, abbr) VALUES (?, ?, ?)",
                 (cid, "Close Co", "CC"))
    conn.execute(
        "INSERT INTO fiscal_year (id, name, start_date, end_date, is_closed, company_id) "
        "VALUES (?, 'FY-2026-CC', '2026-01-01', '2026-12-31', 0, ?)", (_u(), cid))
    cc = _u()
    conn.execute(
        "INSERT INTO cost_center (id, name, company_id, is_group) VALUES (?, ?, ?, 0)",
        (cc, "Main CC", cid))
    conn.execute(
        "INSERT INTO customer (id, name, company_id) VALUES (?, ?, ?)",
        (_u(), "Close Customer", cid))
    customer_id = conn.execute(
        "SELECT id FROM customer WHERE company_id = ?", (cid,)).fetchone()["id"]
    book = {
        "company_id": cid,
        "cc": cc,
        "customer_id": customer_id,
        "receivable": _account(conn, cid, "Accounts Receivable", "1100", "asset", "receivable"),
        "bank": _account(conn, cid, "Operating Bank", "1010", "asset", "bank"),
        "revenue": _account(conn, cid, "Sales Revenue", "4000", "income", "revenue"),
        "expense": _account(conn, cid, "Office Expense", "5100", "expense", "expense"),
        "retained": _account(conn, cid, "Retained Earnings", "3000", "equity", "equity"),
    }
    # The core schema already seeds a 'department' dimension row (UNIQUE on
    # key), so activate it instead of inserting a duplicate.
    cur = conn.execute(
        "UPDATE dimension_registry SET is_active = 1 WHERE key = 'department'")
    if cur.rowcount == 0:
        conn.execute(
            "INSERT INTO dimension_registry (id, key, label, data_type, referenced_table, "
            "allowed_values_json, is_required_on_account_types_json, is_active) "
            "VALUES (?, 'department', 'department', 'text', NULL, NULL, NULL, 1)",
            (_u(),))
    conn.commit()
    return book


def _post_activity(conn, env):
    insert_gl_entries(
        conn,
        [{"account_id": env["receivable"], "debit": "1000.00", "credit": "0",
          "party_type": "customer", "party_id": env["customer_id"]},
         {"account_id": env["revenue"], "debit": "0", "credit": "1000.00",
          "cost_center_id": env["cc"], "fiscal_year": "FY-2026-CC"}],
        voucher_type="journal_entry", voucher_id=_u(),
        posting_date="2026-03-15", company_id=env["company_id"])
    insert_gl_entries(
        conn,
        [{"account_id": env["expense"], "debit": "300.00", "credit": "0",
          "cost_center_id": env["cc"], "fiscal_year": "FY-2026-CC"},
         {"account_id": env["bank"], "debit": "0", "credit": "300.00"}],
        voucher_type="journal_entry", voucher_id=_u(),
        posting_date="2026-04-10", company_id=env["company_id"])
    conn.commit()


def _post_closing(conn, env):
    insert_gl_entries(
        conn,
        [{"account_id": env["revenue"], "debit": "1000.00", "credit": "0",
          "cost_center_id": env["cc"], "fiscal_year": "FY-2026-CC"},
         {"account_id": env["retained"], "debit": "0", "credit": "1000.00",
          "fiscal_year": "FY-2026-CC"},
         {"account_id": env["expense"], "debit": "0", "credit": "300.00",
          "cost_center_id": env["cc"], "fiscal_year": "FY-2026-CC"},
         {"account_id": env["retained"], "debit": "300.00", "credit": "0",
          "fiscal_year": "FY-2026-CC"}],
        voucher_type="period_closing", voucher_id=_u(),
        posting_date="2026-12-31", company_id=env["company_id"],
        remarks="Period close FY-2026-CC")
    conn.commit()


def _flat_pl(conn, env, **extra):
    res = call_action(REP.profit_and_loss, conn, ns(
        company_id=env["company_id"], company_name=None,
        from_date=FROM, to_date=TO, group_by=None, project_id=None,
        dimension_key=[], dimension_value=[], **extra))
    assert is_ok(res), res
    return res


def test_flat_pl_after_close(conn, env):
    _post_activity(conn, env)
    _post_closing(conn, env)
    res = _flat_pl(conn, env)
    assert Decimal(res["income_total"]) == Decimal("1000.00")
    assert Decimal(res["expense_total"]) == Decimal("300.00")
    assert Decimal(res["net_income"]) == Decimal("700.00")
    by_account = {row["account"]: Decimal(row["amount"]) for row in res["income"]}
    assert by_account == {"Sales Revenue": Decimal("1000.00")}


def test_grouped_pl_after_close(conn, env):
    _post_activity(conn, env)
    _post_closing(conn, env)
    res = call_action(REP.profit_and_loss, conn, ns(
        company_id=env["company_id"], company_name=None,
        from_date=FROM, to_date=TO, group_by="department", project_id=None,
        dimension_key=[], dimension_value=[]))
    assert is_ok(res), res
    assert Decimal(res["income_total"]) == Decimal("1000.00")
    assert Decimal(res["expense_total"]) == Decimal("300.00")
    assert Decimal(res["net_income"]) == Decimal("700.00")
    assert len(res["groups"]) == 1
    assert res["groups"][0]["department"] == "(untagged)"
    assert Decimal(res["groups"][0]["revenue"]) == Decimal("1000.00")
    assert Decimal(res["groups"][0]["expenses"]) == Decimal("300.00")


def test_comparative_pl_after_close(conn, env):
    _post_activity(conn, env)
    _post_closing(conn, env)
    res = call_action(REP.comparative_pl, conn, ns(
        company_id=env["company_id"], company_name=None,
        periods=json.dumps([{"from_date": FROM, "to_date": TO, "label": "2026"}])))
    assert is_ok(res), res
    assert len(res["totals"]) == 1
    period = res["totals"][0]
    assert period["label"] == "2026"
    assert Decimal(period["income"]) == Decimal("1000.00")
    assert Decimal(period["expenses"]) == Decimal("300.00")
    assert Decimal(period["net"]) == Decimal("700.00")


def test_balance_sheet_after_close_still_counts_closing_voucher(conn, env):
    _post_activity(conn, env)
    _post_closing(conn, env)
    res = call_action(REP.balance_sheet, conn, ns(
        company_id=env["company_id"], company_name=None, as_of_date=AS_OF,
        project_id=None, dimension_key=[], dimension_value=[]))
    assert is_ok(res), res
    equity = {row["account"]: Decimal(row["amount"]) for row in res["equity"]}
    assert equity == {"Retained Earnings": Decimal("700.00")}
    assert Decimal(res["total_assets"]) == Decimal("700.00")
    assert Decimal(res["total_liabilities"]) == Decimal("0.00")
    assert Decimal(res["total_equity"]) == Decimal("700.00")
    assert Decimal(res["net_income_ytd"]) == Decimal("0.00")
    assert Decimal(res["total_assets"]) == \
        Decimal(res["total_liabilities"]) + Decimal(res["total_equity"])


def test_flat_pl_before_close(conn, env):
    _post_activity(conn, env)
    res = _flat_pl(conn, env)
    assert Decimal(res["income_total"]) == Decimal("1000.00")
    assert Decimal(res["expense_total"]) == Decimal("300.00")
    assert Decimal(res["net_income"]) == Decimal("700.00")
