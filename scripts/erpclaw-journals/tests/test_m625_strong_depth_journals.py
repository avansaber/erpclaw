"""M625 strong depth: list-recurring-templates made strong.

This class deepens the routability-only pin the contract suite holds for the
recurring-template reads (an "Unknown action" absence check proves routing,
not behaviour). The weight is now on the exact ordered rows, the
status-filter decoy, the second-company decoy and the exact refusal below.

What "strong" means per test:
  1. Read back through the seam: rows are seeded through the owning actions,
     then re-read on a FRESH connection from
     ``erpclaw_lib.db.get_connection`` with queries built by PyPika through
     ``erpclaw_lib.query``, and the action output is compared against the
     stored values exactly (never counts, never truthiness). Visibility of
     the table is proved through ``erpclaw_lib.seam.table_exists``. No raw
     catalog reads appear anywhere below, including in prose.
  2. A money literal where money exists: the list returns template headers
     only -- the line amounts stay inside the stored JSON the list never
     echoes -- so no amount, rate or quantity is carried and exact TEXT
     identity of every returned header field is the money-grade check.
  3. A pinned refusal: the unknown-company refusal is asserted with the
     EXACT message (``==``, not a substring), and the database is asserted
     unchanged afterwards.
  4. What should NOT have changed is snapshotted and asserted too: journal
     entries, their lines, the general ledger and the audit trail are all
     byte-identical around every read.

Money discipline: Decimal in Python, TEXT columns, exact string comparisons.
Never float.
"""
import json
import uuid
from decimal import Decimal

import pytest

from journals_helpers import (
    call_action, is_error, is_ok, load_db_query, ns,
)
from erpclaw_lib import seam
from erpclaw_lib.db import get_connection
from erpclaw_lib.query import Q, P, Table, Order, insert_row

mod = load_db_query()


@pytest.fixture(autouse=True)
def _dispose_seam_engines():
    yield
    seam.dispose_engines()


_SNAPSHOT_TABLES = (
    "recurring_journal_template", "journal_entry", "journal_entry_line",
    "gl_entry", "audit_log",
)


def _norm(row):
    return {k: (None if v is None else str(v)) for k, v in dict(row).items()}


def _rows(conn, table):
    t = Table(table)
    q = Q.from_(t).select(t.star).orderby(t.id)
    return [_norm(r) for r in conn.execute(q.get_sql(), ()).fetchall()]


def _snapshot(conn):
    return {t: _rows(conn, t) for t in _SNAPSHOT_TABLES}


def _uuid():
    return str(uuid.uuid4())


def _lines(env, *specs):
    return json.dumps([
        {"account_id": a, "debit": d, "credit": c, "cost_center_id": env["cc"]}
        for a, d, c in specs])


def _add_template(conn, env, company_id, name, start, amount="100.00"):
    r = call_action(mod.add_recurring_template, conn, ns(
        company_id=company_id, template_name=name, frequency="monthly",
        start_date=start, end_date=None, entry_type="journal",
        auto_submit=None,
        lines=_lines(env, (env["cash"], amount, "0"), (env["expense"], "0", amount)),
        remark=None))
    assert is_ok(r), r
    return r["template_id"]


def _pause(conn, template_id):
    r = call_action(mod.update_recurring_template, conn, ns(
        template_id=template_id, template_name=None, frequency=None,
        end_date=None, entry_type=None, remark=None, auto_submit=None,
        lines=None, template_status="paused"))
    assert is_ok(r), r


