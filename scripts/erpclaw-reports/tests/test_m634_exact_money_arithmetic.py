"""m634 exact money arithmetic pins (Part A + Part B items 1, 2, 5).

Money is text: Decimal in Python, TEXT columns, exact string comparisons.
Never float. All seeds and direct reads are parameterised PyPika queries
through erpclaw_lib.query; connections come from the conftest fixtures.

Amounts near 1e14 with cents (for example 100000000000000.07): below about
7e13 a binary float still prints the right cents, so smaller amounts prove
nothing. Each Part A test fails on the pre-fix tree (..."06" where ..."07"
is exact) and passes after the Decimal-subtraction fix.
"""
import importlib.util
import json
import os
import uuid
from decimal import Decimal

from payments_helpers import call_action, is_ok, ns

from erpclaw_lib.query import Q, P, Table, Field, fn, insert_row

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(os.path.dirname(_TESTS_DIR))


def _load(name, domain):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_SCRIPTS_DIR, domain, "db_query.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


REP = _load("db_query_reports_m634", "erpclaw-reports")

BIG = "100000000000000.07"


def _u():
    return str(uuid.uuid4())


def _insert(conn, table, row):
    sql, cols = insert_row(table, {key: P() for key in row})
    conn.execute(sql, [row[c] for c in cols])


def _std_env(conn):
    cid = _u()
    _insert(conn, "company", {
        "id": cid, "name": "Exact Co %s" % cid[:6],
        "abbr": "EX%s" % cid[:4].upper(),
        "default_currency": "USD", "country": "United States",
        "fiscal_year_start_month": 1})
    fy_id = _u()
    _insert(conn, "fiscal_year", {
        "id": fy_id, "name": "FY-2026-%s" % cid[:6],
        "start_date": "2026-01-01", "end_date": "2026-12-31",
        "is_closed": 0, "company_id": cid})
    cc_id = _u()
    _insert(conn, "cost_center", {
        "id": cc_id, "name": "Main %s" % cid[:6],
        "company_id": cid, "is_group": 0})

    def _acct(name, number, root_type, account_type):
        aid = _u()
        direction = ("debit_normal"
                     if root_type in ("asset", "expense")
                     else "credit_normal")
        _insert(conn, "account", {
            "id": aid, "name": name, "account_number": number,
            "root_type": root_type, "account_type": account_type,
            "balance_direction": direction, "company_id": cid,
            "depth": 0, "is_group": 0})
        return aid

    bank = _acct("Operating Bank", "1010", "asset", "bank")
    expense = _acct("Office Expense", "5100", "expense", "expense")
    revenue = _acct("Service Revenue", "4000", "income", "revenue")
    conn.commit()
    return {"company_id": cid, "fiscal_year_id": fy_id,
            "cost_center_id": cc_id, "bank": bank,
            "expense": expense, "revenue": revenue}


def _extra_account(conn, company_id, name, number, root_type, account_type):
    aid = _u()
    direction = ("debit_normal"
                 if root_type in ("asset", "expense")
                 else "credit_normal")
    _insert(conn, "account", {
        "id": aid, "name": name, "account_number": number,
        "root_type": root_type, "account_type": account_type,
        "balance_direction": direction, "company_id": company_id,
        "depth": 0, "is_group": 0})
    conn.commit()
    return aid


def _gl(conn, account_id, posting_date, debit, credit, cancelled=0,
        party_type=None, party_id=None, cost_center_id=None):
    row = {"id": _u(), "posting_date": posting_date,
           "account_id": account_id, "debit": debit, "credit": credit,
           "voucher_type": "journal_entry", "voucher_id": _u(),
           "is_cancelled": cancelled}
    if party_type is not None:
        row["party_type"] = party_type
    if party_id is not None:
        row["party_id"] = party_id
    if cost_center_id is not None:
        row["cost_center_id"] = cost_center_id
    _insert(conn, "gl_entry", row)


def _mk_customer(conn, company_id, name):
    pid = _u()
    _insert(conn, "customer", {
        "id": pid, "name": "%s %s" % (name, pid[:6]),
        "company_id": company_id})
    conn.commit()
    return pid


def _base_ns(**kw):
    base = {"company_id": None, "company_name": None, "from_date": None,
            "to_date": None, "as_of_date": None, "account_id": None,
            "cost_center_id": None, "project_id": None,
            "fiscal_year_id": None, "party_type": None, "party_id": None,
            "voucher_type": None, "periods": None, "group_by": None,
            "dimension_key": None, "dimension_value": None,
            "limit": "100", "offset": "0", "aging_buckets": "30,60,90,120"}
    base.update(kw)
    return ns(**base)


def _q_rows(conn, table, **filters):
    t = Table(table)
    q = Q.from_(t).select(t.star)
    params = []
    for col, val in filters.items():
        q = q.where(Field(col) == P())
        params.append(val)
    rows = conn.execute(q.get_sql(), params).fetchall()
    return [dict(r) for r in rows]


