"""Tests for expense claims with GL posting verification."""
import json
import uuid
import pytest
from decimal import Decimal

from helpers import (
    _call_action,
    setup_hr_environment,
    create_test_employee,
    create_test_account,
)
from db_query import ACTIONS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_and_submit_claim(fresh_db, env, emp_id, items_json=None,
                              expense_date="2026-03-15"):
    """Create a draft expense claim and submit it. Returns claim_id."""
    if items_json is None:
        items_json = json.dumps([
            {"expense_type": "travel", "description": "Flight", "amount": "500.00"},
            {"expense_type": "meals", "description": "Dinner", "amount": "75.00"},
        ])

    add_result = _call_action(
        ACTIONS["add-expense-claim"], fresh_db,
        employee_id=emp_id,
        expense_date=expense_date,
        company_id=env["company_id"],
        items=items_json,
    )
    assert add_result["status"] == "ok"
    claim_id = add_result["expense_claim_id"]

    submit_result = _call_action(
        ACTIONS["submit-expense-claim"], fresh_db,
        expense_claim_id=claim_id,
    )
    assert submit_result["status"] == "ok"

    return claim_id


# ---------------------------------------------------------------------------
# 1. test_add_expense_claim
# ---------------------------------------------------------------------------

def test_add_expense_claim(fresh_db):
    """Create a draft expense claim with two items, verify totals."""
    env = setup_hr_environment(fresh_db)
    emp_id = create_test_employee(fresh_db, env["company_id"], first_name="Alice")

    items = json.dumps([
        {"expense_type": "travel", "description": "Flight", "amount": "500.00"},
        {"expense_type": "meals", "description": "Dinner", "amount": "75.00"},
    ])

    result = _call_action(
        ACTIONS["add-expense-claim"], fresh_db,
        employee_id=emp_id,
        expense_date="2026-03-15",
        company_id=env["company_id"],
        items=items,
    )

    assert result["status"] == "ok"
    assert "expense_claim_id" in result
    assert result["total_amount"] == "575.00"
    assert result["item_count"] == 2

    # Verify DB row
    claim = fresh_db.execute(
        "SELECT * FROM expense_claim WHERE id = ?",
        (result["expense_claim_id"],),
    ).fetchone()
    assert claim is not None
    assert claim["status"] == "draft"
    assert claim["total_amount"] == "575.00"
    assert claim["employee_id"] == emp_id

    # Verify child items
    claim_items = fresh_db.execute(
        "SELECT * FROM expense_claim_item WHERE expense_claim_id = ?",
        (result["expense_claim_id"],),
    ).fetchall()
    assert len(claim_items) == 2


# ---------------------------------------------------------------------------
# 2. test_submit_expense_claim
# ---------------------------------------------------------------------------

def test_submit_expense_claim(fresh_db):
    """Submit a draft expense claim, verify status transition."""
    env = setup_hr_environment(fresh_db)
    emp_id = create_test_employee(fresh_db, env["company_id"], first_name="Bob")

    items = json.dumps([
        {"expense_type": "travel", "description": "Taxi", "amount": "50.00"},
    ])

    add_result = _call_action(
        ACTIONS["add-expense-claim"], fresh_db,
        employee_id=emp_id,
        expense_date="2026-03-15",
        company_id=env["company_id"],
        items=items,
    )
    assert add_result["status"] == "ok"
    claim_id = add_result["expense_claim_id"]

    submit_result = _call_action(
        ACTIONS["submit-expense-claim"], fresh_db,
        expense_claim_id=claim_id,
    )

    assert submit_result["status"] == "ok"

    # Verify DB status
    claim = fresh_db.execute(
        "SELECT status FROM expense_claim WHERE id = ?", (claim_id,),
    ).fetchone()
    assert claim["status"] == "submitted"


# ---------------------------------------------------------------------------
# 3. test_approve_expense_claim_gl
# ---------------------------------------------------------------------------

