"""Behavioural depth for five erpclaw-hr actions (task m490).

Each action below previously had only a routability test (the contract
suite's ``"Unknown action" not in ...``); reject-expense-claim and
update-expense-claim-status additionally had behaviour tests that read the
database back through raw SQL string literals. Neither form observes the
database through the seam, so an action could return a perfect envelope while
writing nothing and stay green. Every test here reads the stored rows back
with PyPika-built queries through ``erpclaw_lib.query`` on a connection from
``erpclaw_lib.db.get_connection`` and compares exact values; money is compared
as exact ``Decimal`` strings, never float.

Per-action depth (stored row vs ledger effect):

- add-holiday-list: stored rows (holiday_list header + holiday children +
  audit). Posts no ledger rows; the test asserts the ledgers are untouched.
- bulk-mark-attendance: stored rows (one attendance row per entry + duplicate
  accounting on re-run). Posts no ledger rows; asserted untouched.
- record-lifecycle-event: stored row (event with JSON round-trip + audit)
  and, for exit events, the employee side-effect (status left +
  date_of_exit). Posts no ledger rows; asserted untouched.
- reject-expense-claim: stored row (submitted -> rejected + audit) with the
  totals, items and approval fields proved unchanged. A rejection posts no
  ledger rows; the test asserts the voucher has no legs before or after.
- update-expense-claim-status: refusal for approved -> paid (the claim's
  payable sits in the payment ledger, so only a submitted payment allocated
  to it marks it paid; both legs asserted exact and balanced) and stored
  rows (draft/submitted -> cancelled + audit). The refusal posts no new legs
  and writes no audit row; the test proves the posting underneath is
  byte-identical.

No test in this file inspects catalog tables or sets connection options;
reads are PyPika-built and run on a connection from
``erpclaw_lib.db.get_connection``.
"""
import json
import os
import sys
import uuid
from decimal import Decimal

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from hr_helpers import (  # noqa: E402
    build_hr_env, call_action, is_error, is_ok, load_db_query, ns,
    seed_account,
)
from erpclaw_lib.db import get_connection  # noqa: E402
from erpclaw_lib.query import Field, P, Q, Table  # noqa: E402

H = load_db_query()

HOL_FROM = "2026-01-01"
HOL_TO = "2026-12-31"
ATT_DATE = "2026-03-03"
CLAIM_DATE = "2026-03-02"


@pytest.fixture
def conn(db_path):
    connection = get_connection(db_path)
    yield connection
    connection.close()


@pytest.fixture
def env(conn):
    return build_hr_env(conn)


def _msg(result):
    return result.get("message", "")


def _row(conn, table, row_id):
    t = Table(table)
    q = Q.from_(t).select(t.star).where(t.id == P())
    found = conn.execute(q.get_sql(), (row_id,)).fetchone()
    assert found is not None, f"{table} {row_id} not found"
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


def _snapshot(conn, tables):
    return {name: _all(conn, name) for name in tables}


def _legs(conn, voucher_id):
    """GL legs for one expense_claim voucher, as plain dicts."""
    t = Table("gl_entry")
    q = (Q.from_(t)
         .select(t.account_id, t.debit, t.credit, t.voucher_type, t.voucher_id,
                 t.posting_date, t.party_type, t.party_id, t.is_cancelled,
                 t.fiscal_year, t.cost_center_id)
         .where(t.voucher_type == P())
         .where(t.voucher_id == P()))
    return [dict(r) for r in
            conn.execute(q.get_sql(), ("expense_claim", voucher_id)).fetchall()]


def _leg_key(leg):
    return (leg["account_id"], leg["debit"], leg["credit"])


def _audit(conn, action, entity_id):
    rows = _where(conn, "audit_log", action=action, entity_id=entity_id)
    mine = [r for r in rows if r["skill"] == "erpclaw-hr"]
    assert len(mine) == 1, f"expected one {action} audit row, saw {len(mine)}"
    return mine[0]


