"""Period close through the house posting path (m324d, m324e follow-ups).

Covers close-fiscal-year row selection (a decoy-rich selection test plus
the pinned company-level zero-net gate), exactness, posting-path fields
(currency, cost centre, checksum chain, integrity), clean refusals with
full-row write-nothing snapshots, reopen-fiscal-year reversal, and the
five dialect-neutral duplicate-key refusals.

Every read or write in this file goes through ``erpclaw_lib.query``
(PyPika, parameterised); connections come from the suite fixtures.
Money is compared as exact text through Decimal, never float.
"""
import json
import uuid

import pytest
from decimal import Decimal

from gl_helpers import (
    call_action, ns, is_error, is_ok, get_conn,
    seed_company, seed_account, seed_fiscal_year, seed_cost_center,
    load_db_query,
)
from erpclaw_lib.db import get_dialect
from erpclaw_lib.query import Q, P, Table, Field, fn, insert_row

mod = load_db_query()

POSTING_DATE = "2026-12-31"
NO_CC_MESSAGE = ("Company has no default cost centre — "
                 "set default_cost_center_id before closing the fiscal year")


def _set_default_cc(conn, company_id, cc_id):
    t = Table("company")
    q = (Q.update(t)
         .set(Field("default_cost_center_id"), P())
         .where(Field("id") == P()))
    conn.execute(q.get_sql(), (cc_id, company_id))
    conn.commit()


def _set_currency(conn, company_id, code):
    t = Table("company")
    q = (Q.update(t)
         .set(Field("default_currency"), P())
         .where(Field("id") == P()))
    conn.execute(q.get_sql(), (code, company_id))
    conn.commit()


def _post(conn, company_id, voucher_id, date, legs):
    result = call_action(mod.post_gl_entries, conn, ns(
        voucher_type="journal_entry", voucher_id=voucher_id,
        posting_date=date, company_id=company_id,
        entries=json.dumps(legs)))
    assert is_ok(result), result
    return result


def _close(conn, fy_id, closing_id, date=POSTING_DATE):
    return call_action(mod.close_fiscal_year, conn, ns(
        fiscal_year_id=fy_id, closing_account_id=closing_id,
        posting_date=date))


def _count(conn, table, filters=None):
    t = Table(table)
    q = Q.from_(t).select(fn.Count("*").as_("n"))
    params = []
    if filters:
        for column, value in filters.items():
            q = q.where(Field(column) == P())
            params.append(value)
    return conn.execute(q.get_sql(), tuple(params)).fetchone()["n"]


_SNAPSHOT_TABLES = ("period_closing_voucher", "gl_entry",
                    "fiscal_year", "audit_log")


def _full_rows(conn, table):
    t = Table(table)
    q = Q.from_(t).select(t.star).orderby(t.id)
    return [dict(r) for r in conn.execute(q.get_sql()).fetchall()]


def _snapshot(conn):
    return {name: _full_rows(conn, name) for name in _SNAPSHOT_TABLES}


def _closing_rows(conn, pcv_id):
    t = Table("gl_entry")
    q = (Q.from_(t).select(t.star)
         .where(Field("voucher_type") == P())
         .where(Field("voucher_id") == P()))
    return [dict(r) for r in
            conn.execute(q.get_sql(), ("period_closing", pcv_id)).fetchall()]


def _env(conn, start="2026-01-01", end="2026-12-31"):
    cid = seed_company(conn)
    cc = seed_cost_center(conn, cid)
    _set_default_cc(conn, cid, cc)
    env = {"company_id": cid, "cc": cc}
    env["cash"] = seed_account(conn, cid, "Cash", "asset", "cash", "1000")
    env["revenue"] = seed_account(conn, cid, "Sales", "income", "revenue", "4000")
    env["expense"] = seed_account(conn, cid, "Supplies", "expense", "expense", "5000")
    env["retained"] = seed_account(conn, cid, "Retained Earnings", "equity",
                                   "equity", "3000")
    env["fy"] = seed_fiscal_year(conn, cid, "FY " + cid[:6], start, end)
    return env


