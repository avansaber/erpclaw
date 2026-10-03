"""Strong-depth tests for seven erpclaw-gl actions (task m325d).

Each action below already has a behavioural test that asserts on content
alone (no money literal, no read-back through the seam, no pinned
refusal). Those tests are NOT touched. This file deepens each one; the
assertion that now carries the weight is named in every test.

Weak-test map (existing test -> what this file adds beside it):

- check-gl-integrity: test_gl_entries.py::TestCheckGLIntegrity ->
  exact total_debit/total_credit/difference strings cross-checked against
  an independent seam summation, a planted real imbalance the check must
  catch with the exact difference, and a pinned refusal.
- list-accounts: test_accounts.py::TestListAccounts ->
  field-for-field match between the action output and seam ground truth,
  cross-company decoy exclusion, read-only snapshot proof, pinned refusal.
- list-budgets: test_cost_center_budget.py::TestListBudgets ->
  hand-computed budget_amount/actual_amount/variance literals, seam
  read-back of the stored budget row, decoy exclusion, pinned refusal.
- list-cost-centers: test_cost_center_budget.py::TestListCostCenters ->
  exact name set, seam read-back, decoy exclusion, pinned refusal.
- list-fiscal-years: test_fiscal_year.py::TestListFiscalYears ->
  exact rows in descending-date order, seam read-back, decoy exclusion,
  pinned refusal.
- list-gl-entries: test_gl_entries.py::TestListGLEntries ->
  per-leg exact debit/credit literals cross-checked against seam legs,
  other-voucher exclusion, read-only snapshot proof, pinned refusal.
- next-series: test_cost_center_budget.py::TestNextSeries ->
  exact series strings, seam read-back of current_value, proof that other
  series and other companies did not move, pinned refusal.

Discipline: every read or write in this file goes through
``erpclaw_lib.query`` (PyPika) on a connection from
``erpclaw_lib.db.get_connection``; catalog questions go through
``erpclaw_lib.seam``. Money is compared as exact strings, never float.
Owner actions perform every legitimate write (post-gl-entries,
add-budget, seed-naming-series, next-series); the only direct insert is
the deliberately corrupt imbalance leg, which simulates damage no owner
action would ever write.
"""
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from gl_helpers import call_action, is_error, is_ok, ns, load_db_query  # noqa: E402

from erpclaw_lib import seam  # noqa: E402
from erpclaw_lib.db import get_connection  # noqa: E402
from erpclaw_lib.query import Field, P, Q, Table, fn, insert_row  # noqa: E402

GL = load_db_query()

import pytest  # noqa: E402


@pytest.fixture
def conn(db_path):
    connection = get_connection(db_path)
    yield connection
    connection.close()


def _u():
    return str(uuid.uuid4())


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


READ_TABLES = ("company", "account", "fiscal_year", "cost_center",
               "budget", "gl_entry", "naming_series")


def _company(conn, name, abbr):
    cid = _u()
    _insert(conn, "company", {"id": cid, "name": name, "abbr": abbr})
    conn.commit()
    return cid


def _account(conn, company_id, name, number, root_type, account_type):
    aid = _u()
    direction = ("debit_normal" if root_type in ("asset", "expense")
                 else "credit_normal")
    _insert(conn, "account", {
        "id": aid, "name": name, "account_number": number,
        "root_type": root_type, "account_type": account_type,
        "balance_direction": direction, "company_id": company_id,
        "depth": 0})
    conn.commit()
    return aid


def _fy(conn, company_id, name, start, end):
    fid = _u()
    _insert(conn, "fiscal_year", {
        "id": fid, "name": name, "start_date": start, "end_date": end,
        "is_closed": 0, "company_id": company_id})
    conn.commit()
    return fid


def _cc(conn, company_id, name):
    ccid = _u()
    _insert(conn, "cost_center", {
        "id": ccid, "name": name, "company_id": company_id,
        "is_group": 0})
    conn.commit()
    return ccid


