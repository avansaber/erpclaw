"""Behaviour of recognize-schedule-entry, update-schedule-amounts,
update-performance-obligation and standards-compliance-dashboard, read back
from the database.

Contract "Northwind Analytics", 2026-01-01 to 2026-03-31, total 10000.00:

  obligation A  "Platform subscription"  standalone 7000.00  allocated 7000.00
  obligation B  "Onboarding"             standalone 3000.00  allocated 3000.00

calculate-revenue-schedule spreads A over three months:
  2026-01-01  2333.33
  2026-02-01  2333.33
  2026-03-01  2333.34   (the last period carries the rounding remainder)

recognition posts its own deferred-revenue / revenue voucher.
"""
import json
from unittest.mock import patch

from advacct_helpers import (
    call_action, ns, is_error, is_ok, load_db_query, seed_recognition_ledger,
)

mod = load_db_query()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_COUNT_SQL = {
    "audit_log": "SELECT COUNT(*) FROM audit_log",
    "gl_entry": "SELECT COUNT(*) FROM gl_entry",
}


def _count(conn, table):
    return conn.execute(_COUNT_SQL[table]).fetchone()[0]


def _schedule(conn, ob_id):
    rows = conn.execute(
        "SELECT period_date, amount, recognized FROM advacct_revenue_schedule "
        "WHERE obligation_id = ? ORDER BY period_date", (ob_id,)).fetchall()
    return [(r["period_date"], r["amount"], r["recognized"]) for r in rows]


def _obligation(conn, ob_id):
    r = conn.execute(
        "SELECT name, standalone_price, allocated_price, recognition_method, "
        "recognition_basis, obligation_status FROM advacct_performance_obligation "
        "WHERE id = ?", (ob_id,)).fetchone()
    return (r["name"], r["standalone_price"], r["allocated_price"],
            r["recognition_method"], r["recognition_basis"], r["obligation_status"])


def _contract_allocated(conn, contract_id):
    r = conn.execute(
        "SELECT total_value, allocated_value FROM advacct_revenue_contract WHERE id = ?",
        (contract_id,)).fetchone()
    return (r["total_value"], r["allocated_value"])


def _snapshot(conn):
    schedule = sorted(
        (r["id"], r["amount"], r["recognized"]) for r in conn.execute(
            "SELECT id, amount, recognized FROM advacct_revenue_schedule").fetchall())
    obligations = sorted(
        (r["id"], r["name"], r["standalone_price"], r["allocated_price"],
         r["recognition_method"], r["recognition_basis"]) for r in conn.execute(
            "SELECT id, name, standalone_price, allocated_price, recognition_method, "
            "recognition_basis FROM advacct_performance_obligation").fetchall())
    contracts = sorted(
        (r["id"], r["total_value"], r["allocated_value"]) for r in conn.execute(
            "SELECT id, total_value, allocated_value FROM advacct_revenue_contract").fetchall())
    return (schedule, obligations, contracts,
            _count(conn, "audit_log"), _count(conn, "gl_entry"))


def _audit_rows(conn, action, entity_id):
    rows = conn.execute(
        "SELECT skill, entity_type, new_values FROM audit_log "
        "WHERE action = ? AND entity_id = ?", (action, entity_id)).fetchall()
    return [(r["skill"], r["entity_type"], json.loads(r["new_values"])) for r in rows]


def _build_contract(conn, env):
    c = call_action(mod.add_revenue_contract, conn, ns(
        company_id=env["company_id"], customer_name="Northwind Analytics",
        total_value="10000.00", contract_number="NW-2026",
        start_date="2026-01-01", end_date="2026-03-31"))
    assert is_ok(c), c
    a = call_action(mod.add_performance_obligation, conn, ns(
        contract_id=c["id"], company_id=env["company_id"],
        name="Platform subscription", standalone_price="7000.00",
        recognition_method="over_time", recognition_basis="time"))
    assert is_ok(a), a
    b = call_action(mod.add_performance_obligation, conn, ns(
        contract_id=c["id"], company_id=env["company_id"],
        name="Onboarding", standalone_price="3000.00",
        recognition_method="over_time", recognition_basis="time"))
    assert is_ok(b), b
    s = call_action(mod.calculate_revenue_schedule, conn, ns(obligation_id=a["id"]))
    assert is_ok(s), s
    assert (s["entries_created"], s["monthly_amount"], s["total_amount"]) == (
        3, "2333.33", "7000.00")
    entry_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM advacct_revenue_schedule WHERE obligation_id = ? "
        "ORDER BY period_date", (a["id"],)).fetchall()]
    return c["id"], a["id"], b["id"], entry_ids


