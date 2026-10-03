"""M621 strong depth for 11 accounting-adv actions.

Prior weak shape (read in the existing suites before writing this file): each
of the 11 actions had a behavioural test that asserted content alone, with no
money literal, no read-back through the seam and no pinned refusal. The
assertions that now carry the weight are the ones in this file:

- list-variable-considerations: TestStrongListVariable.test_output_matches_stored_rows
- revenue-recognition-summary: TestStrongRevenueSummary.test_periods_match_stored_schedule
- revenue-waterfall-report: TestStrongWaterfall.test_row_matches_stored_contract
- get-ic-transaction: TestStrongGetIc.test_output_matches_stored_row
- list-ic-transactions: TestStrongListIc.test_filter_returns_exact_stored_row
- update-ic-transaction: TestStrongUpdateIc.test_update_writes_exact_values
- ic-reconciliation-report: TestStrongIcRecon.test_group_totals_match_stored_rows
- list-transfer-price-rules: TestStrongListRules.test_filter_returns_exact_stored_row
- list-consolidation-groups: TestStrongListGroups.test_output_matches_stored_rows
- run-consolidation: TestStrongRunConsolidation.test_run_reports_stored_entities
- consolidation-summary: TestStrongConsolidationSummary.test_summary_matches_stored_entries

Every happy path reads back through a fresh library connection built with
PyPika, names hand-computed money literals, carries a decoy row that would
differ if the action read the wrong row or company, and asserts the snapshot
of tables that must not change (ledgers included). Every refusal path pins
the exact message and asserts the snapshot is identical afterwards. Five of
the read-only actions have no refusal branch in the handler
(list-variable-considerations, list-ic-transactions, list-transfer-price-rules,
ic-reconciliation-report, list-consolidation-groups); those pin the empty
result instead and CHANGES.md says so. revenue-waterfall-report and
revenue-recognition-summary now refuse an unknown company instead.
"""
import os
import sys

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import json

from decimal import Decimal, ROUND_HALF_UP

import pytest

from advacct_helpers import (
    build_advacct_env, call_action, is_error, is_ok, load_db_query, ns,
    seed_recognition_ledger,
)

from erpclaw_lib.db import get_connection
from erpclaw_lib.query import Field, P, Q, Table, fn
from erpclaw_lib import seam as _seam

mod = load_db_query()


@pytest.fixture
def dconn(db_path):
    conn = get_connection(db_path)
    yield conn
    conn.close()


@pytest.fixture
def denv(dconn):
    return build_advacct_env(dconn)


SNAPSHOT_TABLES = (
    "company",
    "naming_series",
    "audit_log",
    "gl_entry",
    "advacct_revenue_contract",
    "advacct_performance_obligation",
    "advacct_variable_consideration",
    "advacct_revenue_schedule",
    "advacct_lease",
    "advacct_lease_payment",
    "advacct_amortization_entry",
    "advacct_ic_transaction",
    "advacct_transfer_price_rule",
    "advacct_consolidation_group",
    "advacct_group_entity",
    "advacct_elimination_entry",
)


@pytest.fixture(autouse=True)
def _dispose_seam_engines():
    yield
    _seam.dispose_engines()


def _audit_rows(conn, entity_id):
    t_audit = Table("audit_log")
    return [dict(r) for r in conn.execute(
        Q.from_(t_audit).select("*").where(t_audit.entity_id == P()).get_sql(),
        (entity_id,)).fetchall()]


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


def _snapshot(conn):
    snap = {}
    for name in SNAPSHOT_TABLES:
        t = Table(name)
        rows = conn.execute(Q.from_(t).select("*").get_sql()).fetchall()
        snap[name] = sorted(repr(dict(r)) for r in rows)
    return snap


def _changed_tables(before, after):
    return sorted(n for n in SNAPSHOT_TABLES if before[n] != after[n])


def _row(conn, table, row_id):
    t = Table(table)
    r = conn.execute(
        Q.from_(t).select("*").where(t.id == P()).get_sql(), (row_id,)).fetchone()
    return dict(r) if r is not None else None


def _rows_where(conn, table, column, value):
    t = Table(table)
    return [dict(r) for r in conn.execute(
        Q.from_(t).select("*").where(Field(column) == P()).get_sql(),
        (value,)).fetchall()]


def _count(conn, table):
    t = Table(table)
    return conn.execute(Q.from_(t).select(fn.Count("*")).get_sql()).fetchone()[0]


def _fresh(db_path):
    return get_connection(db_path)


def _add_contract(conn, env, customer="Acme Corp", total="9000.00",
                  number="C-001", start="2026-01-01", end="2026-03-31"):
    r = call_action(mod.add_revenue_contract, conn, ns(
        company_id=env["company_id"], customer_name=customer,
        total_value=total, contract_number=number,
        start_date=start, end_date=end))
    assert is_ok(r), r
    return r


def _add_obligation(conn, env, contract_id, name="Sub", price="3000.00"):
    r = call_action(mod.add_performance_obligation, conn, ns(
        contract_id=contract_id, company_id=env["company_id"],
        name=name, standalone_price=price,
        recognition_method="over_time", recognition_basis="time"))
    assert is_ok(r), r
    return r


def _add_ic(conn, env, amount="50000.00", txn_type="sale", desc="Alpha sale",
            frm=None, to=None, method="cost_plus"):
    r = call_action(mod.add_ic_transaction, conn, ns(
        company_id=env["company_id"],
        from_company_id=frm or env["company_id"],
        to_company_id=to or env["company2_id"],
        transaction_type=txn_type, amount=amount, description=desc,
        currency="USD", transfer_price_method=method))
    assert is_ok(r), r
    return r


def _add_group(conn, env, name="Global Holdings Group", currency="USD"):
    r = call_action(mod.add_consolidation_group, conn, ns(
        company_id=env["company_id"], name=name,
        parent_company_id=env["company_id"],
        consolidation_currency=currency))
    assert is_ok(r), r
    return r