def test_approve_expense_claim_gl(fresh_db):
    """Approve an expense claim and verify GL entries balance.

    This is the most critical test: after approval, GL should contain
    DR entries on expense account(s) and a CR entry on the payable account,
    with total debits equaling total credits.
    """
    env = setup_hr_environment(fresh_db)
    claimant_id = create_test_employee(fresh_db, env["company_id"], first_name="Carol")
    approver_id = create_test_employee(fresh_db, env["company_id"], first_name="Manager")

    items = json.dumps([
        {"expense_type": "travel", "description": "Flight", "amount": "500.00"},
        {"expense_type": "meals", "description": "Dinner", "amount": "75.00"},
    ])

    claim_id = _create_and_submit_claim(
        fresh_db, env, claimant_id, items_json=items,
    )

    # Approve
    approve_result = _call_action(
        ACTIONS["approve-expense-claim"], fresh_db,
        expense_claim_id=claim_id,
        approved_by=approver_id,
    )

    assert approve_result["status"] == "ok"
    assert approve_result["gl_entry_count"] > 0

    # Verify DB status = approved
    claim = fresh_db.execute(
        "SELECT status, approved_by FROM expense_claim WHERE id = ?",
        (claim_id,),
    ).fetchone()
    assert claim["status"] == "approved"
    assert claim["approved_by"] == approver_id

    # Verify GL entries
    gl_rows = fresh_db.execute(
        """SELECT * FROM gl_entry
           WHERE voucher_type = 'expense_claim' AND voucher_id = ?
           AND is_cancelled = 0""",
        (claim_id,),
    ).fetchall()
    assert len(gl_rows) >= 3  # 2 DR (travel + meals) + 1 CR (payable)

    total_debit = sum(Decimal(r["debit"]) for r in gl_rows)
    total_credit = sum(Decimal(r["credit"]) for r in gl_rows)

    # Debits must equal credits
    assert total_debit == total_credit, (
        f"GL imbalance: debits={total_debit}, credits={total_credit}"
    )

    # Total should equal claim amount
    assert total_debit == Decimal("575.00"), (
        f"Expected total debit=575.00, got {total_debit}"
    )

    # Verify DR entries are on expense accounts, CR entry is on payable
    dr_entries = [r for r in gl_rows if Decimal(r["debit"]) > 0]
    cr_entries = [r for r in gl_rows if Decimal(r["credit"]) > 0]

    assert len(dr_entries) == 2, f"Expected 2 DR entries, got {len(dr_entries)}"
    assert len(cr_entries) == 1, f"Expected 1 CR entry, got {len(cr_entries)}"

    # CR entry should be on the payable account
    cr_account_id = cr_entries[0]["account_id"]
    assert cr_account_id == env["payable_account_id"], (
        f"CR entry should be on payable account {env['payable_account_id']}, "
        f"got {cr_account_id}"
    )

    # DR amounts should match individual item amounts
    dr_amounts = sorted([Decimal(r["debit"]) for r in dr_entries])
    assert dr_amounts == sorted([Decimal("75.00"), Decimal("500.00")]), (
        f"DR amounts {dr_amounts} don't match expected [75.00, 500.00]"
    )


# ---------------------------------------------------------------------------
# 4. test_reject_expense_claim
# ---------------------------------------------------------------------------

def test_reject_expense_claim(fresh_db):
    """Reject a submitted claim with a reason."""
    env = setup_hr_environment(fresh_db)
    emp_id = create_test_employee(fresh_db, env["company_id"], first_name="Dave")

    claim_id = _create_and_submit_claim(fresh_db, env, emp_id)

    result = _call_action(
        ACTIONS["reject-expense-claim"], fresh_db,
        expense_claim_id=claim_id,
        reason="Not eligible",
    )

    assert result["status"] == "ok"
    assert result["rejection_reason"] == "Not eligible"

    # Verify DB status
    claim = fresh_db.execute(
        "SELECT status FROM expense_claim WHERE id = ?", (claim_id,),
    ).fetchone()
    assert claim["status"] == "rejected"

    # Verify no GL entries were created (reject does not post GL)
    gl_count = fresh_db.execute(
        """SELECT COUNT(*) FROM gl_entry
           WHERE voucher_type = 'expense_claim' AND voucher_id = ?""",
        (claim_id,),
    ).fetchone()[0]
    assert gl_count == 0


# ---------------------------------------------------------------------------
# 5. test_reject_requires_submitted
# ---------------------------------------------------------------------------