class TestRowSelection:
    def test_zero_net_accounts_produce_no_closing_row(self, conn):
        # Accounts that are each zero in the year (an idle
        # income account and an expense account whose debits equal its
        # credits) produce no closing row, even though the close now
        # always runs the per-account selection.
        env = _env(conn)
        cid = env["company_id"]
        # Income account with no entries in the year: closes nothing.
        idle = seed_account(conn, cid, "Idle Income", "income", "revenue", "4010")
        # Expense account whose debits equal its credits: net zero.
        _post(conn, cid, "JE-Z1", "2026-03-01", [
            {"account_id": env["expense"], "debit": "250.00", "credit": "0",
             "cost_center_id": env["cc"]},
            {"account_id": env["cash"], "debit": "0", "credit": "250.00"},
        ])
        _post(conn, cid, "JE-Z2", "2026-04-01", [
            {"account_id": env["cash"], "debit": "250.00", "credit": "0"},
            {"account_id": env["expense"], "debit": "0", "credit": "250.00",
             "cost_center_id": env["cc"]},
        ])
        result = _close(conn, env["fy"], env["retained"])
        assert is_ok(result), result
        assert result["gl_entries_created"] == 0
        for aid in (idle, env["expense"]):
            n = _count(conn, "gl_entry",
                       {"voucher_type": "period_closing", "account_id": aid})
            assert n == 0
        assert _count(conn, "gl_entry",
                       {"voucher_type": "period_closing"}) == 0

    def test_only_nonzero_in_year_company_rows_close(self, conn):
        env = _env(conn)
        cid = env["company_id"]
        idle = seed_account(conn, cid, "Idle Income", "income", "revenue", "4010")
        _post(conn, cid, "JE-Z1", "2026-03-01", [
            {"account_id": env["expense"], "debit": "250.00", "credit": "0",
             "cost_center_id": env["cc"]},
            {"account_id": env["cash"], "debit": "0", "credit": "250.00"},
        ])
        _post(conn, cid, "JE-Z2", "2026-04-01", [
            {"account_id": env["cash"], "debit": "250.00", "credit": "0"},
            {"account_id": env["expense"], "debit": "0", "credit": "250.00",
             "cost_center_id": env["cc"]},
        ])
        consulting = seed_account(conn, cid, "Consulting Income", "income",
                                  "revenue", "4020")
        _post(conn, cid, "JE-CONS", "2026-05-01", [
            {"account_id": env["cash"], "debit": "1000.00", "credit": "0"},
            {"account_id": consulting, "debit": "0", "credit": "1000.00",
             "cost_center_id": env["cc"]},
        ])
        prior = seed_account(conn, cid, "Prior Year Income", "income",
                             "revenue", "4030")
        seed_fiscal_year(conn, cid, "FY25 " + cid[:6],
                         "2025-01-01", "2025-12-31")
        _post(conn, cid, "JE-PRIOR", "2025-12-20", [
            {"account_id": env["cash"], "debit": "300.00", "credit": "0"},
            {"account_id": prior, "debit": "0", "credit": "300.00",
             "cost_center_id": env["cc"]},
        ])
        for account_id, debit, credit in ((env["cash"], "70.00", "0"),
                                          (consulting, "0", "70.00")):
            row_id = str(uuid.uuid4())
            sql, _cols = insert_row("gl_entry",
                                    {"id": P(), "posting_date": P(),
                                     "account_id": P(), "debit": P(),
                                     "credit": P(), "voucher_type": P(),
                                     "voucher_id": P(), "is_cancelled": P()})
            conn.execute(sql, (row_id, "2026-07-01", account_id, debit,
                               credit, "journal_entry", "JE-VOID", 1))
        conn.commit()
        env2 = _env(conn)
        _post(conn, env2["company_id"], "JE-C2", "2026-06-01", [
            {"account_id": env2["cash"], "debit": "500.00", "credit": "0"},
            {"account_id": env2["revenue"], "debit": "0", "credit": "500.00",
             "cost_center_id": env2["cc"]},
        ])
        result = _close(conn, env["fy"], env["retained"])
        assert is_ok(result), result
        assert result["gl_entries_created"] == 2
        assert result["net_pl_transferred"] == "1000.00"
        rows = _closing_rows(conn, result["pcv_id"])
        assert len(rows) == 2
        by_acct = {r["account_id"]: r for r in rows}
        assert set(by_acct) == {consulting, env["retained"]}
        consult_row = by_acct[consulting]
        assert consult_row["debit"] == "1000.00"
        assert Decimal(str(consult_row["credit"])) == 0
        retained_row = by_acct[env["retained"]]
        assert retained_row["credit"] == "1000.00"
        assert Decimal(str(retained_row["debit"])) == 0
        for aid in (idle, env["expense"], prior,
                    env2["cash"], env2["revenue"],
                    env2["expense"], env2["retained"]):
            assert aid not in by_acct
        assert _count(conn, "gl_entry",
                       {"voucher_type": "period_closing"}) == 2

    def test_equal_income_and_expense_still_zero_each_account(self, conn):
        env = _env(conn)
        cid = env["company_id"]
        _post(conn, cid, "JE-INC500", "2026-06-15", [
            {"account_id": env["cash"], "debit": "500.00", "credit": "0"},
            {"account_id": env["revenue"], "debit": "0", "credit": "500.00",
             "cost_center_id": env["cc"]},
        ])
        _post(conn, cid, "JE-EXP500", "2026-06-15", [
            {"account_id": env["expense"], "debit": "500.00", "credit": "0",
             "cost_center_id": env["cc"]},
            {"account_id": env["cash"], "debit": "0", "credit": "500.00"},
        ])
        result = _close(conn, env["fy"], env["retained"])
        assert is_ok(result), result
        assert result["gl_entries_created"] == 4
        rows = _closing_rows(conn, result["pcv_id"])
        assert len(rows) == 4
        rev_rows = [r for r in rows if r["account_id"] == env["revenue"]]
        exp_rows = [r for r in rows if r["account_id"] == env["expense"]]
        ret_rows = [r for r in rows if r["account_id"] == env["retained"]]
        assert len(rev_rows) == 1
        assert rev_rows[0]["debit"] == "500.00"
        assert Decimal(str(rev_rows[0]["credit"])) == 0
        assert len(exp_rows) == 1
        assert Decimal(str(exp_rows[0]["debit"])) == 0
        assert exp_rows[0]["credit"] == "500.00"
        assert len(ret_rows) == 2
        assert sorted(Decimal(str(r["credit"])) for r in ret_rows) == [
            Decimal("0"), Decimal("500.00")]
        assert sorted(Decimal(str(r["debit"])) for r in ret_rows) == [
            Decimal("0"), Decimal("500.00")]
        assert result["net_pl_transferred"] == "0.00"
        t = Table("gl_entry")
        for aid in (env["revenue"], env["expense"]):
            q = (Q.from_(t).select(t.debit, t.credit)
                 .where(Field("account_id") == P())
                 .where(Field("is_cancelled") == P()))
            acct_rows = conn.execute(q.get_sql(), (aid, 0)).fetchall()
            balance = sum((Decimal(str(r["debit"])) - Decimal(str(r["credit"]))
                           for r in acct_rows), Decimal("0"))
            assert balance == Decimal("0")


