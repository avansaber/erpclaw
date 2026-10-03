"""PostgreSQL leg for the m620c accounting-adv follow-ups (item 6).

Re-runs a fixed subset of the SQLite strong-depth cases against a live
PostgreSQL backend:

- disclosure: test_disclosure_totals_are_hand_computed
- recognition: test_periods_match_stored_schedule and
  test_recognize_one_period_reports_recognized_sum
- reconciliation: test_group_totals_match_stored_rows
- consolidation summary: test_summary_matches_stored_entries,
  test_summary_exact_at_large_magnitude and the half-cent case
- allocation: test_allocated_value_is_exact_decimal_sum and
  test_allocated_value_exact_at_large_magnitude

Seeds and hand-computed expected strings match the SQLite originals
exactly. The CASE-inside-exact-sum form in the recognition summary has
only ever run on SQLite; this module is its first PostgreSQL run.

Isolation: the fixture refuses unless current_database() on the live
connection is the database the URL names, then drops and recreates the
public schema and provisions the foundation schema through its installer
with no path. The two companies and their naming series are seeded with
plain parameterised inserts (naming rows via insert_or_ignore); every
other row is seeded through the owning module's own actions, except the
recognition ledger (fiscal year, cost center, two accounts), which
seed_recognition_ledger seeds with parameterized PyPika inserts. Reads go
through erpclaw_lib.db.get_connection() with PyPika-built queries, and
money is compared as exact TEXT, never float.

Requires ERPCLAW_PG_TEST_URL. Skips only when that is unset.
"""
import importlib.util
import json
import os
import sys
import uuid
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlparse

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import pytest

from advacct_helpers import (
    call_action, ns, is_error, is_ok, load_db_query, seed_recognition_ledger,
)

SETUP_DIR = os.path.join(os.path.dirname(os.path.dirname(_TESTS_DIR)), "erpclaw-setup")
_IN_TREE_LIB = os.path.join(SETUP_DIR, "lib")
if _IN_TREE_LIB not in sys.path:
    import importlib as _il
    if _il.util.find_spec("erpclaw_lib") is None:
        sys.path.insert(0, _IN_TREE_LIB)

from erpclaw_lib.db import get_connection
from erpclaw_lib.query import Field, P, Q, Table, fn, insert_or_ignore
from erpclaw_lib import seam as _seam

mod = load_db_query()

INIT_SCHEMA_PATH = os.path.join(SETUP_DIR, "init_schema.py")


def _load_init_schema():
    spec = importlib.util.spec_from_file_location("init_schema_pg_leg", INIT_SCHEMA_PATH)
    schema_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(schema_mod)
    return schema_mod


@pytest.fixture(autouse=True)
def _dispose_seam_engines():
    yield
    _seam.dispose_engines()


@pytest.fixture
def pg_pair(monkeypatch):
    """Live PostgreSQL connection plus a two-company env, reset per test."""
    pg_url = os.environ.get("ERPCLAW_PG_TEST_URL")
    if not pg_url:
        pytest.skip("ERPCLAW_PG_TEST_URL not set (live PostgreSQL required)")
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", pg_url)
    if "ERPCLAW_DB_PATH" in os.environ:
        monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    expected_db = urlparse(pg_url).path.strip("/")
    if not expected_db:
        raise RuntimeError("refusing to reset: ERPCLAW_PG_TEST_URL names no database")
    setup_conn = get_connection()
    try:
        resolved_db = setup_conn.execute("SELECT current_database()").fetchone()[0]
        if resolved_db != expected_db:
            raise RuntimeError(
                "refusing to reset: ERPCLAW_PG_TEST_URL names database %r "
                "but the connection resolved to %r" % (expected_db, resolved_db))
        setup_conn.execute("DROP SCHEMA public CASCADE")
        setup_conn.execute("CREATE SCHEMA public")
        setup_conn.commit()
    finally:
        setup_conn.close()
    _load_init_schema().init_db(None)
    conn = get_connection()
    cid1 = _seed_company(conn, "Parent Corp", "PC")
    cid2 = _seed_company(conn, "Subsidiary Inc", "SI")
    _seed_naming_series(conn, cid1)
    _seed_naming_series(conn, cid2)
    env = {"company_id": cid1, "company2_id": cid2}
    try:
        yield conn, env
    finally:
        conn.close()