def _post(conn, company_id, voucher_id, posting_date, legs):
    result = call_action(GL.post_gl_entries, conn, ns(
        voucher_type="journal_entry", voucher_id=voucher_id,
        posting_date=posting_date, company_id=company_id,
        entries=json.dumps(legs)))
    assert is_ok(result), result
    return result


def _refusal_text(result):
    return result.get("error") or result.get("message") or ""


# ──────────────────────────────────────────────────────────────────────────────
# list-accounts
# ──────────────────────────────────────────────────────────────────────────────

class TestStrongListAccounts:
    def test_exact_rows_match_seam_and_decoy_excluded(self, conn):
        """Carries the weight over TestListAccounts::test_list_with_company,
        which only asserts total_count >= 2."""
        assert seam.table_exists("account")
        cid_a = _company(conn, "Strong Alpha Ltd", "STAL")
        cid_b = _company(conn, "Strong Beta Ltd", "STBL")
        cash_a = _account(conn, cid_a, "Strong Cash", "6001",
                          "asset", "cash")
        rev_a = _account(conn, cid_a, "Strong Revenue", "6002",
                         "income", "revenue")
        decoy = _account(conn, cid_b, "Decoy Cash", "6001",
                         "asset", "cash")

        before = _snapshot(conn, READ_TABLES)
        result = call_action(GL.list_accounts, conn, ns(
            company_id=cid_a, company_name=None, root_type=None,
            account_type=None, parent_id=None, is_group=False,
            include_frozen=False, search=None, limit=None, offset=None))
        assert is_ok(result), result
        assert result["total_count"] == 2

        by_id = {a["id"]: a for a in result["accounts"]}
        assert set(by_id) == {cash_a, rev_a}
        assert decoy not in by_id

        expected = {
            cash_a: ("Strong Cash", "6001", "asset", "cash",
                     "debit_normal"),
            rev_a: ("Strong Revenue", "6002", "income", "revenue",
                    "credit_normal"),
        }
        for aid, (name, number, root, atype, direction) in expected.items():
            got = by_id[aid]
            assert got["name"] == name
            assert got["account_number"] == number
            assert got["root_type"] == root
            assert got["account_type"] == atype
            assert got["balance_direction"] == direction
            assert got["company_id"] == cid_a
            assert got["is_frozen"] == 0
            assert got["disabled"] == 0
            row = _row(conn, "account", aid)
            for field in ("name", "account_number", "root_type",
                          "account_type", "balance_direction",
                          "company_id"):
                assert got[field] == row[field], field

        assert _snapshot(conn, READ_TABLES) == before

    def test_refusal_unknown_company_leaves_db_unchanged(self, conn):
        """Pinned refusal: naming a company that does not exist must fail
        loudly (wrong-entity guard) and write nothing."""
        cid = _company(conn, "Refusal Alpha Ltd", "RFAL")
        _account(conn, cid, "Refusal Cash", "6101", "asset", "cash")
        before = _snapshot(conn, READ_TABLES)
        result = call_action(GL.list_accounts, conn, ns(
            company_id=None, company_name="No Such Company XYZ",
            root_type=None, account_type=None, parent_id=None,
            is_group=False, include_frozen=False, search=None,
            limit=None, offset=None))
        assert result.get("status") != "ok", result
        text = _refusal_text(result)
        assert "No Such Company XYZ" in text
        assert "not found" in text.lower()
        assert _snapshot(conn, READ_TABLES) == before


# ──────────────────────────────────────────────────────────────────────────────
# list-budgets
# ──────────────────────────────────────────────────────────────────────────────