class TestCashFlowExact:
    def test_opening_and_closing_are_exact(self, conn):
        e = _std_env(conn)
        _gl(conn, e["bank"], "2026-02-10", BIG, "0")
        _gl(conn, e["revenue"], "2026-02-10", "0", BIG)
        conn.commit()
        res = call_action(REP.cash_flow, conn, _base_ns(
            company_id=e["company_id"],
            from_date="2026-03-01", to_date="2026-04-30"))
        assert is_ok(res), res
        assert res["opening_balance"] == BIG
        assert res["closing_balance"] == BIG
        assert res["net_change"] == "0.00"


class TestGeneralLedgerExact:
    def test_opening_is_exact(self, conn):
        e = _std_env(conn)
        _gl(conn, e["expense"], "2026-02-10", BIG, "0")
        _gl(conn, e["bank"], "2026-02-10", "0", BIG)
        conn.commit()
        res = call_action(REP.general_ledger, conn, _base_ns(
            company_id=e["company_id"], account_id=e["expense"],
            from_date="2026-03-01", to_date="2026-04-30"))
        assert is_ok(res), res
        assert res["opening_balance"] == BIG
        assert res["closing_balance"] == BIG


class TestPartyLedgerExact:
    def test_opening_and_closing_are_exact(self, conn):
        e = _std_env(conn)
        pid = _mk_customer(conn, e["company_id"], "Exact Customer")
        _gl(conn, e["bank"], "2026-02-10", BIG, "0",
            party_type="customer", party_id=pid)
        conn.commit()
        res = call_action(REP.party_ledger, conn, _base_ns(
            party_type="customer", party_id=pid,
            from_date="2026-03-01", to_date="2026-04-30"))
        assert is_ok(res), res
        assert res["opening_balance"] == BIG
        assert res["entries"] == []
        assert res["closing_balance"] == BIG

    def test_no_from_date_is_exact(self, conn):
        e = _std_env(conn)
        ar = _extra_account(conn, e["company_id"],
                            "Accounts Receivable", "1100",
                            "asset", "receivable")
        pid = _mk_customer(conn, e["company_id"], "NoFrom Customer")
        _gl(conn, ar, "2026-03-10", BIG, "0",
            party_type="customer", party_id=pid)
        _gl(conn, e["revenue"], "2026-03-10", "0", BIG)
        conn.commit()
        res = call_action(REP.party_ledger, conn, _base_ns(
            party_type="customer", party_id=pid,
            from_date=None, to_date="2026-04-30"))
        assert is_ok(res), res
        assert res["opening_balance"] == "0.00"
        assert len(res["entries"]) == 1
        assert res["entries"][0]["debit"] == BIG
        assert res["closing_balance"] == BIG


class TestBudgetVsActualExact:
    def test_actual_variance_and_pct_are_exact(self, conn):
        e = _std_env(conn)
        _insert(conn, "budget", {
            "id": _u(), "fiscal_year_id": e["fiscal_year_id"],
            "account_id": e["expense"], "budget_amount": "100.00",
            "company_id": e["company_id"],
            "action_if_exceeded": "warn"})
        _gl(conn, e["expense"], "2026-04-01", BIG, "0")
        _gl(conn, e["bank"], "2026-04-01", "0", BIG)
        conn.commit()
        fy_rows = _q_rows(conn, "fiscal_year",
                         id=e["fiscal_year_id"])
        assert fy_rows[0]["start_date"] == "2026-01-01"
        res = call_action(REP.budget_vs_actual, conn, _base_ns(
            company_id=e["company_id"],
            fiscal_year_id=e["fiscal_year_id"]))
        assert is_ok(res), res
        item = [i for i in res["items"]
                if i["account_or_cc"] == "Office Expense"][0]
        assert item["budget"] == "100.00"
        assert item["actual"] == BIG
        assert item["variance"] == "-99999999999900.07"
        assert item["variance_pct"] == "-99999999999900.07"