def _seed_company(conn, name, abbr):
    cid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO company (id, name, abbr, default_currency, country,"
        " fiscal_year_start_month)"
        " VALUES (?, ?, ?, 'USD', 'United States', 1)",
        (cid, "%s %s" % (name, cid[:6]), "%s%s" % (abbr, cid[:4])),
    )
    conn.commit()
    return cid


def _seed_naming_series(conn, company_id):
    statement = insert_or_ignore(
        "INSERT OR IGNORE INTO naming_series"
        " (id, entity_type, prefix, current_value, company_id)"
        " VALUES (?, ?, ?, ?, ?)"
    )
    for entity_type, prefix in (
        ("revenue_contract", "RCON-"),
        ("lease", "LEAS-"),
        ("ic_transaction", "ICT-"),
        ("consolidation_group", "CGRP-"),
    ):
        conn.execute(
            statement,
            (str(uuid.uuid4()), entity_type, prefix, 0, company_id),
        )
    conn.commit()


def _verify():
    return get_connection()


def _row(conn, table, row_id):
    t = Table(table)
    found = conn.execute(
        Q.from_(t).select(t.star).where(t.id == P()).get_sql(),
        (row_id,)).fetchone()
    assert found is not None, "%s %s not found" % (table, row_id)
    return dict(found)


def _rows_where(conn, table, column, value):
    t = Table(table)
    return [dict(r) for r in conn.execute(
        Q.from_(t).select(t.star).where(Field(column) == P()).get_sql(),
        (value,)).fetchall()]


def _add_lease(conn, env, lessee="Acme Corp", pay="1000.00", rate="0.05",
               term=24, start="2026-01-01", end="2027-12-31",
               company=None, ltype="operating"):
    r = call_action(mod.add_lease, conn, ns(
        company_id=company or env["company_id"], lessee_name=lessee,
        lessor_name="Property Holdings LLC",
        asset_description="Office space 5th floor",
        lease_type=ltype, start_date=start, end_date=end,
        term_months=term, monthly_payment=pay,
        annual_escalation="0.03", discount_rate=rate,
        purchase_option_price=None,
    ))
    assert is_ok(r), r
    return r


