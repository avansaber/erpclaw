"""Tests for add-tax-template action."""
import json
import uuid
import pytest

from db_query import ACTIONS
from helpers import (
    _call_action,
    create_test_company,
    create_test_account,
)


@pytest.fixture
def setup(fresh_db):
    """Create company + tax account for template tests."""
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
    return {
        "conn": conn,
        "company_id": company_id,
        "tax_acct": tax_acct,
        "tax_acct_2": tax_acct_2,
    }


def test_add_tax_template_basic(setup):
    """Create a basic sales tax template with one line."""
    s = setup
    lines = json.dumps([{
        "tax_account_id": s["tax_acct"],
        "rate": "8.25",
    }])
    result = _call_action(
        ACTIONS["add-tax-template"], s["conn"],
        name="NY Sales Tax",
        tax_type="sales",
        company_id=s["company_id"],
        lines=lines,
    )
    assert result["status"] == "ok"
    assert result["tax_template_id"]
    assert result["name"] == "NY Sales Tax"
    assert result["line_count"] == 1

    # Verify in DB
    row = s["conn"].execute(
        "SELECT * FROM tax_template WHERE id = ?",
        (result["tax_template_id"],),
    ).fetchone()
    assert row is not None
    assert row["name"] == "NY Sales Tax"
    assert row["tax_type"] == "sales"
    assert row["is_default"] == 0


def test_add_tax_template_with_multiple_lines(setup):
    """Create a template with two cascading lines (state + county)."""
    s = setup
    lines = json.dumps([
        {"tax_account_id": s["tax_acct"], "rate": "6.0",
         "charge_type": "on_net_total", "row_order": 0},
        {"tax_account_id": s["tax_acct_2"], "rate": "2.25",
         "charge_type": "on_previous_row_total", "row_order": 1},
    ])
    result = _call_action(
        ACTIONS["add-tax-template"], s["conn"],
        name="Combined State+County Tax",
        tax_type="sales",
        company_id=s["company_id"],
        lines=lines,
    )
    assert result["status"] == "ok"
    assert result["line_count"] == 2

    # Verify lines stored correctly
    db_lines = s["conn"].execute(
        "SELECT * FROM tax_template_line WHERE tax_template_id = ? ORDER BY row_order",
        (result["tax_template_id"],),
    ).fetchall()
    assert len(db_lines) == 2
    assert db_lines[0]["charge_type"] == "on_net_total"
    assert db_lines[1]["charge_type"] == "on_previous_row_total"


def test_add_tax_template_as_default(setup):
    """Creating a template with --is-default should clear other defaults."""
    s = setup
    lines = json.dumps([{"tax_account_id": s["tax_acct"], "rate": "7.0"}])
    # First template as default
    r1 = _call_action(
        ACTIONS["add-tax-template"], s["conn"],
        name="Default Tax A",
        tax_type="sales",
        company_id=s["company_id"],
        lines=lines,
        is_default=True,
    )
    assert r1["status"] == "ok"

    # Second template as default — should replace first
    r2 = _call_action(
        ACTIONS["add-tax-template"], s["conn"],
        name="Default Tax B",
        tax_type="sales",
        company_id=s["company_id"],
        lines=lines,
        is_default=True,
    )
    assert r2["status"] == "ok"

    # First should no longer be default
    row_a = s["conn"].execute(
        "SELECT is_default FROM tax_template WHERE id = ?",
        (r1["tax_template_id"],),
    ).fetchone()
    assert row_a["is_default"] == 0

    row_b = s["conn"].execute(
        "SELECT is_default FROM tax_template WHERE id = ?",
        (r2["tax_template_id"],),
    ).fetchone()
    assert row_b["is_default"] == 1


def test_add_tax_template_missing_required_fields(setup):
    """Missing name, tax_type, or lines should return error."""
    s = setup
    lines = json.dumps([{"tax_account_id": s["tax_acct"], "rate": "5.0"}])

    # Missing name
    r = _call_action(
        ACTIONS["add-tax-template"], s["conn"],
        tax_type="sales", company_id=s["company_id"], lines=lines,
    )
    assert r["status"] == "error"
    assert "name" in r["message"].lower()

    # Missing tax_type
    r = _call_action(
        ACTIONS["add-tax-template"], s["conn"],
        name="X", company_id=s["company_id"], lines=lines,
    )
    assert r["status"] == "error"

    # Missing lines
    r = _call_action(
        ACTIONS["add-tax-template"], s["conn"],
        name="X", tax_type="sales", company_id=s["company_id"],
    )
    assert r["status"] == "error"
    assert "lines" in r["message"].lower()


def test_add_tax_template_invalid_account(setup):
    """Line with non-existent account ID should return error."""
    s = setup
    lines = json.dumps([{"tax_account_id": "nonexistent-id", "rate": "5.0"}])
    r = _call_action(
        ACTIONS["add-tax-template"], s["conn"],
        name="Bad Template",
        tax_type="sales",
        company_id=s["company_id"],
        lines=lines,
    )
    assert r["status"] == "error"
    assert "not found" in r["message"].lower()