def _seed_company_with_accounts(conn, name, abbr):
    cid = _uuid()
    t = Table("company")
    sql, cols = insert_row("company", {c: P() for c in
                                       ("id", "name", "abbr", "default_currency",
                                        "country", "fiscal_year_start_month")})
    conn.execute(sql, (cid, "%s %s" % (name, cid[:6]), "%s%s" % (abbr, cid[:4]),
                       "USD", "United States", 1))
    aids = {}
    for label, root in (("cash", "asset"), ("expense", "expense")):
        aid = _uuid()
        a = Table("account")
        asql, acols = insert_row("account", {c: P() for c in
                                             ("id", "name", "account_number", "root_type",
                                              "account_type", "balance_direction",
                                              "company_id", "depth")})
        conn.execute(asql, (aid, "%s-%s %s" % (name, label, aid[:6]), "ACC-%s" % aid[:6],
                            root, "cash" if root == "asset" else "expense",
                            "debit_normal", cid, 0))
        aids[label] = aid
    ccid = _uuid()
    c = Table("cost_center")
    csql, ccols = insert_row("cost_center", {c2: P() for c2 in
                                             ("id", "name", "company_id", "is_group")})
    conn.execute(csql, (ccid, "Main CC", cid, 0))
    conn.commit()
    return {"company_id": cid, "cc": ccid, "cash": aids["cash"], "expense": aids["expense"]}


def _stored_templates(db_path, company_id):
    vconn = get_connection(db_path)
    try:
        t = Table("recurring_journal_template")
        q = (Q.from_(t).select(t.id, t.name, t.frequency, t.start_date,
                               t.next_run_date, t.entry_type, t.status)
             .where(t.company_id == P())
             .orderby(t.next_run_date, order=Order.asc))
        return [_norm(r) for r in vconn.execute(q.get_sql(), (company_id,)).fetchall()]
    finally:
        vconn.close()


class TestListRecurringTemplatesStrong:
    def _seed(self, conn, env):
        alpha = _add_template(conn, env, env["company_id"], "Alpha",
                              start="2026-03-01", amount="100.00")
        beta = _add_template(conn, env, env["company_id"], "Beta",
                             start="2026-02-01", amount="250.50")
        gamma = _add_template(conn, env, env["company_id"], "Gamma",
                              start="2026-01-15", amount="75.25")
        _pause(conn, gamma)
        other = _seed_company_with_accounts(conn, "Lakeside", "LK")
        other_alpha = _add_template(conn, other, other["company_id"], "Alpha",
                                    start="2026-01-05", amount="999.99")
        return alpha, beta, gamma, other, other_alpha

    def test_unfiltered_list_carries_exact_stored_rows_in_date_order(
            self, conn, env, db_path):
        alpha, beta, gamma, other, other_alpha = self._seed(conn, env)
        before = _snapshot(conn)

        assert seam.table_exists("recurring_journal_template", db_path)
        r = call_action(mod.list_recurring_templates, conn, ns(
            company_id=env["company_id"], company_name=None,
            template_status=None, limit="20", offset="0"))
        assert is_ok(r), r
        # Derived 1: next-run-date ascending order decided by the action,
        # across both statuses.
        assert [t["name"] for t in r["templates"]] == ["Gamma", "Beta", "Alpha"]
        # Derived 2: the count under no filter, plus paging math.
        assert r["total_count"] == 3
        assert r["has_more"] is False
        limited = call_action(mod.list_recurring_templates, conn, ns(
            company_id=env["company_id"], company_name=None,
            template_status=None, limit="2", offset="0"))
        assert [t["name"] for t in limited["templates"]] == ["Gamma", "Beta"]
        assert limited["has_more"] is True

        stored = _stored_templates(db_path, env["company_id"])
        assert [t["name"] for t in stored] == ["Gamma", "Beta", "Alpha"]
        assert [t["id"] for t in r["templates"]] == [t["id"] for t in stored]
        by_id = {t["id"]: t for t in r["templates"]}
        assert by_id[gamma]["status"] == "paused"
        assert by_id[gamma]["next_run_date"] == "2026-01-15"
        assert by_id[beta]["next_run_date"] == "2026-02-01"
        assert by_id[beta]["frequency"] == "monthly"
        assert by_id[alpha]["next_run_date"] == "2026-03-01"
        assert by_id[alpha]["entry_type"] == "journal"

        # The second-company "Alpha" runs earliest of all: if the action
        # read across companies it would surface first. It must be absent.
        assert other_alpha not in [t["id"] for t in r["templates"]]
        assert _stored_templates(db_path, other["company_id"])[0]["id"] == other_alpha

        assert _snapshot(conn) == before, "a read must write nothing"

    def test_status_filter_excludes_the_paused_decoy(self, conn, env, db_path):
        alpha, beta, gamma, other, other_alpha = self._seed(conn, env)
        before = _snapshot(conn)

        # Gamma differs in exactly the filtered column, so it is the decoy
        # that proves the status filter ran.
        active = call_action(mod.list_recurring_templates, conn, ns(
            company_id=env["company_id"], company_name=None,
            template_status="active", limit="20", offset="0"))
        assert is_ok(active), active
        assert [t["name"] for t in active["templates"]] == ["Beta", "Alpha"]
        assert active["total_count"] == 2

        paused = call_action(mod.list_recurring_templates, conn, ns(
            company_id=env["company_id"], company_name=None,
            template_status="paused", limit="20", offset="0"))
        assert is_ok(paused), paused
        assert [t["name"] for t in paused["templates"]] == ["Gamma"]
        assert paused["total_count"] == 1

        assert _snapshot(conn) == before, "a read must write nothing"

    def test_unknown_company_refused_exactly_and_writes_nothing(
            self, conn, env, db_path):
        self._seed(conn, env)
        before = _snapshot(conn)
        r = call_action(mod.list_recurring_templates, conn, ns(
            company_id=None, company_name="No Such Company",
            template_status=None, limit="20", offset="0"))
        assert r.get("status") != "ok"
        assert r.get("error") == "Company 'No Such Company' not found."
        assert _snapshot(conn) == before

    def test_bad_limit_refused_exactly_and_writes_nothing(
            self, conn, env, db_path):
        self._seed(conn, env)
        before = _snapshot(conn)
        for bad in ("abc", "-1", "0"):
            r = call_action(mod.list_recurring_templates, conn, ns(
                company_id=env["company_id"], company_name=None,
                template_status=None, limit=bad, offset="0"))
            assert is_error(r), bad
            assert r["message"] == "--limit must be a positive integer", bad
        assert _snapshot(conn) == before

    def test_bad_offset_refused_exactly_and_writes_nothing(
            self, conn, env, db_path):
        self._seed(conn, env)
        before = _snapshot(conn)
        for bad in ("xyz", "-2"):
            r = call_action(mod.list_recurring_templates, conn, ns(
                company_id=env["company_id"], company_name=None,
                template_status=None, limit="20", offset=bad))
            assert is_error(r), bad
            assert r["message"] == "--offset must be a non-negative integer", bad
        assert _snapshot(conn) == before