_HR_TABLES = ("holiday_list", "holiday", "attendance", "employee",
              "expense_claim", "expense_claim_item",
              "employee_lifecycle_event", "naming_series", "audit_log")
_LEDGERS = ("gl_entry", "payment_ledger_entry", "stock_ledger_entry")


def _employee(conn, env, first, last):
    r = call_action(H.add_employee, conn, ns(
        first_name=first, last_name=last, date_of_birth=None, gender=None,
        date_of_joining="2025-01-01", employment_type=None,
        company_id=env["company_id"], department_id=None, designation_id=None,
        employee_grade_id=None, branch=None, reporting_to=None,
        company_email=None, personal_email=None, cell_phone=None,
        emergency_contact=None, bank_details=None, ssn=None,
        federal_filing_status=None, w4_allowances=None,
        holiday_list_id=None, payroll_cost_center_id=None))
    assert is_ok(r), r
    return r["employee_id"]


def _claim(conn, env, dana, travel):
    items = [
        {"expense_type": "travel", "description": "Flight to Denver",
         "amount": "412.35", "account_id": travel},
        {"expense_type": "meals", "description": "Client dinner",
         "amount": "87.65"},
    ]
    r = call_action(H.add_expense_claim, conn, ns(
        employee_id=dana, expense_date=CLAIM_DATE,
        company_id=env["company_id"], items=json.dumps(items)))
    assert is_ok(r), r
    return r["expense_claim_id"]


def _submitted(conn, env, dana, travel):
    claim_id = _claim(conn, env, dana, travel)
    r = call_action(H.submit_expense_claim, conn, ns(
        expense_claim_id=claim_id))
    assert is_ok(r), r
    return claim_id


def _approved(conn, env, dana, evan, travel):
    claim_id = _submitted(conn, env, dana, travel)
    r = call_action(H.approve_expense_claim, conn, ns(
        expense_claim_id=claim_id, approved_by=evan))
    assert is_ok(r), r
    return claim_id


def _claim_row(conn, claim_id):
    r = _row(conn, "expense_claim", claim_id)
    return {k: r[k] for k in ("status", "total_amount", "approved_by",
                              "approval_date", "payment_entry_id")}


def _claim_items(conn, claim_id):
    rows = _where(conn, "expense_claim_item", expense_claim_id=claim_id)
    return sorted((r["expense_type"], r["amount"], r["account_id"])
                  for r in rows)


def _expense_setup(conn, env):
    dana = _employee(conn, env, "Dana", "Reyes")
    evan = _employee(conn, env, "Evan", "Cole")
    travel = seed_account(conn, env["company_id"], "Travel Expense",
                          "expense", "expense", "5100")
    return dana, evan, travel


# ---------------------------------------------------------------------------
# add-holiday-list — stored rows (no ledger: a holiday list is master data;
# it posts no GL, payment-ledger or stock rows).
# ---------------------------------------------------------------------------