def _add_entity(conn, env, group_id, entity_company_id=None,
                entity_name="Subsidiary Inc", pct="80"):
    r = call_action(mod.add_group_entity, conn, ns(
        group_id=group_id, company_id=env["company_id"],
        entity_company_id=entity_company_id or env["company2_id"],
        entity_name=entity_name, ownership_pct=pct,
        functional_currency="USD", consolidation_method="full"))
    assert is_ok(r), r
    return r


# ---------------------------------------------------------------------------
# list-variable-considerations. No ledger effect: pure read.
# ---------------------------------------------------------------------------
class TestStrongListVariable:
    def test_output_matches_stored_rows(self, dconn, denv, db_path):
        cid = denv["company_id"]
        c1 = _add_contract(dconn, denv)
        v1 = call_action(mod.add_variable_consideration, dconn, ns(
            contract_id=c1["id"], company_id=cid,
            description="Bonus Alpha", estimated_amount="5000.00",
            constraint_amount="3000.00", method="expected_value",
            probability="0.75"))
        assert is_ok(v1), v1
        c2 = _add_contract(dconn, denv, customer="Decoy Ltd",
                           total="100.00", number="C-002")
        v2 = call_action(mod.add_variable_consideration, dconn, ns(
            contract_id=c2["id"], company_id=cid,
            description="Bonus Beta", estimated_amount="999.00",
            constraint_amount="100.00", method="most_likely",
            probability="0.50"))
        assert is_ok(v2), v2
        # WEIGHT: second-company decoy proves the company filter is applied.
        other_contract = call_action(mod.add_revenue_contract, dconn, ns(
            company_id=denv["company2_id"], customer_name="Other Co",
            total_value="777.77", contract_number="C-OTHER-VC",
            start_date="2026-01-01", end_date="2026-03-31"))
        assert is_ok(other_contract), other_contract
        other_vc = call_action(mod.add_variable_consideration, dconn, ns(
            contract_id=other_contract["id"], company_id=denv["company2_id"],
            description="Other Bonus", estimated_amount="111.11",
            constraint_amount="50.00", method="expected_value",
            probability="0.60"))
        assert is_ok(other_vc), other_vc

        before = _snapshot(dconn)
        r = call_action(mod.list_variable_considerations, dconn, ns(
            contract_id=c1["id"], company_id=cid, limit=50, offset=0))
        assert is_ok(r), r
        assert r["total_count"] == 1
        row = r["rows"][0]
        assert row["id"] == v1["id"]
        assert row["contract_id"] == c1["id"]
        assert row["description"] == "Bonus Alpha"
        assert row["estimated_amount"] == "5000.00"
        assert row["constraint_amount"] == "3000.00"
        assert row["method"] == "expected_value"
        assert row["probability"] == "0.75"

        # WEIGHT: the company filter alone isolates this company's rows: with
        # no contract filter both of this company's considerations return and
        # the other company's decoy stays out. The call above cannot prove
        # this — its contract filter already excludes the decoy.
        r_company = call_action(mod.list_variable_considerations, dconn, ns(
            contract_id=None, company_id=cid, limit=50, offset=0))
        assert is_ok(r_company), r_company
        assert r_company["total_count"] == 2
        assert {row["id"] for row in r_company["rows"]} == {v1["id"], v2["id"]}
        assert other_vc["id"] not in [row["id"] for row in r_company["rows"]]

        fresh = _fresh(db_path)
        try:
            stored = _row(fresh, "advacct_variable_consideration", v1["id"])
            assert stored["description"] == "Bonus Alpha"
            assert stored["estimated_amount"] == "5000.00"
            assert stored["constraint_amount"] == "3000.00"
            assert stored["method"] == "expected_value"
            assert stored["contract_id"] == c1["id"]
            decoy = _row(fresh, "advacct_variable_consideration", v2["id"])
            assert decoy["description"] == "Bonus Beta"
            assert decoy["estimated_amount"] == "999.00"
            assert r["total_count"] == 1
            assert other_vc["id"] not in [row["id"] for row in r["rows"]]
            other_stored = _row(fresh, "advacct_variable_consideration", other_vc["id"])
            assert other_stored["company_id"] == denv["company2_id"]
            assert other_stored["estimated_amount"] == "111.11"
            assert _snapshot(fresh) == before
        finally:
            fresh.close()

    def test_unknown_contract_returns_empty_and_writes_nothing(
            self, dconn, denv, db_path):
        c1 = _add_contract(dconn, denv)
        call_action(mod.add_variable_consideration, dconn, ns(
            contract_id=c1["id"], company_id=denv["company_id"],
            description="Bonus Alpha", estimated_amount="5000.00",
            constraint_amount=None, method=None, probability=None))
        before = _snapshot(dconn)
        r = call_action(mod.list_variable_considerations, dconn, ns(
            contract_id="no-such-contract", company_id=None,
            limit=50, offset=0))
        assert is_ok(r), r
        assert r["total_count"] == 0
        assert r["rows"] == []
        assert _snapshot(dconn) == before


