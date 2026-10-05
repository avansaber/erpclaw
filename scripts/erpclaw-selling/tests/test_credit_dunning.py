"""Tests for ROADMAP S1 — credit limit + dunning levels.

Covers:
  - check-credit-limit (read-only: outstanding AR vs credit_limit math)
  - place-customer-on-hold (state transitions, audit log)
  - add-dunning-level (config + uniqueness)
  - run-dunning-cycle (escalation match + action application)
  - invoice-submit credit policy hook (block on suspended/on_hold/over-limit)
"""
import json
import uuid
import pytest
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from selling_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
    seed_company, seed_customer,
)

mod = load_db_query()


# ---------------------------------------------------------------------------
# check-credit-limit
# ---------------------------------------------------------------------------

class TestCheckCreditLimit:
    def test_no_limit_set(self, conn, env):
        """Customer with credit_limit=0 reports limit_enforced=False."""
        result = call_action(mod.check_credit_limit, conn, ns(
            customer_id=env["customer"],
        ))
        assert is_ok(result)
        assert result["limit_enforced"] is False
        assert result["credit_status"] == "active"

    def test_with_limit_no_outstanding(self, conn, env):
        """Limit set, zero outstanding → full credit available."""
        conn.execute(
            "UPDATE customer SET credit_limit='5000' WHERE id=?",
            (env["customer"],),
        )
        result = call_action(mod.check_credit_limit, conn, ns(
            customer_id=env["customer"],
        ))
        assert is_ok(result)
        assert result["limit_enforced"] is True
        assert Decimal(result["credit_limit"]) == Decimal("5000")
        assert Decimal(result["available_credit"]) == Decimal("5000")
        assert Decimal(result["outstanding_ar"]) == Decimal("0")

    def test_missing_customer(self, conn, env):
        result = call_action(mod.check_credit_limit, conn, ns(
            customer_id="non-existent-uuid",
        ))
        assert is_error(result)

    def test_no_customer_id(self, conn, env):
        result = call_action(mod.check_credit_limit, conn, ns(
            customer_id=None,
        ))
        assert is_error(result)


# ---------------------------------------------------------------------------
# place-customer-on-hold
# ---------------------------------------------------------------------------

class TestPlaceCustomerOnHold:
    def test_hold_default(self, conn, env):
        result = call_action(mod.place_customer_on_hold, conn, ns(
            customer_id=env["customer"],
            credit_status=None,
            reason=None,
        ))
        assert is_ok(result)
        assert result["credit_status"] == "on_hold"
        assert result["previous"] == "active"
        # Verify DB state
        row = conn.execute(
            "SELECT credit_status FROM customer WHERE id=?",
            (env["customer"],),
        ).fetchone()
        assert row[0] == "on_hold"

    def test_suspend(self, conn, env):
        result = call_action(mod.place_customer_on_hold, conn, ns(
            customer_id=env["customer"],
            credit_status="suspended",
            reason="non-payment 60+ days",
        ))
        assert is_ok(result)
        assert result["credit_status"] == "suspended"

    def test_reactivate(self, conn, env):
        # Put on hold first
        call_action(mod.place_customer_on_hold, conn, ns(
            customer_id=env["customer"],
            credit_status="on_hold", reason=None,
        ))
        # Now reactivate
        result = call_action(mod.place_customer_on_hold, conn, ns(
            customer_id=env["customer"],
            credit_status="active",
            reason="dispute resolved",
        ))
        assert is_ok(result)
        assert result["credit_status"] == "active"

    def test_invalid_status(self, conn, env):
        result = call_action(mod.place_customer_on_hold, conn, ns(
            customer_id=env["customer"],
            credit_status="frozen",  # not in valid set
            reason=None,
        ))
        assert is_error(result)


# ---------------------------------------------------------------------------
# add-dunning-level
# ---------------------------------------------------------------------------