# ---------------------------------------------------------------------------
# recognize-schedule-entry
# ---------------------------------------------------------------------------

def _acc(ledger, **kw):
    args = {
        "deferred_revenue_account_id": ledger["deferred_revenue_account_id"],
        "revenue_account_id": ledger["revenue_account_id"],
        "cost_center_id": ledger["cost_center_id"],
    }
    args.update(kw)
    return args


def test_recognize_schedule_entry_flags_only_that_entry_and_posts_its_voucher(conn, env):
    contract_id, a_id, b_id, (jan, feb, mar) = _build_contract(conn, env)
    ledger = seed_recognition_ledger(conn, env["company_id"])
    assert _schedule(conn, a_id) == [
        ("2026-01-01", "2333.33", 0),
        ("2026-02-01", "2333.33", 0),
        ("2026-03-01", "2333.34", 0),
    ]

    r = call_action(mod.recognize_schedule_entry, conn, ns(id=feb, **_acc(ledger)))
    assert {k: v for k, v in r.items() if k != "gl_entry_ids"} == {
        "status": "ok", "id": feb, "recognized": 1, "amount": "2333.33"}
    assert len(r["gl_entry_ids"]) == 2

    assert _schedule(conn, a_id) == [
        ("2026-01-01", "2333.33", 0),
        ("2026-02-01", "2333.33", 1),
        ("2026-03-01", "2333.34", 0),
    ]
    assert _count(conn, "gl_entry") == 2
    assert _audit_rows(conn, "recognize-schedule-entry", feb) == [
        ("erpclaw-accounting-adv", "advacct_revenue_schedule",
         {"recognized": 1, "gl_entry_ids": r["gl_entry_ids"]})]
    # Recognition does not re-price anything.
    assert _obligation(conn, a_id) == (
        "Platform subscription", "7000.00", "7000.00", "over_time", "time", "unsatisfied")
    assert _contract_allocated(conn, contract_id) == ("10000.00", "10000.00")

    r = call_action(mod.recognize_schedule_entry, conn, ns(id=mar, **_acc(ledger)))
    assert (r["id"], r["recognized"], r["amount"]) == (mar, 1, "2333.34")
    assert len(r["gl_entry_ids"]) == 2
    assert _count(conn, "gl_entry") == 4
    assert _schedule(conn, a_id) == [
        ("2026-01-01", "2333.33", 0),
        ("2026-02-01", "2333.33", 1),
        ("2026-03-01", "2333.34", 1),
    ]


def test_recognize_schedule_entry_twice_is_refused_and_writes_nothing(conn, env):
    _, a_id, _, (jan, feb, mar) = _build_contract(conn, env)
    ledger = seed_recognition_ledger(conn, env["company_id"])
    assert is_ok(call_action(mod.recognize_schedule_entry, conn,
                             ns(id=jan, **_acc(ledger))))
    before = _snapshot(conn)

    r = call_action(mod.recognize_schedule_entry, conn, ns(id=jan, **_acc(ledger)))
    assert is_error(r)
    assert r["message"] == f"Revenue schedule entry {jan} is already recognized"
    assert _snapshot(conn) == before
    assert len(_audit_rows(conn, "recognize-schedule-entry", jan)) == 1

    r = call_action(mod.recognize_schedule_entry, conn, ns(id=None, **_acc(ledger)))
    assert is_error(r) and r["message"] == "--id is required"
    r = call_action(mod.recognize_schedule_entry, conn,
                    ns(id="no-such-entry", **_acc(ledger)))
    assert is_error(r)
    assert r["message"] == "Revenue schedule entry no-such-entry not found"
    assert _snapshot(conn) == before