# ---------------------------------------------------------------------------
# revenue-recognition-summary. No ledger effect: pure read over the schedule.
# ---------------------------------------------------------------------------
class TestStrongRevenueSummary:
    def test_periods_match_stored_schedule(self, dconn, denv, db_path):
        c = _add_contract(dconn, denv)
        ob = _add_obligation(dconn, denv, c["id"], price="3000.30")
        s = call_action(mod.calculate_revenue_schedule, dconn, ns(
            obligation_id=ob["id"]))
        assert is_ok(s), s
        assert s["total_amount"] == "3000.30"
        assert s["monthly_amount"] == "1000.10"
        dc = call_action(mod.add_revenue_contract, dconn, ns(
            company_id=denv["company2_id"], customer_name="Decoy Ltd",
            total_value="600.00", contract_number="D-1",
            start_date="2026-01-01", end_date="2026-01-31"))
        assert is_ok(dc), dc
        dob = call_action(mod.add_performance_obligation, dconn, ns(
            contract_id=dc["id"], company_id=denv["company2_id"],
            name="Decoy work", standalone_price="600.00",
            recognition_method="over_time", recognition_basis="time"))
        assert is_ok(dob), dob
        ds = call_action(mod.calculate_revenue_schedule, dconn, ns(
            obligation_id=dob["id"]))
        assert is_ok(ds), ds

        before = _snapshot(dconn)
        r = call_action(mod.revenue_recognition_summary, dconn, ns(
            company_id=denv["company_id"]))
        assert is_ok(r), r
        assert r["total_periods"] == 3
        by_period = {row["period_date"]: row for row in r["rows"]}
        assert sorted(by_period) == ["2026-01-01", "2026-02-01", "2026-03-01"]
        for period in ("2026-01-01", "2026-02-01", "2026-03-01"):
            assert by_period[period]["total_amount"] == "1000.10"
            assert by_period[period]["recognized_amount"] == "0.00"
            assert by_period[period]["unrecognized_amount"] == "1000.10"
            assert Decimal(by_period[period]["total_amount"]) == Decimal("1000.10")
            assert by_period[period]["entry_count"] == 1

        fresh = _fresh(db_path)
        try:
            sched = _rows_where(fresh, "advacct_revenue_schedule",
                                "company_id", denv["company_id"])
            assert len(sched) == 3
            assert sorted(x["amount"] for x in sched) == ["1000.10"] * 3
            assert sum(Decimal(x["amount"]) for x in sched) == Decimal("3000.30")
            decoy_sched = _rows_where(fresh, "advacct_revenue_schedule",
                                      "company_id", denv["company2_id"])
            assert len(decoy_sched) == 1
            assert decoy_sched[0]["amount"] == "600.00"
            assert _snapshot(fresh) == before
        finally:
            fresh.close()

    def test_recognize_one_period_reports_recognized_sum(self, dconn, denv, db_path):
        c = _add_contract(dconn, denv)
        ob = _add_obligation(dconn, denv, c["id"], price="3000.30")
        s = call_action(mod.calculate_revenue_schedule, dconn, ns(
            obligation_id=ob["id"]))
        assert is_ok(s), s
        fresh = _fresh(db_path)
        try:
            sched = _rows_where(fresh, "advacct_revenue_schedule",
                                "company_id", denv["company_id"])
            jan = [x for x in sched if x["period_date"] == "2026-01-01"]
            assert len(jan) == 1
            first_id = jan[0]["id"]
        finally:
            fresh.close()
        ledger = seed_recognition_ledger(dconn, denv["company_id"])
        rec = call_action(mod.recognize_schedule_entry, dconn, ns(
            id=first_id,
            deferred_revenue_account_id=ledger["deferred_revenue_account_id"],
            revenue_account_id=ledger["revenue_account_id"],
            cost_center_id=ledger["cost_center_id"]))
        assert is_ok(rec), rec
        legs = _rows_where(dconn, "gl_entry", "voucher_id", first_id)
        assert len(legs) == 2
        by_acct = {leg["account_id"]: leg for leg in legs}
        assert by_acct[ledger["deferred_revenue_account_id"]]["voucher_type"] == \
            "revenue_recognition"
        assert (by_acct[ledger["deferred_revenue_account_id"]]["debit"],
                by_acct[ledger["deferred_revenue_account_id"]]["credit"]) == \
            ("1000.10", "0.00")
        assert (by_acct[ledger["revenue_account_id"]]["debit"],
                by_acct[ledger["revenue_account_id"]]["credit"]) == \
            ("0.00", "1000.10")
        r = call_action(mod.revenue_recognition_summary, dconn, ns(
            company_id=denv["company_id"]))
        assert is_ok(r), r
        by_period = {row["period_date"]: row for row in r["rows"]}
        recognized_total = sum((Decimal(row["recognized_amount"]) for row in r["rows"]), Decimal("0"))
        assert recognized_total == Decimal("1000.10")
        assert by_period["2026-01-01"]["recognized_amount"] == "1000.10"
        assert by_period["2026-01-01"]["unrecognized_amount"] == "0.00"
        assert by_period["2026-01-01"]["total_amount"] == "1000.10"

    def test_unknown_company_refuses_and_writes_nothing(
            self, dconn, denv, db_path):
        c = _add_contract(dconn, denv)
        ob = _add_obligation(dconn, denv, c["id"], price="3000.00")
        call_action(mod.calculate_revenue_schedule, dconn, ns(
            obligation_id=ob["id"]))
        before = _snapshot(dconn)
        r = call_action(mod.revenue_recognition_summary, dconn, ns(
            company_id="00000000-0000-0000-0000-000000000000"))
        assert is_error(r), r
        assert r["error"] == ("Company not found: "
                              "00000000-0000-0000-0000-000000000000")
        assert _snapshot(dconn) == before