class TestAddDunningLevel:
    def test_basic(self, conn, env):
        result = call_action(mod.add_dunning_level, conn, ns(
            company_id=env["company_id"],
            level=1, days_overdue=30,
            dunning_action="email",
            template_id=None, description="First reminder",
        ))
        assert is_ok(result)
        assert result["level"] == 1
        assert result["action"] == "email"

    def test_invalid_action(self, conn, env):
        result = call_action(mod.add_dunning_level, conn, ns(
            company_id=env["company_id"],
            level=1, days_overdue=30,
            dunning_action="ignore",  # not valid
            template_id=None, description=None,
        ))
        assert is_error(result)

    def test_level_out_of_range(self, conn, env):
        result = call_action(mod.add_dunning_level, conn, ns(
            company_id=env["company_id"],
            level=11, days_overdue=30,  # level > 10
            dunning_action="email",
            template_id=None, description=None,
        ))
        assert is_error(result)

    def test_duplicate_level(self, conn, env):
        # First add — should succeed
        result1 = call_action(mod.add_dunning_level, conn, ns(
            company_id=env["company_id"],
            level=2, days_overdue=60,
            dunning_action="hold",
            template_id=None, description=None,
        ))
        assert is_ok(result1)
        # Duplicate — should fail (UNIQUE company_id+level)
        result2 = call_action(mod.add_dunning_level, conn, ns(
            company_id=env["company_id"],
            level=2, days_overdue=90,
            dunning_action="call",
            template_id=None, description=None,
        ))
        assert is_error(result2)


# ---------------------------------------------------------------------------
# Credit policy hook on submit-sales-invoice (smoke level)
# ---------------------------------------------------------------------------

class TestCreditPolicyHook:
    """Validates that _enforce_credit_policy raises in the right conditions.

    We test the helper directly rather than the full submit path because the
    submit path requires fiscal year, accounts, items, etc. Helper-level
    tests are the right granularity for policy logic.
    """

    def test_active_no_limit_allowed(self, conn, env):
        # credit_limit=0 + active → no enforcement, function returns cleanly
        mod._enforce_credit_policy(conn, env["customer"], Decimal("1000"))

    def test_suspended_blocks(self, conn, env):
        conn.execute(
            "UPDATE customer SET credit_status='suspended' WHERE id=?",
            (env["customer"],),
        )
        with pytest.raises(SystemExit):
            mod._enforce_credit_policy(conn, env["customer"], Decimal("1000"))

    def test_on_hold_blocks(self, conn, env):
        conn.execute(
            "UPDATE customer SET credit_status='on_hold' WHERE id=?",
            (env["customer"],),
        )
        with pytest.raises(SystemExit):
            mod._enforce_credit_policy(conn, env["customer"], Decimal("1000"))

    def test_on_hold_message_names_real_remedy(self, conn, env, capsys):
        """R3 (stabilization WS1 D7): the on-hold block message must name the
        real remedy (release the hold via place-customer-on-hold) and must NOT
        promise a --user-confirmed override — no module code reads that flag
        (the router strips it before dispatch)."""
        conn.execute(
            "UPDATE customer SET credit_status='on_hold' WHERE id=?",
            (env["customer"],),
        )
        with pytest.raises(SystemExit):
            mod._enforce_credit_policy(conn, env["customer"], Decimal("1000"))
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        assert payload["message"] == (
            "Customer credit is on hold; cannot submit new invoice. Release "
            "the hold first (place-customer-on-hold --credit-status active), "
            "then resubmit."
        )
        assert "--user-confirmed" not in payload["message"]

    def test_limit_not_exceeded(self, conn, env):
        conn.execute(
            "UPDATE customer SET credit_limit='5000', credit_status='active' WHERE id=?",
            (env["customer"],),
        )
        # 1000 new + 0 outstanding < 5000 limit → pass
        mod._enforce_credit_policy(conn, env["customer"], Decimal("1000"))

    def test_limit_exceeded_blocks(self, conn, env):
        conn.execute(
            "UPDATE customer SET credit_limit='500', credit_status='active' WHERE id=?",
            (env["customer"],),
        )
        # 1000 new + 0 outstanding > 500 limit → block
        with pytest.raises(SystemExit):
            mod._enforce_credit_policy(conn, env["customer"], Decimal("1000"))


# ---------------------------------------------------------------------------
# run-dunning-cycle email retrofit (M8 phase C)
# ---------------------------------------------------------------------------

_RUN_DATE = "2026-06-02"
_DUE_DATE = "2026-04-15"  # ~48 days before _RUN_DATE


def _seed_dunning_level(conn, company_id, level=1, days_overdue=30,
                        action="email", template_id="TPL-DUN"):
    dl_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO dunning_level (id, company_id, level, days_overdue, action, template_id) "
        "VALUES (?,?,?,?,?,?)",
        (dl_id, company_id, level, days_overdue, action, template_id),
    )
    conn.commit()
    return dl_id


def _seed_overdue_invoice(conn, company_id, customer_id, amount="500"):
    inv_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO sales_invoice (id, customer_id, posting_date, due_date, "
        "grand_total, outstanding_amount, status, is_return, company_id) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (inv_id, customer_id, "2026-03-15", _DUE_DATE, amount, amount,
         "submitted", 0, company_id),
    )
    conn.commit()
    return inv_id