def test_reject_requires_submitted(fresh_db):
    """Rejecting a draft (not submitted) claim should fail."""
    env = setup_hr_environment(fresh_db)
    emp_id = create_test_employee(fresh_db, env["company_id"], first_name="Eve")

    items = json.dumps([
        {"expense_type": "travel", "description": "Taxi", "amount": "30.00"},
    ])

    add_result = _call_action(
        ACTIONS["add-expense-claim"], fresh_db,
        employee_id=emp_id,
        expense_date="2026-03-15",
        company_id=env["company_id"],
        items=items,
    )
    assert add_result["status"] == "ok"
    claim_id = add_result["expense_claim_id"]

    # Try to reject without submitting first
    reject_result = _call_action(
        ACTIONS["reject-expense-claim"], fresh_db,
        expense_claim_id=claim_id,
    )

    assert reject_result["status"] == "error"


# ---------------------------------------------------------------------------
# 6. test_approve_expense_claim_self_approval
# ---------------------------------------------------------------------------

def test_approve_expense_claim_self_approval(fresh_db):
    """An employee cannot approve their own expense claim."""
    env = setup_hr_environment(fresh_db)
    emp_id = create_test_employee(fresh_db, env["company_id"], first_name="Frank")

    claim_id = _create_and_submit_claim(fresh_db, env, emp_id)

    # Try to approve with the same employee as approved_by
    result = _call_action(
        ACTIONS["approve-expense-claim"], fresh_db,
        expense_claim_id=claim_id,
        approved_by=emp_id,
    )

    assert result["status"] == "error"
    assert "own" in result["message"].lower() or "cannot" in result["message"].lower()


# ---------------------------------------------------------------------------
# 7. test_update_expense_claim_status
# ---------------------------------------------------------------------------

def test_update_expense_claim_status(fresh_db):
    """After approval, update status to 'paid' with a payment_entry_id."""
    env = setup_hr_environment(fresh_db)
    claimant_id = create_test_employee(fresh_db, env["company_id"], first_name="Grace")
    approver_id = create_test_employee(fresh_db, env["company_id"], first_name="Approver")

    claim_id = _create_and_submit_claim(fresh_db, env, claimant_id)

    # Approve
    approve_result = _call_action(
        ACTIONS["approve-expense-claim"], fresh_db,
        expense_claim_id=claim_id,
        approved_by=approver_id,
    )
    assert approve_result["status"] == "ok"

    # Update to paid
    payment_id = str(uuid.uuid4())

    update_result = _call_action(
        ACTIONS["update-expense-claim-status"], fresh_db,
        expense_claim_id=claim_id,
        status="paid",
        payment_entry_id=payment_id,
    )

    assert update_result["status"] == "ok"
    assert update_result["new_status"] == "paid"
    assert update_result["payment_entry_id"] == payment_id

    # Verify DB
    claim = fresh_db.execute(
        "SELECT status, payment_entry_id FROM expense_claim WHERE id = ?",
        (claim_id,),
    ).fetchone()
    assert claim["status"] == "paid"
    assert claim["payment_entry_id"] == payment_id


# ---------------------------------------------------------------------------
# 8. test_list_expense_claims
# ---------------------------------------------------------------------------

def test_list_expense_claims(fresh_db):
    """Create 3 claims in different statuses, list by status filter."""
    env = setup_hr_environment(fresh_db)
    emp_id = create_test_employee(fresh_db, env["company_id"], first_name="Heidi")
    approver_id = create_test_employee(fresh_db, env["company_id"], first_name="Boss")

    # Claim 1: stays draft
    items1 = json.dumps([
        {"expense_type": "supplies", "description": "Pens", "amount": "10.00"},
    ])
    add1 = _call_action(
        ACTIONS["add-expense-claim"], fresh_db,
        employee_id=emp_id,
        expense_date="2026-03-01",
        company_id=env["company_id"],
        items=items1,
    )
    assert add1["status"] == "ok"

    # Claim 2: submitted
    items2 = json.dumps([
        {"expense_type": "meals", "description": "Lunch", "amount": "25.00"},
    ])
    add2 = _call_action(
        ACTIONS["add-expense-claim"], fresh_db,
        employee_id=emp_id,
        expense_date="2026-03-05",
        company_id=env["company_id"],
        items=items2,
    )
    assert add2["status"] == "ok"
    sub2 = _call_action(
        ACTIONS["submit-expense-claim"], fresh_db,
        expense_claim_id=add2["expense_claim_id"],
    )
    assert sub2["status"] == "ok"

    # Claim 3: approved
    items3 = json.dumps([
        {"expense_type": "travel", "description": "Uber", "amount": "40.00"},
    ])
    add3 = _call_action(
        ACTIONS["add-expense-claim"], fresh_db,
        employee_id=emp_id,
        expense_date="2026-03-10",
        company_id=env["company_id"],
        items=items3,
    )
    assert add3["status"] == "ok"
    sub3 = _call_action(
        ACTIONS["submit-expense-claim"], fresh_db,
        expense_claim_id=add3["expense_claim_id"],
    )
    assert sub3["status"] == "ok"
    appr3 = _call_action(
        ACTIONS["approve-expense-claim"], fresh_db,
        expense_claim_id=add3["expense_claim_id"],
        approved_by=approver_id,
    )
    assert appr3["status"] == "ok"

    # List all: should get 3
    list_all = _call_action(
        ACTIONS["list-expense-claims"], fresh_db,
        company_id=env["company_id"],
    )
    assert list_all["status"] == "ok"
    assert list_all["total_count"] == 3

    # List draft only: should get 1
    list_draft = _call_action(
        ACTIONS["list-expense-claims"], fresh_db,
        company_id=env["company_id"],
        status="draft",
    )
    assert list_draft["status"] == "ok"
    assert list_draft["total_count"] == 1
    assert list_draft["expense_claims"][0]["status"] == "draft"

    # List approved only: should get 1
    list_approved = _call_action(
        ACTIONS["list-expense-claims"], fresh_db,
        company_id=env["company_id"],
        status="approved",
    )
    assert list_approved["status"] == "ok"
    assert list_approved["total_count"] == 1
    assert list_approved["expense_claims"][0]["status"] == "approved"