# ---------------------------------------------------------------------------
# revenue-waterfall-report. No ledger effect: pure read over contracts.
# ---------------------------------------------------------------------------
class TestStrongWaterfall:
    def test_row_matches_stored_contract(self, dconn, denv, db_path):
        c = _add_contract(dconn, denv, customer="Acme Corp", total="9000.00")
        _add_obligation(dconn, denv, c["id"], price="3000.00")
        _add_contract(dconn, denv, customer="Decoy Ltd", total="111.11",
                      number="C-002")
        other = call_action(mod.add_revenue_contract, dconn, ns(
            company_id=denv["company2_id"], customer_name="Other Co",
            total_value="7777.77", contract_number="C-OTHER-WF",
            start_date="2026-01-01", end_date="2026-03-31"))
        assert is_ok(other), other

        before = _snapshot(dconn)
        r = call_action(mod.revenue_waterfall_report, dconn, ns(
            company_id=denv["company_id"]))
        assert is_ok(r), r
        assert r["total_contracts"] == 2
        by_id = {row["contract_id"]: row for row in r["rows"]}
        mine = by_id[c["id"]]
        assert mine["customer_name"] == "Acme Corp"
        assert mine["total_value"] == "9000.00"
        assert mine["allocated_value"] == "3000.00"
        assert mine["contract_status"] == "draft"
        assert mine["obligation_count"] == 1
        assert mine["satisfied_count"] == 0

        fresh = _fresh(db_path)
        try:
            stored = _row(fresh, "advacct_revenue_contract", c["id"])
            assert stored["customer_name"] == "Acme Corp"
            assert stored["total_value"] == "9000.00"
            assert stored["allocated_value"] == "3000.00"
            assert other["id"] not in by_id
            other_stored = _row(fresh, "advacct_revenue_contract", other["id"])
            assert other_stored["company_id"] == denv["company2_id"]
            assert _snapshot(fresh) == before
        finally:
            fresh.close()

    def test_unknown_company_refuses_and_writes_nothing(
            self, dconn, denv, db_path):
        _add_contract(dconn, denv)
        before = _snapshot(dconn)
        r = call_action(mod.revenue_waterfall_report, dconn, ns(
            company_id="00000000-0000-0000-0000-000000000000"))
        assert is_error(r), r
        assert r["error"] == ("Company not found: "
                              "00000000-0000-0000-0000-000000000000")
        assert _snapshot(dconn) == before


# ---------------------------------------------------------------------------
# get-ic-transaction. No ledger effect: pure read.
# ---------------------------------------------------------------------------
class TestStrongGetIc:
    def test_output_matches_stored_row(self, dconn, denv, db_path):
        ic1 = _add_ic(dconn, denv, amount="50000.00", txn_type="sale",
                      desc="Alpha sale")
        _add_ic(dconn, denv, amount="7000.00", txn_type="service",
                desc="Beta service", frm=denv["company2_id"],
                to=denv["company_id"], method=None)

        before = _snapshot(dconn)
        r = call_action(mod.get_ic_transaction, dconn, ns(id=ic1["id"]))
        assert is_ok(r), r
        assert r["id"] == ic1["id"]
        assert r["from_company_id"] == denv["company_id"]
        assert r["to_company_id"] == denv["company2_id"]
        assert r["transaction_type"] == "sale"
        assert r["amount"] == "50000.00"
        assert r["description"] == "Alpha sale"
        assert r["ic_status"] == "draft"

        fresh = _fresh(db_path)
        try:
            stored = _row(fresh, "advacct_ic_transaction", ic1["id"])
            assert stored["amount"] == "50000.00"
            assert stored["transaction_type"] == "sale"
            assert stored["description"] == "Alpha sale"
            assert stored["from_company_id"] == denv["company_id"]
            assert stored["to_company_id"] == denv["company2_id"]
            assert _snapshot(fresh) == before
        finally:
            fresh.close()

    def test_unknown_id_refused_and_writes_nothing(self, dconn, denv, db_path):
        _add_ic(dconn, denv)
        before = _snapshot(dconn)
        r = call_action(mod.get_ic_transaction, dconn, ns(id="no-such-ic"))
        assert is_error(r)
        assert r["message"] == "IC transaction no-such-ic not found"
        assert _snapshot(dconn) == before


# ---------------------------------------------------------------------------
# list-ic-transactions. No ledger effect: pure read.
# ---------------------------------------------------------------------------
class TestStrongListIc:
    def test_filter_returns_exact_stored_row(self, dconn, denv, db_path):
        ic1 = _add_ic(dconn, denv, amount="50000.00", txn_type="sale",
                      desc="Alpha sale")
        _add_ic(dconn, denv, amount="7000.00", txn_type="service",
                desc="Beta service")
        other_ic = call_action(mod.add_ic_transaction, dconn, ns(
            company_id=denv["company2_id"],
            from_company_id=denv["company2_id"],
            to_company_id=denv["company_id"],
            transaction_type="sale", amount="9999.99", description="Other sale",
            currency="USD", transfer_price_method="cost_plus"))
        assert is_ok(other_ic), other_ic

        before = _snapshot(dconn)
        r = call_action(mod.list_ic_transactions, dconn, ns(
            company_id=denv["company_id"], from_company_id=None,
            to_company_id=None, transaction_type="sale",
            ic_status=None, search=None, limit=50, offset=0))
        assert is_ok(r), r
        assert r["total_count"] == 1
        row = r["rows"][0]
        assert row["id"] == ic1["id"]
        assert row["transaction_type"] == "sale"
        assert row["amount"] == "50000.00"
        assert row["description"] == "Alpha sale"

        fresh = _fresh(db_path)
        try:
            stored = _row(fresh, "advacct_ic_transaction", ic1["id"])
            assert stored["amount"] == "50000.00"
            assert stored["transaction_type"] == "sale"
            assert other_ic["id"] not in [row["id"] for row in r["rows"]]
            other_stored = _row(fresh, "advacct_ic_transaction", other_ic["id"])
            assert other_stored["company_id"] == denv["company2_id"]
            assert _snapshot(fresh) == before
        finally:
            fresh.close()

    def test_absent_type_returns_empty_and_writes_nothing(
            self, dconn, denv, db_path):
        _add_ic(dconn, denv)
        before = _snapshot(dconn)
        r = call_action(mod.list_ic_transactions, dconn, ns(
            company_id=denv["company_id"], from_company_id=None,
            to_company_id=None, transaction_type="dividend",
            ic_status=None, search=None, limit=50, offset=0))
        assert is_ok(r), r
        assert r["total_count"] == 0
        assert r["rows"] == []
        assert _snapshot(dconn) == before