class TestDunningEmailRetrofit:
    """run-dunning-cycle 'email' levels enqueue a dunning email via the M8-A
    send-email ACTION (mocked seam) and backfill dunning_run.generated_email_id.
    A missing customer email or dunning template skips-with-note, never failing
    the cycle. Mirrors crm-adv process-drip-sends' _dispatch_email seam.
    """

    def _run(self, conn, company_id):
        return call_action(mod.run_dunning_cycle, conn, ns(
            company_id=company_id, run_date=_RUN_DATE, db_path=None))

    def test_email_path_populates_generated_email_id(self, conn, env):
        company_id = env["company_id"]
        customer_id = env["customer"]
        conn.execute("UPDATE customer SET email='ar@acme.example' WHERE id=?",
                     (customer_id,))
        conn.commit()
        _seed_dunning_level(conn, company_id, action="email", template_id="TPL-DUN")
        _seed_overdue_invoice(conn, company_id, customer_id)

        with patch.object(mod, "_dispatch_dunning_email",
                          return_value=(True, "OUTBOX-123")) as m:
            result = self._run(conn, company_id)

        assert is_ok(result)
        assert result["runs_created"] == 1
        assert result["emails"] == {"sent": 1, "skipped": 0}
        # seam invoked with the resolved recipient + the level's template
        assert m.called
        call = m.call_args
        # _dispatch_dunning_email(conn, to_address, template_id, company_id, db_path)
        assert call.args[1] == "ar@acme.example"
        assert call.args[2] == "TPL-DUN"
        # FK column backfilled with the returned outbox id
        run_id = result["run_ids"][0]
        row = conn.execute(
            "SELECT generated_email_id, action_taken FROM dunning_run WHERE id=?",
            (run_id,)).fetchone()
        assert row["generated_email_id"] == "OUTBOX-123"
        assert row["action_taken"] == "email"

    def test_no_email_skips_cleanly(self, conn, env):
        company_id = env["company_id"]
        customer_id = env["customer"]
        # customer.email stays NULL -> recipient unresolvable
        _seed_dunning_level(conn, company_id, action="email", template_id="TPL-DUN")
        _seed_overdue_invoice(conn, company_id, customer_id)

        with patch.object(mod, "_dispatch_dunning_email") as m:
            result = self._run(conn, company_id)

        assert is_ok(result)  # cycle did NOT fail
        assert result["runs_created"] == 1
        assert result["emails"] == {"sent": 0, "skipped": 1}
        assert not m.called  # never dispatched without an address
        run_id = result["run_ids"][0]
        row = conn.execute(
            "SELECT generated_email_id, notes FROM dunning_run WHERE id=?",
            (run_id,)).fetchone()
        assert row["generated_email_id"] is None
        assert "no email" in row["notes"]

    def test_no_template_skips_cleanly(self, conn, env):
        company_id = env["company_id"]
        customer_id = env["customer"]
        conn.execute("UPDATE customer SET email='ar@acme.example' WHERE id=?",
                     (customer_id,))
        conn.commit()
        _seed_dunning_level(conn, company_id, action="email", template_id=None)
        _seed_overdue_invoice(conn, company_id, customer_id)

        with patch.object(mod, "_dispatch_dunning_email") as m:
            result = self._run(conn, company_id)

        assert is_ok(result)
        assert result["emails"] == {"sent": 0, "skipped": 1}
        assert not m.called
        run_id = result["run_ids"][0]
        row = conn.execute(
            "SELECT generated_email_id, notes FROM dunning_run WHERE id=?",
            (run_id,)).fetchone()
        assert row["generated_email_id"] is None
        assert "no dunning template" in row["notes"]

    def test_send_failure_skips_with_note(self, conn, env):
        company_id = env["company_id"]
        customer_id = env["customer"]
        conn.execute("UPDATE customer SET email='ar@acme.example' WHERE id=?",
                     (customer_id,))
        conn.commit()
        _seed_dunning_level(conn, company_id, action="email", template_id="TPL-DUN")
        _seed_overdue_invoice(conn, company_id, customer_id)

        with patch.object(mod, "_dispatch_dunning_email",
                          return_value=(False, "smtp unreachable")) as m:
            result = self._run(conn, company_id)

        assert is_ok(result)  # provider failure does not fail the cycle
        assert result["emails"] == {"sent": 0, "skipped": 1}
        assert m.called
        run_id = result["run_ids"][0]
        row = conn.execute(
            "SELECT generated_email_id, notes FROM dunning_run WHERE id=?",
            (run_id,)).fetchone()
        assert row["generated_email_id"] is None
        assert "send failed" in row["notes"]

    def test_hold_action_does_not_send_email(self, conn, env):
        """Non-email levels (hold) never touch the email seam."""
        company_id = env["company_id"]
        customer_id = env["customer"]
        _seed_dunning_level(conn, company_id, action="hold", template_id=None)
        _seed_overdue_invoice(conn, company_id, customer_id)

        with patch.object(mod, "_dispatch_dunning_email") as m:
            result = self._run(conn, company_id)

        assert is_ok(result)
        assert result["emails"] == {"sent": 0, "skipped": 0}
        assert not m.called
        assert result["actions"]["hold"] == 1


