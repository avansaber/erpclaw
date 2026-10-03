"""Revenue schedule amounts re-spread from the allocation; recognition posts.

Contract "Northwind Analytics", 2026-01-01 to 2026-03-31, total 10000.00:

  obligation A  "Platform subscription"  standalone 7000.00  allocated 7000.00
  obligation B  "Onboarding"             standalone 3000.00  allocated 3000.00

calculate-revenue-schedule spreads A over three months:
  2026-01-01  2333.33
  2026-02-01  2333.33
  2026-03-01  2333.34   (the last period carries the rounding remainder)

recognize-schedule-entry posts its own deferred-revenue / revenue voucher
(DR deferred revenue / CR revenue for the entry amount) and flags the entry
in one transaction: either both happen or neither does. update-schedule-amounts
takes no --amount; it re-spreads the obligation's allocated price over the
unrecognized periods. Prices are finite, non-negative amounts with at most
two decimal places.
"""
import json
from decimal import Decimal

from advacct_helpers import (
    call_action, ns, is_error, is_ok, load_db_query, seed_recognition_ledger,
)

from erpclaw_lib.query import P, Q, Table, fn

mod = load_db_query()

BOTH_ACCOUNTS = ("--deferred-revenue-account-id and --revenue-account-id are required: "
                 "recognizing revenue posts DR deferred revenue / CR revenue")
NO_AMOUNT = ("update-schedule-amounts takes no --amount: it re-spreads the obligation's "
             "allocated price over its unrecognized periods; set the allocation with "
             "update-performance-obligation --allocated-price")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

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


def _snapshot(conn):
    snap = {}
    for name in ("advacct_revenue_schedule", "advacct_performance_obligation",
                 "advacct_revenue_contract", "gl_entry", "audit_log"):
        t = Table(name)
        rows = conn.execute(Q.from_(t).select(t.star).get_sql()).fetchall()
        snap[name] = sorted(repr(dict(r)) for r in rows)
    return snap


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


def _audit_new_values(conn, action, entity_id):
    t = Table("audit_log")
    rows = conn.execute(
        Q.from_(t).select(t.new_values)
        .where(t.action == P()).where(t.entity_id == P()).get_sql(),
        (action, entity_id)).fetchall()
    return [json.loads(r["new_values"]) for r in rows]


def _audit_old_values(conn, action, entity_id):
    t = Table("audit_log")
    rows = conn.execute(
        Q.from_(t).select(t.old_values)
        .where(t.action == P()).where(t.entity_id == P()).get_sql(),
        (action, entity_id)).fetchall()
    return [json.loads(r["old_values"]) for r in rows]


def _set_schedule_amount(conn, entry_id, amount):
    t = Table("advacct_revenue_schedule")
    conn.execute(
        Q.update(t).set(t.amount, P()).where(t.id == P()).get_sql(),
        (amount, entry_id))
    conn.commit()


def _set_allocated_price(conn, ob_id, price):
    t = Table("advacct_performance_obligation")
    conn.execute(
        Q.update(t).set(t.allocated_price, P()).where(t.id == P()).get_sql(),
        (price, ob_id))
    conn.commit()


def _debit_sum(conn):
    t = Table("gl_entry")
    rows = conn.execute(
        Q.from_(t).select(t.debit).get_sql()).fetchall()
    return sum((Decimal(r["debit"]) for r in rows), Decimal("0"))


# ---------------------------------------------------------------------------
# recognize-schedule-entry posts its voucher together with the flag
# ---------------------------------------------------------------------------

