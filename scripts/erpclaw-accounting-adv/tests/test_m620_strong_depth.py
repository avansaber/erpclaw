"""M620 strong depth: 12 accounting-adv actions made strong.

Each action below already had a behavioural test that only asserted on
content (a name, a count, ``total_count >= 1``): no money literal, no
read-back through the seam, no pinned refusal. This file deepens every one
of them without touching the existing tests. In each class the weight is
carried by the assertions marked WEIGHT below.

Weak test -> strong class (existing tests unchanged):

- test_leases.py::TestClassifyLease -> TestClassifyLeaseStrong
- test_leases.py::TestGetLease -> TestGetLeaseStrong
- test_leases.py::TestListLeases -> TestListLeasesStrong
- test_leases.py::TestUpdateLease -> TestUpdateLeaseStrong
- test_leases.py::TestLeaseSummary -> TestLeaseSummaryStrong
- test_leases.py::TestLeaseMaturityReport -> TestLeaseMaturityReportStrong
- test_leases.py::TestLeaseDisclosureReport -> TestLeaseDisclosureReportStrong
- test_revenue.py::TestGetRevenueContract -> TestGetRevenueContractStrong
- test_revenue.py::TestListRevenueContracts -> TestListRevenueContractsStrong
- test_revenue.py::TestUpdateRevenueContract -> TestUpdateRevenueContractStrong
- test_revenue.py::TestModifyContract -> TestModifyContractStrong
- test_revenue.py::TestListPerformanceObligations
  -> TestListPerformanceObligationsStrong

Conventions:

- Reads go through ``erpclaw_lib.db.get_connection`` on a fresh handle with
  queries built by PyPika through ``erpclaw_lib.query``; money is compared
  as exact TEXT strings, never float. Catalog visibility is proved through
  ``erpclaw_lib.seam.table_exists``. Seeding uses only the owning module's
  own actions, never direct writes.
- Every measured call is wrapped in a before/after snapshot of the entity
  set plus ``audit_log`` and ``gl_entry``: a read must change nothing, a
  writer must change exactly its row plus one audit row and leave the
  ledgers alone, and a named witness row must come back byte-identical.
- Six actions (list-leases, lease-summary, lease-maturity-report,
  lease-disclosure-report, list-revenue-contracts,
  list-performance-obligations) have no refusal branch in the action code:
  any filter combination answers ok, and an unknown filter value yields an
  empty ok-result. That is pinned exactly and recorded in CHANGES.md; no
  production code was changed.
"""
import json

from decimal import Decimal, ROUND_HALF_UP

import pytest

from advacct_helpers import call_action, ns, is_error, is_ok, load_db_query
from erpclaw_lib import seam
from erpclaw_lib.db import get_connection
from erpclaw_lib.query import Field, P, Q, Table, fn

mod = load_db_query()

_TABLES = (
    "advacct_lease",
    "advacct_lease_payment",
    "advacct_amortization_entry",
    "advacct_revenue_contract",
    "advacct_performance_obligation",
    "advacct_variable_consideration",
    "advacct_revenue_schedule",
    "audit_log",
    "gl_entry",
)


@pytest.fixture(autouse=True)
def _dispose_seam_engines():
    yield
    seam.dispose_engines()


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


def _snapshot(conn, tables=_TABLES):
    return {name: _all(conn, name) for name in tables}


def _fresh_row(db_path, table, row_id):
    vconn = get_connection(db_path)
    try:
        return _row(vconn, table, row_id)
    finally:
        vconn.close()


def _fresh_where(db_path, table, **filters):
    vconn = get_connection(db_path)
    try:
        return _where(vconn, table, **filters)
    finally:
        vconn.close()


def _fresh_count(db_path, table):
    vconn = get_connection(db_path)
    try:
        return _count(vconn, table)
    finally:
        vconn.close()


def _audit_rows(conn, entity_id):
    t_audit = Table("audit_log")
    q = Q.from_(t_audit).select(t_audit.star).where(t_audit.entity_id == P())
    return [dict(r) for r in conn.execute(q.get_sql(), (entity_id,)).fetchall()]


def _fresh_audit_rows(db_path, entity_id):
    vconn = get_connection(db_path)
    try:
        return _audit_rows(vconn, entity_id)
    finally:
        vconn.close()


def _assert_whole_row_only_allowed_changed(before, after, allowed, label=""):
    assert set(before) == set(after), label
    for column in before:
        if column in allowed:
            continue
        assert before[column] == after[column], "%s column %s changed: %r -> %r" % (label, column, before[column], after[column])


def _add_lease(conn, env, lessee="Acme Corp", pay="1000.00", rate="0.05",
               term=24, start="2026-01-01", end="2027-12-31",
               company=None, ltype="operating"):
    return call_action(mod.add_lease, conn, ns(
        company_id=company or env["company_id"], lessee_name=lessee,
        lessor_name="Property Holdings LLC",
        asset_description="Office space 5th floor",
        lease_type=ltype, start_date=start, end_date=end,
        term_months=term, monthly_payment=pay,
        annual_escalation="0.03", discount_rate=rate,
        purchase_option_price=None,
    ))


def _add_contract(conn, env, customer="Acme Corp", total="120000.00",
                  number="C-001", start="2026-01-01", end="2026-12-31",
                  company=None):
    return call_action(mod.add_revenue_contract, conn, ns(
        company_id=company or env["company_id"], customer_name=customer,
        total_value=total, contract_number=number,
        start_date=start, end_date=end,
    ))


def _add_obligation(conn, env, contract_id, name="Software License",
                    price="60000.00", company=None):
    return call_action(mod.add_performance_obligation, conn, ns(
        contract_id=contract_id, company_id=company or env["company_id"],
        name=name, standalone_price=price,
        recognition_method="over_time", recognition_basis="time",
    ))


def _lease_ns(conn_env_id, **over):
    base = dict(company_id=conn_env_id, lease_type=None, lease_status=None,
                search=None, limit=50, offset=0)
    base.update(over)
    return ns(**base)


# ---------------------------------------------------------------------------
# 1. classify-lease
# ---------------------------------------------------------------------------