class TestAddHolidayListDepth:
    def test_create_stores_list_children_and_audit(self, conn, env):
        ledgers_before = _snapshot(conn, _LEDGERS)

        r = call_action(H.add_holiday_list, conn, ns(
            name="Festival Calendar 2026", company_id=env["company_id"],
            from_date=HOL_FROM, to_date=HOL_TO,
            holidays=json.dumps([
                {"date": "2026-01-01", "description": "New Year"},
                {"date": "2026-12-25"},
                {"date": "2027-01-01", "description": "Out of range"},
                {"date": "not-a-date", "description": "Invalid"},
                {"description": "No date at all"},
            ])))
        assert is_ok(r), r
        assert r["holiday_count"] == 2
        hl_id = r["holiday_list_id"]

        parent = _row(conn, "holiday_list", hl_id)
        assert (parent["name"], parent["from_date"], parent["to_date"],
                parent["company_id"]) == (
            "Festival Calendar 2026", HOL_FROM, HOL_TO, env["company_id"])

        children = _where(conn, "holiday", holiday_list_id=hl_id)
        assert sorted((c["holiday_date"], c["description"]) for c in children) == [
            ("2026-01-01", "New Year"),
            ("2026-12-25", ""),
        ]

        audit = _audit(conn, "add-holiday-list", hl_id)
        assert audit["entity_type"] == "holiday_list"
        assert audit["old_values"] is None
        assert json.loads(audit["new_values"]) == {
            "name": "Festival Calendar 2026",
            "company_id": env["company_id"],
            "from_date": HOL_FROM, "to_date": HOL_TO, "holiday_count": 2}

        assert _snapshot(conn, _LEDGERS) == ledgers_before

    def test_create_refusals_leave_the_database_identical(self, conn, env):
        pinned = call_action(H.add_holiday_list, conn, ns(
            name="Pinned List 2026", company_id=env["company_id"],
            from_date=HOL_FROM, to_date=HOL_TO,
            holidays=json.dumps([{"date": "2026-01-01"}])))
        assert is_ok(pinned), pinned
        snapshot = _snapshot(conn, _HR_TABLES + _LEDGERS)

        r = call_action(H.add_holiday_list, conn, ns(
            name=None, company_id=env["company_id"],
            from_date=HOL_FROM, to_date=HOL_TO, holidays=None))
        assert is_error(r)
        assert _msg(r) == "--name is required"

        r = call_action(H.add_holiday_list, conn, ns(
            name="Backwards 2026", company_id=env["company_id"],
            from_date=HOL_TO, to_date=HOL_FROM, holidays=None))
        assert is_error(r)
        assert _msg(r) == (f"from-date ({HOL_TO}) must be on or before "
                           f"to-date ({HOL_FROM})")

        r = call_action(H.add_holiday_list, conn, ns(
            name="Bad Format 2026", company_id=env["company_id"],
            from_date="01/01/2026", to_date=HOL_TO, holidays=None))
        assert is_error(r)
        assert _msg(r) == "Invalid from-date format: 01/01/2026. Use YYYY-MM-DD"

        r = call_action(H.add_holiday_list, conn, ns(
            name="No Company 2026", company_id="no-such-company",
            from_date=HOL_FROM, to_date=HOL_TO, holidays=None))
        assert is_error(r)
        assert _msg(r) == "Company no-such-company not found"

        r = call_action(H.add_holiday_list, conn, ns(
            name="Pinned List 2026", company_id=env["company_id"],
            from_date=HOL_FROM, to_date=HOL_TO, holidays=None))
        assert is_error(r)
        assert _msg(r) == (
            f"Holiday list 'Pinned List 2026' already exists "
            f"(id: {pinned['holiday_list_id']})")

        assert _snapshot(conn, _HR_TABLES + _LEDGERS) == snapshot


# ---------------------------------------------------------------------------
# bulk-mark-attendance — stored rows (no ledger: attendance rows are
# operational facts; they post no GL, payment-ledger or stock rows).
# ---------------------------------------------------------------------------