def test_recognize_posts_voucher_and_flags_together(conn, env):
    _, a_id, _, (jan, feb, mar) = _build_contract(conn, env)
    ledger = seed_recognition_ledger(conn, env["company_id"])
    before = _snapshot(conn)

    r = call_action(mod.recognize_schedule_entry, conn,
                    ns(id=feb, **_acc(ledger)))
    assert r["status"] == "ok" and r["id"] == feb
    assert r["recognized"] == 1 and r["amount"] == "2333.33"
    gl_ids = r["gl_entry_ids"]
    assert isinstance(gl_ids, list) and len(gl_ids) == 2
    assert r == {"status": "ok", "id": feb, "recognized": 1,
                 "amount": "2333.33", "gl_entry_ids": gl_ids}

    assert sorted(_voucher_legs(conn, feb)) == sorted([
        (ledger["deferred_revenue_account_id"], "2333.33", "0.00",
         "2026-02-01", None, 0),
        (ledger["revenue_account_id"], "0.00", "2333.33",
         "2026-02-01", ledger["cost_center_id"], 0),
    ])
    assert _schedule(conn, a_id) == [
        ("2026-01-01", "2333.33", 0),
        ("2026-02-01", "2333.33", 1),
        ("2026-03-01", "2333.34", 0),
    ]
    assert _audit_new_values(conn, "recognize-schedule-entry", feb) == [
        {"recognized": 1, "gl_entry_ids": gl_ids}]
    assert _gl_count(conn) == 2
    assert len(before["gl_entry"]) + 2 == len(_snapshot(conn)["gl_entry"])


def test_recognize_refuses_without_accounts(conn, env):
    _, a_id, _, (jan, feb, mar) = _build_contract(conn, env)
    seed_recognition_ledger(conn, env["company_id"])
    before = _snapshot(conn)

    r = call_action(mod.recognize_schedule_entry, conn, ns(id=feb))
    assert is_error(r)
    assert r["message"] == BOTH_ACCOUNTS
    assert _snapshot(conn) == before


def test_recognize_refuses_accounts_with_the_wrong_role(conn, env):
    _, a_id, _, (jan, feb, mar) = _build_contract(conn, env)
    ledger = seed_recognition_ledger(conn, env["company_id"])
    before = _snapshot(conn)

    r = call_action(mod.recognize_schedule_entry, conn, ns(
        id=feb,
        deferred_revenue_account_id=ledger["revenue_account_id"],
        revenue_account_id=ledger["deferred_revenue_account_id"],
        cost_center_id=ledger["cost_center_id"]))
    assert is_error(r)
    assert r["message"] == (
        f"Deferred revenue account {ledger['revenue_account_id']} must be "
        f"a liability account, not income")
    assert _snapshot(conn) == before

    r = call_action(mod.recognize_schedule_entry, conn, ns(
        id=feb,
        deferred_revenue_account_id=ledger["deferred_revenue_account_id"],
        revenue_account_id=ledger["deferred_revenue_account_id"],
        cost_center_id=ledger["cost_center_id"]))
    assert is_error(r)
    assert r["message"] == (
        f"Revenue account {ledger['deferred_revenue_account_id']} must be "
        f"an income account, not liability")
    assert _snapshot(conn) == before


def test_recognize_refuses_another_companys_account(conn, env):
    _, a_id, _, (jan, feb, mar) = _build_contract(conn, env)
    ledger = seed_recognition_ledger(conn, env["company_id"])
    other = seed_recognition_ledger(conn, env["company2_id"])
    before = _snapshot(conn)

    r = call_action(mod.recognize_schedule_entry, conn, ns(
        id=feb,
        deferred_revenue_account_id=other["deferred_revenue_account_id"],
        revenue_account_id=ledger["revenue_account_id"],
        cost_center_id=ledger["cost_center_id"]))
    assert is_error(r)
    assert r["message"] == (
        f"Account {other['deferred_revenue_account_id']} not found "
        f"in company {env['company_id']}")
    assert _snapshot(conn) == before


def test_recognize_ledger_refusal_writes_nothing(conn, env):
    _, a_id, _, (jan, feb, mar) = _build_contract(conn, env)
    ledger = seed_recognition_ledger(conn, env["company_id"])
    before = _snapshot(conn)

    r = call_action(mod.recognize_schedule_entry, conn, ns(
        id=feb,
        deferred_revenue_account_id=ledger["deferred_revenue_account_id"],
        revenue_account_id=ledger["revenue_account_id"],
        cost_center_id=None))
    assert is_error(r)
    assert r["message"].startswith(
        f"Revenue schedule entry {feb} was not recognized: "
        f"the ledger posting was refused: GL Validation Step 6 Failed:")
    assert _snapshot(conn) == before


