"""Tests for conversation context and pending decision actions.

5 tests covering:
  - save_conversation_context
  - get_conversation_context (by id, latest)
  - add_pending_decision (auto-create context, linked to existing context)
"""
import json

from helpers import _call_action, setup_ai_environment
from db_query import (
    save_conversation_context,
    get_conversation_context,
    add_pending_decision,
)


# ── Test 1: Save conversation context ────────────────────────────────────

def test_save_context(fresh_db):
    """Save a context with summary and context_type, verify returned fields."""
    env = setup_ai_environment(fresh_db)

    ctx_data = json.dumps({
        "summary": "Reviewing Q1 financials",
        "context_type": "active_workflow",
        "user_id": "test-user-1",
        "related_entities": {"company_id": env["company_id"]},
        "state": {"step": "initial"},
        "priority": 5,
    })

    result = _call_action(save_conversation_context, fresh_db,
                          context_data=ctx_data)

    assert result["status"] == "ok"
    ctx = result["context"]
    assert ctx["id"] is not None
    assert ctx["summary"] == "Reviewing Q1 financials"
    assert ctx["context_type"] == "active_workflow"
    assert ctx["user_id"] == "test-user-1"
    assert ctx["priority"] == 5


# ── Test 2: Get context by id ────────────────────────────────────────────

def test_get_context_by_id(fresh_db):
    """Save a context, then retrieve it by id. Should match."""
    ctx_data = json.dumps({
        "summary": "Investigating anomaly",
        "context_type": "in_progress_analysis",
    })

    save_result = _call_action(save_conversation_context, fresh_db,
                               context_data=ctx_data)
    ctx_id = save_result["context"]["id"]

    get_result = _call_action(get_conversation_context, fresh_db,
                              context_id=ctx_id)

    assert get_result["status"] == "ok"
    assert get_result["context"]["id"] == ctx_id
    assert get_result["context"]["summary"] == "Investigating anomaly"
    assert get_result["context"]["context_type"] == "in_progress_analysis"
    # pending_decisions should be an empty list (no decisions added)
    assert get_result["context"]["pending_decisions"] == []


# ── Test 3: Get latest context (no id provided) ─────────────────────────

def test_get_latest_context(fresh_db):
    """Save 2 contexts. Get without id returns the most recent one."""
    # Save first context
    ctx_data_1 = json.dumps({
        "summary": "First context",
        "context_type": "active_workflow",
    })
    result_1 = _call_action(save_conversation_context, fresh_db,
                            context_data=ctx_data_1)
    first_id = result_1["context"]["id"]

    # Backdate the first context so the second one is clearly newer
    fresh_db.execute(
        "UPDATE conversation_context SET last_active = '2026-01-01 00:00:00' WHERE id = ?",
        (first_id,),
    )
    fresh_db.commit()

    # Save second context
    ctx_data_2 = json.dumps({
        "summary": "Second context (latest)",
        "context_type": "pending_decision",
    })
    result_2 = _call_action(save_conversation_context, fresh_db,
                            context_data=ctx_data_2)
    second_id = result_2["context"]["id"]

    # Get without context_id -- should return second (most recent)
    get_result = _call_action(get_conversation_context, fresh_db)

    assert get_result["status"] == "ok"
    assert get_result["context"]["id"] == second_id
    assert get_result["context"]["summary"] == "Second context (latest)"


# ── Test 4: Add pending decision (auto-creates context) ──────────────────

def test_add_pending_decision(fresh_db):
    """Add decision without context_id. Should auto-create a context."""
    options = json.dumps(["Approve", "Reject", "Defer"])

    result = _call_action(add_pending_decision, fresh_db,
                          description="Should we approve the vendor contract?",
                          options=options,
                          decision_type="financial")

    assert result["status"] == "ok"
    pd = result["pending_decision"]
    assert pd["id"] is not None
    assert pd["question"] == "Should we approve the vendor contract?"
    assert pd["status"] == "pending"
    assert result["context_id"] is not None

    # Verify the auto-created context exists
    ctx_result = _call_action(get_conversation_context, fresh_db,
                              context_id=result["context_id"])
    assert ctx_result["status"] == "ok"
    assert ctx_result["context"]["id"] == result["context_id"]
    assert ctx_result["context"]["context_type"] == "pending_decision"


# ── Test 5: Pending decision linked to existing context ──────────────────

def test_pending_decision_linked_to_context(fresh_db):
    """Save a context, add a pending decision with that context_id.
    When getting the context, pending_decisions should include the decision."""
    # Create a context first
    ctx_data = json.dumps({
        "summary": "Budget review workflow",
        "context_type": "active_workflow",
    })
    ctx_result = _call_action(save_conversation_context, fresh_db,
                              context_data=ctx_data)
    ctx_id = ctx_result["context"]["id"]

    # Add a pending decision linked to this context
    options = json.dumps(["Increase budget", "Keep current", "Cut 10%"])
    decision_result = _call_action(add_pending_decision, fresh_db,
                                   description="How to adjust Q2 marketing budget?",
                                   options=options,
                                   context_id=ctx_id)

    assert decision_result["status"] == "ok"
    assert decision_result["context_id"] == ctx_id

    # Now get the context -- it should include the pending decision
    get_result = _call_action(get_conversation_context, fresh_db,
                              context_id=ctx_id)

    assert get_result["status"] == "ok"
    pending = get_result["context"]["pending_decisions"]
    assert len(pending) >= 1

    # Find our decision in the list
    questions = [d["question"] for d in pending]
    assert "How to adjust Q2 marketing budget?" in questions