class TestBulkMarkAttendanceDepth:
    def test_bulk_creates_one_row_per_employee(self, conn, env):
        amy = _employee(conn, env, "Amy", "Chen")
        ben = _employee(conn, env, "Ben", "Diaz")
        ledgers_before = _snapshot(conn, _LEDGERS)

        r = call_action(H.bulk_mark_attendance, conn, ns(
            date=ATT_DATE,
            entries=json.dumps([
                {"employee_id": amy, "status": "present"},
                {"employee_id": ben, "status": "absent"},
            ]),
            source="biometric"))
        assert is_ok(r), r
        assert (r["date"], r["total"], r["created"],
                r["skipped_duplicates"], r["errors"]) == (
            ATT_DATE, 2, 2, 0, [])

        rows = _where(conn, "attendance", attendance_date=ATT_DATE)
        assert sorted((x["employee_id"], x["status"], x["source"],
                       x["late_entry"], x["early_exit"]) for x in rows) == sorted([
            (amy, "present", "biometric", 0, 0),
            (ben, "absent", "biometric", 0, 0),
        ])

        again = call_action(H.bulk_mark_attendance, conn, ns(
            date=ATT_DATE,
            entries=json.dumps([
                {"employee_id": amy, "status": "present"},
                {"employee_id": ben, "status": "absent"},
            ]),
            source="biometric"))
        assert is_ok(again), again
        assert (again["created"], again["skipped_duplicates"],
                again["errors"]) == (0, 2, [])
        assert len(_where(conn, "attendance", attendance_date=ATT_DATE)) == 2

        assert _snapshot(conn, _LEDGERS) == ledgers_before

    def test_per_entry_problems_are_reported_not_silent(self, conn, env):
        amy = _employee(conn, env, "Amy", "Chen")

        r = call_action(H.bulk_mark_attendance, conn, ns(
            date=ATT_DATE,
            entries=json.dumps([
                {"employee_id": amy, "status": "present"},
                {"employee_id": amy, "status": "sleeping"},
                {"employee_id": "no-such-employee", "status": "present"},
                {"employee_id": amy},
            ]),
            source=None))
        assert is_ok(r), r
        assert (r["total"], r["created"]) == (4, 1)
        assert r["errors"] == [
            "Entry 1: invalid status 'sleeping'",
            "Entry 2: employee no-such-employee not found",
            "Entry 3: missing employee_id or status",
        ]

        rows = _where(conn, "attendance", attendance_date=ATT_DATE)
        assert [(x["employee_id"], x["status"], x["source"]) for x in rows] == [
            (amy, "present", "manual"),
        ]

    def test_bulk_refusals_leave_the_database_identical(self, conn, env):
        amy = _employee(conn, env, "Amy", "Chen")
        good = json.dumps([{"employee_id": amy, "status": "present"}])
        snapshot = _snapshot(conn, _HR_TABLES + _LEDGERS)

        r = call_action(H.bulk_mark_attendance, conn, ns(
            date=None, entries=good, source=None))
        assert is_error(r)
        assert _msg(r) == "--date is required"

        r = call_action(H.bulk_mark_attendance, conn, ns(
            date="03/03/2026", entries=good, source=None))
        assert is_error(r)
        assert _msg(r) == "Invalid date format: 03/03/2026. Use YYYY-MM-DD"

        r = call_action(H.bulk_mark_attendance, conn, ns(
            date=ATT_DATE, entries=None, source=None))
        assert is_error(r)
        assert _msg(r) == "--entries is required (JSON array)"

        r = call_action(H.bulk_mark_attendance, conn, ns(
            date=ATT_DATE, entries="[]", source=None))
        assert is_error(r)
        assert _msg(r) == "--entries must be a non-empty JSON array"

        r = call_action(H.bulk_mark_attendance, conn, ns(
            date=ATT_DATE, entries="not-json", source=None))
        assert is_error(r)
        assert _msg(r) == "Invalid JSON for --entries: not-json"

        r = call_action(H.bulk_mark_attendance, conn, ns(
            date=ATT_DATE, entries=good, source="pigeon"))
        assert is_error(r)
        assert _msg(r) == ("Invalid attendance source 'pigeon'. Valid: "
                           "('manual', 'biometric', 'app')")

        assert _snapshot(conn, _HR_TABLES + _LEDGERS) == snapshot


# ---------------------------------------------------------------------------
# record-lifecycle-event — stored row (no ledger: lifecycle events are HR
# facts; they post no GL, payment-ledger or stock rows). Exit events carry
# an employee side-effect (status -> left, date_of_exit stamped).
# ---------------------------------------------------------------------------