# ---------------------------------------------------------------------------
# update-schedule-amounts
# ---------------------------------------------------------------------------

def test_update_schedule_amounts_rewrites_only_unrecognized_entries(conn, env):
    contract_id, a_id, b_id, (jan, feb, mar) = _build_contract(conn, env)
    ledger = seed_recognition_ledger(conn, env["company_id"])
    assert is_ok(call_action(mod.recognize_schedule_entry, conn,
                             ns(id=jan, **_acc(ledger))))

    r = call_action(mod.update_schedule_amounts, conn, ns(obligation_id=a_id))
    assert r == {"status": "ok", "obligation_id": a_id,
                 "allocated_price": "7000.00", "recognized_amount": "2333.33",
                 "entries_updated": 2, "amounts": ["2333.33", "2333.34"]}

    # The recognized January entry keeps its amount; only future periods change.
    assert _schedule(conn, a_id) == [
        ("2026-01-01", "2333.33", 1),
        ("2026-02-01", "2333.33", 0),
        ("2026-03-01", "2333.34", 0),
    ]
    assert _audit_rows(conn, "update-schedule-amounts", a_id) == [
        ("erpclaw-accounting-adv", "advacct_revenue_schedule",
         {"allocated_price": "7000.00", "recognized_amount": "2333.33",
          "entries_updated": 2, "amounts": ["2333.33", "2333.34"]})]
    # The schedule update does not touch the obligation, the contract or the ledger.
    assert _obligation(conn, a_id) == (
        "Platform subscription", "7000.00", "7000.00", "over_time", "time", "unsatisfied")
    assert _contract_allocated(conn, contract_id) == ("10000.00", "10000.00")
    assert _count(conn, "gl_entry") == 2


def test_update_schedule_amounts_never_changes_a_recognized_amount(conn, env):
    _, a_id, _, _ = _build_contract(conn, env)
    ledger = seed_recognition_ledger(conn, env["company_id"])
    g = call_action(mod.generate_revenue_entries, conn,
                    ns(obligation_id=a_id, **_acc(ledger)))
    assert (g["recognized_count"], g["total_recognized"]) == (3, "7000.00")

    r = call_action(mod.update_schedule_amounts, conn, ns(obligation_id=a_id))
    assert is_ok(r)
    assert r["entries_updated"] == 0
    assert _schedule(conn, a_id) == [
        ("2026-01-01", "2333.33", 1),
        ("2026-02-01", "2333.33", 1),
        ("2026-03-01", "2333.34", 1),
    ]


def test_update_schedule_amounts_refusals_write_nothing(conn, env):
    _, a_id, _, _ = _build_contract(conn, env)
    before = _snapshot(conn)

    cases = [
        (ns(obligation_id=None), "--obligation-id is required"),
        (ns(obligation_id="no-such-obligation"),
         "Performance obligation no-such-obligation not found"),
        (ns(obligation_id=a_id, amount="2500.00"),
         "update-schedule-amounts takes no --amount: it re-spreads the obligation's "
         "allocated price over its unrecognized periods; set the allocation with "
         "update-performance-obligation --allocated-price"),
    ]
    for args, message in cases:
        r = call_action(mod.update_schedule_amounts, conn, args)
        assert is_error(r), (args, r)
        assert r["message"] == message
        assert _snapshot(conn) == before


# ---------------------------------------------------------------------------
# update-performance-obligation
# ---------------------------------------------------------------------------

