"""Tests for resolve-tax-template action."""
import json
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
    """Create company + accounts + templates + rules for resolve tests."""
    conn = fresh_db
    company_id = create_test_company(conn)
    tax_acct = create_test_account(
        conn, company_id, "Sales Tax Payable", "liability",
        account_type="tax",
    )
    # Create default sales tax template
    default_tmpl_id, _ = create_test_tax_template(
        conn, company_id, tax_acct, name="Default Sales Tax",
        tax_type="sales", rate="6.0", is_default=True,
    )
    # Create NY-specific template
    ny_tmpl_id, _ = create_test_tax_template(
        conn, company_id, tax_acct, name="NY Sales Tax",
        tax_type="sales", rate="8.875",
    )
    # Create customer
    cust_id = create_test_customer(conn, company_id, name="Regular Customer")
    exempt_cust_id = create_test_customer(
        conn, company_id, name="Exempt Customer", exempt=True,
    )

    # Create NY shipping state rule
    _call_action(
        ACTIONS["add-tax-rule"], conn,
        tax_template_id=ny_tmpl_id,
        tax_type="sales", priority=1,
        shipping_state="NY",
    )

    return {
        "conn": conn,
        "company_id": company_id,
        "default_tmpl_id": default_tmpl_id,
        "ny_tmpl_id": ny_tmpl_id,
        "cust_id": cust_id,
        "exempt_cust_id": exempt_cust_id,
    }


def test_resolve_by_rule(setup):
    """Resolve should pick NY template when shipping to NY."""
    s = setup
    shipping = json.dumps({"state": "NY", "zip": "10001"})
    result = _call_action(
        ACTIONS["resolve-tax-template"], s["conn"],
        party_type="customer",
        party_id=s["cust_id"],
        company_id=s["company_id"],
        shipping_address=shipping,
    )
    assert result["status"] == "ok"
    assert result["tax_template_id"] == s["ny_tmpl_id"]
    assert result["template_name"] == "NY Sales Tax"
    assert result["is_exempt"] is False


def test_resolve_fallback_to_default(setup):
    """When no rule matches, resolve should fall back to company default."""
    s = setup
    # Ship to CA — no rule for CA
    shipping = json.dumps({"state": "CA"})
    result = _call_action(
        ACTIONS["resolve-tax-template"], s["conn"],
        party_type="customer",
        party_id=s["cust_id"],
        company_id=s["company_id"],
        shipping_address=shipping,
    )
    assert result["status"] == "ok"
    assert result["tax_template_id"] == s["default_tmpl_id"]
    assert result["template_name"] == "Default Sales Tax"


def test_resolve_exempt_party(setup):
    """Exempt customer should be flagged, but template still resolved."""
    s = setup
    result = _call_action(
        ACTIONS["resolve-tax-template"], s["conn"],
        party_type="customer",
        party_id=s["exempt_cust_id"],
        company_id=s["company_id"],
    )
    assert result["status"] == "ok"
    assert result["is_exempt"] is True
    # Template still resolved (caller decides whether to apply)
    assert result["tax_template_id"] is not None


def test_resolve_no_shipping_no_rule(setup):
    """No shipping address and no matching rule should fall back to default."""
    s = setup
    result = _call_action(
        ACTIONS["resolve-tax-template"], s["conn"],
        party_type="customer",
        party_id=s["cust_id"],
        company_id=s["company_id"],
    )
    assert result["status"] == "ok"
    assert result["tax_template_id"] == s["default_tmpl_id"]
