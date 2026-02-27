"""Tests for tutorial action.

Test IDs: S-T-01 through S-T-03
"""
import db_query
from helpers import _call_action


# ---------------------------------------------------------------------------
# S-T-01: Tutorial creates Acme Corp with accounts and fiscal year
# ---------------------------------------------------------------------------
def test_tutorial_creates_demo(fresh_db):
    result = _call_action(db_query.tutorial, fresh_db)

    assert result["status"] == "ok"
    assert result["company_name"] == "Acme Corp"
    assert result["accounts_created"] == 20
    assert "company_id" in result
    assert "next_steps" in result
    assert len(result["next_steps"]) == 5

    # Verify accounts exist in DB
    accounts = fresh_db.execute(
        "SELECT COUNT(*) as cnt FROM account WHERE company_id = ?",
        (result["company_id"],),
    ).fetchone()["cnt"]
    assert accounts == 20

    # Verify fiscal year
    fy = fresh_db.execute(
        "SELECT * FROM fiscal_year WHERE company_id = ?",
        (result["company_id"],),
    ).fetchone()
    assert fy is not None
    assert "FY" in fy["name"]

    # Verify cost center
    cc = fresh_db.execute(
        "SELECT * FROM cost_center WHERE company_id = ?",
        (result["company_id"],),
    ).fetchone()
    assert cc is not None
    assert cc["name"] == "Main"

    # Verify company defaults are set
    company = fresh_db.execute(
        "SELECT * FROM company WHERE id = ?",
        (result["company_id"],),
    ).fetchone()
    assert company["default_receivable_account_id"] is not None
    assert company["default_payable_account_id"] is not None
    assert company["default_income_account_id"] is not None
    assert company["default_expense_account_id"] is not None
    assert company["default_bank_account_id"] is not None
    assert company["default_cash_account_id"] is not None
    assert company["default_cost_center_id"] is not None


# ---------------------------------------------------------------------------
# S-T-02: Tutorial is idempotent — running twice returns existing data
# ---------------------------------------------------------------------------
def test_tutorial_idempotent(fresh_db):
    result1 = _call_action(db_query.tutorial, fresh_db)
    assert result1["status"] == "ok"
    company_id = result1["company_id"]

    result2 = _call_action(db_query.tutorial, fresh_db)
    assert result2["status"] == "ok"
    assert result2["company_id"] == company_id
    assert "already exists" in result2["message"]


# ---------------------------------------------------------------------------
# S-T-03: Tutorial next_steps have required fields
# ---------------------------------------------------------------------------
def test_tutorial_next_steps_structure(fresh_db):
    result = _call_action(db_query.tutorial, fresh_db)
    for step in result["next_steps"]:
        assert "step" in step
        assert "skill" in step
        assert "action" in step
