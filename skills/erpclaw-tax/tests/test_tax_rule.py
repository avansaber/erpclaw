"""Tests for tax rule actions."""
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
    """Create company + account + template + customer for rule tests."""
    conn = fresh_db
    company_id = create_test_company(conn)
    tax_acct = create_test_account(
        conn, company_id, "Sales Tax Payable", "liability",
        account_type="tax",
    )
    tmpl_id, _ = create_test_tax_template(
        conn, company_id, tax_acct, name="State Tax",
        tax_type="sales", rate="7.0",
    )
    cust_id = create_test_customer(conn, company_id, name="Acme Corp")
    return {
        "conn": conn,
        "company_id": company_id,
        "tax_acct": tax_acct,
        "template_id": tmpl_id,
        "customer_id": cust_id,
    }


def test_add_tax_rule(setup):
    """Add a tax rule with customer filter."""
    s = setup
    result = _call_action(
        ACTIONS["add-tax-rule"], s["conn"],
        tax_template_id=s["template_id"],
        tax_type="sales",
        priority=1,
        customer_id=s["customer_id"],
    )
    assert result["status"] == "ok"
    assert result["tax_rule_id"]

    # Verify in DB
    row = s["conn"].execute(
        "SELECT * FROM tax_rule WHERE id = ?",
        (result["tax_rule_id"],),
    ).fetchone()
    assert row is not None
    assert row["customer_id"] == s["customer_id"]
    assert row["priority"] == 1
    assert row["company_id"] == s["company_id"]


def test_list_tax_rules(setup):
    """List all tax rules for a company, verify template name joined."""
    s = setup
    # Create two rules
    _call_action(
        ACTIONS["add-tax-rule"], s["conn"],
        tax_template_id=s["template_id"],
        tax_type="sales", priority=1,
        customer_id=s["customer_id"],
    )
    _call_action(
        ACTIONS["add-tax-rule"], s["conn"],
        tax_template_id=s["template_id"],
        tax_type="sales", priority=2,
        shipping_state="NY",
    )

    result = _call_action(
        ACTIONS["list-tax-rules"], s["conn"],
        company_id=s["company_id"],
    )
    assert result["status"] == "ok"
    assert len(result["rules"]) == 2
    # Verify template_name is joined
    assert result["rules"][0]["template_name"] == "State Tax"
    # Ordered by priority
    assert result["rules"][0]["priority"] <= result["rules"][1]["priority"]


def test_add_tax_rule_missing_filter(setup):
    """Creating a rule without any filter condition should fail."""
    s = setup
    result = _call_action(
        ACTIONS["add-tax-rule"], s["conn"],
        tax_template_id=s["template_id"],
        tax_type="sales",
        priority=1,
        # No customer_id, supplier_id, shipping_state, or tax_category_id
    )
    assert result["status"] == "error"
    assert "filter" in result["message"].lower()
