"""Tests for status action."""
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


def test_status_empty(fresh_db):
    """Status on a fresh company should show all zeros."""
    conn = fresh_db
    company_id = create_test_company(conn)
    result = _call_action(
        ACTIONS["status"], conn,
        company_id=company_id,
    )
    assert result["status"] == "ok"
    assert result["templates"] == 0
    assert result["rules"] == 0
    assert result["withholding_categories"] == 0
    assert result["ytd_1099_vendors"] == 0


def test_status_with_data(fresh_db):
    """Status with templates and rules should reflect counts."""
    conn = fresh_db
    company_id = create_test_company(conn)
    tax_acct = create_test_account(
        conn, company_id, "Tax Payable", "liability",
        account_type="tax",
    )
    # Create 2 templates
    tmpl_id, _ = create_test_tax_template(
        conn, company_id, tax_acct, name="Tax A", rate="5.0",
    )
    create_test_tax_template(
        conn, company_id, tax_acct, name="Tax B",
        tax_type="purchase", rate="3.0",
    )

    # Create a rule
    cust_id = create_test_customer(conn, company_id)
    _call_action(
        ACTIONS["add-tax-rule"], conn,
        tax_template_id=tmpl_id,
        tax_type="sales", priority=1,
        customer_id=cust_id,
    )

    result = _call_action(
        ACTIONS["status"], conn,
        company_id=company_id,
    )
    assert result["status"] == "ok"
    assert result["templates"] == 2
    assert result["rules"] == 1