# ---------------------------------------------------------------------------
# update-ic-transaction. Writes its own row plus one audit line; the ledgers
# and every other vertical must not move.
# ---------------------------------------------------------------------------
class TestStrongUpdateIc:
    def test_update_writes_exact_values(self, dconn, denv, db_path):
        ic1 = _add_ic(dconn, denv, amount="50000.00", desc="Alpha sale")
        ic2 = _add_ic(dconn, denv, amount="7000.00", txn_type="service",
                      desc="Beta service")
        target_before = _row(dconn, "advacct_ic_transaction", ic1["id"])
        gl_before = _count(dconn, "gl_entry")
        audit_before = _count(dconn, "audit_log")
        before = _snapshot(dconn)

        r = call_action(mod.update_ic_transaction, dconn, ns(
            id=ic1["id"], description="Updated note", amount="52000.00",
            currency=None, transaction_type=None,
            transfer_price_method=None))
        assert is_ok(r), r
        assert r["updated_fields"] == ["description", "amount"]

        fresh = _fresh(db_path)
        try:
            stored = _row(fresh, "advacct_ic_transaction", ic1["id"])
            assert stored["description"] == "Updated note"
            assert stored["amount"] == "52000.00"
            assert stored["ic_status"] == "draft"
            assert stored["from_company_id"] == denv["company_id"]
            assert stored["to_company_id"] == denv["company2_id"]
            # WEIGHT: the whole row moved only in the allowed columns.
            _assert_whole_row_only_allowed_changed(
                target_before, stored,
                {"description", "amount", "updated_at"},
                "advacct_ic_transaction")
            # WEIGHT: the audit row names the action, the entity and the change.
            audits = [a for a in _fresh_audit_rows(db_path, ic1["id"]) if a["action"] == "update-ic-transaction"]
            assert len(audits) >= 1
            latest = audits[-1]
            assert latest["action"] == "update-ic-transaction"
            assert latest["entity_id"] == ic1["id"]
            assert latest["entity_type"] == "advacct_ic_transaction"
            old_values = json.loads(latest["old_values"])
            assert old_values == {"description": "Alpha sale",
                                  "amount": "50000.00"}
            parsed = json.loads(latest["new_values"])
            assert parsed == {"description": "Updated note",
                              "amount": "52000.00"}
            untouched = _row(fresh, "advacct_ic_transaction", ic2["id"])
            assert untouched["amount"] == "7000.00"
            assert untouched["description"] == "Beta service"
            assert _count(fresh, "gl_entry") == gl_before
            assert _count(fresh, "audit_log") == audit_before + 1
            after = _snapshot(fresh)
            assert _changed_tables(before, after) == [
                "advacct_ic_transaction", "audit_log"]
        finally:
            fresh.close()

    def test_update_audit_row_carries_old_and_new_values(
            self, dconn, denv, db_path):
        ic1 = _add_ic(dconn, denv, amount="50000.00", desc="Alpha sale")
        r = call_action(mod.update_ic_transaction, dconn, ns(
            id=ic1["id"], description="Updated note", amount="52000.00",
            currency=None, transaction_type=None,
            transfer_price_method=None))
        assert is_ok(r), r
        audits = [a for a in _fresh_audit_rows(db_path, ic1["id"])
                  if a["action"] == "update-ic-transaction"]
        assert len(audits) >= 1
        latest = audits[-1]
        old_values = json.loads(latest["old_values"] or "{}")
        new_values = json.loads(latest["new_values"] or "{}")
        # House shape, as update-employee writes it:
        # old_values={column: old}, new_values={column: new}.
        assert old_values == {"description": "Alpha sale",
                              "amount": "50000.00"}
        assert new_values == {"description": "Updated note",
                              "amount": "52000.00"}

    def test_update_posted_refused_and_writes_nothing(
            self, dconn, denv, db_path):
        ic1 = _add_ic(dconn, denv)
        call_action(mod.approve_ic_transaction, dconn, ns(id=ic1["id"]))
        call_action(mod.post_ic_transaction, dconn, ns(id=ic1["id"]))
        before = _snapshot(dconn)
        gl_before = _count(dconn, "gl_entry")
        r = call_action(mod.update_ic_transaction, dconn, ns(
            id=ic1["id"], description="Try update", amount=None,
            currency=None, transaction_type=None,
            transfer_price_method=None))
        assert is_error(r)
        assert r["message"] == "Cannot update IC transaction in status 'posted'. Must be draft or pending_approval."
        assert _snapshot(dconn) == before
        assert _count(dconn, "gl_entry") == gl_before
        fresh = _fresh(db_path)
        try:
            stored = _row(fresh, "advacct_ic_transaction", ic1["id"])
            assert stored["description"] == "Alpha sale"
            assert stored["amount"] == "50000.00"
        finally:
            fresh.close()

    def test_unknown_id_refused_truthfully_and_writes_nothing(
            self, dconn, denv, db_path):
        _add_ic(dconn, denv)
        before = _snapshot(dconn)
        r = call_action(mod.update_ic_transaction, dconn, ns(
            id="no-such-ic", description="Try update", amount=None,
            currency=None, transaction_type=None,
            transfer_price_method=None))
        assert is_error(r)
        assert r["message"] == "IC transaction no-such-ic not found"
        assert _snapshot(dconn) == before