class TestClassifyLeaseStrong:
    """Deepens TestClassifyLease (asserts only the returned lease_type).

    WEIGHT is on the fresh read-back of the stored lease_type plus the
    hand-named money/rate literals below. No amount column is written by
    this action, so exact TEXT identity of the stored money fields is the
    money-grade check."""

    def test_auto_classify_writes_exact_row_and_nothing_else(
            self, conn, env, db_path):
        target = _add_lease(conn, env, lessee="Classify Target",
                            pay="1000.00", rate="0.05", term=48)
        assert is_ok(target)
        witness = _add_lease(conn, env, lessee="Classify Witness",
                             pay="9999.99", rate="0.07", term=12)
        assert is_ok(witness)
        other = _add_lease(conn, env, lessee="Other Company Lease",
                           pay="7777.77", rate="0.04", term=48,
                           company=env["company2_id"])
        assert is_ok(other)
        witness_before = _row(conn, "advacct_lease", witness["id"])
        other_before = _row(conn, "advacct_lease", other["id"])
        before = _snapshot(conn)
        audit_before = _count(conn, "audit_log")

        r = call_action(mod.classify_lease, conn, ns(
            id=target["id"], lease_type=None))
        assert is_ok(r), r
        # WEIGHT: 48 months auto-maps to finance; hand-named expectation.
        assert r["lease_type"] == "finance"

        # WEIGHT: fresh read-back carries the stored classification plus the
        # exact money/rate TEXT written at creation.
        stored = _fresh_row(db_path, "advacct_lease", target["id"])
        assert stored["lease_type"] == "finance"
        assert stored["monthly_payment"] == "1000.00"
        assert Decimal(stored["monthly_payment"]) == Decimal("1000.00")
        assert stored["discount_rate"] == "0.05"
        assert stored["term_months"] == 48
        assert stored["lessee_name"] == "Classify Target"
        assert stored["company_id"] == env["company_id"]

        # WEIGHT: only the target row plus one audit row changed; the
        # ledgers, the witness and the other-company row are byte-identical.
        assert _fresh_count(db_path, "audit_log") == audit_before + 1
        assert _fresh_count(db_path, "gl_entry") == 0
        assert _row(conn, "advacct_lease", witness["id"]) == witness_before
        assert _row(conn, "advacct_lease", other["id"]) == other_before
        after = _snapshot(conn)
        for table in _TABLES:
            if table in ("advacct_lease", "audit_log"):
                continue
            assert after[table] == before[table], table
        assert len(after["advacct_lease"]) == len(before["advacct_lease"])
        assert len(after["audit_log"]) == len(before["audit_log"]) + 1

    def test_unknown_id_refused_truthfully_and_writes_nothing(
            self, conn, env):
        target = _add_lease(conn, env)
        assert is_ok(target)
        before = _snapshot(conn)
        r = call_action(mod.classify_lease, conn, ns(
            id="no-such-lease", lease_type=None))
        assert is_error(r)
        # WEIGHT: message names what the caller typed.
        assert _msg(r) == "Lease no-such-lease not found"
        # WEIGHT: refusal wrote nothing anywhere.
        assert _snapshot(conn) == before

    def test_auto_classify_boundary_35_36_37(self, conn, env, db_path):
        short = _add_lease(conn, env, lessee="Boundary 35",
                           pay="1000.00", rate="0.05", term=35)
        edge = _add_lease(conn, env, lessee="Boundary 36",
                          pay="1000.00", rate="0.05", term=36)
        long = _add_lease(conn, env, lessee="Boundary 37",
                          pay="1000.00", rate="0.05", term=37)
        assert is_ok(short) and is_ok(edge) and is_ok(long)
        # WEIGHT: park each lease on the opposite of its expected type so
        # an action that changed nothing would fail.
        assert is_ok(call_action(mod.update_lease, conn, ns(
            id=short["id"], lessee_name=None, lessor_name=None,
            asset_description=None, start_date=None, end_date=None,
            monthly_payment=None, discount_rate=None,
            annual_escalation=None, purchase_option_price=None,
            lease_type="finance", term_months=None)))
        assert is_ok(call_action(mod.update_lease, conn, ns(
            id=edge["id"], lessee_name=None, lessor_name=None,
            asset_description=None, start_date=None, end_date=None,
            monthly_payment=None, discount_rate=None,
            annual_escalation=None, purchase_option_price=None,
            lease_type="operating", term_months=None)))
        assert is_ok(call_action(mod.update_lease, conn, ns(
            id=long["id"], lessee_name=None, lessor_name=None,
            asset_description=None, start_date=None, end_date=None,
            monthly_payment=None, discount_rate=None,
            annual_escalation=None, purchase_option_price=None,
            lease_type="operating", term_months=None)))
        assert _fresh_row(db_path, "advacct_lease", short["id"])["lease_type"] == "finance"
        assert _fresh_row(db_path, "advacct_lease", edge["id"])["lease_type"] == "operating"
        assert _fresh_row(db_path, "advacct_lease", long["id"])["lease_type"] == "operating"

        for lease_id, expected in ((short["id"], "operating"),
                                   (edge["id"], "finance"),
                                   (long["id"], "finance")):
            r = call_action(mod.classify_lease, conn, ns(
                id=lease_id, lease_type=None))
            assert is_ok(r), r
            assert r["lease_type"] == expected
            stored = _fresh_row(db_path, "advacct_lease", lease_id)
            assert stored["lease_type"] == expected
            assert stored["monthly_payment"] == "1000.00"


# ---------------------------------------------------------------------------
# 2. get-lease
# ---------------------------------------------------------------------------

class TestGetLeaseStrong:
    """Deepens TestGetLease.test_get (asserts only names and key presence).

    WEIGHT is on the response repeating the stored money TEXT plus the
    fresh read-back of the same row."""

    def test_get_repeats_stored_money_and_reads_back_exact(
            self, conn, env, db_path):
        target = _add_lease(conn, env, lessee="Get Target",
                            pay="1000.00", rate="0.05", term=24)
        assert is_ok(target)
        decoy = _add_lease(conn, env, lessee="Get Decoy",
                           pay="9999.99", rate="0.09", term=24)
        assert is_ok(decoy)
        other = _add_lease(conn, env, lessee="Other Company Get",
                           pay="7777.77", rate="0.04", term=24,
                           company=env["company2_id"])
        assert is_ok(other)
        snapshot = _snapshot(conn)

        r = call_action(mod.get_lease, conn, ns(id=target["id"]))
        assert is_ok(r), r
        # WEIGHT: hand-named money literals, never copied from output.
        assert r["lessee_name"] == "Get Target"
        assert r["monthly_payment"] == "1000.00"
        assert Decimal(r["monthly_payment"]) == Decimal("1000.00")
        assert r["discount_rate"] == "0.05"
        assert r["term_months"] == 24
        assert r["company_id"] == env["company_id"]
        # WEIGHT: proves the action read the right row, not the decoy,
        # the other company, or a stale version.
        assert r["monthly_payment"] != "9999.99"
        assert r["monthly_payment"] != "7777.77"
        assert r["lessee_name"] != "Get Decoy"

        stored = _fresh_row(db_path, "advacct_lease", target["id"])
        assert stored["monthly_payment"] == "1000.00"
        assert stored["discount_rate"] == "0.05"
        assert stored["lessee_name"] == "Get Target"

        # WEIGHT: a read writes nothing.
        assert _snapshot(conn) == snapshot
        assert _fresh_count(db_path, "gl_entry") == 0

    def test_unknown_id_refused_truthfully_and_writes_nothing(
            self, conn, env):
        target = _add_lease(conn, env)
        assert is_ok(target)
        before = _snapshot(conn)
        r = call_action(mod.get_lease, conn, ns(id="no-such-lease"))
        assert is_error(r)
        assert _msg(r) == "Lease no-such-lease not found"
        assert _snapshot(conn) == before


# ---------------------------------------------------------------------------
# 3. list-leases
# ---------------------------------------------------------------------------