class TestExactness:
    def test_large_amount_posts_exact_text(self, conn, db_path):
        env = _env(conn)
        cid = env["company_id"]
        big = "100000000000000.07"
        _post(conn, cid, "JE-BIG", "2026-06-15", [
            {"account_id": env["cash"], "debit": big, "credit": "0"},
            {"account_id": env["revenue"], "debit": "0", "credit": big,
             "cost_center_id": env["cc"]},
        ])
        result = _close(conn, env["fy"], env["retained"])
        assert is_ok(result), result
        assert result["net_pl_transferred"] == big

        fresh = get_conn(db_path)
        try:
            t = Table("gl_entry")
            q = (Q.from_(t)
                 .select(t.account_id, t.debit, t.credit)
                 .where(Field("voucher_type") == P()))
            rows = fresh.execute(q.get_sql(), ("period_closing",)).fetchall()
            assert len(rows) == 2
            by_acct = {r["account_id"]: (r["debit"], r["credit"]) for r in rows}
            assert by_acct[env["revenue"]][0] == big
            assert Decimal(str(by_acct[env["revenue"]][1])) == 0
            assert Decimal(str(by_acct[env["retained"]][0])) == 0
            assert by_acct[env["retained"]][1] == big
            v = Table("period_closing_voucher")
            qv = (Q.from_(v).select(v.net_pl_amount)
                  .where(Field("id") == P()))
            pcv = fresh.execute(qv.get_sql(),
                                (result["pcv_id"],)).fetchone()
            assert pcv["net_pl_amount"] == big
        finally:
            fresh.close()


