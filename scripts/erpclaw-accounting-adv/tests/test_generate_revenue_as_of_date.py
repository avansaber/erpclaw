"""generate-revenue-entries recognises only periods due on or before --as-of-date.

Schedule of three monthly rows built with the module's own schedule
calculation over a 2026-09-01 to 2026-11-30 contract (allocated 1000.00):

  2026-09-01  333.33
  2026-10-01  333.33
  2026-11-01  333.34   (the last period carries the rounding remainder)

`--as-of-date` defaults to today (UTC); periods that have not arrived are
never booked early. `recognize-schedule-entry` is out of scope: it still
recognises any single entry on explicit request, including a future one.
"""
import json
import sys
from decimal import Decimal
from unittest.mock import patch

from advacct_helpers import (
    call_action, ns, is_error, is_ok, load_db_query, seed_recognition_ledger,
)

from erpclaw_lib.query import P, Q, Table, fn

mod = load_db_query()


# ---------------------------------------------------------------------------
# helpers (mirroring tests/test_revenue_recognition_posting.py)
# ---------------------------------------------------------------------------

def _build_contract(conn, env):
    c = call_action(mod.add_revenue_contract, conn, ns(
        company_id=env["company_id"], customer_name="Acme Sep-Nov",
        total_value="1000.00", contract_number="SN-2026",
        start_date="2026-09-01", end_date="2026-11-30"))
    assert is_ok(c), c
    a = call_action(mod.add_performance_obligation, conn, ns(
        contract_id=c["id"], company_id=env["company_id"],
        name="Subscription", standalone_price="1000.00",
        recognition_method="over_time", recognition_basis="time"))
    assert is_ok(a), a
    s = call_action(mod.calculate_revenue_schedule, conn, ns(obligation_id=a["id"]))
    assert is_ok(s), s
    assert (s["entries_created"], s["monthly_amount"], s["total_amount"]) == (
        3, "333.33", "1000.00")
    entry_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM advacct_revenue_schedule WHERE obligation_id = ? "
        "ORDER BY period_date", (a["id"],)).fetchall()]
    return c["id"], a["id"], entry_ids


def _acc(ledger, **kw):
    args = {
        "deferred_revenue_account_id": ledger["deferred_revenue_account_id"],
        "revenue_account_id": ledger["revenue_account_id"],
        "cost_center_id": ledger["cost_center_id"],
    }
    args.update(kw)
    return args


def _schedule(conn, ob_id):
    t = Table("advacct_revenue_schedule")
    rows = conn.execute(
        Q.from_(t).select(t.period_date, t.amount, t.recognized)
        .where(t.obligation_id == P())
        .orderby(t.period_date, t.id).get_sql(), (ob_id,)).fetchall()
    return [(r["period_date"], r["amount"], r["recognized"]) for r in rows]


def _voucher_legs(conn, voucher_id):
    t = Table("gl_entry")
    rows = conn.execute(
        Q.from_(t).select(
            t.account_id, t.debit, t.credit, t.posting_date,
            t.cost_center_id, t.is_cancelled)
        .where(t.voucher_type == P())
        .where(t.voucher_id == P()).get_sql(),
        ("revenue_recognition", voucher_id)).fetchall()
    return [(r["account_id"], r["debit"], r["credit"], r["posting_date"],
             r["cost_center_id"], r["is_cancelled"]) for r in rows]


def _gl_count(conn):
    t = Table("gl_entry")
    return conn.execute(
        Q.from_(t).select(fn.Count("*")).get_sql()).fetchone()[0]


def _audit_count(conn):
    t = Table("audit_log")
    return conn.execute(
        Q.from_(t).select(fn.Count("*")).get_sql()).fetchone()[0]


def _audit_new_values(conn, action, entity_id):
    t = Table("audit_log")
    rows = conn.execute(
        Q.from_(t).select(t.new_values)
        .where(t.action == P()).where(t.entity_id == P()).get_sql(),
        (action, entity_id)).fetchall()
    return [json.loads(r["new_values"]) for r in rows]


def _deferred_debit_total(conn, ledger):
    t = Table("gl_entry")
    rows = conn.execute(
        Q.from_(t).select(t.debit)
        .where(t.account_id == P()).get_sql(),
        (ledger["deferred_revenue_account_id"],)).fetchall()
    return sum((Decimal(r["debit"]) for r in rows), Decimal("0"))


def _patched_today(date_str):
    return patch.object(sys.modules["revenue"], "_today_utc",
                        return_value=date_str)


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_as_of_posts_only_due_periods(conn, env):
    _, ob_id, (sep, oct_, nov) = _build_contract(conn, env)
    ledger = seed_recognition_ledger(conn, env["company_id"])
    with _patched_today("2026-12-31"):
        g = call_action(mod.generate_revenue_entries, conn,
                        ns(obligation_id=ob_id, as_of_date="2026-09-26",
                           **_acc(ledger)))
    assert is_ok(g), g
    assert g["recognized_count"] == 1
    assert g["total_recognized"] == "333.33"
    assert g["remaining_count"] == 2
    assert g["as_of_date"] == "2026-09-26"

    assert sorted(leg[1:3] for leg in _voucher_legs(conn, sep)) == [
        ("0.00", "333.33"), ("333.33", "0.00")]
    assert [leg[3] for leg in _voucher_legs(conn, sep)] == [
        "2026-09-01", "2026-09-01"]
    assert _gl_count(conn) == 2
    assert _schedule(conn, ob_id) == [
        ("2026-09-01", "333.33", 1),
        ("2026-10-01", "333.33", 0),
        ("2026-11-01", "333.34", 0),
    ]
    assert _audit_new_values(conn, "generate-revenue-entries", ob_id)[-1][
        "as_of_date"] == "2026-09-26"