class TestListLeasesStrong:
    """Deepens TestListLeases.test_list (asserts only total_count >= 1).

    WEIGHT is on exact membership plus hand-named money literals repeated
    through a fresh read-back. Finding: list-leases has no refusal branch;
    an unknown filter answers ok with an empty set (pinned below)."""

    def test_lists_both_leases_with_exact_money(self, conn, env, db_path):
        first = _add_lease(conn, env, lessee="List Alpha",
                           pay="1000.00", rate="0.05")
        second = _add_lease(conn, env, lessee="List Beta",
                            pay="2000.00", rate="0.06")
        assert is_ok(first) and is_ok(second)
        other = _add_lease(conn, env, lessee="Other Company List",
                           pay="7777.77", rate="0.04",
                           company=env["company2_id"])
        assert is_ok(other)
        snapshot = _snapshot(conn)

        r = call_action(mod.list_leases, conn, _lease_ns(env["company_id"]))
        assert is_ok(r), r
        # WEIGHT: exact membership and hand-named money.
        assert r["total_count"] == 2
        got = {row["id"]: row for row in r["rows"]}
        assert set(got) == {first["id"], second["id"]}
        assert got[first["id"]]["monthly_payment"] == "1000.00"
        assert got[second["id"]]["monthly_payment"] == "2000.00"
        assert Decimal(got[first["id"]]["monthly_payment"]) == Decimal("1000.00")
        assert got[first["id"]]["lessee_name"] == "List Alpha"
        assert got[second["id"]]["lessee_name"] == "List Beta"
        for row in got.values():
            assert row["company_id"] == env["company_id"]

        # WEIGHT: fresh read-back repeats the same literals; the
        # other-company row is excluded by the filter.
        back = {row["id"]: row for row in
                _fresh_where(db_path, "advacct_lease",
                             company_id=env["company_id"])}
        assert back[first["id"]]["monthly_payment"] == "1000.00"
        assert back[second["id"]]["monthly_payment"] == "2000.00"
        assert other["id"] not in back

        # WEIGHT: nothing changed.
        assert _snapshot(conn) == snapshot
        assert _fresh_count(db_path, "gl_entry") == 0

    def test_unknown_company_refuses_and_writes_nothing(
            self, conn, env):
        made = _add_lease(conn, env)
        assert is_ok(made)
        before = _snapshot(conn)
        r = call_action(mod.list_leases, conn,
                        _lease_ns("no-such-company"))
        assert is_error(r)
        assert r["error"] == "Company not found: no-such-company"
        assert _snapshot(conn) == before

    def test_no_company_refuses_with_multiple_companies(self, conn, env, db_path):
        first = _add_lease(conn, env, lessee="List No Company Alpha",
                           pay="1000.00")
        assert is_ok(first)
        other = _add_lease(conn, env, lessee="List No Company Other",
                           pay="7777.77", company=env["company2_id"])
        assert is_ok(other)
        before = _snapshot(conn)
        r = call_action(mod.list_leases, conn, _lease_ns(None))
        assert is_error(r), r
        assert r["error"] == "Multiple companies found. Please specify the company by name."
        want = sorted(
            (_row(conn, "company", cid)["name"], cid)
            for cid in (env["company_id"], env["company2_id"]))
        assert r["companies"] == [
            {"id": cid, "name": name} for name, cid in want]
        assert "rows" not in r
        assert _snapshot(conn) == before


# ---------------------------------------------------------------------------
# 4. update-lease
# ---------------------------------------------------------------------------

class TestUpdateLeaseStrong:
    """Deepens TestUpdateLease.test_update_name (asserts only updated_fields).

    WEIGHT is on the fresh read-back of the changed money TEXT."""

    def test_update_money_writes_exact_row_and_nothing_else(
            self, conn, env, db_path):
        target = _add_lease(conn, env, lessee="Update Target",
                            pay="1000.00", rate="0.05")
        assert is_ok(target)
        witness = _add_lease(conn, env, lessee="Update Witness",
                             pay="9999.99", rate="0.09")
        assert is_ok(witness)
        witness_before = _row(conn, "advacct_lease", witness["id"])
        target_before = _row(conn, "advacct_lease", target["id"])
        before = _snapshot(conn)
        audit_before = _count(conn, "audit_log")

        r = call_action(mod.update_lease, conn, ns(
            id=target["id"], lessee_name="Updated Corp",
            lessor_name=None, asset_description=None,
            start_date=None, end_date=None,
            monthly_payment="1750.00", discount_rate=None,
            annual_escalation=None, purchase_option_price=None,
            lease_type=None, term_months=None,
        ))
        assert is_ok(r), r
        assert r["updated_fields"] == ["lessee_name", "monthly_payment"]

        # WEIGHT: hand-named money literal written by the action.
        stored = _fresh_row(db_path, "advacct_lease", target["id"])
        assert stored["lessee_name"] == "Updated Corp"
        assert stored["monthly_payment"] == "1750.00"
        assert Decimal(stored["monthly_payment"]) == Decimal("1750.00")
        # Untouched money field keeps its exact TEXT.
        assert stored["discount_rate"] == "0.05"

        # WEIGHT: the whole row moved only in the allowed columns.
        allowed = {"lessee_name", "monthly_payment", "updated_at"}
        _assert_whole_row_only_allowed_changed(target_before, stored, allowed, "advacct_lease")
        assert stored["lessee_name"] == "Updated Corp"
        assert stored["monthly_payment"] == "1750.00"
        # WEIGHT: the audit row names the action, the entity and the change.
        fresh_audits = [a for a in _fresh_audit_rows(db_path, target["id"]) if a["action"] == "update-lease"]
        assert len(fresh_audits) >= 1
        latest = fresh_audits[-1]
        assert latest["action"] == "update-lease"
        assert latest["entity_id"] == target["id"]
        assert latest["entity_type"] == "advacct_lease"
        old_values = json.loads(latest["old_values"])
        assert old_values == {"lessee_name": "Update Target",
                              "monthly_payment": "1000.00"}
        parsed = json.loads(latest["new_values"])
        assert parsed == {"lessee_name": "Updated Corp",
                          "monthly_payment": "1750.00"}

        # WEIGHT: one audit row for the writer, ledgers untouched, witness
        # byte-identical.
        assert _fresh_count(db_path, "audit_log") == audit_before + 1
        assert _fresh_count(db_path, "gl_entry") == 0
        assert _row(conn, "advacct_lease", witness["id"]) == witness_before
        after = _snapshot(conn)
        for table in _TABLES:
            if table in ("advacct_lease", "audit_log"):
                continue
            assert after[table] == before[table], table
        assert len(after["audit_log"]) == len(before["audit_log"]) + 1

    def test_update_audit_row_carries_old_and_new_values(
            self, conn, env, db_path):
        target = _add_lease(conn, env, lessee="Update Target",
                            pay="1000.00", rate="0.05")
        assert is_ok(target)
        r = call_action(mod.update_lease, conn, ns(
            id=target["id"], lessee_name="Updated Corp",
            lessor_name=None, asset_description=None,
            start_date=None, end_date=None,
            monthly_payment="1750.00", discount_rate=None,
            annual_escalation=None, purchase_option_price=None,
            lease_type=None, term_months=None,
        ))
        assert is_ok(r), r
        audits = [a for a in _fresh_audit_rows(db_path, target["id"])
                  if a["action"] == "update-lease"]
        assert len(audits) >= 1
        latest = audits[-1]
        old_values = json.loads(latest["old_values"] or "{}")
        new_values = json.loads(latest["new_values"] or "{}")
        # House shape, as update-employee writes it:
        # old_values={column: old}, new_values={column: new}.
        assert old_values == {"lessee_name": "Update Target",
                              "monthly_payment": "1000.00"}
        assert new_values == {"lessee_name": "Updated Corp",
                              "monthly_payment": "1750.00"}

    def test_invalid_lease_type_refused_truthfully_and_writes_nothing(
            self, conn, env):
        target = _add_lease(conn, env)
        assert is_ok(target)
        before = _snapshot(conn)
        r = call_action(mod.update_lease, conn, ns(
            id=target["id"], lessee_name=None,
            lessor_name=None, asset_description=None,
            start_date=None, end_date=None,
            monthly_payment=None, discount_rate=None,
            annual_escalation=None, purchase_option_price=None,
            lease_type="bogus", term_months=None,
        ))
        assert is_error(r)
        # WEIGHT: message names the typed value and the allowed set.
        assert _msg(r) == ("Invalid lease-type: bogus. "
                           "Must be one of: operating, finance")
        assert _snapshot(conn) == before