# ---------------------------------------------------------------------------
# ic-reconciliation-report. No ledger effect: pure read with grouping.
# ---------------------------------------------------------------------------
class TestStrongIcRecon:
    def test_group_totals_match_stored_rows(self, dconn, denv, db_path):
        _add_ic(dconn, denv, amount="1000.10", txn_type="sale",
                desc="Alpha one")
        _add_ic(dconn, denv, amount="2000.20", txn_type="sale",
                desc="Alpha two")
        _add_ic(dconn, denv, amount="7000.00", txn_type="service",
                desc="Beta service", frm=denv["company2_id"],
                to=denv["company_id"], method=None)

        before = _snapshot(dconn)
        r = call_action(mod.ic_reconciliation_report, dconn, ns(
            company_id=denv["company_id"]))
        assert is_ok(r), r
        key = (denv["company_id"], denv["company2_id"], "sale", "draft")
        groups = {(g["from_company_id"], g["to_company_id"],
                   g["transaction_type"], g["ic_status"]): g
                  for g in r["rows"]}
        assert key in groups
        assert groups[key]["transaction_count"] == 2
        assert groups[key]["total_amount"] == "3000.30"
        assert Decimal(groups[key]["total_amount"]) == Decimal("3000.30")

        fresh = _fresh(db_path)
        try:
            rows = _rows_where(fresh, "advacct_ic_transaction",
                               "company_id", denv["company_id"])
            sale_amounts = sorted(
                x["amount"] for x in rows if x["transaction_type"] == "sale")
            assert sale_amounts == ["1000.10", "2000.20"]
            assert sum(Decimal(x) for x in sale_amounts) == Decimal("3000.30")
            assert _snapshot(fresh) == before
        finally:
            fresh.close()

    def test_unknown_company_returns_empty_and_writes_nothing(
            self, dconn, denv, db_path):
        _add_ic(dconn, denv)
        before = _snapshot(dconn)
        r = call_action(mod.ic_reconciliation_report, dconn, ns(
            company_id="00000000-0000-0000-0000-000000000000"))
        assert is_ok(r), r
        assert r["rows"] == []
        assert _snapshot(dconn) == before


# ---------------------------------------------------------------------------
# list-transfer-price-rules. No ledger effect: pure read.
# ---------------------------------------------------------------------------
class TestStrongListRules:
    def test_filter_returns_exact_stored_row(self, dconn, denv, db_path):
        rule = call_action(mod.add_transfer_price_rule, dconn, ns(
            company_id=denv["company_id"],
            from_company_id=denv["company_id"],
            to_company_id=denv["company2_id"],
            transaction_type="sale", method="cost_plus",
            markup_pct="15.00", effective_date="2026-01-01",
            expiry_date="2026-12-31"))
        assert is_ok(rule), rule
        decoy = call_action(mod.add_transfer_price_rule, dconn, ns(
            company_id=denv["company_id"],
            from_company_id=None, to_company_id=None,
            transaction_type="service", method="resale_minus",
            markup_pct="5.00", effective_date=None, expiry_date=None))
        assert is_ok(decoy), decoy
        other_rule = call_action(mod.add_transfer_price_rule, dconn, ns(
            company_id=denv["company2_id"],
            from_company_id=denv["company2_id"],
            to_company_id=denv["company_id"],
            transaction_type="sale", method="cost_plus",
            markup_pct="15.00", effective_date="2026-01-01",
            expiry_date="2026-12-31"))
        assert is_ok(other_rule), other_rule

        before = _snapshot(dconn)
        r = call_action(mod.list_transfer_price_rules, dconn, ns(
            company_id=denv["company_id"], from_company_id=None,
            to_company_id=None, transaction_type="sale",
            limit=50, offset=0))
        assert is_ok(r), r
        assert r["total_count"] == 1
        row = r["rows"][0]
        assert row["id"] == rule["id"]
        assert row["method"] == "cost_plus"
        assert row["markup_pct"] == "15.00"
        assert row["transaction_type"] == "sale"

        fresh = _fresh(db_path)
        try:
            stored = _row(fresh, "advacct_transfer_price_rule", rule["id"])
            assert stored["method"] == "cost_plus"
            assert stored["markup_pct"] == "15.00"
            assert stored["transaction_type"] == "sale"
            assert other_rule["id"] not in [row["id"] for row in r["rows"]]
            other_stored = _row(fresh, "advacct_transfer_price_rule", other_rule["id"])
            assert other_stored["company_id"] == denv["company2_id"]
            assert _snapshot(fresh) == before
        finally:
            fresh.close()

    def test_absent_type_returns_empty_and_writes_nothing(
            self, dconn, denv, db_path):
        call_action(mod.add_transfer_price_rule, dconn, ns(
            company_id=denv["company_id"],
            from_company_id=None, to_company_id=None,
            transaction_type="sale", method="cost_plus",
            markup_pct="15.00", effective_date=None, expiry_date=None))
        before = _snapshot(dconn)
        r = call_action(mod.list_transfer_price_rules, dconn, ns(
            company_id=denv["company_id"], from_company_id=None,
            to_company_id=None, transaction_type="dividend",
            limit=50, offset=0))
        assert is_ok(r), r
        assert r["total_count"] == 0
        assert r["rows"] == []
        assert _snapshot(dconn) == before


# ---------------------------------------------------------------------------
# list-consolidation-groups. Group rows carry no money; the rate-like
# ownership figures are pinned on the run-consolidation path instead.
# ---------------------------------------------------------------------------
class TestStrongListGroups:
    def test_output_matches_stored_rows(self, dconn, denv, db_path):
        g1 = _add_group(dconn, denv, name="Global Holdings Group",
                        currency="USD")
        _add_group(dconn, denv, name="Other Group", currency="EUR")
        other_group = call_action(mod.add_consolidation_group, dconn, ns(
            company_id=denv["company2_id"], name="Second Company Group",
            parent_company_id=denv["company2_id"],
            consolidation_currency="USD"))
        assert is_ok(other_group), other_group

        before = _snapshot(dconn)
        r = call_action(mod.list_consolidation_groups, dconn, ns(
            company_id=denv["company_id"], group_status=None,
            search=None, limit=50, offset=0))
        assert is_ok(r), r
        assert r["total_count"] == 2
        by_id = {g["id"]: g for g in r["rows"]}
        assert by_id[g1["id"]]["name"] == "Global Holdings Group"
        assert by_id[g1["id"]]["consolidation_currency"] == "USD"
        assert by_id[g1["id"]]["group_status"] == "active"
        assert by_id[g1["id"]]["company_id"] == denv["company_id"]

        fresh = _fresh(db_path)
        try:
            stored = _row(fresh, "advacct_consolidation_group", g1["id"])
            assert stored["name"] == "Global Holdings Group"
            assert stored["consolidation_currency"] == "USD"
            assert stored["group_status"] == "active"
            assert other_group["id"] not in by_id
            other_stored = _row(fresh, "advacct_consolidation_group", other_group["id"])
            assert other_stored["company_id"] == denv["company2_id"]
            assert _snapshot(fresh) == before
        finally:
            fresh.close()

    def test_absent_search_returns_empty_and_writes_nothing(
            self, dconn, denv, db_path):
        _add_group(dconn, denv)
        before = _snapshot(dconn)
        r = call_action(mod.list_consolidation_groups, dconn, ns(
            company_id=denv["company_id"], group_status=None,
            search="no-such-group-xyz", limit=50, offset=0))
        assert is_ok(r), r
        assert r["total_count"] == 0
        assert r["rows"] == []
        assert _snapshot(dconn) == before