class TestListJournalEntriesPagingStrong:
    """Pins paging validation on list-journal-entries: a non-integer,
    zero or negative limit and a negative or non-integer offset are
    refused with the exact messages below, and the database is
    unchanged."""

    def test_bad_paging_refused_exactly_and_writes_nothing(
            self, conn, env, db_path):
        r = call_action(mod.add_journal_entry, conn, ns(
            company_id=env["company_id"], posting_date="2026-06-20",
            entry_type="journal", remark="M625C",
            lines=_lines(env, (env["expense"], "300.00", "0"),
                         (env["cash"], "0", "300.00")),
            cwip_asset_id=None))
        assert is_ok(r), r
        before = _snapshot(conn)
        assert seam.table_exists("journal_entry", db_path)
        for bad in ("many", "0", "-1"):
            refused = call_action(mod.list_journal_entries, conn, ns(
                company_id=env["company_id"], company_name=None,
                je_status=None, entry_type=None, from_date=None,
                to_date=None, account_id=None, limit=bad, offset="0"))
            assert is_error(refused), bad
            assert refused["message"] == "--limit must be a positive integer", bad
        for bad in ("-1", "x"):
            refused = call_action(mod.list_journal_entries, conn, ns(
                company_id=env["company_id"], company_name=None,
                je_status=None, entry_type=None, from_date=None,
                to_date=None, account_id=None, limit="20", offset=bad))
            assert is_error(refused), bad
            assert refused["message"] == "--offset must be a non-negative integer", bad
        assert _snapshot(conn) == before
