"""Tests for update-tax-template action."""
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
    """Create company + tax account + template for update tests."""
    conn = fresh_db
    company_id = create_test_company(conn)
    tax_acct = create_test_account(
        conn, company_id, "Sales Tax Payable", "liability",
        account_type="tax",
    )
    tax_acct_2 = create_test_account(
        conn, company_id, "State Tax Payable", "liability",
        account_type="tax",
    )
    tmpl_id, line_id = create_test_tax_template(
        conn, company_id, tax_acct, name="Original Tax", rate="6.0",
    )
    return {
        "conn": conn,
        "company_id": company_id,
        "tax_acct": tax_acct,
        "tax_acct_2": tax_acct_2,
        "template_id": tmpl_id,
        "line_id": line_id,
    }


def test_update_tax_template_name(setup):
    """Update just the name of a template."""
    s = setup
    result = _call_action(
        ACTIONS["update-tax-template"], s["conn"],
        tax_template_id=s["template_id"],
        name="Renamed Tax Template",
    )
    assert result["status"] == "ok"
    assert "name" in result["updated_fields"]

    row = s["conn"].execute(
        "SELECT name FROM tax_template WHERE id = ?",
        (s["template_id"],),
    ).fetchone()
    assert row["name"] == "Renamed Tax Template"


def test_update_tax_template_replace_lines(setup):
    """Replace all lines with new ones."""
    s = setup
    new_lines = json.dumps([
        {"tax_account_id": s["tax_acct"], "rate": "4.0", "charge_type": "on_net_total"},
        {"tax_account_id": s["tax_acct_2"], "rate": "2.5", "charge_type": "on_net_total"},
    ])
    result = _call_action(
        ACTIONS["update-tax-template"], s["conn"],
        tax_template_id=s["template_id"],
        lines=new_lines,
    )
    assert result["status"] == "ok"
    assert "lines" in result["updated_fields"]

    db_lines = s["conn"].execute(
        "SELECT * FROM tax_template_line WHERE tax_template_id = ? ORDER BY row_order",
        (s["template_id"],),
    ).fetchall()
    assert len(db_lines) == 2
    assert db_lines[0]["rate"] == "4.00"
    assert db_lines[1]["rate"] == "2.50"


def test_update_tax_template_set_default(setup):
    """Setting is_default on update should work."""
    s = setup
    result = _call_action(
        ACTIONS["update-tax-template"], s["conn"],
        tax_template_id=s["template_id"],
        is_default=True,
    )
    assert result["status"] == "ok"
    assert "is_default" in result["updated_fields"]

    row = s["conn"].execute(
        "SELECT is_default FROM tax_template WHERE id = ?",
        (s["template_id"],),
    ).fetchone()
    assert row["is_default"] == 1