class TestPostingPath:
    def _closed_book(self, conn):
        env = _env(conn)
        cid = env["company_id"]
        _set_currency(conn, cid, "EUR")
        _post(conn, cid, "JE-INC", "2026-06-15", [
            {"account_id": env["cash"], "debit": "8000.00", "credit": "0"},
            {"account_id": env["revenue"], "debit": "0", "credit": "8000.00",
             "cost_center_id": env["cc"]},
        ])
        _post(conn, cid, "JE-EXP", "2026-06-15", [
            {"account_id": env["expense"], "debit": "3000.00", "credit": "0",
             "cost_center_id": env["cc"]},
            {"account_id": env["cash"], "debit": "0", "credit": "3000.00"},
        ])
        result = _close(conn, env["fy"], env["retained"])
        assert is_ok(result), result
        return env, result

    def test_closing_rows_carry_currency_cost_centre_and_chain(self, conn):
        env, result = self._closed_book(conn)
        t = Table("gl_entry")
        q = (Q.from_(t).select(t.star)
             .where(Field("voucher_type") == P()))
        rows = [dict(r) for r in
                conn.execute(q.get_sql(), ("period_closing",)).fetchall()]
        assert len(rows) == 4
        f = Table("fiscal_year")
        qf = (Q.from_(f).select(f.name).where(Field("id") == P()))
        fy_name = conn.execute(qf.get_sql(), (env["fy"],)).fetchone()["name"]
        assert fy_name == "FY " + env["company_id"][:6]
        pl_ids = {env["revenue"], env["expense"]}
        for r in rows:
            assert r["currency"] == "EUR"
            assert r["exchange_rate"] == "1"
            assert r["gl_checksum"]
            assert r["remarks"] == "Period close " + fy_name
            if r["account_id"] in pl_ids:
                assert r["cost_center_id"] == env["cc"]
            if r["account_id"] == env["retained"]:
                assert r["cost_center_id"] is None
        integrity = call_action(mod.check_gl_integrity, conn,
                                ns(company_id=env["company_id"]))
        assert is_ok(integrity), integrity
        assert integrity["balanced"] is True
        assert Decimal(integrity["total_debit"]) == Decimal(integrity["total_credit"])
        if get_dialect() == "sqlite":
            assert integrity["chain_intact"] is True
            assert integrity["broken_links"] == 0

    def test_freeze_date_refusal_writes_nothing(self, conn):
        env = _env(conn)
        cid = env["company_id"]
        _post(conn, cid, "JE-INC", "2026-06-15", [
            {"account_id": env["cash"], "debit": "8000.00", "credit": "0"},
            {"account_id": env["revenue"], "debit": "0", "credit": "8000.00",
             "cost_center_id": env["cc"]},
        ])
        t = Table("company")
        q = (Q.update(t)
             .set(Field("accounts_frozen_till_date"), P())
             .set(Field("role_allowed_for_frozen_entries"), P())
             .where(Field("id") == P()))
        conn.execute(q.get_sql(), (POSTING_DATE, "System Manager", cid))
        conn.commit()
        before = _snapshot(conn)
        result = _close(conn, env["fy"], env["retained"])
        assert is_error(result), result
        assert result["message"] == (
            "GL Validation Step 10 Failed: Accounts are frozen till "
            + POSTING_DATE + ". Posting date " + POSTING_DATE +
            " is within the frozen period. Role 'System Manager' required")
        assert _snapshot(conn) == before

    def test_missing_default_cost_centre_refusal_writes_nothing(self, conn):
        cid = seed_company(conn)
        cc = seed_cost_center(conn, cid)
        cash = seed_account(conn, cid, "Cash", "asset", "cash", "1000")
        revenue = seed_account(conn, cid, "Sales", "income", "revenue", "4000")
        retained = seed_account(conn, cid, "Retained Earnings", "equity",
                                "equity", "3000")
        fy = seed_fiscal_year(conn, cid, "FY " + cid[:6],
                              "2026-01-01", "2026-12-31")
        _post(conn, cid, "JE-INC", "2026-06-15", [
            {"account_id": cash, "debit": "8000.00", "credit": "0"},
            {"account_id": revenue, "debit": "0", "credit": "8000.00",
             "cost_center_id": cc},
        ])
        before = _snapshot(conn)
        result = _close(conn, fy, retained)
        assert is_error(result), result
        assert result["message"] == NO_CC_MESSAGE
        assert _snapshot(conn) == before