# ---------------------------------------------------------------------------
# Follow-up agent Pack 1 v1: set-follow-up-threshold + run-follow-up-cycle
# ---------------------------------------------------------------------------

_FOLLOW_RUN_DATE = "2026-10-04"  # threshold 30: due 2026-08-31 (34d) in, 2026-09-20 (14d) out
_FOLLOW_OLD_DUE = "2026-08-31"
_FOLLOW_NEW_DUE = "2026-09-20"


def _set_threshold(conn, company_id, days):
    return call_action(mod.set_follow_up_threshold, conn, ns(
        company_id=company_id, days_stale=days))


def _run_follow_up(conn, company_id, run_date=_FOLLOW_RUN_DATE):
    return call_action(mod.run_follow_up_cycle, conn, ns(
        company_id=company_id, run_date=run_date))


def _seed_follow_invoice(conn, company_id, customer_id, due_date,
                         amount="500", status="submitted"):
    inv_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO sales_invoice (id, customer_id, posting_date, due_date, "
        "grand_total, outstanding_amount, status, is_return, company_id) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (inv_id, customer_id, "2026-03-15", due_date, amount, amount,
         status, 0, company_id),
    )
    conn.commit()
    return inv_id


def _threshold_rows(conn, company_id):
    return conn.execute(
        "SELECT id, days_stale, is_active FROM follow_up_threshold "
        "WHERE company_id=?", (company_id,)).fetchall()


def _table_count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class TestSetFollowUpThreshold:
    def test_set_and_update_same_row(self, conn, env):
        company_id = env["company_id"]
        first = _set_threshold(conn, company_id, 30)
        assert is_ok(first)
        assert first["days_stale"] == 30
        assert first["company_id"] == company_id
        assert first["created"] is True

        second = _set_threshold(conn, company_id, 45)
        assert is_ok(second)
        assert second["days_stale"] == 45
        assert second["created"] is False
        assert second["id"] == first["id"]
        rows = _threshold_rows(conn, company_id)
        assert len(rows) == 1
        assert rows[0]["days_stale"] == 45

    def test_invalid_days_refuse_without_writes(self, conn, env):
        company_id = env["company_id"]
        before = _table_count(conn, "follow_up_threshold")
        for bad in (0, -5, 367, "abc", None):
            result = _set_threshold(conn, company_id, bad)
            assert is_error(result), bad
        assert _table_count(conn, "follow_up_threshold") == before

    def test_missing_company_refuses(self, conn, env):
        result = _set_threshold(conn, "non-existent-company", 30)
        assert is_error(result)


