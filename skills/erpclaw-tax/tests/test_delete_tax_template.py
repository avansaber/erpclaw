"""Tests for delete-tax-template action."""
import json
import uuid
import pytest

from db_query import ACTIONS
from helpers import (
    _call_action,
    create_test_company,
    create_test_account,
    create_test_tax_template,
    create_test_customer,
)


@pytest.fixture
def setup(fresh_db):
    """Create company + account + templates for delete tests."""
    conn = fresh_db
    company_id = create_test_company(conn)
    tax_acct = create_test_account(
        conn, company_id, "Sales Tax Payable", "liability",
        account_type="tax",
    )
    tmpl_id, _ = create_test_tax_template(
        conn, company_id, tax_acct, name="Deletable Tax",
        tax_type="sales", rate="5.0",
    )
    return {
        "conn": conn,
        "company_id": company_id,
        "tax_acct": tax_acct,
        "template_id": tmpl_id,
    }


def test_delete_tax_template(setup):
    """Delete an unreferenced template."""
    s = setup
    result = _call_action(
        ACTIONS["delete-tax-template"], s["conn"],
        tax_template_id=s["template_id"],
    )
    assert result["status"] == "ok"
    assert result["deleted"] is True

    # Verify removed from DB
    row = s["conn"].execute(
        "SELECT id FROM tax_template WHERE id = ?",
        (s["template_id"],),
    ).fetchone()
    assert row is None

    # Lines should also be gone
    lines = s["conn"].execute(
        "SELECT id FROM tax_template_line WHERE tax_template_id = ?",
        (s["template_id"],),
    ).fetchall()
    assert len(lines) == 0


def test_delete_tax_template_blocked_by_rule(setup):
    """Cannot delete a template referenced by a tax rule."""
    s = setup
    # Create a customer so we have a valid filter for the rule
    cust_id = create_test_customer(s["conn"], s["company_id"])

    # Create a tax rule referencing this template
    rule_result = _call_action(
        ACTIONS["add-tax-rule"], s["conn"],
        tax_template_id=s["template_id"],
        tax_type="sales",
        priority=1,
        customer_id=cust_id,
        company_id=s["company_id"],
    )
    assert rule_result["status"] == "ok"

    # Now try to delete — should fail
    result = _call_action(
        ACTIONS["delete-tax-template"], s["conn"],
        tax_template_id=s["template_id"],
    )
    assert result["status"] == "error"
    assert "referenced" in result["message"].lower() or "cannot delete" in result["message"].lower()