class TestReopen:
    def test_reopen_reverses_every_closing_row(self, conn):
        env = _env(conn)
        cid = env["company_id"]
        _post(conn, cid, "JE-INC", "2026-06-15", [
            {"account_id": env["cash"], "debit": "8000.00", "credit": "0"},
            {"account_id": env["revenue"], "debit": "0", "credit": "8000.00",
             "cost_center_id": env["cc"]},
        ])
        _post(conn, cid, "JE-EXP", "2026-06-15", [
            {"account_id": env["expense"], "debit": "3000.00", "credit": "0",
             "cost_center_id": env["cc"]},
            {"account_id": env["cash"], "debit": "0", "credit": "3000.00"},
        ])
        close = _close(conn, env["fy"], env["retained"])
        assert is_ok(close), close
        pcv_id = close["pcv_id"]

        t = Table("gl_entry")
        q = (Q.from_(t).select(t.star)
             .where(Field("voucher_type") == P())
             .where(Field("voucher_id") == P()))
        before_rows = {r["id"]: dict(r) for r in
                       conn.execute(q.get_sql(),
                                    ("period_closing", pcv_id)).fetchall()}
        assert before_rows
        assert all(r["is_cancelled"] == 0 for r in before_rows.values())
        count_before = _count(conn, "gl_entry")

        result = call_action(mod.reopen_fiscal_year, conn,
                             ns(fiscal_year_id=env["fy"]))
        assert is_ok(result), result
        assert result["pcv_reversed"] is True

        after = {r["id"]: dict(r) for r in
                 conn.execute(q.get_sql(),
                              ("period_closing", pcv_id)).fetchall()}
        assert set(before_rows) <= set(after)
        assert _count(conn, "gl_entry") == count_before + len(before_rows)
        originals = {i: r for i, r in after.items() if i in before_rows}
        mirrors = {i: r for i, r in after.items() if i not in before_rows}
        assert len(mirrors) == len(before_rows)
        for i, row in originals.items():
            old = before_rows[i]
            assert row["is_cancelled"] == 1
            for key, value in old.items():
                if key == "is_cancelled":
                    assert value == 0
                else:
                    assert row[key] == value, key
        for row in mirrors.values():
            assert row["is_cancelled"] == 1
        nets = {}
        for row in after.values():
            delta = Decimal(str(row["debit"])) - Decimal(str(row["credit"]))
            nets[row["account_id"]] = nets.get(row["account_id"], Decimal("0")) + delta
        assert nets and all(v == 0 for v in nets.values())

        f = Table("fiscal_year")
        qf = (Q.from_(f).select(f.is_closed).where(Field("id") == P()))
        fy = conn.execute(qf.get_sql(), (env["fy"],)).fetchone()
        assert fy["is_closed"] == 0
        integrity = call_action(mod.check_gl_integrity, conn,
                                ns(company_id=cid))
        assert is_ok(integrity), integrity
        assert integrity["balanced"] is True
        if get_dialect() == "sqlite":
            assert integrity["chain_intact"] is True

    def test_reopen_reverses_zero_net_closing_rows(self, conn):
        env = _env(conn)
        cid = env["company_id"]
        _post(conn, cid, "JE-INC500", "2026-06-15", [
            {"account_id": env["cash"], "debit": "500.00", "credit": "0"},
            {"account_id": env["revenue"], "debit": "0", "credit": "500.00",
             "cost_center_id": env["cc"]},
        ])
        _post(conn, cid, "JE-EXP500", "2026-06-15", [
            {"account_id": env["expense"], "debit": "500.00", "credit": "0",
             "cost_center_id": env["cc"]},
            {"account_id": env["cash"], "debit": "0", "credit": "500.00"},
        ])
        close = _close(conn, env["fy"], env["retained"])
        assert is_ok(close), close
        pcv_id = close["pcv_id"]

        t = Table("gl_entry")
        q = (Q.from_(t).select(t.star)
             .where(Field("voucher_type") == P())
             .where(Field("voucher_id") == P()))
        before_rows = {r["id"]: dict(r) for r in
                       conn.execute(q.get_sql(),
                                    ("period_closing", pcv_id)).fetchall()}
        assert len(before_rows) == 4
        assert all(r["is_cancelled"] == 0 for r in before_rows.values())
        count_before = _count(conn, "gl_entry")

        result = call_action(mod.reopen_fiscal_year, conn,
                             ns(fiscal_year_id=env["fy"]))
        assert is_ok(result), result
        assert result["pcv_reversed"] is True

        after = {r["id"]: dict(r) for r in
                 conn.execute(q.get_sql(),
                              ("period_closing", pcv_id)).fetchall()}
        assert set(before_rows) <= set(after)
        assert _count(conn, "gl_entry") == count_before + 4
        originals = {i: r for i, r in after.items() if i in before_rows}
        mirrors = {i: r for i, r in after.items() if i not in before_rows}
        assert len(mirrors) == 4
        for i, row in originals.items():
            old = before_rows[i]
            assert row["is_cancelled"] == 1
            for key, value in old.items():
                if key == "is_cancelled":
                    assert value == 0
                else:
                    assert row[key] == value, key
        for row in mirrors.values():
            assert row["is_cancelled"] == 1
        nets = {}
        for row in after.values():
            delta = Decimal(str(row["debit"])) - Decimal(str(row["credit"]))
            nets[row["account_id"]] = nets.get(row["account_id"], Decimal("0")) + delta
        assert nets and all(v == 0 for v in nets.values())

        f = Table("fiscal_year")
        qf = (Q.from_(f).select(f.is_closed).where(Field("id") == P()))
        fy = conn.execute(qf.get_sql(), (env["fy"],)).fetchone()
        assert fy["is_closed"] == 0
        integrity = call_action(mod.check_gl_integrity, conn,
                                ns(company_id=cid))
        assert is_ok(integrity), integrity
        assert integrity["balanced"] is True
        if get_dialect() == "sqlite":
            assert integrity["chain_intact"] is True