class TestStrongListBudgets:
    def test_variance_money_literals_and_seam_readback(self, conn):
        """Carries the weight over TestListBudgets::test_list_with_variance,
        which only asserts total_count >= 1 with no amount checks."""
        assert seam.table_exists("budget")
        cid_a = _company(conn, "Budget Alpha Ltd", "BGAL")
        fy_a = _fy(conn, cid_a, "FY-SB-2026-A", "2026-01-01",
                   "2026-12-31")
        cc_a = _cc(conn, cid_a, "Main CC")
        cash_a = _account(conn, cid_a, "Budget Cash", "1000",
                          "asset", "cash")
        travel_a = _account(conn, cid_a, "Budget Travel", "5210",
                            "expense", "expense")
        created = call_action(GL.add_budget, conn, ns(
            fiscal_year_id=fy_a, account_id=travel_a,
            cost_center_id=None, budget_amount="25000.00",
            action_if_exceeded=None))
        assert is_ok(created), created

        cid_b = _company(conn, "Budget Beta Ltd", "BGBL")
        fy_b = _fy(conn, cid_b, "FY-SB-2026-B", "2026-01-01",
                   "2026-12-31")
        travel_b = _account(conn, cid_b, "Decoy Travel", "5210",
                            "expense", "expense")
        decoy = call_action(GL.add_budget, conn, ns(
            fiscal_year_id=fy_b, account_id=travel_b,
            cost_center_id=None, budget_amount="99999.99",
            action_if_exceeded=None))
        assert is_ok(decoy), decoy

        _post(conn, cid_a, "JE-SB-1", "2026-06-15", [
            {"account_id": travel_a, "debit": "4000.00",
             "credit": "0", "cost_center_id": cc_a},
            {"account_id": cash_a, "debit": "0",
             "credit": "4000.00"},
        ])

        before = _snapshot(conn, READ_TABLES)
        result = call_action(GL.list_budgets, conn, ns(
            fiscal_year_id=fy_a, company_id=cid_a,
            company_name=None, limit=None, offset=None))
        assert is_ok(result), result
        assert result["total_count"] == 1
        got = result["budgets"][0]
        assert got["id"] == created["budget_id"]
        assert got["budget_amount"] == "25000.00"
        assert got["actual_amount"] == "4000.00"
        assert got["variance"] == "21000.00"
        assert Decimal(got["variance"]) == Decimal("21000.00")

        row = _row(conn, "budget", created["budget_id"])
        assert row["budget_amount"] == "25000.00"
        assert row["account_id"] == travel_a
        assert row["fiscal_year_id"] == fy_a
        assert row["company_id"] == cid_a
        assert row["action_if_exceeded"] == "warn"
        assert all(b["id"] != decoy["budget_id"]
                   for b in result["budgets"])
        assert _snapshot(conn, READ_TABLES) == before

    def test_refusal_unknown_fiscal_year_leaves_db_unchanged(self, conn):
        """Pinned refusal: an unknown fiscal year id must be refused with
        a truthful message and leave every table untouched."""
        cid = _company(conn, "Budget Refusal Ltd", "BGRL")
        fy = _fy(conn, cid, "FY-SB-2026-R", "2026-01-01",
                 "2026-12-31")
        travel = _account(conn, cid, "Refusal Travel", "5210",
                          "expense", "expense")
        created = call_action(GL.add_budget, conn, ns(
            fiscal_year_id=fy, account_id=travel,
            cost_center_id=None, budget_amount="1000.00",
            action_if_exceeded=None))
        assert is_ok(created), created
        before = _snapshot(conn, READ_TABLES)
        bogus = "fy-does-not-exist-0000"
        result = call_action(GL.list_budgets, conn, ns(
            fiscal_year_id=bogus, company_id=cid,
            company_name=None, limit=None, offset=None))
        assert is_error(result), result
        assert result["message"] == "Fiscal year %s not found" % bogus
        assert _snapshot(conn, READ_TABLES) == before


# ──────────────────────────────────────────────────────────────────────────────
# list-cost-centers
# ──────────────────────────────────────────────────────────────────────────────

