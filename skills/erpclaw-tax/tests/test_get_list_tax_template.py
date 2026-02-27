"""Tests for get-tax-template and list-tax-templates actions."""
import json
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
    """Create company + accounts + templates for get/list tests."""
    conn = fresh_db
    company_id = create_test_company(conn)
    tax_acct = create_test_account(
        conn, company_id, "Sales Tax Payable", "liability",
        account_type="tax",
    )
    tmpl_sales_id, _ = create_test_tax_template(
        conn, company_id, tax_acct, name="Sales Tax 8%",
        tax_type="sales", rate="8.0",
    )
    tmpl_purchase_id, _ = create_test_tax_template(
        conn, company_id, tax_acct, name="Purchase Tax 5%",
        tax_type="purchase", rate="5.0",
    )
    return {
        "conn": conn,
        "company_id": company_id,
        "tax_acct": tax_acct,
        "sales_tmpl_id": tmpl_sales_id,
        "purchase_tmpl_id": tmpl_purchase_id,
    }


def test_get_tax_template(setup):
    """Get a specific template by ID, verify lines included."""
    s = setup
    result = _call_action(
        ACTIONS["get-tax-template"], s["conn"],
        tax_template_id=s["sales_tmpl_id"],
    )
    assert result["status"] == "ok"
    assert result["name"] == "Sales Tax 8%"
    assert result["tax_type"] == "sales"
    assert len(result["lines"]) == 1
    assert result["lines"][0]["rate"] == "8.00"


def test_list_tax_templates_all(setup):
    """List all templates for a company."""
    s = setup
    result = _call_action(
        ACTIONS["list-tax-templates"], s["conn"],
        company_id=s["company_id"],
    )
    assert result["status"] == "ok"
    assert len(result["templates"]) == 2
    names = [t["name"] for t in result["templates"]]
    assert "Sales Tax 8%" in names
    assert "Purchase Tax 5%" in names


def test_list_tax_templates_filtered_by_type(setup):
    """List templates filtered by tax_type."""
    s = setup
    result = _call_action(
        ACTIONS["list-tax-templates"], s["conn"],
        company_id=s["company_id"],
        tax_type="sales",
    )
    assert result["status"] == "ok"
    assert len(result["templates"]) == 1
    assert result["templates"][0]["name"] == "Sales Tax 8%"