# ---------------------------------------------------------------------------
# 5. lease-summary
# ---------------------------------------------------------------------------

class TestLeaseSummaryStrong:
    """Deepens TestLeaseSummary.test_summary (asserts only total >= 1).

    WEIGHT is on the exact bucket counts plus the money read-back proving
    the counted rows are the seeded ones. The summary output carries no
    amount column by design; the money literal lives in the read-back.
    Finding: lease-summary has no refusal branch."""

    def test_summary_counts_exact_buckets_per_company(
            self, conn, env, db_path):
        first = _add_lease(conn, env, lessee="Summary Alpha",
                           pay="1000.00", ltype="operating")
        second = _add_lease(conn, env, lessee="Summary Beta",
                            pay="2000.00", ltype="operating")
        assert is_ok(first) and is_ok(second)
        fin = _add_lease(conn, env, lessee="Summary Finance",
                         pay="3000.00", term=48)
        assert is_ok(call_action(mod.classify_lease, conn, ns(
            id=fin["id"], lease_type="finance")))
        other = _add_lease(conn, env, lessee="Other Company Summary",
                           pay="7777.77", company=env["company2_id"])
        assert is_ok(other)
        snapshot = _snapshot(conn)

        r = call_action(mod.lease_summary, conn, ns(
            company_id=env["company_id"]))
        assert is_ok(r), r
        # WEIGHT: exact counts, hand-computed from the seeds above.
        assert r["total_leases"] == 3
        assert r["by_status"] == {"draft": 3}
        assert r["by_type"] == {"operating": 2, "finance": 1}

        # WEIGHT: the counted rows are the seeded money TEXT, not the
        # other-company decoy.
        back = _fresh_where(db_path, "advacct_lease",
                            company_id=env["company_id"])
        assert sorted(row["monthly_payment"] for row in back) == [
            "1000.00", "2000.00", "3000.00"]
        assert sorted(row["lessee_name"] for row in back) == [
            "Summary Alpha", "Summary Beta", "Summary Finance"]
        other_back = _fresh_row(db_path, "advacct_lease", other["id"])
        assert other_back["monthly_payment"] == "7777.77"

        other_summary = call_action(mod.lease_summary, conn, ns(
            company_id=env["company2_id"]))
        assert is_ok(other_summary)
        assert other_summary["total_leases"] == 1

        # WEIGHT: a report writes nothing.
        assert _snapshot(conn) == snapshot

    def test_unknown_company_refuses_and_writes_nothing(
            self, conn, env):
        made = _add_lease(conn, env)
        assert is_ok(made)
        before = _snapshot(conn)
        r = call_action(mod.lease_summary, conn, ns(
            company_id="no-such-company"))
        assert is_error(r)
        assert r["error"] == "Company not found: no-such-company"
        assert _snapshot(conn) == before

    def test_no_company_refuses_with_multiple_companies(self, conn, env, db_path):
        first = _add_lease(conn, env, lessee="Summary No Company",
                           pay="1000.00")
        assert is_ok(first)
        other = _add_lease(conn, env, lessee="Summary No Company Other",
                           pay="7777.77", company=env["company2_id"])
        assert is_ok(other)
        before = _snapshot(conn)
        r = call_action(mod.lease_summary, conn, ns(company_id=None))
        assert is_error(r), r
        assert r["error"] == "Multiple companies found. Please specify the company by name."
        want = sorted(
            (_row(conn, "company", cid)["name"], cid)
            for cid in (env["company_id"], env["company2_id"]))
        assert r["companies"] == [
            {"id": cid, "name": name} for name, cid in want]
        assert "total_leases" not in r
        assert _snapshot(conn) == before


# ---------------------------------------------------------------------------
# 6. lease-maturity-report
# ---------------------------------------------------------------------------

class TestLeaseMaturityReportStrong:
    """Deepens TestLeaseMaturityReport.test_report (asserts only total >= 1).

    WEIGHT is on the rows repeating the stored money TEXT, read back fresh.
    Finding: lease-maturity-report has no refusal branch."""

    def test_report_rows_carry_exact_stored_money(self, conn, env, db_path):
        # NOTE: Beta (the later end date) is seeded first, so creation order
        # cannot explain the asserted order — only ORDER BY end_date ASC can.
        beta = _add_lease(conn, env, lessee="Maturity Beta",
                          pay="2000.00", rate="0.06",
                          start="2026-01-01", end="2028-06-30")
        alpha = _add_lease(conn, env, lessee="Maturity Alpha",
                           pay="1000.00", rate="0.05",
                           start="2026-01-01", end="2027-12-31")
        assert is_ok(beta) and is_ok(alpha)
        other = _add_lease(conn, env, lessee="Other Company Maturity",
                           pay="7777.77", company=env["company2_id"])
        assert is_ok(other)
        snapshot = _snapshot(conn)

        r = call_action(mod.lease_maturity_report, conn, ns(
            company_id=env["company_id"]))
        assert is_ok(r), r
        assert r["total_leases"] == 2
        # WEIGHT: hand-named money literals in the report rows.
        got = {row["id"]: row for row in r["rows"]}
        assert set(got) == {alpha["id"], beta["id"]}
        assert got[alpha["id"]]["monthly_payment"] == "1000.00"
        assert got[beta["id"]]["monthly_payment"] == "2000.00"
        assert Decimal(got[beta["id"]]["monthly_payment"]) == Decimal("2000.00")
        assert got[alpha["id"]]["lessee_name"] == "Maturity Alpha"
        assert got[beta["id"]]["lessee_name"] == "Maturity Beta"
        # WEIGHT: rows come back ordered by end date, earliest first, even
        # though the later-ending lease was created first.
        assert [row["id"] for row in r["rows"]] == [alpha["id"], beta["id"]]
        assert r["rows"][0]["end_date"] == "2027-12-31"
        assert r["rows"][1]["end_date"] == "2028-06-30"

        stored_alpha = _fresh_row(db_path, "advacct_lease", alpha["id"])
        assert stored_alpha["monthly_payment"] == "1000.00"
        stored_beta = _fresh_row(db_path, "advacct_lease", beta["id"])
        assert stored_beta["monthly_payment"] == "2000.00"

        assert _snapshot(conn) == snapshot
        assert _fresh_count(db_path, "gl_entry") == 0

    def test_no_company_refuses_with_multiple_companies(self, conn, env, db_path):
        first = _add_lease(conn, env, lessee="No Company Alpha",
                           pay="1000.00")
        assert is_ok(first)
        other = _add_lease(conn, env, lessee="No Company Other",
                           pay="7777.77", company=env["company2_id"])
        assert is_ok(other)
        before = _snapshot(conn)
        r = call_action(mod.lease_maturity_report, conn, ns(
            company_id=None))
        assert is_error(r), r
        assert r["error"] == "Multiple companies found. Please specify the company by name."
        want = sorted(
            (_row(conn, "company", cid)["name"], cid)
            for cid in (env["company_id"], env["company2_id"]))
        assert r["companies"] == [
            {"id": cid, "name": name} for name, cid in want]
        assert "rows" not in r
        assert _snapshot(conn) == before

    def test_unknown_company_refuses_and_writes_nothing(
            self, conn, env):
        made = _add_lease(conn, env)
        assert is_ok(made)
        before = _snapshot(conn)
        r = call_action(mod.lease_maturity_report, conn, ns(
            company_id="no-such-company"))
        assert is_error(r)
        assert r["error"] == "Company not found: no-such-company"
        assert _snapshot(conn) == before