class TestStrongListCostCenters:
    def test_exact_names_and_decoy_excluded(self, conn):
        """Carries the weight over TestListCostCenters::test_list, which
        only asserts total_count >= 2."""
        assert seam.table_exists("cost_center")
        cid_a = _company(conn, "CC Alpha Ltd", "CCAL")
        cid_b = _company(conn, "CC Beta Ltd", "CCBL")
        alpha = _cc(conn, cid_a, "CC-Alpha")
        beta = _cc(conn, cid_a, "CC-Beta")
        decoy = _cc(conn, cid_b, "CC-Decoy")

        before = _snapshot(conn, READ_TABLES)
        result = call_action(GL.list_cost_centers, conn, ns(
            company_id=cid_a, company_name=None, parent_id=None,
            is_group=None, limit=None, offset=None))
        assert is_ok(result), result
        assert result["total_count"] == 2
        assert [c["name"] for c in result["cost_centers"]] == [
            "CC-Alpha", "CC-Beta"]
        by_id = {c["id"]: c for c in result["cost_centers"]}
        assert set(by_id) == {alpha, beta}
        assert decoy not in by_id
        for ccid, name in ((alpha, "CC-Alpha"), (beta, "CC-Beta")):
            assert by_id[ccid]["company_id"] == cid_a
            row = _row(conn, "cost_center", ccid)
            assert row["name"] == name
            assert by_id[ccid]["name"] == row["name"]
            assert row["company_id"] == cid_a
        assert _snapshot(conn, READ_TABLES) == before

    def test_refusal_unknown_company_leaves_db_unchanged(self, conn):
        """Pinned refusal: unknown company name fails loudly, writes
        nothing."""
        cid = _company(conn, "CC Refusal Ltd", "CCRL")
        _cc(conn, cid, "CC-Only")
        before = _snapshot(conn, READ_TABLES)
        result = call_action(GL.list_cost_centers, conn, ns(
            company_id=None, company_name="No Such Company XYZ",
            parent_id=None, is_group=None, limit=None, offset=None))
        assert result.get("status") != "ok", result
        text = _refusal_text(result)
        assert "No Such Company XYZ" in text
        assert "not found" in text.lower()
        assert _snapshot(conn, READ_TABLES) == before


# ──────────────────────────────────────────────────────────────────────────────
# list-fiscal-years
# ──────────────────────────────────────────────────────────────────────────────

class TestStrongListFiscalYears:
    def test_exact_rows_in_order_and_decoy_excluded(self, conn):
        """Carries the weight over
        TestListFiscalYears::test_list_by_company, which only asserts
        total_count >= 2."""
        assert seam.table_exists("fiscal_year")
        cid_a = _company(conn, "FY Alpha Ltd", "FYAL")
        cid_b = _company(conn, "FY Beta Ltd", "FYBL")
        fy25 = _fy(conn, cid_a, "FY-S-2025-A", "2025-01-01",
                   "2025-12-31")
        fy26 = _fy(conn, cid_a, "FY-S-2026-A", "2026-01-01",
                   "2026-12-31")
        decoy = _fy(conn, cid_b, "FY-S-2026-B", "2026-01-01",
                    "2026-12-31")

        before = _snapshot(conn, READ_TABLES)
        result = call_action(GL.list_fiscal_years, conn, ns(
            company_id=cid_a, company_name=None, limit=None,
            offset=None))
        assert is_ok(result), result
        assert result["total_count"] == 2
        names = [f["name"] for f in result["fiscal_years"]]
        assert names == ["FY-S-2026-A", "FY-S-2025-A"]
        by_id = {f["id"]: f for f in result["fiscal_years"]}
        assert set(by_id) == {fy26, fy25}
        assert decoy not in by_id
        assert by_id[fy26]["start_date"] == "2026-01-01"
        assert by_id[fy26]["end_date"] == "2026-12-31"
        assert by_id[fy25]["start_date"] == "2025-01-01"
        assert by_id[fy25]["end_date"] == "2025-12-31"
        for fid in (fy26, fy25):
            row = _row(conn, "fiscal_year", fid)
            assert by_id[fid]["name"] == row["name"]
            assert by_id[fid]["start_date"] == row["start_date"]
            assert by_id[fid]["end_date"] == row["end_date"]
            assert row["company_id"] == cid_a
            assert row["is_closed"] == 0
        assert _snapshot(conn, READ_TABLES) == before

    def test_refusal_unknown_company_leaves_db_unchanged(self, conn):
        """Pinned refusal: unknown company name fails loudly, writes
        nothing."""
        cid = _company(conn, "FY Refusal Ltd", "FYRL")
        _fy(conn, cid, "FY-S-2026-R", "2026-01-01", "2026-12-31")
        before = _snapshot(conn, READ_TABLES)
        result = call_action(GL.list_fiscal_years, conn, ns(
            company_id=None, company_name="No Such Company XYZ",
            limit=None, offset=None))
        assert result.get("status") != "ok", result
        text = _refusal_text(result)
        assert "No Such Company XYZ" in text
        assert "not found" in text.lower()
        assert _snapshot(conn, READ_TABLES) == before