def test_update_performance_obligation_reallocates_the_contract_value(conn, env):
    contract_id, a_id, b_id, _ = _build_contract(conn, env)
    assert _contract_allocated(conn, contract_id) == ("10000.00", "10000.00")

    r = call_action(mod.update_performance_obligation, conn, ns(
        id=b_id, standalone_price="2800.00", allocated_price="2500.00",
        name="Onboarding and training", recognition_method="point_in_time",
        recognition_basis=None))
    assert r == {"status": "ok", "id": b_id, "updated_fields": [
        "standalone_price", "allocated_price", "name", "recognition_method"]}

    assert _obligation(conn, b_id) == (
        "Onboarding and training", "2800.00", "2500.00", "point_in_time", "time",
        "unsatisfied")
    assert _obligation(conn, a_id) == (
        "Platform subscription", "7000.00", "7000.00", "over_time", "time", "unsatisfied")
    # The contract's allocated value stays the sum of its obligations' allocations:
    # 7000.00 + 2500.00. The contract's total value is not the handler's to change.
    assert _contract_allocated(conn, contract_id) == ("10000.00", "9500.00")
    assert _audit_rows(conn, "update-performance-obligation", b_id) == [
        ("erpclaw-accounting-adv", "advacct_performance_obligation",
         {"standalone_price": "2800.00", "allocated_price": "2500.00",
          "name": "Onboarding and training",
          "recognition_method": "point_in_time"})]
    old_row = conn.execute(
        "SELECT old_values FROM audit_log "
        "WHERE action = ? AND entity_id = ?",
        ("update-performance-obligation", b_id)).fetchone()
    assert json.loads(old_row["old_values"]) == {
        "standalone_price": "3000.00", "allocated_price": "3000.00",
        "name": "Onboarding", "recognition_method": "over_time"}

    # A standalone price alone is not an allocation: the contract does not move.
    r = call_action(mod.update_performance_obligation, conn, ns(
        id=a_id, standalone_price="7200.00"))
    assert r["updated_fields"] == ["standalone_price"]
    assert _obligation(conn, a_id)[1:3] == ("7200.00", "7000.00")
    assert _contract_allocated(conn, contract_id) == ("10000.00", "9500.00")

    # Neither update rewrites the obligation's existing schedule.
    assert _schedule(conn, a_id) == [
        ("2026-01-01", "2333.33", 0),
        ("2026-02-01", "2333.33", 0),
        ("2026-03-01", "2333.34", 0),
    ]
    assert _count(conn, "gl_entry") == 0


def test_update_performance_obligation_refusals_write_nothing(conn, env):
    contract_id, a_id, b_id, _ = _build_contract(conn, env)
    before = _snapshot(conn)

    cases = [
        (ns(id=None, name="X"), "--id is required"),
        (ns(id="no-such-obligation", name="X"),
         "Performance obligation no-such-obligation not found"),
        (ns(id=b_id), "No fields to update"),
        (ns(id=b_id, allocated_price="abc"), "Invalid allocated-price: abc"),
        (ns(id=b_id, standalone_price="12,000.00"), "Invalid standalone-price: 12,000.00"),
        (ns(id=b_id, allocated_price="NaN", name="Renamed"), "Invalid allocated-price: NaN"),
    ]
    for args, message in cases:
        r = call_action(mod.update_performance_obligation, conn, args)
        assert is_error(r), (args, r)
        assert r["message"] == message
        assert _snapshot(conn) == before
    assert _contract_allocated(conn, contract_id) == ("10000.00", "10000.00")


def test_update_performance_obligation_accepts_allocated_price_on_the_command_line(
        conn, env, db_path):
    contract_id, a_id, b_id, _ = _build_contract(conn, env)
    argv = ["db_query.py", "--db-path", db_path,
            "--action", "update-performance-obligation",
            "--id", b_id, "--allocated-price", "2500.00"]
    with patch("sys.argv", argv):
        r = call_action(lambda _conn, _args: mod.main(), None, None)
    assert r == {"status": "ok", "id": b_id, "updated_fields": ["allocated_price"]}
    assert _obligation(conn, b_id)[1:3] == ("3000.00", "2500.00")
    assert _contract_allocated(conn, contract_id) == ("10000.00", "9500.00")


# ---------------------------------------------------------------------------
# standards-compliance-dashboard
# ---------------------------------------------------------------------------

