"""Tests for prepaid credit actions."""
import sys
import os

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

TESTS_DIR = os.path.dirname(__file__)
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

from db_query import ACTIONS
from helpers import (
    _call_action, create_test_company, create_test_customer,
    create_test_rate_plan,
)


def test_add_prepaid_credit(fresh_db):
    """Add prepaid credit with remaining = original."""
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id)
    rate_plan_id = create_test_rate_plan(fresh_db)
    result = _call_action(ACTIONS["add-prepaid-credit"], fresh_db,
                          customer_id=customer_id, amount="500.00",
                          valid_until="2026-12-31",
                          rate_plan_id=rate_plan_id)
    assert result["status"] == "ok"
    credit = result["prepaid_credit"]
    assert credit["original_amount"] == "500.00"
    assert credit["remaining_amount"] == "500.00"
    assert credit["status"] == "active"


def test_get_prepaid_balance(fresh_db):
    """Get prepaid balance shows active credits."""
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id)
    rate_plan_id = create_test_rate_plan(fresh_db)
    _call_action(ACTIONS["add-prepaid-credit"], fresh_db,
                 customer_id=customer_id, amount="500.00",
                 valid_until="2026-12-31", rate_plan_id=rate_plan_id)
    _call_action(ACTIONS["add-prepaid-credit"], fresh_db,
                 customer_id=customer_id, amount="200.00",
                 valid_until="2026-12-31", rate_plan_id=rate_plan_id)
    result = _call_action(ACTIONS["get-prepaid-balance"], fresh_db,
                          customer_id=customer_id)
    assert result["status"] == "ok"
    assert result["active_credits"] == 2
    assert result["total_remaining"] == "700.00"


def test_prepaid_expired_balance(fresh_db):
    """Expired balance updates via direct SQL (simulating expiry)."""
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id)
    rate_plan_id = create_test_rate_plan(fresh_db)
    r = _call_action(ACTIONS["add-prepaid-credit"], fresh_db,
                     customer_id=customer_id, amount="100.00",
                     valid_until="2026-12-31", rate_plan_id=rate_plan_id)
    credit_id = r["prepaid_credit"]["id"]
    # Simulate expiry
    fresh_db.execute(
        "UPDATE prepaid_credit_balance SET status = 'expired' WHERE id = ?",
        (credit_id,))
    fresh_db.commit()
    result = _call_action(ACTIONS["get-prepaid-balance"], fresh_db,
                          customer_id=customer_id)
    assert result["active_credits"] == 0
    assert result["total_remaining"] == "0.00"
    assert len(result["balances"]) == 1
    assert result["balances"][0]["status"] == "expired"


def test_get_prepaid_balance_no_credits(fresh_db):
    """No credits returns empty result."""
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id)
    result = _call_action(ACTIONS["get-prepaid-balance"], fresh_db,
                          customer_id=customer_id)
    assert result["status"] == "ok"
    assert result["active_credits"] == 0
    assert result["total_remaining"] == "0.00"
    assert result["balances"] == []