# ---------------------------------------------------------------------------
# 9. test_expense_claim_with_account_ids
# ---------------------------------------------------------------------------

def test_expense_claim_with_account_ids(fresh_db):
    """Expense claim items with explicit account_id fields use those in GL."""
    env = setup_hr_environment(fresh_db)
    claimant_id = create_test_employee(fresh_db, env["company_id"], first_name="Ivan")
    approver_id = create_test_employee(fresh_db, env["company_id"], first_name="Janet")

    # Create two distinct expense accounts
    travel_acct_id = create_test_account(
        fresh_db, env["company_id"],
        name="Travel Expenses", root_type="expense",
        account_type="expense", account_number="6300",
    )
    meals_acct_id = create_test_account(
        fresh_db, env["company_id"],
        name="Meals Expenses", root_type="expense",
        account_type="expense", account_number="6400",
    )

    items = json.dumps([
        {
            "expense_type": "travel",
            "description": "Flight to NYC",
            "amount": "800.00",
            "account_id": travel_acct_id,
        },
        {
            "expense_type": "meals",
            "description": "Team dinner",
            "amount": "200.00",
            "account_id": meals_acct_id,
        },
    ])

    claim_id = _create_and_submit_claim(
        fresh_db, env, claimant_id, items_json=items,
    )

    # Approve
    approve_result = _call_action(
        ACTIONS["approve-expense-claim"], fresh_db,
        expense_claim_id=claim_id,
        approved_by=approver_id,
    )
    assert approve_result["status"] == "ok"

    # Verify GL entries use the explicit account IDs
    gl_rows = fresh_db.execute(
        """SELECT * FROM gl_entry
           WHERE voucher_type = 'expense_claim' AND voucher_id = ?
           AND is_cancelled = 0""",
        (claim_id,),
    ).fetchall()

    # DR entries: one for travel account, one for meals account
    dr_entries = [r for r in gl_rows if Decimal(r["debit"]) > 0]
    dr_account_ids = {r["account_id"] for r in dr_entries}

    assert travel_acct_id in dr_account_ids, (
        f"Travel account {travel_acct_id} should appear in GL DR entries"
    )
    assert meals_acct_id in dr_account_ids, (
        f"Meals account {meals_acct_id} should appear in GL DR entries"
    )

    # Verify specific amounts per account
    for dr in dr_entries:
        if dr["account_id"] == travel_acct_id:
            assert Decimal(dr["debit"]) == Decimal("800.00")
        elif dr["account_id"] == meals_acct_id:
            assert Decimal(dr["debit"]) == Decimal("200.00")

    # Total debits = total credits = 1000.00
    total_debit = sum(Decimal(r["debit"]) for r in gl_rows)
    total_credit = sum(Decimal(r["credit"]) for r in gl_rows)
    assert total_debit == total_credit == Decimal("1000.00"), (
        f"GL totals: debit={total_debit}, credit={total_credit}, expected 1000.00"
    )
