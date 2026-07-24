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
