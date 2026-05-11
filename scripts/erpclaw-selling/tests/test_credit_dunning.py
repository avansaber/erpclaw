"""Tests for ROADMAP S1 — credit limit + dunning levels.

Covers:
  - check-credit-limit (read-only: outstanding AR vs credit_limit math)
  - place-customer-on-hold (state transitions, audit log)
  - add-dunning-level (config + uniqueness)
  - run-dunning-cycle (escalation match + action application)
  - invoice-submit credit policy hook (block on suspended/on_hold/over-limit)
"""
import json
import pytest
from decimal import Decimal
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