class TestRecordLifecycleEventDepth:
    def test_promotion_stores_event_and_leaves_employee_active(
            self, conn, env):
        amy = _employee(conn, env, "Amy", "Chen")
        before = _row(conn, "employee", amy)
        assert (before["status"], before["date_of_exit"]) == ("active", None)
        ledgers_before = _snapshot(conn, _LEDGERS)

        r = call_action(H.record_lifecycle_event, conn, ns(
            employee_id=amy, event_type="promotion", event_date="2026-04-01",
            details=json.dumps({"title": "Senior Analyst"}),
            old_values=json.dumps({"level": "L1"}),
            new_values=json.dumps({"level": "L2"})))
        assert is_ok(r), r
        assert (r["employee_id"], r["event_type"],
                r["event_date"]) == (amy, "promotion", "2026-04-01")
        assert "employee_status_updated" not in r

        event = _row(conn, "employee_lifecycle_event", r["event_id"])
        assert (event["employee_id"], event["event_type"],
                event["event_date"]) == (amy, "promotion", "2026-04-01")
        assert json.loads(event["details"]) == {"title": "Senior Analyst"}
        assert json.loads(event["old_values"]) == {"level": "L1"}
        assert json.loads(event["new_values"]) == {"level": "L2"}

        after = _row(conn, "employee", amy)
        assert (after["status"], after["date_of_exit"]) == ("active", None)

        audit = _audit(conn, "record-lifecycle-event", r["event_id"])
        assert audit["entity_type"] == "employee_lifecycle_event"
        assert json.loads(audit["new_values"]) == {
            "employee_id": amy, "event_type": "promotion",
            "event_date": "2026-04-01", "employee_status_updated": False}

        assert _snapshot(conn, _LEDGERS) == ledgers_before

    def test_resignation_marks_employee_left_with_exit_date(self, conn, env):
        amy = _employee(conn, env, "Amy", "Chen")
        ledgers_before = _snapshot(conn, _LEDGERS)

        r = call_action(H.record_lifecycle_event, conn, ns(
            employee_id=amy, event_type="resignation",
            event_date="2026-05-01", details=None, old_values=None,
            new_values=None))
        assert is_ok(r), r
        assert (r["employee_status_updated"], r["new_employee_status"],
                r["date_of_exit"]) == (True, "left", "2026-05-01")

        event = _row(conn, "employee_lifecycle_event", r["event_id"])
        assert (event["employee_id"], event["event_type"],
                event["event_date"]) == (amy, "resignation", "2026-05-01")
        assert (event["details"], event["old_values"],
                event["new_values"]) == (None, None, None)

        after = _row(conn, "employee", amy)
        assert (after["status"], after["date_of_exit"]) == ("left", "2026-05-01")

        assert _snapshot(conn, _LEDGERS) == ledgers_before

    def test_event_refusals_leave_the_database_identical(self, conn, env):
        amy = _employee(conn, env, "Amy", "Chen")
        snapshot = _snapshot(conn, _HR_TABLES + _LEDGERS)

        r = call_action(H.record_lifecycle_event, conn, ns(
            employee_id=None, event_type="promotion",
            event_date="2026-04-01", details=None, old_values=None,
            new_values=None))
        assert is_error(r)
        assert _msg(r) == "--employee-id is required"

        r = call_action(H.record_lifecycle_event, conn, ns(
            employee_id="no-such-employee", event_type="promotion",
            event_date="2026-04-01", details=None, old_values=None,
            new_values=None))
        assert is_error(r)
        assert _msg(r) == "Employee no-such-employee not found"

        r = call_action(H.record_lifecycle_event, conn, ns(
            employee_id=amy, event_type="graduation",
            event_date="2026-04-01", details=None, old_values=None,
            new_values=None))
        assert is_error(r)
        assert _msg(r) == (
            "Invalid event type 'graduation'. Valid: "
            "('hiring', 'confirmation', 'promotion', 'transfer', "
            "'separation', 'resignation', 'retirement')")

        r = call_action(H.record_lifecycle_event, conn, ns(
            employee_id=amy, event_type="promotion", event_date="04/01/2026",
            details=None, old_values=None, new_values=None))
        assert is_error(r)
        assert _msg(r) == "Invalid event-date format: 04/01/2026. Use YYYY-MM-DD"

        assert _snapshot(conn, _HR_TABLES + _LEDGERS) == snapshot
        assert _row(conn, "employee", amy)["status"] == "active"