class TestIntegrityRefusals:
    def test_add_account_duplicate_number(self, conn):
        cid = seed_company(conn)
        first = call_action(mod.add_account, conn, ns(
            name="Cash A", company_id=cid, root_type="asset",
            account_type=None, account_number="DUP-001", parent_id=None,
            currency="USD", is_group=False))
        assert is_ok(first), first
        n_before = _count(conn, "account")
        result = call_action(mod.add_account, conn, ns(
            name="Cash B", company_id=cid, root_type="asset",
            account_type=None, account_number="DUP-001", parent_id=None,
            currency="USD", is_group=False))
        assert is_error(result), result
        assert result["message"] == (
            "Account creation failed — check for duplicates or invalid data")
        assert _count(conn, "account") == n_before

    def test_add_fiscal_year_duplicate_name(self, conn):
        cid = seed_company(conn)
        first = call_action(mod.add_fiscal_year, conn, ns(
            name="FY DUP " + cid[:6], start_date="2026-01-01",
            end_date="2026-12-31", company_id=cid))
        assert is_ok(first), first
        n_before = _count(conn, "fiscal_year")
        result = call_action(mod.add_fiscal_year, conn, ns(
            name=first["name"], start_date="2027-01-01",
            end_date="2027-12-31", company_id=cid))
        assert is_error(result), result
        assert result["message"] == (
            "Fiscal year creation failed — check for duplicates or invalid data")
        assert _count(conn, "fiscal_year") == n_before

    def test_add_dimension_duplicate_key(self, conn):
        key = "x_dup_dim"
        first = call_action(mod.add_dimension, conn, ns(
            key=key, label="Dup Dim", dimension_type="text",
            refers_to=None, allowed_values=None,
            required_on_account_types=None))
        assert is_ok(first), first
        n_before = _count(conn, "dimension_registry")
        result = call_action(mod.add_dimension, conn, ns(
            key=key, label="Dup Dim Again", dimension_type="text",
            refers_to=None, allowed_values=None,
            required_on_account_types=None))
        assert is_error(result), result
        assert result["message"] == f"Dimension '{key}' already exists"
        assert _count(conn, "dimension_registry") == n_before

    def test_add_cost_center_unknown_company(self, conn):
        import uuid as _uuid
        n_before = _count(conn, "cost_center")
        result = call_action(mod.add_cost_center, conn, ns(
            name="Orphan CC", company_id=str(_uuid.uuid4()),
            parent_id=None, is_group=False))
        assert is_error(result), result
        assert result["message"] == (
            "Cost center creation failed — check for duplicates or invalid data")
        assert _count(conn, "cost_center") == n_before

    def test_add_budget_unknown_account(self, conn):
        import uuid as _uuid
        cid = seed_company(conn)
        fy = seed_fiscal_year(conn, cid, "FY B " + cid[:6],
                              "2026-01-01", "2026-12-31")
        n_before = _count(conn, "budget")
        result = call_action(mod.add_budget, conn, ns(
            fiscal_year_id=fy, budget_amount="1000.00",
            account_id=str(_uuid.uuid4()), cost_center_id=None,
            action_if_exceeded="warn"))
        assert is_error(result), result
        assert result["message"] == (
            "Budget creation failed — check for duplicates or invalid data")
        assert _count(conn, "budget") == n_before
