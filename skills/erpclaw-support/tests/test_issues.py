"""Tests for issue management actions (add, update, get, list, comment, resolve, reopen)."""
import pytest
from helpers import (
    _call_action,
    create_test_company,
    create_test_customer,
    create_test_sla,
    create_test_issue,
)
from db_query import ACTIONS


# ---------------------------------------------------------------------------
# 1. add issue with default SLA auto-assigned
# ---------------------------------------------------------------------------

def test_add_issue_with_sla_auto_assign(fresh_db):
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id=company_id)
    sla_id = create_test_sla(fresh_db, name="Default SLA", is_default=True)

    result = _call_action(
        ACTIONS["add-issue"], fresh_db,
        customer_id=customer_id, subject="Laptop overheating", priority="high",
    )

    assert result["status"] == "ok"
    issue = result["issue"]
    assert issue["sla_id"] == sla_id, "Default SLA should be auto-assigned"
    assert issue["response_due"] is not None
    assert issue["resolution_due"] is not None


# ---------------------------------------------------------------------------
# 2. add issue with explicit SLA
# ---------------------------------------------------------------------------

def test_add_issue_with_explicit_sla(fresh_db):
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id=company_id)
    sla_id = create_test_sla(fresh_db, name="Gold SLA", is_default=False)

    result = _call_action(
        ACTIONS["add-issue"], fresh_db,
        customer_id=customer_id, subject="Broken monitor",
        priority="medium", sla_id=sla_id,
    )

    assert result["status"] == "ok"
    assert result["issue"]["sla_id"] == sla_id


# ---------------------------------------------------------------------------
# 3. add issue missing subject
# ---------------------------------------------------------------------------

def test_add_issue_missing_subject(fresh_db):
    company_id = create_test_company(fresh_db)

    result = _call_action(
        ACTIONS["add-issue"], fresh_db,
        customer_id=None, subject=None, priority="medium",
        company_id=company_id,
    )

    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# 4. update issue status and priority
# ---------------------------------------------------------------------------

def test_update_issue_status_and_priority(fresh_db):
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id=company_id)
    issue_id = create_test_issue(fresh_db, customer_id=customer_id)

    result = _call_action(
        ACTIONS["update-issue"], fresh_db,
        issue_id=issue_id, status="in_progress", priority="critical",
    )

    assert result["status"] == "ok"
    assert result["issue"]["status"] == "in_progress"
    assert result["issue"]["priority"] == "critical"


# ---------------------------------------------------------------------------
# 5. cannot update a closed issue
# ---------------------------------------------------------------------------

def test_cannot_update_closed_issue(fresh_db):
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id=company_id)
    issue_id = create_test_issue(fresh_db, customer_id=customer_id)

    # Resolve the issue first
    _call_action(ACTIONS["resolve-issue"], fresh_db, issue_id=issue_id)

    # Close the issue via update
    _call_action(
        ACTIONS["update-issue"], fresh_db,
        issue_id=issue_id, status="closed",
    )

    # Now try to update the closed issue
    result = _call_action(
        ACTIONS["update-issue"], fresh_db,
        issue_id=issue_id, priority="low",
    )

    assert result["status"] == "error"
    assert "closed" in result["message"].lower()


# ---------------------------------------------------------------------------
# 6. get issue with comments and SLA
# ---------------------------------------------------------------------------

def test_get_issue_with_comments_and_sla(fresh_db):
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id=company_id)
    sla_id = create_test_sla(fresh_db, name="Default SLA", is_default=True)
    issue_id = create_test_issue(fresh_db, customer_id=customer_id)

    # Add two comments: one employee, one customer
    _call_action(
        ACTIONS["add-issue-comment"], fresh_db,
        issue_id=issue_id, comment="Looking into this", comment_by="employee",
    )
    _call_action(
        ACTIONS["add-issue-comment"], fresh_db,
        issue_id=issue_id, comment="Thanks for the update", comment_by="customer",
    )

    result = _call_action(ACTIONS["get-issue"], fresh_db, issue_id=issue_id)

    assert result["status"] == "ok"
    issue = result["issue"]
    assert len(issue["comments"]) == 2
    assert issue["comments"][0]["comment_by"] == "employee"
    assert issue["comments"][1]["comment_by"] == "customer"
    assert issue["sla"] is not None
    assert issue["sla"]["id"] == sla_id
    assert issue["sla_status"] is not None
    assert "is_response_overdue" in issue["sla_status"]
    assert "is_resolution_overdue" in issue["sla_status"]


# ---------------------------------------------------------------------------
# 7. list issues with priority filter
# ---------------------------------------------------------------------------

def test_list_issues_with_filters(fresh_db):
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id=company_id)

    # Create 3 issues: 2 high, 1 low
    create_test_issue(fresh_db, customer_id=customer_id,
                      subject="High issue 1", priority="high")
    create_test_issue(fresh_db, customer_id=customer_id,
                      subject="High issue 2", priority="high")
    create_test_issue(fresh_db, customer_id=customer_id,
                      subject="Low issue", priority="low")

    result = _call_action(
        ACTIONS["list-issues"], fresh_db, priority="high",
    )

    assert result["status"] == "ok"
    assert result["total"] == 2


# ---------------------------------------------------------------------------
# 8. employee comment triggers first_response_at
# ---------------------------------------------------------------------------

def test_add_comment_triggers_first_response(fresh_db):
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id=company_id)
    create_test_sla(fresh_db, name="Default SLA", is_default=True)
    issue_id = create_test_issue(fresh_db, customer_id=customer_id)

    # Verify first_response_at is initially None
    get_result = _call_action(ACTIONS["get-issue"], fresh_db, issue_id=issue_id)
    assert get_result["issue"]["first_response_at"] is None

    # Add employee comment
    _call_action(
        ACTIONS["add-issue-comment"], fresh_db,
        issue_id=issue_id, comment="We are investigating", comment_by="employee",
    )

    # Verify first_response_at is now set
    get_result = _call_action(ACTIONS["get-issue"], fresh_db, issue_id=issue_id)
    assert get_result["issue"]["first_response_at"] is not None


# ---------------------------------------------------------------------------
# 9. resolve issue sets resolved_at and resolution_notes
# ---------------------------------------------------------------------------

def test_resolve_issue_sets_resolved_at(fresh_db):
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id=company_id)
    issue_id = create_test_issue(fresh_db, customer_id=customer_id)

    result = _call_action(
        ACTIONS["resolve-issue"], fresh_db,
        issue_id=issue_id, resolution_notes="Fixed the printer",
    )

    assert result["status"] == "ok"
    issue = result["issue"]
    assert issue["status"] == "resolved"
    assert issue["resolved_at"] is not None
    assert issue["resolution_notes"] == "Fixed the printer"


# ---------------------------------------------------------------------------
# 10. reopen issue clears resolved_at and resolution_notes
# ---------------------------------------------------------------------------

def test_reopen_issue_clears_resolved(fresh_db):
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id=company_id)
    issue_id = create_test_issue(fresh_db, customer_id=customer_id)

    # Resolve first
    _call_action(
        ACTIONS["resolve-issue"], fresh_db,
        issue_id=issue_id, resolution_notes="Temporary fix applied",
    )

    # Reopen
    result = _call_action(
        ACTIONS["reopen-issue"], fresh_db,
        issue_id=issue_id, reason="Problem reoccurred",
    )

    assert result["status"] == "ok"
    issue = result["issue"]
    assert issue["status"] == "open"
    assert issue["resolved_at"] is None
    assert issue["resolution_notes"] is None