# ---------------------------------------------------------------------------
# reject-expense-claim — stored row (no ledger: a rejection is a status
# write; it posts no GL, payment-ledger or stock rows, so the voucher must
# have no legs before or after).
# ---------------------------------------------------------------------------

class TestRejectExpenseClaimDepth:
    def test_reject_stores_rejected_and_posts_nothing(self, conn, env):
        dana, evan, travel = _expense_setup(conn, env)
        claim_id = _submitted(conn, env, dana, travel)
        other = _submitted(conn, env, dana, travel)
        assert _claim_row(conn, claim_id) == {
            "status": "submitted", "total_amount": "500.00",
            "approved_by": None, "approval_date": None,
            "payment_entry_id": None}
        assert Decimal(_claim_row(conn, claim_id)["total_amount"]) == Decimal(
            "500.00")
        assert _claim_items(conn, claim_id) == [
            ("meals", "87.65", None),
            ("travel", "412.35", travel),
        ]
        assert _legs(conn, claim_id) == []
        ledgers_before = _snapshot(conn, _LEDGERS)

        r = call_action(H.reject_expense_claim, conn, ns(
            expense_claim_id=claim_id, reason="Receipts missing"))
        assert is_ok(r), r
        assert (r["expense_claim_id"], r["employee_id"],
                r["rejection_reason"]) == (claim_id, dana, "Receipts missing")

        assert _claim_row(conn, claim_id) == {
            "status": "rejected", "total_amount": "500.00",
            "approved_by": None, "approval_date": None,
            "payment_entry_id": None}
        assert _claim_items(conn, claim_id) == [
            ("meals", "87.65", None),
            ("travel", "412.35", travel),
        ]
        assert _legs(conn, claim_id) == []

        audit = _audit(conn, "reject-expense-claim", claim_id)
        assert audit["entity_type"] == "expense_claim"
        assert json.loads(audit["old_values"]) == {"status": "submitted"}
        assert json.loads(audit["new_values"]) == {
            "status": "rejected", "rejection_reason": "Receipts missing"}

        assert _claim_row(conn, other)["status"] == "submitted"
        assert _snapshot(conn, _LEDGERS) == ledgers_before

    def test_reject_refusals_leave_the_database_identical(self, conn, env):
        dana, evan, travel = _expense_setup(conn, env)
        draft = _claim(conn, env, dana, travel)
        approved = _approved(conn, env, dana, evan, travel)
        paid = _approved(conn, env, dana, evan, travel)
        # m805b: the bare approved -> paid mark is refused for a claim the
        # ledger owes to the employee, so no claim reaches 'paid' here.
        r = call_action(H.update_expense_claim_status, conn, ns(
            expense_claim_id=paid, status="paid",
            payment_entry_id="PAY-0001"))
        assert is_error(r), r
        assert _msg(r) == (
            f"Expense claim {paid} is owed to the employee in the payment "
            "ledger; it is marked paid when a submitted payment is allocated to it")
        rejected = _submitted(conn, env, dana, travel)
        assert is_ok(call_action(H.reject_expense_claim, conn, ns(
            expense_claim_id=rejected, reason="Duplicate")))
        snapshot = _snapshot(conn, _HR_TABLES + _LEDGERS)

        for claim_id, status in ((draft, "draft"), (approved, "approved"),
                                 (paid, "approved"), (rejected, "rejected")):
            r = call_action(H.reject_expense_claim, conn, ns(
                expense_claim_id=claim_id, reason="Too late"))
            assert is_error(r)
            assert _msg(r) == (
                f"Expense claim {claim_id} cannot be rejected. "
                f"Current status: {status} (must be 'submitted')")
            assert _claim_row(conn, claim_id)["status"] == status

        r = call_action(H.reject_expense_claim, conn, ns(
            expense_claim_id=None, reason="Too late"))
        assert is_error(r)
        assert _msg(r) == "--expense-claim-id is required"

        r = call_action(H.reject_expense_claim, conn, ns(
            expense_claim_id="no-such-claim", reason="Too late"))
        assert is_error(r)
        assert _msg(r) == "Expense claim no-such-claim not found"

        assert _snapshot(conn, _HR_TABLES + _LEDGERS) == snapshot
        assert _claim_row(conn, paid)["payment_entry_id"] is None