def _add_contract(conn, env, customer="Acme Corp", total="9000.00",
                  number="C-001", start="2026-01-01", end="2026-03-31",
                  company=None):
    r = call_action(mod.add_revenue_contract, conn, ns(
        company_id=company or env["company_id"], customer_name=customer,
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


class TestPgDisclosure:
    def test_disclosure_totals_are_hand_computed(self, pg_pair):
        conn, env = pg_pair
        first = _add_lease(conn, env, lessee="Disclosure Alpha",
                           pay="1000.10", ltype="operating")
        second = _add_lease(conn, env, lessee="Disclosure Beta",
                            pay="2000.20", ltype="operating")
        fin = _add_lease(conn, env, lessee="Disclosure Finance",
                         pay="5000.00", term=48)
        assert is_ok(call_action(mod.classify_lease, conn, ns(
            id=fin["id"], lease_type="finance")))
        other = _add_lease(conn, env, lessee="Other Company Disclosure",
                           pay="7777.77", company=env["company2_id"])
        assert is_ok(call_action(mod.calculate_rou_asset, conn, ns(id=first["id"])))
        assert is_ok(call_action(mod.calculate_lease_liability, conn, ns(id=first["id"])))
        assert is_ok(call_action(mod.calculate_rou_asset, conn, ns(id=second["id"])))
        assert is_ok(call_action(mod.calculate_lease_liability, conn, ns(id=second["id"])))

        vconn = _verify()
        try:
            first_rou = _row(vconn, "advacct_lease", first["id"])["rou_asset_value"]
            first_lia = _row(vconn, "advacct_lease", first["id"])["lease_liability"]
            second_rou = _row(vconn, "advacct_lease", second["id"])["rou_asset_value"]
            second_lia = _row(vconn, "advacct_lease", second["id"])["lease_liability"]
        finally:
            vconn.close()
        assert first_rou is not None and second_rou is not None
        assert first_lia is not None and second_lia is not None
        expected_rou = str((Decimal(first_rou) + Decimal(second_rou)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP))
        expected_lia = str((Decimal(first_lia) + Decimal(second_lia)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP))

        r = call_action(mod.lease_disclosure_report, conn, ns(
            company_id=env["company_id"]))
        assert is_ok(r), r
        by_type = {row["lease_type"]: row for row in r["rows"]}
        assert set(by_type) == {"operating", "finance"}
        assert by_type["operating"]["lease_count"] == 2
        assert by_type["operating"]["total_monthly_payments"] == "3000.30"
        assert Decimal(by_type["operating"]["total_monthly_payments"]) == Decimal("3000.30")
        assert by_type["operating"]["total_rou_assets"] == expected_rou
        assert by_type["operating"]["total_lease_liabilities"] == expected_lia
        assert by_type["finance"]["lease_count"] == 1
        assert by_type["finance"]["total_monthly_payments"] == "5000.00"

        vconn = _verify()
        try:
            back = _rows_where(vconn, "advacct_lease", "company_id", env["company_id"])
            assert sorted(row["monthly_payment"] for row in back) == [
                "1000.10", "2000.20", "5000.00"]
            assert _row(vconn, "advacct_lease", other["id"])["monthly_payment"] == "7777.77"
        finally:
            vconn.close()


class TestPgRecognition:
    def test_periods_match_stored_schedule(self, pg_pair):
        conn, env = pg_pair
        c = _add_contract(conn, env)
        ob = _add_obligation(conn, env, c["id"], price="3000.30")
        s = call_action(mod.calculate_revenue_schedule, conn, ns(
            obligation_id=ob["id"]))
        assert is_ok(s), s
        assert s["total_amount"] == "3000.30"
        assert s["monthly_amount"] == "1000.10"
        dc = _add_contract(conn, env, customer="Decoy Ltd", total="600.00",
                           number="D-1", start="2026-01-01", end="2026-01-31",
                           company=env["company2_id"])
        dob = call_action(mod.add_performance_obligation, conn, ns(
            contract_id=dc["id"], company_id=env["company2_id"],
            name="Decoy work", standalone_price="600.00",
            recognition_method="over_time", recognition_basis="time"))
        assert is_ok(dob), dob
        ds = call_action(mod.calculate_revenue_schedule, conn, ns(
            obligation_id=dob["id"]))
        assert is_ok(ds), ds

        r = call_action(mod.revenue_recognition_summary, conn, ns(
            company_id=env["company_id"]))
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

        vconn = _verify()
        try:
            sched = _rows_where(vconn, "advacct_revenue_schedule",
                                "company_id", env["company_id"])
            assert len(sched) == 3
            assert sorted(x["amount"] for x in sched) == ["1000.10"] * 3
            assert sum(Decimal(x["amount"]) for x in sched) == Decimal("3000.30")
            decoy_sched = _rows_where(vconn, "advacct_revenue_schedule",
                                      "company_id", env["company2_id"])
            assert len(decoy_sched) == 1
            assert decoy_sched[0]["amount"] == "600.00"
        finally:
            vconn.close()

    def test_recognize_one_period_reports_recognized_sum(self, pg_pair):
        conn, env = pg_pair
        c = _add_contract(conn, env)
        ob = _add_obligation(conn, env, c["id"], price="3000.30")
        s = call_action(mod.calculate_revenue_schedule, conn, ns(
            obligation_id=ob["id"]))
        assert is_ok(s), s
        vconn = _verify()
        try:
            sched = _rows_where(vconn, "advacct_revenue_schedule",
                                "company_id", env["company_id"])
            jan = [x for x in sched if x["period_date"] == "2026-01-01"]
            assert len(jan) == 1
            first_id = jan[0]["id"]
        finally:
            vconn.close()
        ledger = seed_recognition_ledger(conn, env["company_id"])
        rec = call_action(mod.recognize_schedule_entry, conn, ns(
            id=first_id,
            deferred_revenue_account_id=ledger["deferred_revenue_account_id"],
            revenue_account_id=ledger["revenue_account_id"],
            cost_center_id=ledger["cost_center_id"]))
        assert is_ok(rec), rec
        legs = _rows_where(conn, "gl_entry", "voucher_id", first_id)
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
        r = call_action(mod.revenue_recognition_summary, conn, ns(
            company_id=env["company_id"]))
        assert is_ok(r), r
        by_period = {row["period_date"]: row for row in r["rows"]}
        recognized_total = sum((Decimal(row["recognized_amount"]) for row in r["rows"]),
                               Decimal("0"))
        assert recognized_total == Decimal("1000.10")
        assert by_period["2026-01-01"]["recognized_amount"] == "1000.10"
        assert by_period["2026-01-01"]["unrecognized_amount"] == "0.00"
        assert by_period["2026-01-01"]["total_amount"] == "1000.10"


class TestPgRecon:
    def test_group_totals_match_stored_rows(self, pg_pair):
        conn, env = pg_pair
        _add_ic(conn, env, amount="1000.10", txn_type="sale", desc="Alpha one")
        _add_ic(conn, env, amount="2000.20", txn_type="sale", desc="Alpha two")
        _add_ic(conn, env, amount="7000.00", txn_type="service",
                desc="Beta service", frm=env["company2_id"],
                to=env["company_id"], method=None)

        r = call_action(mod.ic_reconciliation_report, conn, ns(
            company_id=env["company_id"]))
        assert is_ok(r), r
        key = (env["company_id"], env["company2_id"], "sale", "draft")
        groups = {(g["from_company_id"], g["to_company_id"],
                   g["transaction_type"], g["ic_status"]): g
                  for g in r["rows"]}
        assert key in groups
        assert groups[key]["transaction_count"] == 2
        assert groups[key]["total_amount"] == "3000.30"
        assert Decimal(groups[key]["total_amount"]) == Decimal("3000.30")

        vconn = _verify()
        try:
            rows = _rows_where(vconn, "advacct_ic_transaction",
                               "company_id", env["company_id"])
            sale_amounts = sorted(
                x["amount"] for x in rows if x["transaction_type"] == "sale")
            assert sale_amounts == ["1000.10", "2000.20"]
            assert sum(Decimal(x) for x in sale_amounts) == Decimal("3000.30")
        finally:
            vconn.close()


class TestPgConsolidationSummary:
    def test_summary_matches_stored_entries(self, pg_pair):
        conn, env = pg_pair
        g = _add_group(conn, env)
        _add_entity(conn, env, g["id"], env["company_id"], "Parent Corp", "100")
        _add_entity(conn, env, g["id"], env["company2_id"], "Subsidiary Inc", "80")
        first = call_action(mod.add_currency_translation, conn, ns(
            group_id=g["id"], company_id=env["company_id"],
            period_date="2026-06-30", amount="1000.10",
            debit_account=None, credit_account=None, description=None))
        assert is_ok(first), first
        second = call_action(mod.add_currency_translation, conn, ns(
            group_id=g["id"], company_id=env["company_id"],
            period_date="2026-06-30", amount="2000.20",
            debit_account=None, credit_account=None, description=None))
        assert is_ok(second), second
        other = _add_group(conn, env, name="Other Group", currency="EUR")

        r = call_action(mod.consolidation_summary, conn, ns(group_id=g["id"]))
        assert is_ok(r), r
        assert r["group_name"] == "Global Holdings Group"
        assert r["group_status"] == "active"
        assert r["consolidation_currency"] == "USD"
        assert r["entity_count"] == 2
        assert r["elimination_count"] == 2
        assert r["eliminations_by_type"]["currency_translation"]["count"] == 2
        assert r["eliminations_by_type"]["currency_translation"]["total"] == "3000.30"
        assert Decimal(r["eliminations_by_type"]["currency_translation"]["total"]) == Decimal("3000.30")

        vconn = _verify()
        try:
            entries = _rows_where(vconn, "advacct_elimination_entry",
                                  "group_id", g["id"])
            assert len(entries) == 2
            assert sorted(x["amount"] for x in entries) == ["1000.10", "2000.20"]
            assert all(x["entry_type"] == "currency_translation" for x in entries)
            assert sum((Decimal(x["amount"]) for x in entries), Decimal("0")) == Decimal("3000.30")
            assert _rows_where(vconn, "advacct_elimination_entry",
                               "group_id", other["id"]) == []
        finally:
            vconn.close()

    def test_summary_exact_at_large_magnitude(self, pg_pair):
        conn, env = pg_pair
        g = _add_group(conn, env)
        _add_entity(conn, env, g["id"], env["company_id"], "Parent Corp", "100")
        _add_entity(conn, env, g["id"], env["company2_id"], "Subsidiary Inc", "80")
        first = call_action(mod.add_currency_translation, conn, ns(
            group_id=g["id"], company_id=env["company_id"],
            period_date="2026-06-30", amount="1000000000000000.01",
            debit_account=None, credit_account=None, description=None))
        assert is_ok(first), first
        second = call_action(mod.add_currency_translation, conn, ns(
            group_id=g["id"], company_id=env["company_id"],
            period_date="2026-06-30", amount="1000000000000000.02",
            debit_account=None, credit_account=None, description=None))
        assert is_ok(second), second
        r = call_action(mod.consolidation_summary, conn, ns(group_id=g["id"]))
        assert is_ok(r), r
        assert r["eliminations_by_type"]["currency_translation"]["count"] == 2
        assert r["eliminations_by_type"]["currency_translation"]["total"] == "2000000000000000.03"

    def test_summary_half_cent_rounds_half_up(self, pg_pair):
        conn, env = pg_pair
        g = _add_group(conn, env)
        _add_entity(conn, env, g["id"], env["company_id"], "Parent Corp", "100")
        _add_entity(conn, env, g["id"], env["company2_id"], "Subsidiary Inc", "80")
        first = call_action(mod.add_currency_translation, conn, ns(
            group_id=g["id"], company_id=env["company_id"],
            period_date="2026-06-30", amount="0.005",
            debit_account=None, credit_account=None, description=None))
        assert is_ok(first), first
        second = call_action(mod.add_currency_translation, conn, ns(
            group_id=g["id"], company_id=env["company_id"],
            period_date="2026-06-30", amount="0.000",
            debit_account=None, credit_account=None, description=None))
        assert is_ok(second), second
        r = call_action(mod.consolidation_summary, conn, ns(group_id=g["id"]))
        assert is_ok(r), r
        assert r["eliminations_by_type"]["currency_translation"]["count"] == 2
        assert r["eliminations_by_type"]["currency_translation"]["total"] == "0.01"
        assert Decimal(r["eliminations_by_type"]["currency_translation"]["total"]) == Decimal("0.01")


class TestPgAllocation:
    def test_allocated_value_is_exact_decimal_sum(self, pg_pair):
        conn, env = pg_pair
        contract = _add_contract(conn, env, customer="Allocation Owner",
                                 total="100000.00", number="C-ALLOC-1")
        first = _add_obligation(conn, env, contract["id"],
                                name="Part One", price="1200.10")
        second = _add_obligation(conn, env, contract["id"],
                                 name="Part Two", price="1800.25")
        vconn = _verify()
        try:
            stored = _row(vconn, "advacct_revenue_contract", contract["id"])
            assert stored["allocated_value"] == "3000.35"
            assert Decimal(stored["allocated_value"]) == Decimal("3000.35")
            first_stored = _row(vconn, "advacct_performance_obligation", first["id"])
            assert first_stored["allocated_price"] == "1200.10"
            second_stored = _row(vconn, "advacct_performance_obligation", second["id"])
            assert second_stored["allocated_price"] == "1800.25"
        finally:
            vconn.close()

    def test_allocated_value_exact_at_large_magnitude(self, pg_pair):
        conn, env = pg_pair
        contract = _add_contract(conn, env, customer="Allocation Large",
                                 total="4000000000000000.00", number="C-ALLOC-LARGE")
        first = _add_obligation(conn, env, contract["id"],
                                name="Large One", price="1000000000000000.01")
        second = _add_obligation(conn, env, contract["id"],
                                 name="Large Two", price="1000000000000000.02")
        vconn = _verify()
        try:
            stored = _row(vconn, "advacct_revenue_contract", contract["id"])
            assert stored["allocated_value"] == "2000000000000000.03"
            assert Decimal(stored["allocated_value"]) == Decimal("2000000000000000.03")
        finally:
            vconn.close()


_PG_SNAPSHOT_TABLES = (
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


def _pg_snapshot(conn):
    snap = {}
    for name in _PG_SNAPSHOT_TABLES:
        t = Table(name)
        rows = conn.execute(Q.from_(t).select(t.star).get_sql()).fetchall()
        snap[name] = sorted(repr(dict(r)) for r in rows)
    return snap


def _pg_audits(conn, entity_id):
    t = Table("audit_log")
    return [dict(r) for r in conn.execute(
        Q.from_(t).select(t.star).where(t.entity_id == P()).get_sql(),
        (entity_id,)).fetchall()]


class TestPgMoneyValidation:
    def test_currency_translation_rejects_nonfinite_amount(self, pg_pair):
        conn, env = pg_pair
        g = _add_group(conn, env)
        before = _pg_snapshot(conn)
        r = call_action(mod.add_currency_translation, conn, ns(
            group_id=g["id"], company_id=env["company_id"],
            period_date="2026-06-30", amount="abc",
            debit_account=None, credit_account=None, description=None))
        assert is_error(r), r
        assert r["message"] == "Invalid amount: abc"
        assert _pg_snapshot(conn) == before

    def test_add_lease_rejects_nonfinite_monthly_payment(self, pg_pair):
        conn, env = pg_pair
        before = _pg_snapshot(conn)
        r = call_action(mod.add_lease, conn, ns(
            company_id=env["company_id"], lessee_name="Money Target",
            lessor_name="Property Holdings LLC",
            asset_description="Office space 5th floor",
            lease_type="operating", start_date="2026-01-01",
            end_date="2027-12-31", term_months=24, monthly_payment="abc",
            annual_escalation="0.03", discount_rate="0.05",
            purchase_option_price=None))
        assert is_error(r), r
        assert r["message"] == "Invalid monthly-payment: abc"
        assert _pg_snapshot(conn) == before

    def test_update_lease_rejects_nonfinite_monthly_payment(self, pg_pair):
        conn, env = pg_pair
        target = _add_lease(conn, env, lessee="Money Target", pay="1000.00")
        before = _pg_snapshot(conn)
        r = call_action(mod.update_lease, conn, ns(
            id=target["id"], lessee_name=None,
            lessor_name=None, asset_description=None,
            start_date=None, end_date=None,
            monthly_payment="abc", discount_rate=None,
            annual_escalation=None, purchase_option_price=None,
            lease_type=None, term_months=None))
        assert is_error(r), r
        assert r["message"] == "Invalid monthly-payment: abc"
        assert _pg_snapshot(conn) == before

    def test_update_revenue_contract_rejects_nonfinite_total_value(
            self, pg_pair):
        conn, env = pg_pair
        target = _add_contract(conn, env, customer="Money Target",
                               total="120000.00", number="C-MONEY-1")
        before = _pg_snapshot(conn)
        r = call_action(mod.update_revenue_contract, conn, ns(
            id=target["id"], customer_name=None,
            contract_number=None, start_date=None,
            end_date=None, total_value="abc", contract_status=None))
        assert is_error(r), r
        assert r["message"] == "Invalid total-value: abc"
        assert _pg_snapshot(conn) == before

    def test_update_ic_transaction_rejects_bad_amount(self, pg_pair):
        conn, env = pg_pair
        ic1 = _add_ic(conn, env, amount="50000.00", desc="Alpha sale")
        before = _pg_snapshot(conn)
        r = call_action(mod.update_ic_transaction, conn, ns(
            id=ic1["id"], description=None, amount="abc",
            currency=None, transaction_type=None,
            transfer_price_method=None))
        assert is_error(r), r
        assert r["message"] == "Invalid amount: abc"
        assert _pg_snapshot(conn) == before
        r = call_action(mod.update_ic_transaction, conn, ns(
            id=ic1["id"], description=None, amount="0",
            currency=None, transaction_type=None,
            transfer_price_method=None))
        assert is_error(r), r
        assert r["message"] == "Amount must be greater than zero"
        assert _pg_snapshot(conn) == before


class TestPgUpdaterAuditShape:
    def test_update_lease_audit_row_carries_old_and_new_values(self, pg_pair):
        conn, env = pg_pair
        target = _add_lease(conn, env, lessee="Update Target",
                            pay="1000.00", rate="0.05")
        r = call_action(mod.update_lease, conn, ns(
            id=target["id"], lessee_name="Updated Corp",
            lessor_name=None, asset_description=None,
            start_date=None, end_date=None,
            monthly_payment="1750.00", discount_rate=None,
            annual_escalation=None, purchase_option_price=None,
            lease_type=None, term_months=None))
        assert is_ok(r), r
        assert r["updated_fields"] == ["lessee_name", "monthly_payment"]
        vconn = _verify()
        try:
            audits = [a for a in _pg_audits(vconn, target["id"])
                      if a["action"] == "update-lease"]
            assert len(audits) >= 1
            latest = audits[-1]
            assert json.loads(latest["old_values"] or "{}") == {
                "lessee_name": "Update Target",
                "monthly_payment": "1000.00"}
            assert json.loads(latest["new_values"] or "{}") == {
                "lessee_name": "Updated Corp",
                "monthly_payment": "1750.00"}
        finally:
            vconn.close()

    def test_update_revenue_contract_audit_row_carries_old_and_new_values(
            self, pg_pair):
        conn, env = pg_pair
        target = _add_contract(conn, env, customer="Update Target",
                               total="120000.00", number="C-UPD-1")
        r = call_action(mod.update_revenue_contract, conn, ns(
            id=target["id"], customer_name="Updated Corp",
            contract_number=None, start_date=None,
            end_date=None, total_value="135000.00", contract_status=None))
        assert is_ok(r), r
        assert r["updated_fields"] == ["customer_name", "total_value"]
        vconn = _verify()
        try:
            audits = [a for a in _pg_audits(vconn, target["id"])
                      if a["action"] == "update-revenue-contract"]
            assert len(audits) >= 1
            latest = audits[-1]
            assert json.loads(latest["old_values"] or "{}") == {
                "customer_name": "Update Target",
                "total_value": "120000.00"}
            assert json.loads(latest["new_values"] or "{}") == {
                "customer_name": "Updated Corp",
                "total_value": "135000.00"}
        finally:
            vconn.close()

    def test_update_ic_transaction_audit_row_carries_old_and_new_values(
            self, pg_pair):
        conn, env = pg_pair
        ic1 = _add_ic(conn, env, amount="50000.00", desc="Alpha sale")
        r = call_action(mod.update_ic_transaction, conn, ns(
            id=ic1["id"], description="Updated note", amount="52000.00",
            currency=None, transaction_type=None,
            transfer_price_method=None))
        assert is_ok(r), r
        assert r["updated_fields"] == ["description", "amount"]
        vconn = _verify()
        try:
            audits = [a for a in _pg_audits(vconn, ic1["id"])
                      if a["action"] == "update-ic-transaction"]
            assert len(audits) >= 1
            latest = audits[-1]
            assert json.loads(latest["old_values"] or "{}") == {
                "description": "Alpha sale", "amount": "50000.00"}
            assert json.loads(latest["new_values"] or "{}") == {
                "description": "Updated note", "amount": "52000.00"}
        finally:
            vconn.close()

    def test_update_performance_obligation_audit_row_carries_old_and_new_values(
            self, pg_pair):
        conn, env = pg_pair
        contract = _add_contract(conn, env)
        ob = _add_obligation(conn, env, contract["id"])
        r = call_action(mod.update_performance_obligation, conn, ns(
            id=ob["id"], standalone_price="3200.00"))
        assert is_ok(r), r
        assert r["updated_fields"] == ["standalone_price"]
        vconn = _verify()
        try:
            audits = [a for a in _pg_audits(vconn, ob["id"])
                      if a["action"] == "update-performance-obligation"]
            assert len(audits) == 1
            assert json.loads(audits[0]["old_values"] or "{}") == {
                "standalone_price": "3000.00"}
            assert json.loads(audits[0]["new_values"] or "{}") == {
                "standalone_price": "3200.00"}
        finally:
            vconn.close()
