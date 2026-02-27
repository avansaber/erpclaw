"""Tests for add-item-tax-template action."""
import json
import uuid
import pytest

from db_query import ACTIONS
from helpers import (
    _call_action,
    create_test_company,
    create_test_account,
    create_test_tax_template,
)


@pytest.fixture
def setup(fresh_db):
    """Create company + account + template for item tax template tests."""
    conn = fresh_db
    company_id = create_test_company(conn)
    tax_acct = create_test_account(
        conn, company_id, "Sales Tax Payable", "liability",
        account_type="tax",
    )
    tmpl_id, _ = create_test_tax_template(
        conn, company_id, tax_acct, name="Item-level Tax",
        tax_type="sales", rate="10.0",
    )
    return {
        "conn": conn,
        "company_id": company_id,
        "template_id": tmpl_id,
    }


def test_add_item_tax_template(setup):
    """Add an item-specific tax template override."""
    s = setup
    item_id = str(uuid.uuid4())
    result = _call_action(
        ACTIONS["add-item-tax-template"], s["conn"],
        item_id=item_id,
        tax_template_id=s["template_id"],
        tax_rate="5.0",
    )
    assert result["status"] == "ok"
    assert result["item_tax_template_id"]

    # Verify in DB
    row = s["conn"].execute(
        "SELECT * FROM item_tax_template WHERE id = ?",
        (result["item_tax_template_id"],),
    ).fetchone()
    assert row is not None
    assert row["item_id"] == item_id
    assert row["tax_template_id"] == s["template_id"]
    assert row["tax_rate"] == "5.0"


def test_add_item_tax_template_missing_template(setup):
    """Referencing a non-existent template should fail."""
    s = setup
    result = _call_action(
        ACTIONS["add-item-tax-template"], s["conn"],
        item_id=str(uuid.uuid4()),
        tax_template_id="nonexistent-template-id",
    )
    assert result["status"] == "error"
    assert "not found" in result["message"].lower()