# ──────────────────────────────────────────────────────────────────────────────
# list-gl-entries
# ──────────────────────────────────────────────────────────────────────────────

class TestStrongListGLEntries:
    def test_exact_legs_match_seam_and_other_voucher_excluded(self, conn):
        """Carries the weight over TestListGLEntries::test_list_by_voucher,
        which only asserts total_count == 2."""
        assert seam.table_exists("gl_entry")
        cid = _company(conn, "GL Alpha Ltd", "GLAL")
        _fy(conn, cid, "FY-SG-2026", "2026-01-01", "2026-12-31")
        cc = _cc(conn, cid, "Main CC")
        cash = _account(conn, cid, "Strong Cash", "1000", "asset",
                        "cash")
        revenue = _account(conn, cid, "Strong Revenue", "4000",
                           "income", "revenue")
        _post(conn, cid, "JE-STRONG-1", "2026-06-15", [
            {"account_id": cash, "debit": "1000.00", "credit": "0"},
            {"account_id": revenue, "debit": "0",
             "credit": "1000.00", "cost_center_id": cc},
        ])
        _post(conn, cid, "JE-OTHER-1", "2026-06-16", [
            {"account_id": cash, "debit": "250.00", "credit": "0"},
            {"account_id": revenue, "debit": "0",
             "credit": "250.00", "cost_center_id": cc},
        ])

        before = _snapshot(conn, READ_TABLES)
        result = call_action(GL.list_gl_entries, conn, ns(
            company_id=cid, company_name=None, account_id=None,
            voucher_type="journal_entry", voucher_id="JE-STRONG-1",
            party_type=None, party_id=None, from_date=None,
            to_date=None, is_cancelled=None, limit=None, offset=None))
        assert is_ok(result), result
        assert result["total_count"] == 2
        legs = {e["account_id"]: e for e in result["entries"]}
        assert set(legs) == {cash, revenue}
        assert legs[cash]["debit"] == "1000.00"
        assert legs[cash]["credit"] == "0.00"
        assert legs[cash]["account_name"] == "Strong Cash"
        assert legs[revenue]["debit"] == "0.00"
        assert legs[revenue]["credit"] == "1000.00"
        assert legs[revenue]["account_name"] == "Strong Revenue"
        for leg in result["entries"]:
            assert leg["posting_date"] == "2026-06-15"
            assert leg["voucher_id"] == "JE-STRONG-1"
            assert leg["voucher_type"] == "journal_entry"
            assert leg["is_cancelled"] == 0

        stored = _gl_legs(conn, "journal_entry", "JE-STRONG-1")
        assert len(stored) == 2
        stored_by_acct = {r["account_id"]: r for r in stored}
        for aid in (cash, revenue):
            assert legs[aid]["debit"] == stored_by_acct[aid]["debit"]
            assert legs[aid]["credit"] == stored_by_acct[aid]["credit"]
        assert stored_by_acct[cash]["debit"] == "1000.00"
        assert stored_by_acct[revenue]["credit"] == "1000.00"
        assert all(e["voucher_id"] != "JE-OTHER-1"
                   for e in result["entries"])
        assert _snapshot(conn, READ_TABLES) == before

    def test_refusal_unknown_company_leaves_db_unchanged(self, conn):
        """Pinned refusal: unknown company name fails loudly, writes
        nothing."""
        cid = _company(conn, "GL Refusal Ltd", "GLRL")
        _fy(conn, cid, "FY-SG-2026-R", "2026-01-01", "2026-12-31")
        cc = _cc(conn, cid, "Main CC")
        cash = _account(conn, cid, "Refusal Cash", "1000", "asset",
                        "cash")
        revenue = _account(conn, cid, "Refusal Revenue", "4000",
                           "income", "revenue")
        _post(conn, cid, "JE-REF-1", "2026-06-15", [
            {"account_id": cash, "debit": "100.00", "credit": "0"},
            {"account_id": revenue, "debit": "0",
             "credit": "100.00", "cost_center_id": cc},
        ])
        before = _snapshot(conn, READ_TABLES)
        result = call_action(GL.list_gl_entries, conn, ns(
            company_id=None, company_name="No Such Company XYZ",
            account_id=None, voucher_type=None, voucher_id=None,
            party_type=None, party_id=None, from_date=None,
            to_date=None, is_cancelled=None, limit=None, offset=None))
        assert result.get("status") != "ok", result
        text = _refusal_text(result)
        assert "No Such Company XYZ" in text
        assert "not found" in text.lower()
        assert _snapshot(conn, READ_TABLES) == before