# ---------------------------------------------------------------------------
# run-consolidation. Writes one audit line and nothing else; the ledgers and
# every vertical must not move.
# ---------------------------------------------------------------------------
class TestStrongRunConsolidation:
    def test_run_reports_stored_entities(self, dconn, denv, db_path):
        g = _add_group(dconn, denv)
        _add_entity(dconn, denv, g["id"], denv["company_id"],
                    "Parent Corp", "100")
        _add_entity(dconn, denv, g["id"], denv["company2_id"],
                    "Subsidiary Inc", "80")
        gl_before = _count(dconn, "gl_entry")
        elim_before = _count(dconn, "advacct_elimination_entry")
        audit_before = _count(dconn, "audit_log")
        before = _snapshot(dconn)

        r = call_action(mod.run_consolidation, dconn, ns(
            group_id=g["id"], period_date="2026-06-30"))
        assert is_ok(r), r
        assert r["entity_count"] == 2
        assert r["group_name"] == "Global Holdings Group"
        assert r["period_date"] == "2026-06-30"
        assert r["consolidation_run"] == "completed"
        by_name = {e["entity_name"]: e for e in r["entities"]}
        assert by_name["Parent Corp"]["ownership_pct"] == "100"
        assert by_name["Subsidiary Inc"]["ownership_pct"] == "80"

        fresh = _fresh(db_path)
        try:
            stored = _row(fresh, "advacct_consolidation_group", g["id"])
            assert stored["name"] == "Global Holdings Group"
            assert stored["group_status"] == "active"
            assert stored["consolidation_currency"] == "USD"
            ents = _rows_where(fresh, "advacct_group_entity",
                               "group_id", g["id"])
            assert sorted(x["entity_name"] for x in ents) == [
                "Parent Corp", "Subsidiary Inc"]
            assert _count(fresh, "gl_entry") == gl_before
            assert _count(fresh, "advacct_elimination_entry") == elim_before
            assert _count(fresh, "audit_log") == audit_before + 1
            assert _changed_tables(before, _snapshot(fresh)) == ["audit_log"]
            # WEIGHT: the single audit row names the action and the group.
            audits = [a for a in _fresh_audit_rows(db_path, g["id"]) if a["action"] == "run-consolidation"]
            assert len(audits) >= 1
            latest = audits[-1]
            assert latest["action"] == "run-consolidation"
            assert latest["entity_id"] == g["id"]
            assert latest["entity_type"] == "advacct_consolidation_group"
            parsed = json.loads(latest["new_values"])
            assert parsed["period_date"] == "2026-06-30"
            assert parsed["entity_count"] == 2
        finally:
            fresh.close()

    def test_missing_period_refused_and_writes_nothing(
            self, dconn, denv, db_path):
        g = _add_group(dconn, denv)
        _add_entity(dconn, denv, g["id"], denv["company_id"],
                    "Parent Corp", "100")
        _add_entity(dconn, denv, g["id"], denv["company2_id"],
                    "Subsidiary Inc", "80")
        before = _snapshot(dconn)
        r = call_action(mod.run_consolidation, dconn, ns(
            group_id=g["id"], period_date=None))
        assert is_error(r)
        assert r["message"] == "--period-date is required"
        assert _snapshot(dconn) == before


