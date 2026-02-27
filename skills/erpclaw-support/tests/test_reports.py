"""Tests for support reports and status actions."""
import pytest
from datetime import datetime
from helpers import (
    _call_action,
    create_test_company,
    create_test_customer,
    create_test_sla,
    create_test_issue,
    create_test_warranty_claim,
    create_test_maintenance_schedule,
)
from db_query import ACTIONS


# ---------------------------------------------------------------------------
# 1. SLA compliance report — mixed resolved and in-progress issues
# ---------------------------------------------------------------------------

def test_sla_compliance_report(fresh_db):
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id)
    _sla_id = create_test_sla(fresh_db, name="Default SLA", is_default=True)

    # Create 3 issues (all auto-assigned the default SLA via customer_id)
    issue_ids = []
    for i in range(3):
        iid = create_test_issue(
            fresh_db, subject=f"Issue {i+1}", customer_id=customer_id,
        )
        issue_ids.append(iid)

    # Resolve 2 of them (compliant — sla_breached stays 0, status becomes resolved)
    for iid in issue_ids[:2]:
        res = _call_action(
            ACTIONS["resolve-issue"], fresh_db,
            issue_id=iid, resolution_notes="Fixed",
        )
        assert res["status"] == "ok"

    # Third issue stays open (in_progress count)

    # Run the SLA compliance report
    result = _call_action(ACTIONS["sla-compliance-report"], fresh_db)

    assert result["status"] == "ok"
    report = result["report"]
    assert report["total_with_sla"] == 3
    assert report["compliant"] == 2
    assert report["in_progress"] == 1
    assert report["breached"] == 0
    # compliance_rate = 2 / (2 + 0) * 100 = 100.00
    assert report["compliance_rate_pct"] == "100.00"


# ---------------------------------------------------------------------------
# 2. Overdue issues report — empty when no overdue issues exist
# ---------------------------------------------------------------------------

def test_overdue_issues_report_empty(fresh_db):
    _company_id = create_test_company(fresh_db)

    result = _call_action(ACTIONS["overdue-issues-report"], fresh_db)

    assert result["status"] == "ok"
    assert result["total"] == 0
    assert result["overdue_issues"] == []


# ---------------------------------------------------------------------------
# 3. Status summary counts — issues, warranty claims, maintenance schedules
# ---------------------------------------------------------------------------

def test_status_summary_counts(fresh_db):
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id)
    _sla_id = create_test_sla(fresh_db, name="Default SLA", is_default=True)

    # Create 2 issues (both will be open)
    create_test_issue(fresh_db, subject="Issue A", customer_id=customer_id)
    create_test_issue(fresh_db, subject="Issue B", customer_id=customer_id)

    # Create 1 warranty claim
    create_test_warranty_claim(fresh_db, customer_id=customer_id)

    # Create 1 maintenance schedule (status defaults to active)
    create_test_maintenance_schedule(fresh_db, customer_id=customer_id)

    # Call status (no company filter — counts all records)
    result = _call_action(ACTIONS["status"], fresh_db)

    assert result["status"] == "ok"
    ss = result["support_status"]

    assert ss["issues"]["total"] == 2
    assert ss["issues"]["open"] == 2

    assert ss["warranty_claims"]["total"] == 1

    assert ss["maintenance_schedules"]["total"] == 1
    assert ss["maintenance_schedules"]["active"] == 1


# ---------------------------------------------------------------------------
# 4. Status returns zeroes when no data exists
# ---------------------------------------------------------------------------

def test_status_empty_returns_zeroes(fresh_db):
    _company_id = create_test_company(fresh_db)

    result = _call_action(ACTIONS["status"], fresh_db)

    assert result["status"] == "ok"
    ss = result["support_status"]

    assert ss["issues"]["total"] == 0
    assert ss["issues"]["open"] == 0
    assert ss["issues"]["in_progress"] == 0
    assert ss["issues"]["resolved"] == 0
    assert ss["issues"]["closed"] == 0
    assert ss["issues"]["breached"] == 0

    assert ss["warranty_claims"]["total"] == 0
    assert ss["warranty_claims"]["open"] == 0

    assert ss["maintenance_schedules"]["total"] == 0
    assert ss["maintenance_schedules"]["active"] == 0
    assert ss["maintenance_schedules"]["expired"] == 0

    assert ss["maintenance_visits"]["total"] == 0
    assert ss["maintenance_visits"]["scheduled"] == 0
    assert ss["maintenance_visits"]["completed"] == 0

    assert ss["overdue_issues"] == 0


# ---------------------------------------------------------------------------
# 5. SLA compliance report with date filter
# ---------------------------------------------------------------------------

def test_sla_compliance_with_date_filter(fresh_db):
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id)
    _sla_id = create_test_sla(fresh_db, name="Default SLA", is_default=True)

    # Create 2 issues (both auto-assigned default SLA)
    iid1 = create_test_issue(
        fresh_db, subject="Filter issue 1", customer_id=customer_id,
    )
    iid2 = create_test_issue(
        fresh_db, subject="Filter issue 2", customer_id=customer_id,
    )

    # Resolve both
    _call_action(
        ACTIONS["resolve-issue"], fresh_db,
        issue_id=iid1, resolution_notes="Done",
    )
    _call_action(
        ACTIONS["resolve-issue"], fresh_db,
        issue_id=iid2, resolution_notes="Done",
    )

    # Use today as from/to date range
    today = datetime.utcnow().strftime("%Y-%m-%d")

    result = _call_action(
        ACTIONS["sla-compliance-report"], fresh_db,
        from_date=today, to_date=today,
    )

    assert result["status"] == "ok"
    report = result["report"]
    assert report["compliant"] == 2