def _lease(conn, company_id, lessee, status=None, rou=False):
    r = call_action(mod.add_lease, conn, ns(
        company_id=company_id, lessee_name=lessee, lessor_name="Harbor Properties",
        asset_description="Warehouse bay", lease_type="operating",
        start_date="2026-01-01", end_date="2027-12-31", term_months=24,
        monthly_payment="1000.00", annual_escalation="0", discount_rate="0.05",
        purchase_option_price=None))
    assert is_ok(r), r
    if rou:
        assert is_ok(call_action(mod.calculate_rou_asset, conn, ns(id=r["id"])))
    if status:
        # No action moves a lease out of draft, so the status is seeded directly.
        conn.execute("UPDATE advacct_lease SET lease_status = ? WHERE id = ?",
                     (status, r["id"]))
        conn.commit()
    return r["id"]


def _ic(conn, company_id, from_id, to_id, approve=False, post=False):
    r = call_action(mod.add_ic_transaction, conn, ns(
        company_id=company_id, from_company_id=from_id, to_company_id=to_id,
        transaction_type="service", amount="1500.00", description="Shared services",
        currency="USD", transfer_price_method="cost_plus"))
    assert is_ok(r), r
    if approve:
        assert is_ok(call_action(mod.approve_ic_transaction, conn, ns(id=r["id"])))
    if post:
        assert is_ok(call_action(mod.post_ic_transaction, conn, ns(id=r["id"])))
    return r["id"]


def _seed_dashboard(conn, env):
    c1, c2 = env["company_id"], env["company2_id"]
    # Company 1: two contracts; obligations unsatisfied, satisfied, partially satisfied.
    x = call_action(mod.add_revenue_contract, conn, ns(
        company_id=c1, customer_name="Northwind Analytics", total_value="10000.00",
        contract_number="NW-1", start_date="2026-01-01", end_date="2026-12-31"))
    call_action(mod.add_revenue_contract, conn, ns(
        company_id=c1, customer_name="Contoso Retail", total_value="2500.00",
        contract_number="CR-1", start_date="2026-01-01", end_date="2026-06-30"))
    obs = {}
    for name in ("Licence", "Support", "Implementation"):
        o = call_action(mod.add_performance_obligation, conn, ns(
            contract_id=x["id"], company_id=c1, name=name, standalone_price="1000.00",
            recognition_method="over_time", recognition_basis="time"))
        obs[name] = o["id"]
    assert is_ok(call_action(mod.satisfy_performance_obligation, conn, ns(
        id=obs["Support"], pct_complete="100")))
    assert is_ok(call_action(mod.satisfy_performance_obligation, conn, ns(
        id=obs["Implementation"], pct_complete="40")))
    # Company 2: one contract with one unsatisfied obligation.
    z = call_action(mod.add_revenue_contract, conn, ns(
        company_id=c2, customer_name="Fabrikam", total_value="800.00",
        contract_number="FB-1", start_date="2026-01-01", end_date="2026-02-28"))
    call_action(mod.add_performance_obligation, conn, ns(
        contract_id=z["id"], company_id=c2, name="Audit", standalone_price="800.00",
        recognition_method="point_in_time", recognition_basis="output"))

    # Leases. Company 1: draft, active with ROU, active without ROU, terminated
    # without ROU. Company 2: active with ROU.
    _lease(conn, c1, "Draft lessee")
    _lease(conn, c1, "Active with ROU", status="active", rou=True)
    _lease(conn, c1, "Active without ROU", status="active")
    _lease(conn, c1, "Terminated without ROU", status="terminated")
    _lease(conn, c2, "Company two lessee", status="active", rou=True)

    # Intercompany. Company 1: draft, approved, posted. Company 2: draft.
    _ic(conn, c1, c1, c2)
    _ic(conn, c1, c1, c2, approve=True)
    _ic(conn, c1, c1, c2, approve=True, post=True)
    _ic(conn, c2, c2, c1)

    # Consolidation groups: two for company 1, one for company 2.
    for company_id, name in ((c1, "North America"), (c1, "Europe"), (c2, "Holding")):
        assert is_ok(call_action(mod.add_consolidation_group, conn, ns(
            company_id=company_id, name=name, parent_company_id=company_id,
            consolidation_currency="USD")))
    return obs