def test_later_run_posts_the_rest(conn, env):
    _, ob_id, (sep, oct_, nov) = _build_contract(conn, env)
    ledger = seed_recognition_ledger(conn, env["company_id"])
    with _patched_today("2026-12-31"):
        first = call_action(mod.generate_revenue_entries, conn,
                            ns(obligation_id=ob_id, as_of_date="2026-09-26",
                               **_acc(ledger)))
    assert is_ok(first), first
    assert (first["recognized_count"], first["total_recognized"]) == (
        1, "333.33")

    with _patched_today("2026-12-31"):
        second = call_action(mod.generate_revenue_entries, conn,
                             ns(obligation_id=ob_id, as_of_date="2026-11-01",
                                **_acc(ledger)))
    assert is_ok(second), second
    assert second["recognized_count"] == 2
    assert second["total_recognized"] == "666.67"
    assert second["remaining_count"] == 0
    assert second["as_of_date"] == "2026-11-01"
    assert _schedule(conn, ob_id) == [
        ("2026-09-01", "333.33", 1),
        ("2026-10-01", "333.33", 1),
        ("2026-11-01", "333.34", 1),
    ]
    assert _deferred_debit_total(conn, ledger) == Decimal("1000.00")


def test_nothing_due_refused(conn, env):
    _, ob_id, _ = _build_contract(conn, env)
    ledger = seed_recognition_ledger(conn, env["company_id"])
    gl_before, audit_before = _gl_count(conn), _audit_count(conn)
    schedule_before = _schedule(conn, ob_id)
    with _patched_today("2026-12-31"):
        r = call_action(mod.generate_revenue_entries, conn,
                        ns(obligation_id=ob_id, as_of_date="2026-08-31",
                           **_acc(ledger)))
    assert is_error(r)
    assert r["message"] == (
        f"No revenue schedule entries for obligation {ob_id} are due on or "
        f"before 2026-08-31; the next is 2026-09-01")
    assert _gl_count(conn) == gl_before
    assert _schedule(conn, ob_id) == schedule_before
    assert _audit_count(conn) == audit_before


def test_boundary_date_is_included(conn, env):
    _, ob_id, _ = _build_contract(conn, env)
    ledger = seed_recognition_ledger(conn, env["company_id"])
    with _patched_today("2026-12-31"):
        g = call_action(mod.generate_revenue_entries, conn,
                        ns(obligation_id=ob_id, as_of_date="2026-10-01",
                           **_acc(ledger)))
    assert is_ok(g), g
    assert g["recognized_count"] == 2
    assert g["total_recognized"] == "666.66"
    assert g["remaining_count"] == 1
    assert g["as_of_date"] == "2026-10-01"
    assert _schedule(conn, ob_id) == [
        ("2026-09-01", "333.33", 1),
        ("2026-10-01", "333.33", 1),
        ("2026-11-01", "333.34", 0),
    ]


def test_bad_as_of_refused(conn, env):
    _, ob_id, _ = _build_contract(conn, env)
    ledger = seed_recognition_ledger(conn, env["company_id"])
    for bad in ("2026-13-01", "26-09-01", "yesterday"):
        gl_before, audit_before = _gl_count(conn), _audit_count(conn)
        schedule_before = _schedule(conn, ob_id)
        with _patched_today("2026-12-31"):
            r = call_action(mod.generate_revenue_entries, conn,
                            ns(obligation_id=ob_id, as_of_date=bad,
                               **_acc(ledger)))
        assert is_error(r), (bad, r)
        assert r["message"] == (
            "--as-of-date must be a date in YYYY-MM-DD form"), (bad, r)
        assert _gl_count(conn) == gl_before
        assert _schedule(conn, ob_id) == schedule_before
        assert _audit_count(conn) == audit_before


def test_default_is_today(conn, env):
    _, ob_id, _ = _build_contract(conn, env)
    ledger = seed_recognition_ledger(conn, env["company_id"])
    with _patched_today("2026-10-15"):
        g = call_action(mod.generate_revenue_entries, conn,
                        ns(obligation_id=ob_id, **_acc(ledger)))
    assert is_ok(g), g
    assert g["recognized_count"] == 2
    assert g["as_of_date"] == "2026-10-15"
    assert g["remaining_count"] == 1
    assert _schedule(conn, ob_id) == [
        ("2026-09-01", "333.33", 1),
        ("2026-10-01", "333.33", 1),
        ("2026-11-01", "333.34", 0),
    ]


def test_future_as_of_refused(conn, env):
    _, ob_id, _ = _build_contract(conn, env)
    ledger = seed_recognition_ledger(conn, env["company_id"])
    gl_before, audit_before = _gl_count(conn), _audit_count(conn)
    schedule_before = _schedule(conn, ob_id)
    with _patched_today("2026-09-26"):
        r = call_action(mod.generate_revenue_entries, conn,
                        ns(obligation_id=ob_id, as_of_date="2026-10-01",
                           **_acc(ledger)))
    assert is_error(r)
    assert r["message"] == (
        "--as-of-date 2026-10-01 is later than today (2026-09-26); "
        "revenue for a period that has not arrived is not recognized")
    assert _gl_count(conn) == gl_before
    assert _schedule(conn, ob_id) == schedule_before
    assert _audit_count(conn) == audit_before