# ──────────────────────────────────────────────────────────────────────────────
# next-series
# ──────────────────────────────────────────────────────────────────────────────

class TestStrongNextSeries:
    def test_exact_sequence_and_counter_readback(self, conn):
        """Carries the weight over
        TestNextSeries::test_next_series_increments, which only asserts
        the two series differ."""
        assert seam.table_exists("naming_series")
        cid_a = _company(conn, "Series Alpha Ltd", "SRAL")
        cid_b = _company(conn, "Series Beta Ltd", "SRBL")
        seeded = call_action(GL.seed_naming_series, conn, ns(
            company_id=cid_a))
        assert is_ok(seeded), seeded
        year = datetime.now(timezone.utc).year

        first = call_action(GL.next_series, conn, ns(
            entity_type="journal_entry", company_id=cid_a))
        assert is_ok(first), first
        assert first["series"] == "JE-%d-00001" % year
        second = call_action(GL.next_series, conn, ns(
            entity_type="journal_entry", company_id=cid_a))
        assert is_ok(second), second
        assert second["series"] == "JE-%d-00002" % year

        rows = _where(conn, "naming_series",
                      entity_type="journal_entry", company_id=cid_a)
        assert len(rows) == 1
        assert rows[0]["prefix"] == "JE-%d-" % year
        assert rows[0]["current_value"] == 2

        others = _where(conn, "naming_series",
                        entity_type="sales_invoice", company_id=cid_a)
        assert len(others) == 1
        assert others[0]["current_value"] == 0
        assert _where(conn, "naming_series",
                      company_id=cid_b) == []

    def test_refusal_unknown_entity_leaves_db_unchanged(self, conn):
        """Pinned refusal: an unregistered entity type must be refused
        with a truthful message and move no counter."""
        cid = _company(conn, "Series Refusal Ltd", "SRRL")
        seeded = call_action(GL.seed_naming_series, conn, ns(
            company_id=cid))
        assert is_ok(seeded), seeded
        before = _snapshot(conn, READ_TABLES)
        result = call_action(GL.next_series, conn, ns(
            entity_type="not_a_real_entity", company_id=cid))
        assert is_error(result), result
        assert ("Unknown entity type 'not_a_real_entity'"
                in result["message"])
        assert _snapshot(conn, READ_TABLES) == before


# ──────────────────────────────────────────────────────────────────────────────
# check-gl-integrity
# ──────────────────────────────────────────────────────────────────────────────