def test_recognize_refuses_an_invalid_stored_amount(conn, env):
    _, a_id, _, (jan, feb, mar) = _build_contract(conn, env)
    ledger = seed_recognition_ledger(conn, env["company_id"])
    _set_schedule_amount(conn, mar, "NaN")
    before = _snapshot(conn)

    r = call_action(mod.recognize_schedule_entry, conn,
                    ns(id=mar, **_acc(ledger)))
    assert is_error(r)
    assert r["message"] == (
        f"Revenue schedule entry {mar} was not recognized: "
        f"the ledger posting was refused: Revenue schedule entry {mar} "
        f"has amount NaN; only a positive two-decimal amount can be recognized")
    assert _snapshot(conn) == before


# ---------------------------------------------------------------------------
# generate-revenue-entries posts every open entry or nothing
# ---------------------------------------------------------------------------

def test_generate_posts_every_open_entry(conn, env):
    _, a_id, _, (jan, feb, mar) = _build_contract(conn, env)
    ledger = seed_recognition_ledger(conn, env["company_id"])
    first = call_action(mod.recognize_schedule_entry, conn,
                        ns(id=jan, **_acc(ledger)))
    assert is_ok(first), first

    g = call_action(mod.generate_revenue_entries, conn,
                    ns(obligation_id=a_id, **_acc(ledger)))
    assert is_ok(g), g
    assert (g["recognized_count"], g["total_recognized"],
            g["gl_entry_count"]) == (2, "4666.67", 4)

    feb_legs = _voucher_legs(conn, feb)
    assert sorted(leg[1:3] for leg in feb_legs) == [
        ("0.00", "2333.33"), ("2333.33", "0.00")]
    mar_legs = _voucher_legs(conn, mar)
    assert sorted(leg[1:3] for leg in mar_legs) == [
        ("0.00", "2333.34"), ("2333.34", "0.00")]
    assert _schedule(conn, a_id) == [
        ("2026-01-01", "2333.33", 1),
        ("2026-02-01", "2333.33", 1),
        ("2026-03-01", "2333.34", 1),
    ]
    assert _debit_sum(conn) == Decimal("7000.00")


def test_generate_is_all_or_nothing(conn, env):
    _, a_id, _, (jan, feb, mar) = _build_contract(conn, env)
    ledger = seed_recognition_ledger(conn, env["company_id"])
    _set_schedule_amount(conn, mar, "-5.00")
    before = _snapshot(conn)

    g = call_action(mod.generate_revenue_entries, conn,
                    ns(obligation_id=a_id, **_acc(ledger)))
    assert is_error(g)
    assert g["message"] == (
        f"Revenue for obligation {a_id} was not recognized: "
        f"schedule entry {mar} has amount -5.00; "
        f"only a positive two-decimal amount can be recognized")
    assert _schedule(conn, a_id) == [
        ("2026-01-01", "2333.33", 0),
        ("2026-02-01", "2333.33", 0),
        ("2026-03-01", "-5.00", 0),
    ]
    assert _gl_count(conn) == 0
    assert _snapshot(conn) == before

    g = call_action(mod.generate_revenue_entries, conn,
                    ns(obligation_id=a_id))
    assert is_error(g)
    assert g["message"] == BOTH_ACCOUNTS
    assert _snapshot(conn) == before


# ---------------------------------------------------------------------------
# update-schedule-amounts re-spreads the allocation
# ---------------------------------------------------------------------------

