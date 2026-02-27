"""Tests for budget actions.

Test IDs: GL-BG-01 through GL-BG-03
"""
import db_query
from helpers import (
    _call_action,
    create_test_company,
    create_test_fiscal_year,
    create_test_account,
    create_test_cost_center,
    post_test_gl_entries,
)


# ---------------------------------------------------------------------------
# GL-BG-01: add-budget
# ---------------------------------------------------------------------------
def test_add_budget(fresh_db):
    company_id = create_test_company(fresh_db)
    fy_id = create_test_fiscal_year(fresh_db, company_id)
    acct_id = create_test_account(
        fresh_db, company_id, "Marketing Expense", "expense",
        account_number="5100",
    )

    result = _call_action(
        db_query.add_budget, fresh_db,
        fiscal_year_id=fy_id,
        account_id=acct_id,
        budget_amount="50000.00",
        action_if_exceeded="warn",
    )
    assert result["status"] == "ok"
    assert "budget_id" in result

    # Verify in DB
    row = fresh_db.execute(
        "SELECT * FROM budget WHERE id = ?", (result["budget_id"],)
    ).fetchone()
    assert row["budget_amount"] == "50000.00"
    assert row["action_if_exceeded"] == "warn"
    assert row["account_id"] == acct_id
    assert row["company_id"] == company_id


# ---------------------------------------------------------------------------
# GL-BG-02: list-budgets with actual computation
# ---------------------------------------------------------------------------
def test_list_budgets_with_actual(fresh_db):
    company_id = create_test_company(fresh_db)
    fy_id = create_test_fiscal_year(fresh_db, company_id)
    expense_id = create_test_account(
        fresh_db, company_id, "Travel Expense", "expense",
        account_number="5200",
    )
    equity_id = create_test_account(
        fresh_db, company_id, "Owner Equity", "equity",
        account_number="3000",
    )

    # Create budget
    _call_action(
        db_query.add_budget, fresh_db,
        fiscal_year_id=fy_id,
        account_id=expense_id,
        budget_amount="10000.00",
    )

    # Post some expense GL entries
    post_test_gl_entries(fresh_db, company_id, [
        {"account_id": expense_id, "debit": "3000.00", "credit": "0"},
        {"account_id": equity_id, "debit": "0", "credit": "3000.00"},
    ], posting_date="2026-06-15")

    result = _call_action(
        db_query.list_budgets, fresh_db,
        fiscal_year_id=fy_id, company_id=company_id,
    )
    assert result["status"] == "ok"
    assert len(result["budgets"]) == 1

    budget = result["budgets"][0]
    assert budget["budget_amount"] == "10000.00"
    assert budget["actual_amount"] == "3000.00"
    # budget_amount is Decimal("10000.00"), actual is Decimal("3000.00")
    # Decimal subtraction preserves the precision: 10000.00 - 3000.00 = 7000.00
    assert budget["variance"] == "7000.00"


# ---------------------------------------------------------------------------
# GL-BG-03: budget requires account_id or cost_center_id
# ---------------------------------------------------------------------------
def test_budget_requires_account_or_cost_center(fresh_db):
    company_id = create_test_company(fresh_db)
    fy_id = create_test_fiscal_year(fresh_db, company_id)

    result = _call_action(
        db_query.add_budget, fresh_db,
        fiscal_year_id=fy_id,
        budget_amount="10000.00",
        # No account_id or cost_center_id
    )
    assert result["status"] == "error"
    assert "account-id" in result["message"].lower() or "cost-center-id" in result["message"].lower()
