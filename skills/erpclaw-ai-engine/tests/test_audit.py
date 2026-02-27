"""Tests for audit conversation logging and AI engine status.

3 tests covering:
  - log_audit_conversation basic entry
  - status_action on empty DB (all zeros)
  - status_action after inserting data
"""
from helpers import (
    _call_action,
    setup_ai_environment,
)
from db_query import log_audit_conversation, status_action, add_business_rule


# ---------------------------------------------------------------------------
# 1. Log a conversation audit entry
# ---------------------------------------------------------------------------

def test_log_conversation(fresh_db):
    """Log with action_name='detect-anomalies', details and result.
    Verify audit_entry has voucher_type, ai_interpretation, actions_taken."""
    env = setup_ai_environment(fresh_db)

    result = _call_action(
        log_audit_conversation, fresh_db,
        action_name="detect-anomalies",
        details='{"scan_type": "full"}',
        result="Found 3 anomalies",
    )

    assert result["status"] == "ok"
    entry = result["audit_entry"]
    assert entry["voucher_type"] == "detect-anomalies"
    assert entry["ai_interpretation"] == "Found 3 anomalies"
    # actions_taken stores the JSON-encoded details
    assert "scan_type" in entry["actions_taken"]
    assert "full" in entry["actions_taken"]
    assert entry["id"] is not None
    assert entry["timestamp"] is not None


# ---------------------------------------------------------------------------
# 2. Status on empty DB -- all zeros
# ---------------------------------------------------------------------------

def test_status_all_tables(fresh_db):
    """On empty DB (with tables but no data), status should return all zeros."""
    env = setup_ai_environment(fresh_db)

    result = _call_action(
        status_action, fresh_db,
    )

    assert result["status"] == "ok"
    assert result["anomalies"]["new_total"] == 0
    assert result["anomalies"]["by_severity"] == {}
    assert result["forecasts"] == 0
    assert result["business_rules"]["active"] == 0
    assert result["business_rules"]["total"] == 0
    assert result["categorization_rules"] == 0
    assert result["correlations"] == 0
    assert result["scenarios"] == 0
    assert result["relationship_scores"] == 0
    assert result["pending_decisions"] == 0
    assert result["active_contexts"] == 0
    assert result["audit_entries"] == 0


# ---------------------------------------------------------------------------
# 3. Status with data -- counts reflect inserted rows
# ---------------------------------------------------------------------------

def test_status_with_data(fresh_db):
    """Add a business rule and log an audit entry.
    Status should show business_rules.total=1, business_rules.active=1,
    audit_entries=1."""
    env = setup_ai_environment(fresh_db)

    # Add a business rule
    _call_action(
        add_business_rule, fresh_db,
        rule_text="Block payments over $10,000",
        severity="block",
        company_id=env["company_id"],
    )

    # Log an audit entry
    _call_action(
        log_audit_conversation, fresh_db,
        action_name="test-action",
        details='{"test": true}',
        result="test result",
    )

    # Check status
    result = _call_action(
        status_action, fresh_db,
    )

    assert result["status"] == "ok"
    assert result["business_rules"]["total"] == 1
    assert result["business_rules"]["active"] == 1
    assert result["audit_entries"] == 1