def test_respread_follows_the_allocation(conn, env):
    contract_id, a_id, _, (jan, feb, mar) = _build_contract(conn, env)
    ledger = seed_recognition_ledger(conn, env["company_id"])
    first = call_action(mod.recognize_schedule_entry, conn,
                        ns(id=jan, **_acc(ledger)))
    assert is_ok(first), first

    up = call_action(mod.update_performance_obligation, conn, ns(
        id=a_id, standalone_price=None, allocated_price="7500.00",
        name=None, recognition_method=None, recognition_basis=None))
    assert is_ok(up), up

    r = call_action(mod.update_schedule_amounts, conn, ns(obligation_id=a_id))
    assert r == {"status": "ok", "obligation_id": a_id,
                 "allocated_price": "7500.00", "recognized_amount": "2333.33",
                 "entries_updated": 2, "amounts": ["2583.33", "2583.34"]}

    assert _schedule(conn, a_id) == [
        ("2026-01-01", "2333.33", 1),
        ("2026-02-01", "2583.33", 0),
        ("2026-03-01", "2583.34", 0),
    ]
    amounts = [piece[1] for piece in _schedule(conn, a_id)]
    assert sum((Decimal(item) for item in amounts),
               Decimal("0")) == Decimal("7500.00")
    assert _gl_count(conn) == 2
    assert _audit_old_values(conn, "update-schedule-amounts", a_id) == [
        {"amounts": ["2333.33", "2333.34"]}]


def test_respread_refusals_write_nothing(conn, env):
    _, a_id, _, (jan, feb, mar) = _build_contract(conn, env)
    ledger = seed_recognition_ledger(conn, env["company_id"])

    before = _snapshot(conn)
    r = call_action(mod.update_schedule_amounts, conn, ns(
        obligation_id=a_id, amount="2500.00"))
    assert is_error(r)
    assert r["message"] == NO_AMOUNT
    assert _snapshot(conn) == before

    first = call_action(mod.recognize_schedule_entry, conn,
                        ns(id=jan, **_acc(ledger)))
    assert is_ok(first), first
    up = call_action(mod.update_performance_obligation, conn, ns(
        id=a_id, standalone_price=None, allocated_price="2000.00",
        name=None, recognition_method=None, recognition_basis=None))
    assert is_ok(up), up

    before = _snapshot(conn)
    r = call_action(mod.update_schedule_amounts, conn, ns(obligation_id=a_id))
    assert is_error(r)
    assert r["message"] == (
        f"Allocated price 2000.00 is below the 2333.33 already recognized "
        f"for obligation {a_id}")
    assert _snapshot(conn) == before

    _set_allocated_price(conn, a_id, "NaN")
    before = _snapshot(conn)
    r = call_action(mod.update_schedule_amounts, conn, ns(obligation_id=a_id))
    assert is_error(r)
    assert r["message"] == (
        f"Performance obligation {a_id} has an invalid allocated price: NaN")
    assert _snapshot(conn) == before


def test_prices_must_be_non_negative_two_decimal_amounts(conn, env):
    _, a_id, _, (jan, feb, mar) = _build_contract(conn, env)
    seed_recognition_ledger(conn, env["company_id"])
    before = _snapshot(conn)

    cases = [
        ({"allocated_price": "-100"}, "Invalid allocated-price: -100"),
        ({"allocated_price": "33.333"}, "Invalid allocated-price: 33.333"),
        ({"allocated_price": "1e3"}, "Invalid allocated-price: 1e3"),
        ({"standalone_price": "-1.00"}, "Invalid standalone-price: -1.00"),
    ]
    for prices, message in cases:
        args = {"id": a_id, "standalone_price": None,
                "allocated_price": None, "name": None,
                "recognition_method": None, "recognition_basis": None}
        args.update(prices)
        r = call_action(mod.update_performance_obligation, conn, ns(**args))
        assert is_error(r), (prices, r)
        assert r["message"] == message, (prices, r)
        assert _snapshot(conn) == before


def test_respread_of_a_fully_recognized_obligation_is_a_no_op(conn, env):
    _, a_id, _, (jan, feb, mar) = _build_contract(conn, env)
    ledger = seed_recognition_ledger(conn, env["company_id"])
    g = call_action(mod.generate_revenue_entries, conn,
                    ns(obligation_id=a_id, **_acc(ledger)))
    assert is_ok(g), g

    r = call_action(mod.update_schedule_amounts, conn, ns(obligation_id=a_id))
    assert is_ok(r), r
    assert r["entries_updated"] == 0
    assert r["amounts"] == []
    assert _audit_new_values(conn, "update-schedule-amounts", a_id) == []
