"""Tests for the onboarding-step wizard action."""
import json
import os
import sys
import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from helpers import _call_action  # noqa: E402
import db_query  # noqa: E402


@pytest.fixture(autouse=True)
def clean_onboarding_state():
    """Remove onboarding state file before and after each test."""
    state_file = db_query.ONBOARDING_STATE_FILE
    if os.path.exists(state_file):
        os.remove(state_file)
    yield
    if os.path.exists(state_file):
        os.remove(state_file)


# ─── Test 1: Step 1 — prompt for company name ────────────────────────────────

def test_step1_prompt(fresh_db):
    """First call with no answer shows the company name prompt."""
    result = _call_action(db_query.onboarding_step, fresh_db)
    assert result["step"] == 1
    assert result["completed"] is False
    assert "company name" in result["prompt"].lower()
    assert result["field"] == "company_name"


# ─── Test 2: Step 1 → Step 2 — answer company name ──────────────────────────

def test_step1_to_step2(fresh_db):
    """Answering step 1 with a company name advances to step 2 (currency)."""
    # First call to initialize
    _call_action(db_query.onboarding_step, fresh_db)
    # Answer with company name
    result = _call_action(db_query.onboarding_step, fresh_db,
                          answer="Stark Manufacturing")
    assert result["step"] == 2
    assert result["completed"] is False
    assert "currency" in result["prompt"].lower()
    assert "USD" in result["options"]
    assert result["field"] == "currency"


# ─── Test 3: Step 2 — valid currency ────────────────────────────────────────

def test_step2_valid_currency(fresh_db):
    """Answering step 2 with a valid currency advances to step 3."""
    _call_action(db_query.onboarding_step, fresh_db)
    _call_action(db_query.onboarding_step, fresh_db, answer="Acme Corp")
    result = _call_action(db_query.onboarding_step, fresh_db, answer="CAD")
    assert result["step"] == 3
    assert result["completed"] is False
    assert "fiscal" in result["prompt"].lower()
    assert result["field"] == "fiscal_month"


# ─── Test 4: Step 2 — invalid currency ──────────────────────────────────────

def test_step2_invalid_currency(fresh_db):
    """Answering step 2 with an invalid currency stays on step 2."""
    _call_action(db_query.onboarding_step, fresh_db)
    _call_action(db_query.onboarding_step, fresh_db, answer="Acme Corp")
    result = _call_action(db_query.onboarding_step, fresh_db, answer="XYZ")
    assert result["step"] == 2
    assert result["completed"] is False
    assert "error" in result
    assert result["error"] == "invalid_currency"


# ─── Test 5: Step 3 — valid fiscal month ────────────────────────────────────

def test_step3_valid_month(fresh_db):
    """Answering step 3 with a valid month advances to step 4."""
    _call_action(db_query.onboarding_step, fresh_db)
    _call_action(db_query.onboarding_step, fresh_db, answer="Acme Corp")
    _call_action(db_query.onboarding_step, fresh_db, answer="USD")
    result = _call_action(db_query.onboarding_step, fresh_db, answer="4")
    assert result["step"] == 4
    assert result["completed"] is False
    assert "demo" in result["prompt"].lower()
    assert result["field"] == "load_demo"


# ─── Test 6: Step 3 — invalid fiscal month ──────────────────────────────────

def test_step3_invalid_month(fresh_db):
    """Answering step 3 with an invalid month stays on step 3."""
    _call_action(db_query.onboarding_step, fresh_db)
    _call_action(db_query.onboarding_step, fresh_db, answer="Acme Corp")
    _call_action(db_query.onboarding_step, fresh_db, answer="USD")
    result = _call_action(db_query.onboarding_step, fresh_db, answer="13")
    assert result["step"] == 3
    assert result["completed"] is False
    assert "error" in result
    assert result["error"] == "invalid_month"


# ─── Test 7: Full flow — no demo data ───────────────────────────────────────

def test_full_flow_no_demo(fresh_db):
    """Complete onboarding flow without demo data creates company successfully."""
    _call_action(db_query.onboarding_step, fresh_db)
    _call_action(db_query.onboarding_step, fresh_db, answer="Test Co")
    _call_action(db_query.onboarding_step, fresh_db, answer="USD")
    _call_action(db_query.onboarding_step, fresh_db, answer="1")
    result = _call_action(db_query.onboarding_step, fresh_db, answer="no")
    assert result["step"] == 5
    assert result["completed"] is True
    assert "Test Co" in result["prompt"]
    assert result["company_name"] == "Test Co"
    assert result["currency"] == "USD"
    assert result["fiscal_month"] == 1
    assert result["load_demo"] is False
    assert "setup-company" in result["results"]["steps_completed"]


# ─── Test 8: Reset wizard ───────────────────────────────────────────────────

def test_reset_wizard(fresh_db):
    """Resetting the wizard clears state and returns step 1."""
    # Advance to step 2
    _call_action(db_query.onboarding_step, fresh_db)
    _call_action(db_query.onboarding_step, fresh_db, answer="Acme Corp")
    # Reset
    result = _call_action(db_query.onboarding_step, fresh_db, reset=True)
    assert result["step"] == 1
    assert result["completed"] is False
    assert "company name" in result["prompt"].lower()


# ─── Test 9: Idempotency — calling after completion ─────────────────────────

def test_idempotent_after_completion(fresh_db):
    """Calling onboarding-step after completion reports already done."""
    _call_action(db_query.onboarding_step, fresh_db)
    _call_action(db_query.onboarding_step, fresh_db, answer="Idem Corp")
    _call_action(db_query.onboarding_step, fresh_db, answer="EUR")
    _call_action(db_query.onboarding_step, fresh_db, answer="7")
    _call_action(db_query.onboarding_step, fresh_db, answer="no")
    # Call again — should say already completed
    result = _call_action(db_query.onboarding_step, fresh_db)
    assert result["completed"] is True
    assert "already completed" in result["prompt"].lower()