class TestRunFollowUpCycle:
    def test_threshold_boundary(self, conn, env):
        """34-day-old invoice qualifies at threshold 30; 14-day-old does not."""
        company_id = env["company_id"]
        customer_id = env["customer"]
        assert is_ok(_set_threshold(conn, company_id, 30))
        old_id = _seed_follow_invoice(conn, company_id, customer_id,
                                      _FOLLOW_OLD_DUE, amount="500")
        _seed_follow_invoice(conn, company_id, customer_id,
                             _FOLLOW_NEW_DUE, amount="700")

        result = _run_follow_up(conn, company_id)
        assert is_ok(result)
        assert result["run_date"] == _FOLLOW_RUN_DATE
        assert result["days_stale"] == 30
        assert result["count"] == 1
        customer = result["customers"][0]
        assert customer["customer_id"] == customer_id
        assert customer["invoice_ids"] == [old_id]
        assert customer["oldest_due_date"] == _FOLLOW_OLD_DUE
        assert customer["days_stale"] == 34
        assert Decimal(customer["outstanding_total"]) == Decimal("500")

    def test_exact_total_and_stable_ordering(self, conn, env):
        """Two qualifying invoices total exactly (Decimal) in sorted order."""
        company_id = env["company_id"]
        customer_id = env["customer"]
        assert is_ok(_set_threshold(conn, company_id, 30))
        first_id = _seed_follow_invoice(conn, company_id, customer_id,
                                        "2026-08-15", amount="0.10")
        second_id = _seed_follow_invoice(conn, company_id, customer_id,
                                         _FOLLOW_OLD_DUE, amount="0.20")

        result = _run_follow_up(conn, company_id)
        assert is_ok(result)
        assert result["count"] == 1
        customer = result["customers"][0]
        assert customer["invoice_ids"] == sorted([first_id, second_id])
        assert customer["oldest_due_date"] == "2026-08-15"
        assert Decimal(customer["outstanding_total"]) == Decimal("0.30")
        assert customer["outstanding_total"] == str(
            Decimal("0.10") + Decimal("0.20"))

        # Deterministic across runs.
        again = _run_follow_up(conn, company_id)
        assert is_ok(again)
        assert again["customers"] == result["customers"]

    def test_excluded_invoices(self, conn, env):
        """Paid, cancelled, zero-outstanding, foreign, and future invoices out."""
        company_id = env["company_id"]
        customer_id = env["customer"]
        other_company = seed_company(conn)
        assert is_ok(_set_threshold(conn, company_id, 30))
        _seed_follow_invoice(conn, company_id, customer_id,
                             _FOLLOW_OLD_DUE, status="paid")
        _seed_follow_invoice(conn, company_id, customer_id,
                             _FOLLOW_OLD_DUE, status="cancelled")
        _seed_follow_invoice(conn, company_id, customer_id,
                             _FOLLOW_OLD_DUE, amount="0.00")
        other_customer = seed_customer(conn, other_company, "Other Co")
        _seed_follow_invoice(conn, other_company, other_customer,
                             _FOLLOW_OLD_DUE, amount="900")
        _seed_follow_invoice(conn, company_id, customer_id,
                             "2026-10-10", amount="900")

        result = _run_follow_up(conn, company_id)
        assert is_ok(result)
        assert result["customers"] == []
        assert result["count"] == 0

    def test_missing_threshold_refuses_without_writes(self, conn, env):
        company_id = env["company_id"]
        customer_id = env["customer"]
        _seed_follow_invoice(conn, company_id, customer_id, _FOLLOW_OLD_DUE)
        before_threshold = _table_count(conn, "follow_up_threshold")
        before_invoices = _table_count(conn, "sales_invoice")
        before_audit = _table_count(conn, "audit_log")

        result = _run_follow_up(conn, company_id)
        assert is_error(result)
        assert _table_count(conn, "follow_up_threshold") == before_threshold
        assert _table_count(conn, "sales_invoice") == before_invoices
        assert _table_count(conn, "audit_log") == before_audit

    def test_malformed_run_date_refuses_without_writes(self, conn, env):
        company_id = env["company_id"]
        assert is_ok(_set_threshold(conn, company_id, 30))
        before_threshold = _table_count(conn, "follow_up_threshold")
        before_audit = _table_count(conn, "audit_log")
        for bad in ("not-a-date", "2026-13-01", "10/04/2026", None):
            result = _run_follow_up(conn, company_id, run_date=bad)
            assert is_error(result), bad
        assert _table_count(conn, "follow_up_threshold") == before_threshold
        assert _table_count(conn, "audit_log") == before_audit

    def test_cycle_writes_nothing(self, conn, env):
        """Two identical runs change no row and no audit record."""
        company_id = env["company_id"]
        customer_id = env["customer"]
        assert is_ok(_set_threshold(conn, company_id, 30))
        _seed_follow_invoice(conn, company_id, customer_id, _FOLLOW_OLD_DUE)

        first = _run_follow_up(conn, company_id)
        assert is_ok(first)
        before = {
            "follow_up_threshold": _table_count(conn, "follow_up_threshold"),
            "sales_invoice": _table_count(conn, "sales_invoice"),
            "audit_log": _table_count(conn, "audit_log"),
        }
        second = _run_follow_up(conn, company_id)
        assert is_ok(second)
        assert second == first
        assert _table_count(conn, "follow_up_threshold") == before["follow_up_threshold"]
        assert _table_count(conn, "sales_invoice") == before["sales_invoice"]
        assert _table_count(conn, "audit_log") == before["audit_log"]