# ---------------------------------------------------------------------------
# consolidation-summary. No ledger effect: pure read over stored entries.
# ---------------------------------------------------------------------------
class TestStrongConsolidationSummary:
    def test_summary_matches_stored_entries(self, dconn, denv, db_path):
        g = _add_group(dconn, denv)
        _add_entity(dconn, denv, g["id"], denv["company_id"],
                    "Parent Corp", "100")
        _add_entity(dconn, denv, g["id"], denv["company2_id"],
                    "Subsidiary Inc", "80")
        first = call_action(mod.add_currency_translation, dconn, ns(
            group_id=g["id"], company_id=denv["company_id"],
            period_date="2026-06-30", amount="1000.10",
            debit_account=None, credit_account=None, description=None))
        assert is_ok(first), first
        second = call_action(mod.add_currency_translation, dconn, ns(
            group_id=g["id"], company_id=denv["company_id"],
            period_date="2026-06-30", amount="2000.20",
            debit_account=None, credit_account=None, description=None))
        assert is_ok(second), second
        other = _add_group(dconn, denv, name="Other Group", currency="EUR")

        before = _snapshot(dconn)
        r = call_action(mod.consolidation_summary, dconn, ns(group_id=g["id"]))
        assert is_ok(r), r
        assert r["group_name"] == "Global Holdings Group"
        assert r["group_status"] == "active"
        assert r["consolidation_currency"] == "USD"
        assert r["entity_count"] == 2
        assert r["elimination_count"] == 2
        assert r["eliminations_by_type"]["currency_translation"]["count"] == 2
        assert r["eliminations_by_type"]["currency_translation"]["total"] == "3000.30"
        assert Decimal(r["eliminations_by_type"]["currency_translation"]["total"]) == Decimal("3000.30")

        fresh = _fresh(db_path)
        try:
            entries = _rows_where(fresh, "advacct_elimination_entry",
                                  "group_id", g["id"])
            assert len(entries) == 2
            assert sorted(x["amount"] for x in entries) == ["1000.10", "2000.20"]
            assert all(x["entry_type"] == "currency_translation" for x in entries)
            assert sum((Decimal(x["amount"]) for x in entries), Decimal("0")) == Decimal("3000.30")
            assert _rows_where(fresh, "advacct_elimination_entry",
                               "group_id", other["id"]) == []
            assert _snapshot(fresh) == before
        finally:
            fresh.close()

    def test_summary_exact_at_large_magnitude(self, dconn, denv, db_path):
        g = _add_group(dconn, denv)
        _add_entity(dconn, denv, g["id"], denv["company_id"],
                    "Parent Corp", "100")
        _add_entity(dconn, denv, g["id"], denv["company2_id"],
                    "Subsidiary Inc", "80")
        first = call_action(mod.add_currency_translation, dconn, ns(
            group_id=g["id"], company_id=denv["company_id"],
            period_date="2026-06-30", amount="1000000000000000.01",
            debit_account=None, credit_account=None, description=None))
        assert is_ok(first), first
        second = call_action(mod.add_currency_translation, dconn, ns(
            group_id=g["id"], company_id=denv["company_id"],
            period_date="2026-06-30", amount="1000000000000000.02",
            debit_account=None, credit_account=None, description=None))
        assert is_ok(second), second
        r = call_action(mod.consolidation_summary, dconn, ns(group_id=g["id"]))
        assert is_ok(r), r
        # WEIGHT: beyond binary-float precision the exact sum is 2000000000000000.03.
        assert r["eliminations_by_type"]["currency_translation"]["count"] == 2
        assert r["eliminations_by_type"]["currency_translation"]["total"] == "2000000000000000.03"

    def test_summary_half_cent_rounds_half_up(self, dconn, denv, db_path):
        g = _add_group(dconn, denv)
        _add_entity(dconn, denv, g["id"], denv["company_id"],
                    "Parent Corp", "100")
        _add_entity(dconn, denv, g["id"], denv["company2_id"],
                    "Subsidiary Inc", "80")
        first = call_action(mod.add_currency_translation, dconn, ns(
            group_id=g["id"], company_id=denv["company_id"],
            period_date="2026-06-30", amount="0.005",
            debit_account=None, credit_account=None, description=None))
        assert is_ok(first), first
        second = call_action(mod.add_currency_translation, dconn, ns(
            group_id=g["id"], company_id=denv["company_id"],
            period_date="2026-06-30", amount="0.000",
            debit_account=None, credit_account=None, description=None))
        assert is_ok(second), second
        r = call_action(mod.consolidation_summary, dconn, ns(group_id=g["id"]))
        assert is_ok(r), r
        # WEIGHT: 0.005 + 0.000 = 0.005, which quantizes to 0.01 under
        # ROUND_HALF_UP and to 0.00 under the context default (half-even).
        # The product pins half-up here.
        assert r["eliminations_by_type"]["currency_translation"]["count"] == 2
        assert r["eliminations_by_type"]["currency_translation"]["total"] == "0.01"
        assert Decimal(r["eliminations_by_type"]["currency_translation"]["total"]) == Decimal("0.01")

    def test_unknown_group_refused_and_writes_nothing(
            self, dconn, denv, db_path):
        g = _add_group(dconn, denv)
        _add_entity(dconn, denv, g["id"], denv["company_id"],
                    "Parent Corp", "100")
        _add_entity(dconn, denv, g["id"], denv["company2_id"],
                    "Subsidiary Inc", "80")
        before = _snapshot(dconn)
        r = call_action(mod.consolidation_summary, dconn, ns(
            group_id="no-such-group"))
        assert is_error(r)
        assert r["message"] == "Consolidation group no-such-group not found"
        assert _snapshot(dconn) == before


# ---------------------------------------------------------------------------
# Money validation (m620d item 2): non-finite money is refused, nothing written.
# ---------------------------------------------------------------------------

class TestMoneyValidation:
    """Each refusal below fails on the unchanged product (the value is stored)."""

    def test_add_currency_translation_rejects_nonfinite_amount_and_writes_nothing(
            self, dconn, denv):
        group = _add_group(dconn, denv)
        before = _snapshot(dconn)
        r = call_action(mod.add_currency_translation, dconn, ns(
            group_id=group["id"], company_id=denv["company_id"],
            period_date="2026-06-30", amount="abc",
            debit_account=None, credit_account=None, description=None))
        assert is_error(r), r
        assert r["message"] == "Invalid amount: abc"
        assert _snapshot(dconn) == before

    def test_update_ic_transaction_rejects_bad_amount_and_writes_nothing(
            self, dconn, denv):
        ic1 = _add_ic(dconn, denv, amount="50000.00", desc="Alpha sale")
        assert is_ok(ic1)
        before = _snapshot(dconn)
        r = call_action(mod.update_ic_transaction, dconn, ns(
            id=ic1["id"], description=None, amount="abc",
            currency=None, transaction_type=None,
            transfer_price_method=None))
        assert is_error(r), r
        assert r["message"] == "Invalid amount: abc"
        assert _snapshot(dconn) == before

        r = call_action(mod.update_ic_transaction, dconn, ns(
            id=ic1["id"], description=None, amount="0",
            currency=None, transaction_type=None,
            transfer_price_method=None))
        assert is_error(r), r
        assert r["message"] == "Amount must be greater than zero"
        assert _snapshot(dconn) == before
