"""Tests for transaction categorization: add rule, categorize."""
import json
import uuid
import pytest
from helpers import (
    _call_action, setup_ai_environment, create_test_account,
    create_test_categorization_rule,
)
from db_query import add_categorization_rule, categorize_transaction


# ---------------------------------------------------------------------------
# 1. Add categorization rule -- basic creation
# ---------------------------------------------------------------------------

def test_add_categorization_rule(fresh_db):
    """Adding a categorization rule should store the pattern, target account,
    default confidence='0.5', and default source='bank_feed'."""
    env = setup_ai_environment(fresh_db)

    result = _call_action(
        add_categorization_rule, fresh_db,
        pattern="office supplies",
        account_id=env["expense_account_id"],
    )

    assert result["status"] == "ok"
    rule = result["categorization_rule"]
    assert rule["pattern"] == "office supplies"
    assert rule["target_account_id"] == env["expense_account_id"]
    assert rule["confidence"] == "0.5"
    assert rule["source"] == "bank_feed"
    assert rule["times_applied"] == 0
    assert rule["times_overridden"] == 0


# ---------------------------------------------------------------------------
# 2. Categorize matching -- pattern substring match (case-insensitive)
# ---------------------------------------------------------------------------

def test_categorize_matching(fresh_db):
    """A rule with pattern='coffee' should match description='Starbucks Coffee Shop'
    via case-insensitive substring matching."""
    env = setup_ai_environment(fresh_db)

    _call_action(
        add_categorization_rule, fresh_db,
        pattern="coffee",
        account_id=env["expense_account_id"],
    )

    result = _call_action(
        categorize_transaction, fresh_db,
        description="Starbucks Coffee Shop",
    )

    assert result["status"] == "ok"
    assert result["match"] is True
    assert result["pattern"] == "coffee"
    assert result["account_id"] == env["expense_account_id"]
    assert result["confidence"] == "0.5"


# ---------------------------------------------------------------------------
# 3. Categorize no match -- returns match=false
# ---------------------------------------------------------------------------

def test_categorize_no_match(fresh_db):
    """Categorizing a description that matches no rule should return
    match=false with null rule_id and confidence='0'."""
    env = setup_ai_environment(fresh_db)

    # Add a rule that won't match
    _call_action(
        add_categorization_rule, fresh_db,
        pattern="coffee",
        account_id=env["expense_account_id"],
    )

    result = _call_action(
        categorize_transaction, fresh_db,
        description="Random text xyz that matches nothing",
    )

    assert result["status"] == "ok"
    assert result["match"] is False
    assert result["rule_id"] is None
    assert result["account_id"] is None
    assert result["confidence"] == "0"


# ---------------------------------------------------------------------------
# 4. Best confidence wins -- higher confidence rule takes priority
# ---------------------------------------------------------------------------

def test_best_confidence_wins(fresh_db):
    """When two rules match, the one with higher confidence should win.
    Rules are ordered by confidence DESC then times_applied DESC."""
    env = setup_ai_environment(fresh_db)

    # Rule A via action -- confidence='0.5' (default)
    result_a = _call_action(
        add_categorization_rule, fresh_db,
        pattern="amazon",
        account_id=env["expense_account_id"],
    )
    rule_a_id = result_a["categorization_rule"]["id"]

    # Rule B via helper -- confidence='0.8' (higher)
    rule_b_id = create_test_categorization_rule(
        fresh_db, "amazon prime", env["revenue_account_id"],
        confidence="0.8",
    )

    # Both patterns match "Amazon Prime subscription"
    # "amazon prime" (conf=0.8) and "amazon" (conf=0.5) both appear in the description
    result = _call_action(
        categorize_transaction, fresh_db,
        description="Amazon Prime subscription",
    )

    assert result["status"] == "ok"
    assert result["match"] is True
    # Rule B has higher confidence (0.8 > 0.5) so it should win
    assert result["rule_id"] == rule_b_id
    assert result["confidence"] == "0.8"
    assert result["account_id"] == env["revenue_account_id"]


# ---------------------------------------------------------------------------
# 5. times_applied increments on each match
# ---------------------------------------------------------------------------

def test_times_applied_increments(fresh_db):
    """Categorizing a matching description twice should increment
    times_applied to 2."""
    env = setup_ai_environment(fresh_db)

    add_result = _call_action(
        add_categorization_rule, fresh_db,
        pattern="uber",
        account_id=env["expense_account_id"],
    )
    rule_id = add_result["categorization_rule"]["id"]

    # First categorization
    _call_action(
        categorize_transaction, fresh_db,
        description="Uber ride to airport",
    )

    # Check times_applied = 1
    row = fresh_db.execute(
        "SELECT times_applied FROM categorization_rule WHERE id = ?", (rule_id,)
    ).fetchone()
    assert row["times_applied"] == 1

    # Second categorization
    _call_action(
        categorize_transaction, fresh_db,
        description="Uber Eats delivery",
    )

    # Check times_applied = 2
    row = fresh_db.execute(
        "SELECT times_applied FROM categorization_rule WHERE id = ?", (rule_id,)
    ).fetchone()
    assert row["times_applied"] == 2


# ---------------------------------------------------------------------------
# 6. Validates account FK -- non-existent account returns error
# ---------------------------------------------------------------------------

def test_validates_account_fk(fresh_db):
    """Adding a categorization rule with a non-existent account_id should
    return an error."""
    env = setup_ai_environment(fresh_db)

    fake_account_id = str(uuid.uuid4())

    result = _call_action(
        add_categorization_rule, fresh_db,
        pattern="some pattern",
        account_id=fake_account_id,
    )

    assert result["status"] == "error"
    assert "not found" in result["message"].lower()