# ---------------------------------------------------------------------------
# update-expense-claim-status — refusal for approved -> paid (the claim's
# payable sits in the payment ledger, so only a submitted payment allocated
# to it marks it paid; both legs asserted exact and balanced) and stored rows
# for draft/submitted -> cancelled. The refusal posts no new legs and writes
# no audit row; the posting underneath must be byte-identical afterwards.
# ---------------------------------------------------------------------------

class TestUpdateExpenseClaimStatusDepth:
    def test_paid_mark_is_refused_over_a_balanced_posting(self, conn, env):
        dana, evan, travel = _expense_setup(conn, env)
        claim_id = _approved(conn, env, dana, evan, travel)

        legs = _legs(conn, claim_id)
        assert sorted(_leg_key(leg) for leg in legs) == sorted([
            (travel, "412.35", "0.00"),
            (env["expense_account"], "87.65", "0.00"),
            (env["payable_account"], "0.00", "500.00"),
        ])
        assert sum(Decimal(leg["debit"]) for leg in legs) == Decimal("500.00")
        assert sum(Decimal(leg["credit"]) for leg in legs) == Decimal("500.00")
        for leg in legs:
            assert (leg["voucher_type"], leg["voucher_id"],
                    leg["posting_date"], leg["is_cancelled"],
                    leg["fiscal_year"]) == (
                "expense_claim", claim_id, CLAIM_DATE, 0,
                env["fiscal_year_name"])
        by_account = {leg["account_id"]: leg for leg in legs}
        assert (by_account[env["payable_account"]]["party_type"],
                by_account[env["payable_account"]]["party_id"]) == (
            "employee", dana)
        assert (by_account[travel]["party_type"],
                by_account[travel]["party_id"]) == (None, None)
        assert (by_account[travel]["cost_center_id"],
                by_account[env["expense_account"]]["cost_center_id"]) == (
            env["cost_center_id"], env["cost_center_id"])

        row = _claim_row(conn, claim_id)
        assert (row["status"], row["approved_by"],
                row["payment_entry_id"]) == ("approved", evan, None)

        # m805b: the approval posted the payable to the payment ledger, so
        # the bare mark is refused — the claim, its balanced posting and the
        # audit trail stand exactly as approved.
        r = call_action(H.update_expense_claim_status, conn, ns(
            expense_claim_id=claim_id, status="paid",
            payment_entry_id="PAY-0042"))
        assert is_error(r), r
        assert _msg(r) == (
            f"Expense claim {claim_id} is owed to the employee in the payment "
            "ledger; it is marked paid when a submitted payment is allocated to it")

        assert _claim_row(conn, claim_id) == {
            "status": "approved", "total_amount": "500.00",
            "approved_by": evan, "approval_date": row["approval_date"],
            "payment_entry_id": None}
        assert _legs(conn, claim_id) == legs

        assert _where(conn, "audit_log", action="update-expense-claim-status",
                      entity_id=claim_id) == []

    def test_cancel_unposted_claim_stores_cancelled(self, conn, env):
        dana, evan, travel = _expense_setup(conn, env)
        draft = _claim(conn, env, dana, travel)
        submitted = _submitted(conn, env, dana, travel)
        ledgers_before = _snapshot(conn, _LEDGERS)

        for claim_id, old in ((draft, "draft"), (submitted, "submitted")):
            r = call_action(H.update_expense_claim_status, conn, ns(
                expense_claim_id=claim_id, status="cancelled",
                payment_entry_id=None))
            assert is_ok(r), r
            assert (r["old_status"], r["new_status"]) == (old, "cancelled")
            assert _claim_row(conn, claim_id) == {
                "status": "cancelled", "total_amount": "500.00",
                "approved_by": None, "approval_date": None,
                "payment_entry_id": None}
            assert _legs(conn, claim_id) == []
            audit = _audit(conn, "update-expense-claim-status", claim_id)
            assert json.loads(audit["old_values"]) == {"status": old}
            assert json.loads(audit["new_values"]) == {
                "status": "cancelled", "payment_entry_id": None}

        assert _snapshot(conn, _LEDGERS) == ledgers_before

    def test_status_refusals_leave_the_database_identical(self, conn, env):
        dana, evan, travel = _expense_setup(conn, env)
        draft = _claim(conn, env, dana, travel)
        submitted = _submitted(conn, env, dana, travel)
        approved = _approved(conn, env, dana, evan, travel)
        rejected = _submitted(conn, env, dana, travel)
        assert is_ok(call_action(H.reject_expense_claim, conn, ns(
            expense_claim_id=rejected, reason="Duplicate")))
        # m805b: no claim reaches 'paid' through this action any more, so
        # the paid transition-out entries live in the payments suite now.
        cancelled = _claim(conn, env, dana, travel)
        assert is_ok(call_action(H.update_expense_claim_status, conn, ns(
            expense_claim_id=cancelled, status="cancelled",
            payment_entry_id=None)))
        snapshot = _snapshot(conn, _HR_TABLES + _LEDGERS)

        refused = [
            ("draft", "paid"), ("draft", "submitted"), ("draft", "approved"),
            ("submitted", "paid"), ("submitted", "approved"),
            ("submitted", "rejected"),
            ("approved", "cancelled"), ("approved", "submitted"),
            ("rejected", "paid"), ("rejected", "cancelled"),
            ("cancelled", "paid"), ("cancelled", "draft"),
        ]
        claims = {"draft": draft, "submitted": submitted, "approved": approved,
                  "rejected": rejected, "cancelled": cancelled}
        allowed = {"draft": "cancelled", "submitted": "cancelled",
                   "approved": "paid"}
        for old, new in refused:
            claim_id = claims[old]
            r = call_action(H.update_expense_claim_status, conn, ns(
                expense_claim_id=claim_id, status=new,
                payment_entry_id="PAY-9999"))
            assert is_error(r), (old, new, r)
            assert _msg(r) == (
                f"Expense claim {claim_id} cannot change from "
                f"'{old}' to '{new}'. Allowed from '{old}': "
                f"{allowed.get(old, 'none')}"), (old, new)
            assert _claim_row(conn, claim_id)["status"] == old

        r = call_action(H.update_expense_claim_status, conn, ns(
            expense_claim_id=None, status="paid", payment_entry_id=None))
        assert is_error(r)
        assert _msg(r) == "--expense-claim-id is required"

        r = call_action(H.update_expense_claim_status, conn, ns(
            expense_claim_id=approved, status=None, payment_entry_id=None))
        assert is_error(r)
        assert _msg(r) == "--status is required"

        r = call_action(H.update_expense_claim_status, conn, ns(
            expense_claim_id=approved, status="settled",
            payment_entry_id=None))
        assert is_error(r)
        assert _msg(r) == (
            "Invalid expense claim status 'settled'. Valid: ('draft', "
            "'submitted', 'approved', 'rejected', 'paid', 'cancelled')")

        r = call_action(H.update_expense_claim_status, conn, ns(
            expense_claim_id="no-such-claim", status="paid",
            payment_entry_id=None))
        assert is_error(r)
        assert _msg(r) == "Expense claim no-such-claim not found"

        assert _snapshot(conn, _HR_TABLES + _LEDGERS) == snapshot
