"""Tests for business rules: add, list, evaluate."""
import json
import uuid
import pytest
from helpers import _call_action, setup_ai_environment, deactivate_business_rule
from db_query import add_business_rule, list_business_rules, evaluate_business_rules


# ---------------------------------------------------------------------------
# 1. Add a business rule -- basic creation
# ---------------------------------------------------------------------------

def test_add_business_rule(fresh_db):
    """Adding a business rule with severity='block' should create an active
    rule with action='block' and applies_to set to company_id."""
    env = setup_ai_environment(fresh_db)

    result = _call_action(
        add_business_rule, fresh_db,
        rule_text="No purchases over $5000",
        severity="block",
        company_id=env["company_id"],
    )

    assert result["status"] == "ok"
    rule = result["business_rule"]
    assert rule["rule_text"] == "No purchases over $5000"
    assert rule["action"] == "block"
    assert rule["active"] == 1
    assert rule["applies_to"] == env["company_id"]
    assert rule["times_triggered"] == 0


# ---------------------------------------------------------------------------
# 2. Severity mapping -- "warning" maps to action "warn"
# ---------------------------------------------------------------------------

def test_add_rule_severity_mapping(fresh_db):
    """Severity 'warning' should be mapped to action 'warn' via
    SEVERITY_TO_ACTION lookup."""
    env = setup_ai_environment(fresh_db)

    result = _call_action(
        add_business_rule, fresh_db,
        rule_text="Alert on large refunds",
        severity="warning",
        company_id=env["company_id"],
    )

    assert result["status"] == "ok"
    assert result["business_rule"]["action"] == "warn"


# ---------------------------------------------------------------------------
# 3. List active rules -- returns only active=1
# ---------------------------------------------------------------------------

def test_list_active_rules(fresh_db):
    """Adding 2 rules and listing with is_active='1' should return both."""
    env = setup_ai_environment(fresh_db)

    _call_action(
        add_business_rule, fresh_db,
        rule_text="Rule one",
        severity="block",
        company_id=env["company_id"],
    )
    _call_action(
        add_business_rule, fresh_db,
        rule_text="Rule two",
        severity="warn",
        company_id=env["company_id"],
    )

    result = _call_action(
        list_business_rules, fresh_db,
        is_active="1",
        company_id=env["company_id"],
    )

    assert result["status"] == "ok"
    assert result["total_count"] == 2
    for rule in result["business_rules"]:
        assert rule["active"] == 1


# ---------------------------------------------------------------------------
# 4. List inactive rules -- returns only active=0
# ---------------------------------------------------------------------------

def test_list_inactive_rules(fresh_db):
    """After adding a rule and directly setting active=0, listing with
    is_active='0' should return exactly 1 rule."""
    env = setup_ai_environment(fresh_db)

    add_result = _call_action(
        add_business_rule, fresh_db,
        rule_text="Deactivated rule",
        severity="notify",
        company_id=env["company_id"],
    )
    rule_id = add_result["business_rule"]["id"]

    # Deactivate the rule via helper
    deactivate_business_rule(fresh_db, rule_id)

    result = _call_action(
        list_business_rules, fresh_db,
        is_active="0",
    )

    assert result["status"] == "ok"
    assert result["total_count"] == 1
    assert result["business_rules"][0]["id"] == rule_id
    assert result["business_rules"][0]["active"] == 0


# ---------------------------------------------------------------------------
# 5. Evaluate -- rule with no conditions matches everything
# ---------------------------------------------------------------------------

def test_evaluate_block_match(fresh_db):
    """A rule with no parsed conditions matches any action evaluation.
    It should trigger and return recommended_action='block'."""
    env = setup_ai_environment(fresh_db)

    _call_action(
        add_business_rule, fresh_db,
        rule_text="Block all large purchases",
        severity="block",
        company_id=env["company_id"],
    )

    result = _call_action(
        evaluate_business_rules, fresh_db,
        action_type="create-purchase-invoice",
        action_data='{"amount": "6000"}',
        company_id=env["company_id"],
    )

    assert result["status"] == "ok"
    assert result["triggered"] is True
    assert len(result["rules"]) >= 1
    assert result["recommended_action"] == "block"