class TestStrongCheckGLIntegrity:
    def _book(self, conn, company_name, abbr, fy_name):
        cid = _company(conn, company_name, abbr)
        _fy(conn, cid, fy_name, "2026-01-01", "2026-12-31")
        cc = _cc(conn, cid, "Main CC")
        cash = _account(conn, cid, "Integrity Cash", "1000",
                        "asset", "cash")
        revenue = _account(conn, cid, "Integrity Revenue", "4000",
                           "income", "revenue")
        return {"company_id": cid, "cc": cc, "cash": cash,
                "revenue": revenue}

    def test_balanced_totals_are_exact_strings(self, conn):
        """Carries the weight over
        TestCheckGLIntegrity::test_integrity_after_balanced_posts, which
        only asserts debit equals credit with no literal and no seam
        cross-check."""
        assert seam.table_exists("gl_entry")
        env = self._book(conn, "Integrity Alpha Ltd", "IGAL",
                         "FY-SI-2026-A")
        for vid in ("JE-INT-A", "JE-INT-B"):
            _post(conn, env["company_id"], vid, "2026-06-15", [
                {"account_id": env["cash"], "debit": "1000.00",
                 "credit": "0"},
                {"account_id": env["revenue"], "debit": "0",
                 "credit": "1000.00",
                 "cost_center_id": env["cc"]},
            ])
        before = _snapshot(conn, READ_TABLES)
        result = call_action(GL.check_gl_integrity, conn, ns(
            company_id=env["company_id"], company_name=None))
        assert is_ok(result), result
        assert result["balanced"] is True
        assert result["total_debit"] == "2000.00"
        assert result["total_credit"] == "2000.00"
        assert result["difference"] == "0.00"
        assert result["total_entries"] == 4
        assert result["chain_intact"] is True
        assert result["broken_links"] == 0

        rows = _where(conn, "gl_entry")
        mine = [r for r in rows if r["voucher_id"] in
                ("JE-INT-A", "JE-INT-B")]
        assert len(mine) == 4
        debit = sum((Decimal(r["debit"]) for r in mine), Decimal("0"))
        credit = sum((Decimal(r["credit"]) for r in mine), Decimal("0"))
        assert str(debit) == "2000.00"
        assert str(credit) == "2000.00"
        assert _snapshot(conn, READ_TABLES) == before

    def test_planted_imbalance_is_caught_with_exact_difference(self, conn):
        """Plants one real one-sided leg no owner action would ever write
        and proves the check reports it with the exact difference."""
        env = self._book(conn, "Integrity Beta Ltd", "IGBL",
                         "FY-SI-2026-B")
        _post(conn, env["company_id"], "JE-INT-C", "2026-06-15", [
            {"account_id": env["cash"], "debit": "1000.00",
             "credit": "0"},
            {"account_id": env["revenue"], "debit": "0",
             "credit": "1000.00", "cost_center_id": env["cc"]},
        ])
        rogue_id = _u()
        _insert(conn, "gl_entry", {
            "id": rogue_id, "posting_date": "2026-07-01",
            "account_id": env["cash"], "debit": "500.00",
            "credit": "0", "voucher_type": "journal_entry",
            "voucher_id": "JE-ROGUE-1"})
        conn.commit()

        result = call_action(GL.check_gl_integrity, conn, ns(
            company_id=env["company_id"], company_name=None))
        assert is_ok(result), result
        assert result["balanced"] is False
        assert result["total_debit"] == "1500.00"
        assert result["total_credit"] == "1000.00"
        assert result["difference"] == "500.00"
        assert result["total_entries"] == 3
        assert result["chain_intact"] is True

        rogue = _row(conn, "gl_entry", rogue_id)
        assert rogue["debit"] == "500.00"
        assert rogue["credit"] == "0"
        assert rogue["voucher_id"] == "JE-ROGUE-1"

    def test_refusal_unknown_company_leaves_db_unchanged(self, conn):
        """Pinned refusal: unknown company name fails loudly, writes
        nothing."""
        env = self._book(conn, "Integrity Refusal Ltd", "IGRL",
                         "FY-SI-2026-R")
        _post(conn, env["company_id"], "JE-INT-R", "2026-06-15", [
            {"account_id": env["cash"], "debit": "100.00",
             "credit": "0"},
            {"account_id": env["revenue"], "debit": "0",
             "credit": "100.00", "cost_center_id": env["cc"]},
        ])
        before = _snapshot(conn, READ_TABLES)
        result = call_action(GL.check_gl_integrity, conn, ns(
            company_id=None, company_name="No Such Company XYZ"))
        assert result.get("status") != "ok", result
        text = _refusal_text(result)
        assert "No Such Company XYZ" in text
        assert "not found" in text.lower()
        assert _snapshot(conn, READ_TABLES) == before
