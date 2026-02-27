"""Tests for calculate-tax action."""
import json
from decimal import Decimal
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
    """Create company + accounts for calculate tests."""
    conn = fresh_db
    company_id = create_test_company(conn)
    tax_acct = create_test_account(
        conn, company_id, "Sales Tax Payable", "liability",
        account_type="tax",
    )
    tax_acct_2 = create_test_account(
        conn, company_id, "County Tax Payable", "liability",
        account_type="tax",
    )
    return {
        "conn": conn,
        "company_id": company_id,
        "tax_acct": tax_acct,
        "tax_acct_2": tax_acct_2,
    }


def test_calculate_on_net_total(setup):
    """Basic 8% tax on a $1000 item = $80 tax."""
    s = setup
    tmpl_id, _ = create_test_tax_template(
        s["conn"], s["company_id"], s["tax_acct"],
        name="8% Sales Tax", rate="8.0",
    )
    items = json.dumps([{"item_id": "item-1", "net_amount": "1000.00"}])
    result = _call_action(
        ACTIONS["calculate-tax"], s["conn"],
        tax_template_id=tmpl_id,
        items=items,
    )
    assert result["status"] == "ok"
    assert Decimal(result["total_tax"]) == Decimal("80.00")
    assert Decimal(result["net_total"]) == Decimal("1000.00")
    assert Decimal(result["grand_total"]) == Decimal("1080.00")
    assert len(result["tax_lines"]) == 1
    assert Decimal(result["tax_lines"][0]["amount"]) == Decimal("80.00")


def test_calculate_cascading_on_previous_row(setup):
    """Tax line 2 at 2% on_previous_row_amount of line 1 (8% on $1000 = $80).
    Line 2 should be 2% of $80 = $1.60.
    """
    s = setup
    lines = json.dumps([
        {"tax_account_id": s["tax_acct"], "rate": "8.0",
         "charge_type": "on_net_total", "row_order": 0},
        {"tax_account_id": s["tax_acct_2"], "rate": "2.0",
         "charge_type": "on_previous_row_amount", "row_order": 1},
    ])
    tmpl_result = _call_action(
        ACTIONS["add-tax-template"], s["conn"],
        name="Cascading Tax", tax_type="sales",
        company_id=s["company_id"], lines=lines,
    )
    assert tmpl_result["status"] == "ok"
    tmpl_id = tmpl_result["tax_template_id"]

    items = json.dumps([{"item_id": "item-1", "net_amount": "1000.00"}])
    result = _call_action(
        ACTIONS["calculate-tax"], s["conn"],
        tax_template_id=tmpl_id,
        items=items,
    )
    assert result["status"] == "ok"
    assert Decimal(result["tax_lines"][0]["amount"]) == Decimal("80.00")
    assert Decimal(result["tax_lines"][1]["amount"]) == Decimal("1.60")
    assert Decimal(result["total_tax"]) == Decimal("81.60")
    assert Decimal(result["grand_total"]) == Decimal("1081.60")


def test_calculate_actual_charge(setup):
    """Actual charge type: rate IS the fixed amount (e.g. $25 flat fee)."""
    s = setup
    lines = json.dumps([
        {"tax_account_id": s["tax_acct"], "rate": "25.0",
         "charge_type": "actual"},
    ])
    tmpl_result = _call_action(
        ACTIONS["add-tax-template"], s["conn"],
        name="Flat Fee", tax_type="sales",
        company_id=s["company_id"], lines=lines,
    )
    tmpl_id = tmpl_result["tax_template_id"]

    items = json.dumps([{"item_id": "item-1", "net_amount": "500.00"}])
    result = _call_action(
        ACTIONS["calculate-tax"], s["conn"],
        tax_template_id=tmpl_id,
        items=items,
    )
    assert result["status"] == "ok"
    assert Decimal(result["total_tax"]) == Decimal("25.00")
    assert Decimal(result["grand_total"]) == Decimal("525.00")


def test_calculate_deduct(setup):
    """Deduct charge should produce negative tax."""
    s = setup
    lines = json.dumps([
        {"tax_account_id": s["tax_acct"], "rate": "5.0",
         "charge_type": "on_net_total", "add_deduct": "deduct"},
    ])
    tmpl_result = _call_action(
        ACTIONS["add-tax-template"], s["conn"],
        name="Deduction", tax_type="sales",
        company_id=s["company_id"], lines=lines,
    )
    tmpl_id = tmpl_result["tax_template_id"]

    items = json.dumps([{"item_id": "item-1", "net_amount": "200.00"}])
    result = _call_action(
        ACTIONS["calculate-tax"], s["conn"],
        tax_template_id=tmpl_id,
        items=items,
    )
    assert result["status"] == "ok"
    assert Decimal(result["total_tax"]) == Decimal("-10.00")
    assert Decimal(result["grand_total"]) == Decimal("190.00")


def test_calculate_per_item_distribution(setup):
    """Tax should be distributed proportionally across multiple items."""
    s = setup
    tmpl_id, _ = create_test_tax_template(
        s["conn"], s["company_id"], s["tax_acct"],
        name="10% Tax", rate="10.0",
    )
    items = json.dumps([
        {"item_id": "item-a", "net_amount": "300.00"},
        {"item_id": "item-b", "net_amount": "700.00"},
    ])
    result = _call_action(
        ACTIONS["calculate-tax"], s["conn"],
        tax_template_id=tmpl_id,
        items=items,
    )
    assert result["status"] == "ok"
    assert Decimal(result["total_tax"]) == Decimal("100.00")

    per_item = {p["item_id"]: p["tax_amount"] for p in result["per_item_tax"]}
    assert Decimal(per_item["item-a"]) == Decimal("30.00")
    assert Decimal(per_item["item-b"]) == Decimal("70.00")