def _dashboard(conn, company_id):
    r = call_action(mod.standards_compliance_dashboard, conn, ns(company_id=company_id))
    assert is_ok(r), r
    return {k: r[k] for k in ("report", "asc_606", "asc_842", "intercompany",
                              "consolidation")}


def _figures(contracts, unsatisfied, active, no_rou, unposted, groups):
    return {
        "report": "standards_compliance_dashboard",
        "asc_606": {"revenue_contracts": contracts, "unsatisfied_obligations": unsatisfied},
        "asc_842": {"active_leases": active, "leases_without_rou_calculation": no_rou},
        "intercompany": {"unposted_transactions": unposted},
        "consolidation": {"active_groups": groups},
    }


def test_standards_compliance_dashboard_figures_per_company(conn, env):
    obs = _seed_dashboard(conn, env)
    # The seeded states the dashboard counts, read back from the tables.
    statuses = sorted(
        (r["name"], r["obligation_status"], r["pct_complete"]) for r in conn.execute(
            "SELECT name, obligation_status, pct_complete FROM advacct_performance_obligation "
            "WHERE company_id = ?", (env["company_id"],)).fetchall())
    assert statuses == [("Implementation", "partially_satisfied", "40"),
                        ("Licence", "unsatisfied", "0"),
                        ("Support", "satisfied", "100")]
    leases = sorted(
        (r["lessee_name"], r["lease_status"], r["rou_asset_value"] is None)
        for r in conn.execute(
            "SELECT lessee_name, lease_status, rou_asset_value FROM advacct_lease "
            "WHERE company_id = ?", (env["company_id"],)).fetchall())
    assert leases == [("Active with ROU", "active", False),
                      ("Active without ROU", "active", True),
                      ("Draft lessee", "draft", True),
                      ("Terminated without ROU", "terminated", True)]
    ic = sorted(r["ic_status"] for r in conn.execute(
        "SELECT ic_status FROM advacct_ic_transaction WHERE company_id = ?",
        (env["company_id"],)).fetchall())
    assert ic == ["approved", "draft", "posted"]
    before = _snapshot(conn)

    assert _dashboard(conn, env["company_id"]) == _figures(2, 2, 2, 2, 2, 2)
    assert _dashboard(conn, env["company2_id"]) == _figures(1, 1, 1, 0, 1, 1)
    r_none = call_action(mod.standards_compliance_dashboard, conn,
                         ns(company_id=None))
    assert is_error(r_none)
    assert r_none["error"] == ("Multiple companies found. "
                               "Please specify the company by name.")
    r_unknown = call_action(mod.standards_compliance_dashboard, conn,
                            ns(company_id="no-such-company"))
    assert is_error(r_unknown)
    assert r_unknown["error"] == "Company not found: no-such-company"
    # The dashboard itself writes nothing.
    assert _snapshot(conn) == before

    # Satisfying the partially satisfied obligation drops it from the count.
    assert is_ok(call_action(mod.satisfy_performance_obligation, conn, ns(
        id=obs["Implementation"], pct_complete="100")))
    assert _dashboard(conn, env["company_id"])["asc_606"] == {
        "revenue_contracts": 2, "unsatisfied_obligations": 1}

def test_update_performance_obligation_audit_row_carries_old_and_new_values(conn, env):
    contract_id, a_id, b_id, _ = _build_contract(conn, env)
    r = call_action(mod.update_performance_obligation, conn, ns(
        id=b_id, standalone_price="3200.00"))
    assert is_ok(r), r
    assert r["updated_fields"] == ["standalone_price"]
    rows = conn.execute(
        "SELECT old_values, new_values FROM audit_log "
        "WHERE action = ? AND entity_id = ?",
        ("update-performance-obligation", b_id)).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["old_values"] or "{}") == {
        "standalone_price": "3000.00"}
    assert json.loads(rows[0]["new_values"] or "{}") == {
        "standalone_price": "3200.00"}