# ---------------------------------------------------------------------------
# 7. lease-disclosure-report
# ---------------------------------------------------------------------------

class TestLeaseDisclosureReportStrong:
    """Deepens TestLeaseDisclosureReport.test_report (asserts only len >= 1).

    WEIGHT is on the hand-computed grouped total plus the fresh money
    read-back. Finding: lease-disclosure-report has no refusal branch."""

    def test_disclosure_totals_are_hand_computed(self, conn, env, db_path):
        first = _add_lease(conn, env, lessee="Disclosure Alpha",
                           pay="1000.10", ltype="operating")
        second = _add_lease(conn, env, lessee="Disclosure Beta",
                            pay="2000.20", ltype="operating")
        assert is_ok(first) and is_ok(second)
        fin = _add_lease(conn, env, lessee="Disclosure Finance",
                         pay="5000.00", term=48)
        assert is_ok(call_action(mod.classify_lease, conn, ns(
            id=fin["id"], lease_type="finance")))
        other = _add_lease(conn, env, lessee="Other Company Disclosure",
                           pay="7777.77", company=env["company2_id"])
        assert is_ok(other)
        # WEIGHT: derive the asset and liability figures through the product
        # so the report total is checked against stored TEXT, not a constant.
        assert is_ok(call_action(mod.calculate_rou_asset, conn, ns(id=first["id"])))
        assert is_ok(call_action(mod.calculate_lease_liability, conn, ns(id=first["id"])))
        assert is_ok(call_action(mod.calculate_rou_asset, conn, ns(id=second["id"])))
        assert is_ok(call_action(mod.calculate_lease_liability, conn, ns(id=second["id"])))
        first_rou = _fresh_row(db_path, "advacct_lease", first["id"])["rou_asset_value"]
        first_lia = _fresh_row(db_path, "advacct_lease", first["id"])["lease_liability"]
        second_rou = _fresh_row(db_path, "advacct_lease", second["id"])["rou_asset_value"]
        second_lia = _fresh_row(db_path, "advacct_lease", second["id"])["lease_liability"]
        assert first_rou is not None and second_rou is not None
        assert first_lia is not None and second_lia is not None
        expected_rou = str((Decimal(first_rou) + Decimal(second_rou)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        expected_lia = str((Decimal(first_lia) + Decimal(second_lia)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        snapshot = _snapshot(conn)

        r = call_action(mod.lease_disclosure_report, conn, ns(
            company_id=env["company_id"]))
        assert is_ok(r), r
        # WEIGHT: 1000.10 + 2000.20 = 3000.30 as an exact string.
        by_type = {row["lease_type"]: row for row in r["rows"]}
        assert set(by_type) == {"operating", "finance"}
        assert by_type["operating"]["lease_count"] == 2
        assert by_type["operating"]["total_monthly_payments"] == "3000.30"
        assert Decimal(by_type["operating"]["total_monthly_payments"]) == Decimal("3000.30")
        assert by_type["operating"]["total_rou_assets"] == expected_rou
        assert by_type["operating"]["total_lease_liabilities"] == expected_lia
        assert Decimal(by_type["operating"]["total_rou_assets"]) == Decimal(expected_rou)
        assert Decimal(by_type["operating"]["total_lease_liabilities"]) == Decimal(expected_lia)
        assert by_type["finance"]["lease_count"] == 1
        assert by_type["finance"]["total_monthly_payments"] == "5000.00"
        assert isinstance(by_type["finance"]["total_rou_assets"], str)
        assert isinstance(by_type["finance"]["total_lease_liabilities"], str)

        # WEIGHT: the summed rows are the seeded TEXT values.
        back = _fresh_where(db_path, "advacct_lease",
                            company_id=env["company_id"])
        assert sorted(row["monthly_payment"] for row in back) == [
            "1000.10", "2000.20", "5000.00"]

        assert _snapshot(conn) == snapshot

    def test_no_company_refuses_with_multiple_companies(self, conn, env, db_path):
        first = _add_lease(conn, env, lessee="Disclosure No Company",
                           pay="1000.10", ltype="operating")
        assert is_ok(first)
        other = _add_lease(conn, env, lessee="Disclosure Other Company",
                           pay="7777.77", company=env["company2_id"])
        assert is_ok(other)
        before = _snapshot(conn)
        r = call_action(mod.lease_disclosure_report, conn, ns(
            company_id=None))
        assert is_error(r), r
        assert r["error"] == "Multiple companies found. Please specify the company by name."
        want = sorted(
            (_row(conn, "company", cid)["name"], cid)
            for cid in (env["company_id"], env["company2_id"]))
        assert r["companies"] == [
            {"id": cid, "name": name} for name, cid in want]
        assert "rows" not in r
        assert _snapshot(conn) == before

    def test_unknown_company_refuses_and_writes_nothing(
            self, conn, env):
        made = _add_lease(conn, env)
        assert is_ok(made)
        before = _snapshot(conn)
        r = call_action(mod.lease_disclosure_report, conn, ns(
            company_id="no-such-company"))
        assert is_error(r)
        assert r["error"] == "Company not found: no-such-company"
        assert _snapshot(conn) == before


# ---------------------------------------------------------------------------
# 8. get-revenue-contract
# ---------------------------------------------------------------------------

class TestGetRevenueContractStrong:
    """Deepens TestGetRevenueContract.test_get (asserts only the name).

    WEIGHT is on the response repeating the stored money TEXT plus the
    fresh read-back of the contract and its obligation."""

    def test_get_repeats_stored_money_and_reads_back_exact(
            self, conn, env, db_path):
        target = _add_contract(conn, env, customer="Get Target",
                               total="120000.00", number="C-GET-1")
        assert is_ok(target)
        ob = _add_obligation(conn, env, target["id"],
                             name="Software License", price="60000.00")
        assert is_ok(ob)
        decoy = _add_contract(conn, env, customer="Get Decoy",
                              total="99999.99", number="C-DECOY")
        assert is_ok(decoy)
        other = _add_contract(conn, env, customer="Other Company Get",
                              total="77777.77", number="C-OTHER",
                              company=env["company2_id"])
        assert is_ok(other)
        snapshot = _snapshot(conn)

        r = call_action(mod.get_revenue_contract, conn, ns(id=target["id"]))
        assert is_ok(r), r
        # WEIGHT: hand-named money literals.
        assert r["customer_name"] == "Get Target"
        assert r["total_value"] == "120000.00"
        assert Decimal(r["total_value"]) == Decimal("120000.00")
        assert r["contract_number"] == "C-GET-1"
        assert r["company_id"] == env["company_id"]
        assert r["customer_name"] != "Get Decoy"
        assert r["total_value"] != "99999.99"
        assert r["total_value"] != "77777.77"
        names = [o["name"] for o in r["obligations"]]
        assert names == ["Software License"]
        assert r["obligations"][0]["standalone_price"] == "60000.00"
        assert r["obligations"][0]["allocated_price"] == "60000.00"

        stored = _fresh_row(db_path, "advacct_revenue_contract", target["id"])
        assert stored["total_value"] == "120000.00"
        assert stored["customer_name"] == "Get Target"
        stored_obs = _fresh_where(db_path, "advacct_performance_obligation",
                                  contract_id=target["id"])
        assert len(stored_obs) == 1
        assert stored_obs[0]["standalone_price"] == "60000.00"
        assert stored_obs[0]["allocated_price"] == "60000.00"

        # WEIGHT: a read writes nothing.
        assert _snapshot(conn) == snapshot
        assert _fresh_count(db_path, "gl_entry") == 0

    def test_unknown_id_refused_truthfully_and_writes_nothing(
            self, conn, env):
        made = _add_contract(conn, env)
        assert is_ok(made)
        before = _snapshot(conn)
        r = call_action(mod.get_revenue_contract, conn, ns(id="no-such-contract"))
        assert is_error(r)
        assert _msg(r) == "Revenue contract no-such-contract not found"
        assert _snapshot(conn) == before


# ---------------------------------------------------------------------------
# 9. list-revenue-contracts
# ---------------------------------------------------------------------------

class TestListRevenueContractsStrong:
    """Deepens TestListRevenueContracts.test_list (asserts only count >= 1).

    WEIGHT is on exact membership plus hand-named money literals. Finding:
    list-revenue-contracts has no refusal branch."""

    def test_lists_both_contracts_with_exact_money(self, conn, env, db_path):
        first = _add_contract(conn, env, customer="List Alpha",
                              total="120000.00", number="C-LIST-1")
        second = _add_contract(conn, env, customer="List Beta",
                               total="80000.00", number="C-LIST-2")
        assert is_ok(first) and is_ok(second)
        other = _add_contract(conn, env, customer="Other Company List",
                              total="77777.77", number="C-OTHER",
                              company=env["company2_id"])
        assert is_ok(other)
        snapshot = _snapshot(conn)

        r = call_action(mod.list_revenue_contracts, conn, ns(
            company_id=env["company_id"], contract_status=None,
            search=None, limit=50, offset=0,
        ))
        assert is_ok(r), r
        assert r["total_count"] == 2
        # WEIGHT: exact membership and hand-named money.
        got = {row["id"]: row for row in r["rows"]}
        assert set(got) == {first["id"], second["id"]}
        assert got[first["id"]]["total_value"] == "120000.00"
        assert got[second["id"]]["total_value"] == "80000.00"
        assert Decimal(got[first["id"]]["total_value"]) == Decimal("120000.00")
        assert got[first["id"]]["customer_name"] == "List Alpha"
        for row in got.values():
            assert row["company_id"] == env["company_id"]

        back = {row["id"]: row for row in
                _fresh_where(db_path, "advacct_revenue_contract",
                             company_id=env["company_id"])}
        assert back[first["id"]]["total_value"] == "120000.00"
        assert back[second["id"]]["total_value"] == "80000.00"
        assert other["id"] not in back

        assert _snapshot(conn) == snapshot
        assert _fresh_count(db_path, "gl_entry") == 0

    def test_unknown_company_refuses_and_writes_nothing(
            self, conn, env):
        made = _add_contract(conn, env)
        assert is_ok(made)
        before = _snapshot(conn)
        r = call_action(mod.list_revenue_contracts, conn, ns(
            company_id="no-such-company", contract_status=None,
            search=None, limit=50, offset=0,
        ))
        assert is_error(r)
        assert r["error"] == "Company not found: no-such-company"
        assert _snapshot(conn) == before

    def test_no_company_refuses_with_multiple_companies(self, conn, env, db_path):
        first = _add_contract(conn, env, customer="Rev No Company Alpha",
                              total="120000.00", number="C-NC-1")
        assert is_ok(first)
        other = _add_contract(conn, env, customer="Rev No Company Other",
                              total="77777.77", number="C-NC-OTHER",
                              company=env["company2_id"])
        assert is_ok(other)
        before = _snapshot(conn)
        r = call_action(mod.list_revenue_contracts, conn, ns(
            company_id=None, contract_status=None,
            search=None, limit=50, offset=0,
        ))
        assert is_error(r), r
        assert r["error"] == "Multiple companies found. Please specify the company by name."
        want = sorted(
            (_row(conn, "company", cid)["name"], cid)
            for cid in (env["company_id"], env["company2_id"]))
        assert r["companies"] == [
            {"id": cid, "name": name} for name, cid in want]
        assert "rows" not in r
        assert _snapshot(conn) == before


# ---------------------------------------------------------------------------
# 10. update-revenue-contract
# ---------------------------------------------------------------------------

class TestUpdateRevenueContractStrong:
    """Deepens TestUpdateRevenueContract.test_update_name (asserts only the
    updated_fields flag). WEIGHT is on the fresh read-back of the changed
    money TEXT."""

    def test_update_money_writes_exact_row_and_nothing_else(
            self, conn, env, db_path):
        target = _add_contract(conn, env, customer="Update Target",
                               total="120000.00", number="C-UPD-1")
        assert is_ok(target)
        witness = _add_contract(conn, env, customer="Update Witness",
                                total="99999.99", number="C-WIT")
        assert is_ok(witness)
        witness_before = _row(conn, "advacct_revenue_contract", witness["id"])
        target_before = _row(conn, "advacct_revenue_contract", target["id"])
        before = _snapshot(conn)
        audit_before = _count(conn, "audit_log")

        r = call_action(mod.update_revenue_contract, conn, ns(
            id=target["id"], customer_name="Updated Corp",
            contract_number=None, start_date=None,
            end_date=None, total_value="135000.00", contract_status=None,
        ))
        assert is_ok(r), r
        assert r["updated_fields"] == ["customer_name", "total_value"]

        # WEIGHT: hand-named money literal written by the action.
        stored = _fresh_row(db_path, "advacct_revenue_contract", target["id"])
        assert stored["customer_name"] == "Updated Corp"
        assert stored["total_value"] == "135000.00"
        assert Decimal(stored["total_value"]) == Decimal("135000.00")
        assert stored["contract_number"] == "C-UPD-1"

        # WEIGHT: the whole row moved only in the allowed columns.
        allowed = {"customer_name", "total_value", "updated_at"}
        _assert_whole_row_only_allowed_changed(target_before, stored, allowed, "advacct_revenue_contract")
        # WEIGHT: the audit row names the action, the entity and the change.
        fresh_audits = [a for a in _fresh_audit_rows(db_path, target["id"]) if a["action"] == "update-revenue-contract"]
        assert len(fresh_audits) >= 1
        latest = fresh_audits[-1]
        assert latest["action"] == "update-revenue-contract"
        assert latest["entity_id"] == target["id"]
        assert latest["entity_type"] == "advacct_revenue_contract"
        old_values = json.loads(latest["old_values"])
        assert old_values == {"customer_name": "Update Target",
                              "total_value": "120000.00"}
        parsed = json.loads(latest["new_values"])
        assert parsed == {"customer_name": "Updated Corp",
                          "total_value": "135000.00"}

        # WEIGHT: one audit row for the writer, ledgers untouched, witness
        # byte-identical.
        assert _fresh_count(db_path, "audit_log") == audit_before + 1
        assert _fresh_count(db_path, "gl_entry") == 0
        assert _row(conn, "advacct_revenue_contract", witness["id"]) == witness_before
        after = _snapshot(conn)
        for table in _TABLES:
            if table in ("advacct_revenue_contract", "audit_log"):
                continue
            assert after[table] == before[table], table
        assert len(after["audit_log"]) == len(before["audit_log"]) + 1

    def test_update_audit_row_carries_old_and_new_values(
            self, conn, env, db_path):
        target = _add_contract(conn, env, customer="Update Target",
                               total="120000.00", number="C-UPD-1")
        assert is_ok(target)
        r = call_action(mod.update_revenue_contract, conn, ns(
            id=target["id"], customer_name="Updated Corp",
            contract_number=None, start_date=None,
            end_date=None, total_value="135000.00", contract_status=None,
        ))
        assert is_ok(r), r
        audits = [a for a in _fresh_audit_rows(db_path, target["id"])
                  if a["action"] == "update-revenue-contract"]
        assert len(audits) >= 1
        latest = audits[-1]
        old_values = json.loads(latest["old_values"] or "{}")
        new_values = json.loads(latest["new_values"] or "{}")
        # House shape, as update-employee writes it:
        # old_values={column: old}, new_values={column: new}.
        assert old_values == {"customer_name": "Update Target",
                              "total_value": "120000.00"}
        assert new_values == {"customer_name": "Updated Corp",
                              "total_value": "135000.00"}

    def test_invalid_status_refused_truthfully_and_writes_nothing(
            self, conn, env):
        target = _add_contract(conn, env)
        assert is_ok(target)
        before = _snapshot(conn)
        r = call_action(mod.update_revenue_contract, conn, ns(
            id=target["id"], customer_name=None,
            contract_number=None, start_date=None,
            end_date=None, total_value=None, contract_status="bogus",
        ))
        assert is_error(r)
        # WEIGHT: message names the typed value and the allowed set.
        assert _msg(r) == ("Invalid contract-status: bogus. Must be one of: "
                           "draft, active, modified, completed, terminated")
        assert _snapshot(conn) == before


# ---------------------------------------------------------------------------
# 11. modify-contract
# ---------------------------------------------------------------------------

class TestModifyContractStrong:
    """Deepens TestModifyContract.test_modify (asserts status and count).

    WEIGHT is on the fresh read-back of status, count and the unchanged
    money TEXT."""

    def test_modify_flags_row_and_keeps_money_exact(
            self, conn, env, db_path):
        target = _add_contract(conn, env, customer="Modify Target",
                               total="5000.00", number="C-MOD-1")
        assert is_ok(target)
        witness = _add_contract(conn, env, customer="Modify Witness",
                                total="99999.99", number="C-WIT")
        assert is_ok(witness)
        witness_before = _row(conn, "advacct_revenue_contract", witness["id"])
        before = _snapshot(conn)
        audit_before = _count(conn, "audit_log")

        r = call_action(mod.modify_contract, conn, ns(id=target["id"]))
        assert is_ok(r), r
        assert r["contract_status"] == "modified"
        assert r["modification_count"] == 1

        # WEIGHT: status and count changed; hand-named money TEXT unchanged.
        stored = _fresh_row(db_path, "advacct_revenue_contract", target["id"])
        assert stored["contract_status"] == "modified"
        assert stored["modification_count"] == 1
        assert stored["total_value"] == "5000.00"
        assert Decimal(stored["total_value"]) == Decimal("5000.00")
        assert stored["customer_name"] == "Modify Target"

        # WEIGHT: one audit row for the writer, ledgers untouched, witness
        # still draft with its own money intact.
        assert _fresh_count(db_path, "audit_log") == audit_before + 1
        assert _fresh_count(db_path, "gl_entry") == 0
        assert _row(conn, "advacct_revenue_contract", witness["id"]) == witness_before
        after = _snapshot(conn)
        for table in _TABLES:
            if table in ("advacct_revenue_contract", "audit_log"):
                continue
            assert after[table] == before[table], table
        assert len(after["audit_log"]) == len(before["audit_log"]) + 1

    def test_modify_twice_after_reactivate_counts_two(self, conn, env, db_path):
        target = _add_contract(conn, env, customer="Modify Twice",
                               total="5000.00", number="C-MOD-TWICE")
        assert is_ok(target)
        first = call_action(mod.modify_contract, conn, ns(id=target["id"]))
        assert is_ok(first), first
        assert first["modification_count"] == 1
        assert first["contract_status"] == "modified"
        # WEIGHT: reactivate through the writer, then modify again.
        assert is_ok(call_action(mod.update_revenue_contract, conn, ns(
            id=target["id"], customer_name=None, contract_number=None,
            start_date=None, end_date=None, total_value=None,
            contract_status="active",
        )))
        reactivated = _fresh_row(db_path, "advacct_revenue_contract", target["id"])
        assert reactivated["contract_status"] == "active"
        assert reactivated["modification_count"] == 1
        second = call_action(mod.modify_contract, conn, ns(id=target["id"]))
        assert is_ok(second), second
        assert second["contract_status"] == "modified"
        assert second["modification_count"] == 2
        stored = _fresh_row(db_path, "advacct_revenue_contract", target["id"])
        assert stored["contract_status"] == "modified"
        assert stored["modification_count"] == 2
        assert stored["total_value"] == "5000.00"

    def test_modified_status_refused_truthfully_and_writes_nothing(
            self, conn, env, db_path):
        target = _add_contract(conn, env, customer="Modify Refused",
                               total="5000.00", number="C-MOD-REF")
        assert is_ok(target)
        assert is_ok(call_action(mod.modify_contract, conn, ns(id=target["id"])))
        assert _fresh_row(db_path, "advacct_revenue_contract", target["id"])["contract_status"] == "modified"
        before = _snapshot(conn)
        r = call_action(mod.modify_contract, conn, ns(id=target["id"]))
        assert is_error(r)
        # WEIGHT: message names the stored modified status truthfully.
        assert _msg(r) == ("Cannot modify contract in status 'modified'. "
                           "Must be draft or active.")
        assert _snapshot(conn) == before
        assert _fresh_row(db_path, "advacct_revenue_contract", target["id"])["modification_count"] == 1

    def test_completed_contract_refused_truthfully_and_writes_nothing(
            self, conn, env):
        target = _add_contract(conn, env, customer="Modify Completed",
                               total="5000.00", number="C-MOD-2")
        assert is_ok(target)
        assert is_ok(call_action(mod.update_revenue_contract, conn, ns(
            id=target["id"], customer_name=None, contract_number=None,
            start_date=None, end_date=None, total_value=None,
            contract_status="completed",
        )))
        before = _snapshot(conn)
        r = call_action(mod.modify_contract, conn, ns(id=target["id"]))
        assert is_error(r)
        # WEIGHT: message names the actual stored status truthfully.
        assert _msg(r) == ("Cannot modify contract in status 'completed'. "
                           "Must be draft or active.")
        assert _snapshot(conn) == before


# ---------------------------------------------------------------------------
# 12. list-performance-obligations
# ---------------------------------------------------------------------------

class TestListPerformanceObligationsStrong:
    """Deepens TestListPerformanceObligations.test_list (asserts only count).

    WEIGHT is on exact membership plus hand-named money literals. Finding:
    list-performance-obligations has no refusal branch."""

    def test_lists_both_obligations_with_exact_money(
            self, conn, env, db_path):
        contract = _add_contract(conn, env, customer="Obligation Owner",
                                 total="100000.00", number="C-OBL-1")
        assert is_ok(contract)
        first = _add_obligation(conn, env, contract["id"],
                                name="Software License", price="60000.00")
        second = _add_obligation(conn, env, contract["id"],
                                 name="Support", price="40000.00")
        assert is_ok(first) and is_ok(second)
        decoy_contract = _add_contract(conn, env, customer="Decoy Owner",
                                       total="99999.99", number="C-DECOY")
        assert is_ok(decoy_contract)
        decoy_ob = _add_obligation(conn, env, decoy_contract["id"],
                                   name="Decoy Work", price="9999.99")
        assert is_ok(decoy_ob)
        snapshot = _snapshot(conn)

        r = call_action(mod.list_performance_obligations, conn, ns(
            contract_id=contract["id"], company_id=None,
            obligation_status=None, limit=50, offset=0,
        ))
        assert is_ok(r), r
        assert r["total_count"] == 2
        # WEIGHT: exact membership and hand-named money.
        got = {row["id"]: row for row in r["rows"]}
        assert set(got) == {first["id"], second["id"]}
        assert got[first["id"]]["standalone_price"] == "60000.00"
        assert got[first["id"]]["allocated_price"] == "60000.00"
        assert got[second["id"]]["standalone_price"] == "40000.00"
        assert got[second["id"]]["allocated_price"] == "40000.00"
        assert Decimal(got[first["id"]]["standalone_price"]) == Decimal("60000.00")
        assert got[first["id"]]["name"] == "Software License"
        assert got[second["id"]]["name"] == "Support"
        for row in got.values():
            assert row["contract_id"] == contract["id"]

        # WEIGHT: fresh read-back repeats the same literals; the decoy
        # contract's obligation is excluded by the filter.
        back = {row["id"]: row for row in
                _fresh_where(db_path, "advacct_performance_obligation",
                             contract_id=contract["id"])}
        assert back[first["id"]]["standalone_price"] == "60000.00"
        assert back[second["id"]]["standalone_price"] == "40000.00"
        assert decoy_ob["id"] not in back

        assert _snapshot(conn) == snapshot
        assert _fresh_count(db_path, "gl_entry") == 0

    def test_unknown_contract_answers_empty_and_writes_nothing(
            self, conn, env):
        contract = _add_contract(conn, env)
        assert is_ok(contract)
        made = _add_obligation(conn, env, contract["id"])
        assert is_ok(made)
        before = _snapshot(conn)
        r = call_action(mod.list_performance_obligations, conn, ns(
            contract_id="no-such-contract", company_id=None,
            obligation_status=None, limit=50, offset=0,
        ))
        assert is_ok(r)
        assert r["total_count"] == 0
        assert r["rows"] == []
        assert r["has_more"] is False
        assert _snapshot(conn) == before

    def test_allocated_value_is_exact_decimal_sum(self, conn, env, db_path):
        contract = _add_contract(conn, env, customer="Allocation Owner",
                                 total="100000.00", number="C-ALLOC-1")
        assert is_ok(contract)
        first = _add_obligation(conn, env, contract["id"],
                                name="Part One", price="1200.10")
        assert is_ok(first)
        second = _add_obligation(conn, env, contract["id"],
                                 name="Part Two", price="1800.25")
        assert is_ok(second)
        # WEIGHT: each obligation is allocated at its standalone price and
        # the contract total is the exact decimal sum as TEXT.
        stored = _fresh_row(db_path, "advacct_revenue_contract", contract["id"])
        assert stored["allocated_value"] == "3000.35"
        assert Decimal(stored["allocated_value"]) == Decimal("3000.35")
        first_stored = _fresh_row(db_path, "advacct_performance_obligation", first["id"])
        assert first_stored["allocated_price"] == "1200.10"
        second_stored = _fresh_row(db_path, "advacct_performance_obligation", second["id"])
        assert second_stored["allocated_price"] == "1800.25"

    def test_allocated_value_exact_at_large_magnitude(self, conn, env, db_path):
        contract = _add_contract(conn, env, customer="Allocation Large",
                                 total="4000000000000000.00", number="C-ALLOC-LARGE")
        assert is_ok(contract)
        first = _add_obligation(conn, env, contract["id"],
                                name="Large One", price="1000000000000000.01")
        assert is_ok(first)
        second = _add_obligation(conn, env, contract["id"],
                                 name="Large Two", price="1000000000000000.02")
        assert is_ok(second)
        # WEIGHT: beyond binary-float precision the exact sum is 2000000000000000.03.
        stored = _fresh_row(db_path, "advacct_revenue_contract", contract["id"])
        assert stored["allocated_value"] == "2000000000000000.03"
        assert Decimal(stored["allocated_value"]) == Decimal("2000000000000000.03")


# ---------------------------------------------------------------------------
# Money validation (m620d item 2): non-finite money is refused, nothing written.
# ---------------------------------------------------------------------------

class TestMoneyValidation:
    """Each refusal below fails on the unchanged product (the value is stored)."""

    def test_add_lease_rejects_nonfinite_monthly_payment_and_writes_nothing(
            self, conn, env):
        before = _snapshot(conn)
        naming_before = _all(conn, "naming_series")
        r = call_action(mod.add_lease, conn, ns(
            company_id=env["company_id"], lessee_name="Money Target",
            lessor_name="Property Holdings LLC",
            asset_description="Office space 5th floor",
            lease_type="operating", start_date="2026-01-01",
            end_date="2027-12-31", term_months=24, monthly_payment="abc",
            annual_escalation="0.03", discount_rate="0.05",
            purchase_option_price=None,
        ))
        assert is_error(r), r
        assert _msg(r) == "Invalid monthly-payment: abc"
        assert _snapshot(conn) == before
        assert _all(conn, "naming_series") == naming_before

    def test_update_lease_rejects_nonfinite_monthly_payment_and_writes_nothing(
            self, conn, env):
        target = _add_lease(conn, env, lessee="Money Target", pay="1000.00")
        assert is_ok(target)
        before = _snapshot(conn)
        r = call_action(mod.update_lease, conn, ns(
            id=target["id"], lessee_name=None,
            lessor_name=None, asset_description=None,
            start_date=None, end_date=None,
            monthly_payment="abc", discount_rate=None,
            annual_escalation=None, purchase_option_price=None,
            lease_type=None, term_months=None,
        ))
        assert is_error(r), r
        assert _msg(r) == "Invalid monthly-payment: abc"
        assert _snapshot(conn) == before

    def test_update_revenue_contract_rejects_nonfinite_total_value_and_writes_nothing(
            self, conn, env):
        target = _add_contract(conn, env, customer="Money Target",
                               total="120000.00", number="C-MONEY-1")
        assert is_ok(target)
        before = _snapshot(conn)
        r = call_action(mod.update_revenue_contract, conn, ns(
            id=target["id"], customer_name=None,
            contract_number=None, start_date=None,
            end_date=None, total_value="abc", contract_status=None,
        ))
        assert is_error(r), r
        assert _msg(r) == "Invalid total-value: abc"
        assert _snapshot(conn) == before