class TestComparativePlExact:
    _PERIODS = [{"from_date": "2026-03-01", "to_date": "2026-03-31",
                 "label": "Mar"},
                {"from_date": "2026-04-01", "to_date": "2026-04-30",
                 "label": "Apr"}]

    def test_income_branch_is_exact(self, conn):
        e = _std_env(conn)
        _gl(conn, e["bank"], "2026-04-02", BIG, "0")
        _gl(conn, e["revenue"], "2026-04-02", "0", BIG)
        conn.commit()
        res = call_action(REP.comparative_pl, conn, _base_ns(
            company_id=e["company_id"],
            periods=json.dumps(self._PERIODS)))
        assert is_ok(res), res
        rev = [a for a in res["accounts"]
               if a["account"] == "Service Revenue"][0]
        assert rev["periods"] == [
            {"label": "Mar", "amount": "0.00"},
            {"label": "Apr", "amount": BIG}]
        totals = {t["label"]: t for t in res["totals"]}
        assert totals["Mar"] == {"label": "Mar", "income": "0.00",
                                 "expenses": "0.00", "net": "0.00"}
        assert totals["Apr"]["income"] == BIG
        assert totals["Apr"]["expenses"] == "0.00"
        assert totals["Apr"]["net"] == BIG

    def test_expense_branch_is_exact(self, conn):
        e = _std_env(conn)
        _gl(conn, e["bank"], "2026-04-01", "100.00", "0")
        _gl(conn, e["revenue"], "2026-04-01", "0", "100.00")
        _gl(conn, e["expense"], "2026-04-02", BIG, "0")
        _gl(conn, e["bank"], "2026-04-02", "0", BIG)
        conn.commit()
        res = call_action(REP.comparative_pl, conn, _base_ns(
            company_id=e["company_id"],
            periods=json.dumps(self._PERIODS)))
        assert is_ok(res), res
        by_name = {a["account"]: a for a in res["accounts"]}
        assert by_name["Office Expense"]["periods"] == [
            {"label": "Mar", "amount": "0.00"},
            {"label": "Apr", "amount": BIG}]
        assert by_name["Service Revenue"]["periods"] == [
            {"label": "Mar", "amount": "0.00"},
            {"label": "Apr", "amount": "100.00"}]
        totals = {t["label"]: t for t in res["totals"]}
        assert totals["Mar"] == {"label": "Mar", "income": "0.00",
                                 "expenses": "0.00", "net": "0.00"}
        assert totals["Apr"]["income"] == "100.00"
        assert totals["Apr"]["expenses"] == BIG
        assert totals["Apr"]["net"] == "-99999999999900.07"


class TestProfitAndLossExact:
    def test_income_side_is_exact(self, conn):
        e = _std_env(conn)
        _gl(conn, e["bank"], "2026-04-01", BIG, "0")
        _gl(conn, e["revenue"], "2026-04-01", "0", BIG)
        conn.commit()
        res = call_action(REP.profit_and_loss, conn, _base_ns(
            company_id=e["company_id"],
            from_date="2026-03-01", to_date="2026-04-30"))
        assert is_ok(res), res
        assert res["income"] == [
            {"account": "Service Revenue", "account_id": e["revenue"],
             "amount": BIG}]
        assert res["income_total"] == BIG
        assert res["expenses"] == []
        assert res["expense_total"] == "0.00"
        assert res["net_income"] == BIG

    def test_expense_side_is_exact(self, conn):
        e = _std_env(conn)
        _gl(conn, e["bank"], "2026-04-01", "100.00", "0")
        _gl(conn, e["revenue"], "2026-04-01", "0", "100.00")
        _gl(conn, e["expense"], "2026-04-05", BIG, "0")
        _gl(conn, e["bank"], "2026-04-05", "0", BIG)
        conn.commit()
        res = call_action(REP.profit_and_loss, conn, _base_ns(
            company_id=e["company_id"],
            from_date="2026-03-01", to_date="2026-04-30"))
        assert is_ok(res), res
        assert res["income"] == [
            {"account": "Service Revenue", "account_id": e["revenue"],
             "amount": "100.00"}]
        assert res["income_total"] == "100.00"
        assert res["expenses"] == [
            {"account": "Office Expense", "account_id": e["expense"],
             "amount": BIG}]
        assert res["expense_total"] == BIG
        assert res["net_income"] == "-99999999999900.07"


class TestBalanceSheetExact:
    def test_ytd_income_side_is_exact(self, conn):
        e = _std_env(conn)
        _gl(conn, e["bank"], "2026-04-01", BIG, "0")
        _gl(conn, e["revenue"], "2026-04-01", "0", BIG)
        conn.commit()
        res = call_action(REP.balance_sheet, conn, _base_ns(
            company_id=e["company_id"], as_of_date="2026-04-30"))
        assert is_ok(res), res
        assert res["net_income_ytd"] == BIG
        assert res["total_equity"] == BIG
        assert res["total_assets"] == BIG

    def test_expense_aggregate_is_exact(self, conn):
        e = _std_env(conn)
        payable = _extra_account(conn, e["company_id"],
                                 "Trade Payables", "2000",
                                 "liability", "payable")
        _gl(conn, e["expense"], "2026-04-01", BIG, "0")
        _gl(conn, payable, "2026-04-01", "0", BIG)
        conn.commit()
        res = call_action(REP.balance_sheet, conn, _base_ns(
            company_id=e["company_id"], as_of_date="2026-04-30"))
        assert is_ok(res), res
        assert res["net_income_ytd"] == "-100000000000000.07"
        assert res["total_equity"] == "-100000000000000.07"