# ---------------------------------------------------------------------------
# 6. Evaluate with conditions -- conditional match and miss
# ---------------------------------------------------------------------------

def test_evaluate_with_conditions(fresh_db):
    """A rule with condition 'amount > 5000' should trigger when amount=6000
    and NOT trigger when amount=3000."""
    env = setup_ai_environment(fresh_db)

    # Insert a rule directly with parsed_condition containing conditions
    rule_id = str(uuid.uuid4())
    parsed_condition = json.dumps({
        "conditions": [
            {"field": "amount", "operator": ">", "value": "5000"}
        ]
    })
    fresh_db.execute(
        """INSERT INTO business_rule (id, rule_text, parsed_condition,
           applies_to, action, active, times_triggered, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'block', 1, 0, '2026-01-01 00:00:00',
                   '2026-01-01 00:00:00')""",
        (rule_id, "No purchases over $5000", parsed_condition,
         env["company_id"]),
    )
    fresh_db.commit()

    # Evaluate with amount=6000 -- should trigger
    result_high = _call_action(
        evaluate_business_rules, fresh_db,
        action_type="create-purchase-invoice",
        action_data='{"amount": "6000"}',
        company_id=env["company_id"],
    )

    assert result_high["status"] == "ok"
    assert result_high["triggered"] is True
    assert result_high["recommended_action"] == "block"

    # Evaluate with amount=3000 -- should NOT trigger
    result_low = _call_action(
        evaluate_business_rules, fresh_db,
        action_type="create-purchase-invoice",
        action_data='{"amount": "3000"}',
        company_id=env["company_id"],
    )

    assert result_low["status"] == "ok"
    assert result_low["triggered"] is False
    assert result_low["rules"] == []
    assert result_low["recommended_action"] is None


# ---------------------------------------------------------------------------
# 7. Evaluate no match -- rule for company A, evaluate for company B
# ---------------------------------------------------------------------------

def test_evaluate_no_match(fresh_db):
    """A rule scoped to company A should not trigger when evaluating
    for a different company B."""
    env = setup_ai_environment(fresh_db)

    # Add rule scoped to env company
    _call_action(
        add_business_rule, fresh_db,
        rule_text="Company-specific rule",
        severity="block",
        company_id=env["company_id"],
    )

    # Evaluate for a completely different (non-existent) company
    other_company_id = str(uuid.uuid4())

    result = _call_action(
        evaluate_business_rules, fresh_db,
        action_type="create-invoice",
        action_data='{"amount": "1000"}',
        company_id=other_company_id,
    )

    assert result["status"] == "ok"
    assert result["triggered"] is False
    assert result["rules"] == []


# ---------------------------------------------------------------------------
# 8. Trigger count increments on repeated evaluations
# ---------------------------------------------------------------------------

def test_trigger_count_increments(fresh_db):
    """Evaluating a matching rule twice should increment times_triggered to 2."""
    env = setup_ai_environment(fresh_db)

    add_result = _call_action(
        add_business_rule, fresh_db,
        rule_text="Always-match rule",
        severity="warn",
        company_id=env["company_id"],
    )
    rule_id = add_result["business_rule"]["id"]

    # First evaluation
    _call_action(
        evaluate_business_rules, fresh_db,
        action_type="any-action",
        action_data='{"key": "value"}',
        company_id=env["company_id"],
    )

    # Second evaluation
    _call_action(
        evaluate_business_rules, fresh_db,
        action_type="any-action",
        action_data='{"key": "value"}',
        company_id=env["company_id"],
    )

    # Check times_triggered directly in DB
    row = fresh_db.execute(
        "SELECT times_triggered FROM business_rule WHERE id = ?", (rule_id,)
    ).fetchone()
    assert row["times_triggered"] == 2
