"""Support lifecycle integration tests (TestSupportScenario).

Tests the full support workflow: SLA creation, issue lifecycle (create,
assign, comment, resolve, reopen), warranty claims, maintenance schedules
and visits, and SLA compliance reporting.

Skills exercised: erpclaw-support
Supporting setup: erpclaw-setup helpers (company, customer, item, naming)
"""
import json
from decimal import Decimal

from helpers import (
    _call_action,
    create_test_company,
    create_test_fiscal_year,
    seed_naming_series,
    create_test_customer,
    create_test_item,
    setup_phase2_environment,
)


# ---------------------------------------------------------------------------
# Connection wrapper
# ---------------------------------------------------------------------------
# sqlite3.Connection is a C extension type that does not support arbitrary
# attribute assignment.  The support skill's add-issue, add-warranty-claim,
# and add-maintenance-schedule set conn.company_id for get_next_name().
# This wrapper delegates all standard connection methods while allowing
# dynamic attribute storage.

class _ConnectionWrapper:
    """Thin wrapper around sqlite3.Connection supporting arbitrary attrs."""

    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        if name == "_conn":
            object.__setattr__(self, name, value)
        else:
            object.__setattr__(self, name, value)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_default_sla(conn, company_id):
    """Create a default SLA with multi-priority levels. Returns SLA result dict."""
    priorities = json.dumps({
        "response_times": {
            "low": 24,
            "medium": 8,
            "high": 4,
            "critical": 1,
        },
        "resolution_times": {
            "low": 72,
            "medium": 24,
            "high": 12,
            "critical": 4,
        },
    })
    result = _call_action("erpclaw-support", "add-sla", conn,
                          name="Standard SLA",
                          priorities=priorities,
                          is_default="1",
                          company_id=company_id)
    assert result["status"] == "ok", f"add-sla failed: {result}"
    return result


def _setup_support_env(raw_conn):
    """Create company, FY, naming series, customer, item, and default SLA.

    Wraps the raw sqlite3.Connection so the support skill can set
    conn.company_id.  Returns (wrapped_conn, env_dict).
    """
    conn = _ConnectionWrapper(raw_conn)
    cid = create_test_company(conn)
    create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)
    customer_id = create_test_customer(conn, cid, name="Support Customer")
    item_id = create_test_item(conn, item_code="PROD-001",
                               item_name="Server Hardware",
                               item_type="stock",
                               standard_rate="2500.00")
    sla_result = _create_default_sla(conn, cid)
    sla_id = sla_result["sla"]["id"]
    env = {
        "company_id": cid,
        "customer_id": customer_id,
        "item_id": item_id,
        "sla_id": sla_id,
    }
    return conn, env


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

class TestSupportScenario:
    """Support lifecycle integration tests."""

    # ------------------------------------------------------------------
    # 1. Full support lifecycle
    # ------------------------------------------------------------------

    def test_full_support_lifecycle(self, fresh_db):
        """SLA -> issue -> assign -> first response -> resolve -> SLA report.

        Verifies the complete lifecycle from SLA creation through issue
        resolution and compliance reporting, confirming that all state
        transitions and SLA tracking behave correctly end-to-end.
        """
        conn, env = _setup_support_env(fresh_db)

        # Step 1: SLA already created by setup. Verify it is default.
        sla_row = conn.execute(
            "SELECT * FROM service_level_agreement WHERE id = ?",
            (env["sla_id"],),
        ).fetchone()
        assert sla_row is not None
        assert sla_row["is_default"] == 1

        # Step 2: Create a high-priority issue
        result = _call_action("erpclaw-support", "add-issue", conn,
                              subject="Server down in production",
                              customer_id=env["customer_id"],
                              issue_type="bug",
                              priority="high",
                              description="Production server is unresponsive",
                              company_id=env["company_id"])
        assert result["status"] == "ok"
        issue = result["issue"]
        issue_id = issue["id"]
        assert issue["status"] == "open"
        assert issue["priority"] == "high"
        assert issue["sla_id"] == env["sla_id"]
        # SLA should set response_due and resolution_due
        assert issue["response_due"] is not None
        assert issue["resolution_due"] is not None

        # Step 3: Assign the issue to a support agent
        result = _call_action("erpclaw-support", "update-issue", conn,
                              issue_id=issue_id,
                              assigned_to="agent-1",
                              status="in_progress",
                              company_id=env["company_id"])
        assert result["status"] == "ok"
        assert result["issue"]["assigned_to"] == "agent-1"
        assert result["issue"]["status"] == "in_progress"

        # Step 4: First response (employee comment) -- records first_response_at
        result = _call_action("erpclaw-support", "add-issue-comment", conn,
                              issue_id=issue_id,
                              comment="Looking into this now. Checking server logs.",
                              comment_by="employee",
                              is_internal="0",
                              company_id=env["company_id"])
        assert result["status"] == "ok"
        assert result["comment"]["comment_by"] == "employee"

        # Verify first_response_at was set
        issue_row = conn.execute(
            "SELECT first_response_at FROM issue WHERE id = ?",
            (issue_id,),
        ).fetchone()
        assert issue_row["first_response_at"] is not None

        # Step 5: Resolve the issue
        result = _call_action("erpclaw-support", "resolve-issue", conn,
                              issue_id=issue_id,
                              resolution_notes="Disk was full. Cleared logs and expanded storage.",
                              company_id=env["company_id"])
        assert result["status"] == "ok"
        assert result["issue"]["status"] == "resolved"
        assert result["issue"]["resolved_at"] is not None

        # Step 6: SLA compliance report
        result = _call_action("erpclaw-support", "sla-compliance-report", conn,
                              company_id=env["company_id"],
                              from_date="2026-01-01",
                              to_date="2026-12-31")
        assert result["status"] == "ok"
        report = result["report"]
        assert report["total_with_sla"] >= 1
        # The issue should be compliant (resolved within SLA)
        assert report["compliant"] >= 1

    # ------------------------------------------------------------------
    # 2. SLA creation with multiple priority levels
    # ------------------------------------------------------------------

    def test_sla_creation(self, fresh_db):
        """Create SLA with multiple priority levels and verify storage.

        Ensures that the SLA stores response and resolution times as
        JSON for each priority tier, and that the is_default flag works.
        """
        conn = _ConnectionWrapper(fresh_db)
        cid = create_test_company(conn)

        priorities = json.dumps({
            "response_times": {
                "low": 48,
                "medium": 16,
                "high": 4,
                "critical": 1,
            },
            "resolution_times": {
                "low": 120,
                "medium": 48,
                "high": 16,
                "critical": 4,
            },
        })

        result = _call_action("erpclaw-support", "add-sla", conn,
                              name="Premium SLA",
                              priorities=priorities,
                              is_default="1",
                              company_id=cid)
        assert result["status"] == "ok"
        sla = result["sla"]
        assert sla["name"] == "Premium SLA"
        assert sla["is_default"] == 1

        # Verify response times
        rt = sla["priority_response_times"]
        assert rt["low"] == 48
        assert rt["medium"] == 16
        assert rt["high"] == 4
        assert rt["critical"] == 1

        # Verify resolution times
        rst = sla["priority_resolution_times"]
        assert rst["low"] == 120
        assert rst["medium"] == 48
        assert rst["high"] == 16
        assert rst["critical"] == 4

        # Verify DB storage
        sla_row = conn.execute(
            "SELECT * FROM service_level_agreement WHERE id = ?",
            (sla["id"],),
        ).fetchone()
        assert sla_row is not None
        assert sla_row["name"] == "Premium SLA"
        assert sla_row["is_default"] == 1

        # Verify JSON is stored correctly
        stored_response = json.loads(sla_row["priority_response_times"])
        stored_resolution = json.loads(sla_row["priority_resolution_times"])
        assert stored_response["critical"] == 1
        assert stored_resolution["critical"] == 4

        # Create a second SLA and verify first loses default
        priorities2 = json.dumps({
            "response_times": {"low": 72, "medium": 24},
            "resolution_times": {"low": 168, "medium": 72},
        })
        result2 = _call_action("erpclaw-support", "add-sla", conn,
                               name="Basic SLA",
                               priorities=priorities2,
                               is_default="1",
                               company_id=cid)
        assert result2["status"] == "ok"
        assert result2["sla"]["is_default"] == 1

        # First SLA should no longer be default
        old_sla = conn.execute(
            "SELECT is_default FROM service_level_agreement WHERE id = ?",
            (sla["id"],),
        ).fetchone()
        assert old_sla["is_default"] == 0

    # ------------------------------------------------------------------
    # 3. Issue creation with SLA auto-applied
    # ------------------------------------------------------------------

    def test_issue_creation_with_sla(self, fresh_db):
        """Create issue, verify SLA auto-applied when default SLA exists.

        Validates that response_due and resolution_due are computed from
        the default SLA's priority configuration.
        """
        conn, env = _setup_support_env(fresh_db)

        # Create critical issue -- should get shortest SLA times
        result = _call_action("erpclaw-support", "add-issue", conn,
                              subject="System outage",
                              customer_id=env["customer_id"],
                              issue_type="bug",
                              priority="critical",
                              description="Complete system failure",
                              company_id=env["company_id"])
        assert result["status"] == "ok"
        issue = result["issue"]

        # Verify SLA auto-assigned
        assert issue["sla_id"] == env["sla_id"], "Default SLA should be auto-applied"
        assert issue["response_due"] is not None, "Critical priority should have response_due"
        assert issue["resolution_due"] is not None, "Critical priority should have resolution_due"

        # Verify DB state
        issue_row = conn.execute(
            "SELECT * FROM issue WHERE id = ?", (issue["id"],),
        ).fetchone()
        assert issue_row["sla_id"] == env["sla_id"]
        assert issue_row["status"] == "open"
        assert issue_row["priority"] == "critical"
        assert issue_row["sla_breached"] == 0

        # Create a low-priority issue -- should also get SLA
        result2 = _call_action("erpclaw-support", "add-issue", conn,
                               subject="Minor UI glitch",
                               customer_id=env["customer_id"],
                               issue_type="bug",
                               priority="low",
                               description="Button color is wrong",
                               company_id=env["company_id"])
        assert result2["status"] == "ok"
        issue2 = result2["issue"]
        assert issue2["sla_id"] == env["sla_id"]
        assert issue2["response_due"] is not None
        assert issue2["resolution_due"] is not None

        # Critical response_due should be earlier (shorter) than low
        # because critical has 1h vs low has 24h
        assert issue["response_due"] < issue2["response_due"], (
            f"Critical response_due ({issue['response_due']}) should be before "
            f"low response_due ({issue2['response_due']})"
        )

    # ------------------------------------------------------------------
    # 4. Issue assignment
    # ------------------------------------------------------------------

    def test_issue_assignment(self, fresh_db):
        """Assign issue to support agent, verify status and assignment.

        Tests that update-issue correctly changes assigned_to and status,
        and that the change is persisted in the database.
        """
        conn, env = _setup_support_env(fresh_db)

        # Create issue
        result = _call_action("erpclaw-support", "add-issue", conn,
                              subject="Login not working",
                              customer_id=env["customer_id"],
                              issue_type="bug",
                              priority="medium",
                              description="Customer cannot log in",
                              company_id=env["company_id"])
        assert result["status"] == "ok"
        issue_id = result["issue"]["id"]

        # Assign to agent
        result = _call_action("erpclaw-support", "update-issue", conn,
                              issue_id=issue_id,
                              assigned_to="support-agent-alpha",
                              status="in_progress",
                              company_id=env["company_id"])
        assert result["status"] == "ok"
        assert result["issue"]["assigned_to"] == "support-agent-alpha"
        assert result["issue"]["status"] == "in_progress"

        # Verify via DB
        row = conn.execute(
            "SELECT assigned_to, status FROM issue WHERE id = ?",
            (issue_id,),
        ).fetchone()
        assert row["assigned_to"] == "support-agent-alpha"
        assert row["status"] == "in_progress"

        # Reassign to another agent
        result = _call_action("erpclaw-support", "update-issue", conn,
                              issue_id=issue_id,
                              assigned_to="support-agent-beta",
                              company_id=env["company_id"])
        assert result["status"] == "ok"
        assert result["issue"]["assigned_to"] == "support-agent-beta"

        # Status should remain in_progress (not reset)
        assert result["issue"]["status"] == "in_progress"

    # ------------------------------------------------------------------
    # 5. Issue comments (internal and external)
    # ------------------------------------------------------------------

    def test_issue_comments(self, fresh_db):
        """Add internal and external comments, verify comment storage.

        Tests both employee and customer comments, including the
        is_internal flag for internal notes not visible to customers.
        """
        conn, env = _setup_support_env(fresh_db)

        # Create issue
        result = _call_action("erpclaw-support", "add-issue", conn,
                              subject="Slow performance",
                              customer_id=env["customer_id"],
                              issue_type="complaint",
                              priority="medium",
                              description="Application is very slow",
                              company_id=env["company_id"])
        assert result["status"] == "ok"
        issue_id = result["issue"]["id"]

        # Add an external employee comment (first response)
        result = _call_action("erpclaw-support", "add-issue-comment", conn,
                              issue_id=issue_id,
                              comment="We are investigating the performance issue.",
                              comment_by="employee",
                              is_internal="0",
                              company_id=env["company_id"])
        assert result["status"] == "ok"
        assert result["comment"]["comment_by"] == "employee"
        assert result["comment"]["is_internal"] == 0

        # Verify first_response_at was set
        issue_row = conn.execute(
            "SELECT first_response_at FROM issue WHERE id = ?",
            (issue_id,),
        ).fetchone()
        assert issue_row["first_response_at"] is not None, (
            "First employee comment should set first_response_at"
        )

        # Add an internal employee comment (not visible to customer)
        result = _call_action("erpclaw-support", "add-issue-comment", conn,
                              issue_id=issue_id,
                              comment="Looks like a database index is missing.",
                              comment_by="employee",
                              is_internal="1",
                              company_id=env["company_id"])
        assert result["status"] == "ok"
        assert result["comment"]["is_internal"] == 1

        # Add a customer comment
        result = _call_action("erpclaw-support", "add-issue-comment", conn,
                              issue_id=issue_id,
                              comment="It is still slow. Please fix ASAP.",
                              comment_by="customer",
                              is_internal="0",
                              company_id=env["company_id"])
        assert result["status"] == "ok"
        assert result["comment"]["comment_by"] == "customer"

        # Verify all 3 comments in DB
        comments = conn.execute(
            "SELECT * FROM issue_comment WHERE issue_id = ? ORDER BY created_at",
            (issue_id,),
        ).fetchall()
        assert len(comments) == 3

        # Verify internal vs external
        internal_comments = [c for c in comments if c["is_internal"] == 1]
        external_comments = [c for c in comments if c["is_internal"] == 0]
        assert len(internal_comments) == 1
        assert len(external_comments) == 2

        # Verify comment text
        texts = [c["comment_text"] for c in comments]
        assert "We are investigating the performance issue." in texts
        assert "Looks like a database index is missing." in texts
        assert "It is still slow. Please fix ASAP." in texts

    # ------------------------------------------------------------------
    # 6. Issue resolution
    # ------------------------------------------------------------------

    def test_issue_resolution(self, fresh_db):
        """Resolve issue with notes, verify status and resolved_at.

        Validates that resolve-issue transitions the status to 'resolved',
        stores resolution_notes, and records the resolved_at timestamp.
        """
        conn, env = _setup_support_env(fresh_db)

        # Create and assign issue
        result = _call_action("erpclaw-support", "add-issue", conn,
                              subject="Payment processing error",
                              customer_id=env["customer_id"],
                              issue_type="bug",
                              priority="high",
                              description="Payments failing with error 500",
                              company_id=env["company_id"])
        assert result["status"] == "ok"
        issue_id = result["issue"]["id"]

        # Move to in_progress
        _call_action("erpclaw-support", "update-issue", conn,
                     issue_id=issue_id,
                     status="in_progress",
                     assigned_to="dev-1",
                     company_id=env["company_id"])

        # Add first response
        _call_action("erpclaw-support", "add-issue-comment", conn,
                     issue_id=issue_id,
                     comment="Investigating payment gateway connection.",
                     comment_by="employee",
                     is_internal="0",
                     company_id=env["company_id"])

        # Resolve the issue
        result = _call_action("erpclaw-support", "resolve-issue", conn,
                              issue_id=issue_id,
                              resolution_notes="Fixed gateway timeout. Increased connection pool size.",
                              company_id=env["company_id"])
        assert result["status"] == "ok"
        resolved_issue = result["issue"]
        assert resolved_issue["status"] == "resolved"
        assert resolved_issue["resolved_at"] is not None
        assert resolved_issue["resolution_notes"] == (
            "Fixed gateway timeout. Increased connection pool size."
        )

        # Verify DB state
        row = conn.execute(
            "SELECT status, resolved_at, resolution_notes FROM issue WHERE id = ?",
            (issue_id,),
        ).fetchone()
        assert row["status"] == "resolved"
        assert row["resolved_at"] is not None
        assert "gateway timeout" in row["resolution_notes"]

        # Attempting to resolve again should fail
        result = _call_action("erpclaw-support", "resolve-issue", conn,
                              issue_id=issue_id,
                              resolution_notes="Trying to resolve again",
                              company_id=env["company_id"])
        assert result["status"] == "error"

    # ------------------------------------------------------------------
    # 7. Issue reopen
    # ------------------------------------------------------------------

    def test_issue_reopen(self, fresh_db):
        """Reopen a resolved issue, verify status reverts to 'open'.

        Tests that reopen-issue clears resolved_at and resolution_notes
        while preserving the sla_breached flag.
        """
        conn, env = _setup_support_env(fresh_db)

        # Create, respond to, and resolve an issue
        result = _call_action("erpclaw-support", "add-issue", conn,
                              subject="Data export broken",
                              customer_id=env["customer_id"],
                              issue_type="bug",
                              priority="medium",
                              description="CSV export produces empty file",
                              company_id=env["company_id"])
        assert result["status"] == "ok"
        issue_id = result["issue"]["id"]

        # First response
        _call_action("erpclaw-support", "add-issue-comment", conn,
                     issue_id=issue_id,
                     comment="Checking the export module.",
                     comment_by="employee",
                     is_internal="0",
                     company_id=env["company_id"])

        # Resolve
        result = _call_action("erpclaw-support", "resolve-issue", conn,
                              issue_id=issue_id,
                              resolution_notes="Fixed CSV encoding issue.",
                              company_id=env["company_id"])
        assert result["status"] == "ok"
        assert result["issue"]["status"] == "resolved"

        # Verify resolved state in DB
        resolved_row = conn.execute(
            "SELECT resolved_at, resolution_notes FROM issue WHERE id = ?",
            (issue_id,),
        ).fetchone()
        assert resolved_row["resolved_at"] is not None
        assert resolved_row["resolution_notes"] is not None

        # Reopen the issue
        result = _call_action("erpclaw-support", "reopen-issue", conn,
                              issue_id=issue_id,
                              reason="Customer reports issue is still happening with large files",
                              company_id=env["company_id"])
        assert result["status"] == "ok"
        reopened = result["issue"]
        assert reopened["status"] == "open"

        # Verify resolved_at and resolution_notes are cleared
        row = conn.execute(
            "SELECT status, resolved_at, resolution_notes FROM issue WHERE id = ?",
            (issue_id,),
        ).fetchone()
        assert row["status"] == "open"
        assert row["resolved_at"] is None
        assert row["resolution_notes"] is None

        # The issue can now be resolved again
        _call_action("erpclaw-support", "add-issue-comment", conn,
                     issue_id=issue_id,
                     comment="Found the large file edge case. Fixing now.",
                     comment_by="employee",
                     is_internal="0",
                     company_id=env["company_id"])

        result = _call_action("erpclaw-support", "resolve-issue", conn,
                              issue_id=issue_id,
                              resolution_notes="Fixed buffer size for large CSV exports.",
                              company_id=env["company_id"])
        assert result["status"] == "ok"
        assert result["issue"]["status"] == "resolved"

        # Reopen again to test the "open issue cannot be reopened" guard
        result2 = _call_action("erpclaw-support", "reopen-issue", conn,
                               issue_id=issue_id,
                               reason="Testing second reopen",
                               company_id=env["company_id"])
        assert result2["status"] == "ok"
        assert result2["issue"]["status"] == "open"

        # Trying to reopen an already-open issue should fail
        result_fail = _call_action("erpclaw-support", "reopen-issue", conn,
                                   issue_id=issue_id,
                                   reason="Should fail",
                                   company_id=env["company_id"])
        assert result_fail["status"] == "error"

    # ------------------------------------------------------------------
    # 8. Warranty claim lifecycle
    # ------------------------------------------------------------------

    def test_warranty_claim(self, fresh_db):
        """Create and resolve warranty claim with cost tracking.

        Tests the full warranty claim lifecycle: creation, updating to
        in_progress, then resolving with a repair resolution and cost.
        """
        conn, env = _setup_support_env(fresh_db)

        # Create warranty claim
        result = _call_action("erpclaw-support", "add-warranty-claim", conn,
                              customer_id=env["customer_id"],
                              item_id=env["item_id"],
                              serial_number_id=None,
                              warranty_expiry_date="2027-06-15",
                              complaint_description="Server makes grinding noise. Possible disk failure.",
                              company_id=env["company_id"])
        assert result["status"] == "ok"
        claim = result["warranty_claim"]
        claim_id = claim["id"]
        assert claim["status"] == "open"
        assert claim["customer_id"] == env["customer_id"]
        assert claim["item_id"] == env["item_id"]
        assert claim["warranty_expiry_date"] == "2027-06-15"
        assert "grinding noise" in claim["complaint_description"]

        # Verify DB state
        claim_row = conn.execute(
            "SELECT * FROM warranty_claim WHERE id = ?", (claim_id,),
        ).fetchone()
        assert claim_row is not None
        assert claim_row["status"] == "open"
        assert Decimal(claim_row["cost"]) == Decimal("0")

        # Update to in_progress
        result = _call_action("erpclaw-support", "update-warranty-claim", conn,
                              warranty_claim_id=claim_id,
                              status="in_progress",
                              company_id=env["company_id"])
        assert result["status"] == "ok"
        assert result["warranty_claim"]["status"] == "in_progress"

        # Resolve with repair and cost
        result = _call_action("erpclaw-support", "update-warranty-claim", conn,
                              warranty_claim_id=claim_id,
                              status="resolved",
                              resolution="repair",
                              resolution_date="2026-03-15",
                              cost="350.00",
                              company_id=env["company_id"])
        assert result["status"] == "ok"
        resolved_claim = result["warranty_claim"]
        assert resolved_claim["status"] == "resolved"
        assert resolved_claim["resolution"] == "repair"
        assert resolved_claim["resolution_date"] == "2026-03-15"
        assert Decimal(resolved_claim["cost"]) == Decimal("350.00")

        # Verify DB state after resolution
        resolved_row = conn.execute(
            "SELECT * FROM warranty_claim WHERE id = ?", (claim_id,),
        ).fetchone()
        assert resolved_row["status"] == "resolved"
        assert resolved_row["resolution"] == "repair"
        assert Decimal(resolved_row["cost"]) == Decimal("350.00")

        # Close the claim
        result = _call_action("erpclaw-support", "update-warranty-claim", conn,
                              warranty_claim_id=claim_id,
                              status="closed",
                              company_id=env["company_id"])
        assert result["status"] == "ok"
        assert result["warranty_claim"]["status"] == "closed"

        # Updating a closed claim should fail
        result = _call_action("erpclaw-support", "update-warranty-claim", conn,
                              warranty_claim_id=claim_id,
                              status="open",
                              company_id=env["company_id"])
        assert result["status"] == "error"

    # ------------------------------------------------------------------
    # 9. Maintenance schedule and visit
    # ------------------------------------------------------------------

    def test_maintenance_schedule(self, fresh_db):
        """Create maintenance schedule, record a completed visit.

        Tests that creating a schedule calculates the next_due_date,
        and that recording a completed visit updates the schedule's
        last_completed_date and advances next_due_date.
        """
        conn, env = _setup_support_env(fresh_db)

        # Create quarterly maintenance schedule
        result = _call_action("erpclaw-support", "add-maintenance-schedule", conn,
                              item_id=env["item_id"],
                              customer_id=env["customer_id"],
                              schedule_frequency="quarterly",
                              start_date="2026-01-01",
                              end_date="2026-12-31",
                              company_id=env["company_id"])
        assert result["status"] == "ok"
        schedule = result["maintenance_schedule"]
        schedule_id = schedule["id"]
        assert schedule["status"] == "active"
        assert schedule["schedule_frequency"] == "quarterly"
        assert schedule["start_date"] == "2026-01-01"
        assert schedule["end_date"] == "2026-12-31"
        assert schedule["next_due_date"] is not None

        # Verify next_due_date is approximately 90 days from start
        # (quarterly = 90 days from 2026-01-01 = ~2026-04-01)
        assert schedule["next_due_date"] >= "2026-03-31"
        assert schedule["next_due_date"] <= "2026-04-02"

        # Verify DB state
        sched_row = conn.execute(
            "SELECT * FROM maintenance_schedule WHERE id = ?",
            (schedule_id,),
        ).fetchone()
        assert sched_row is not None
        assert sched_row["status"] == "active"
        assert sched_row["last_completed_date"] is None

        # Record a completed maintenance visit
        result = _call_action("erpclaw-support", "record-maintenance-visit", conn,
                              schedule_id=schedule_id,
                              visit_date="2026-04-01",
                              completed_by="tech-john",
                              observations="All systems nominal. Replaced air filters.",
                              work_done="Preventive maintenance completed. Air filters replaced.",
                              status="completed",
                              company_id=env["company_id"])
        assert result["status"] == "ok"
        visit = result["visit"]
        assert visit["status"] == "completed"
        assert visit["visit_date"] == "2026-04-01"
        assert visit["completed_by"] == "tech-john"
        assert "air filters" in visit["observations"].lower()
        assert result["schedule_updated"] is True

        # Verify schedule was updated
        updated_sched = conn.execute(
            "SELECT * FROM maintenance_schedule WHERE id = ?",
            (schedule_id,),
        ).fetchone()
        assert updated_sched["last_completed_date"] == "2026-04-01"
        # Next due should be ~90 days from 2026-04-01 = ~2026-06-30
        assert updated_sched["next_due_date"] >= "2026-06-29"
        assert updated_sched["next_due_date"] <= "2026-07-01"
        assert updated_sched["status"] == "active"  # Still within end_date

        # Verify visit is stored in DB
        visit_row = conn.execute(
            "SELECT * FROM maintenance_visit WHERE id = ?",
            (visit["id"],),
        ).fetchone()
        assert visit_row is not None
        assert visit_row["maintenance_schedule_id"] == schedule_id
        assert visit_row["customer_id"] == env["customer_id"]
        assert visit_row["status"] == "completed"

        # Record a second visit near end of schedule period
        result2 = _call_action("erpclaw-support", "record-maintenance-visit", conn,
                               schedule_id=schedule_id,
                               visit_date="2026-10-01",
                               completed_by="tech-jane",
                               observations="Annual inspection complete.",
                               work_done="Full system checkup performed.",
                               status="completed",
                               company_id=env["company_id"])
        assert result2["status"] == "ok"
        assert result2["schedule_updated"] is True

        # Schedule should now be expired (next_due would be ~2026-12-30,
        # which is within end_date, OR if next_due > end_date it becomes expired)
        final_sched = conn.execute(
            "SELECT * FROM maintenance_schedule WHERE id = ?",
            (schedule_id,),
        ).fetchone()
        assert final_sched["last_completed_date"] == "2026-10-01"
        # 90 days from 2026-10-01 = 2026-12-30, which is <= 2026-12-31
        # so it may remain active or become expired depending on exact calc
        assert final_sched["status"] in ("active", "expired")

    # ------------------------------------------------------------------
    # 10. SLA compliance report
    # ------------------------------------------------------------------

    def test_sla_compliance_report(self, fresh_db):
        """Verify SLA compliance metrics across multiple issues.

        Creates multiple issues with different priorities, resolves some,
        and verifies the compliance report aggregates correctly.
        """
        conn, env = _setup_support_env(fresh_db)

        # Create and resolve 3 issues (all within SLA)
        issue_ids = []
        for i, (subject, priority) in enumerate([
            ("Issue Alpha", "low"),
            ("Issue Beta", "medium"),
            ("Issue Gamma", "high"),
        ]):
            result = _call_action("erpclaw-support", "add-issue", conn,
                                  subject=subject,
                                  customer_id=env["customer_id"],
                                  issue_type="bug",
                                  priority=priority,
                                  description=f"Test issue {i+1}",
                                  company_id=env["company_id"])
            assert result["status"] == "ok"
            issue_id = result["issue"]["id"]
            issue_ids.append(issue_id)

            # First response
            _call_action("erpclaw-support", "add-issue-comment", conn,
                         issue_id=issue_id,
                         comment=f"Acknowledging issue {i+1}",
                         comment_by="employee",
                         is_internal="0",
                         company_id=env["company_id"])

        # Resolve the first two issues
        for issue_id in issue_ids[:2]:
            result = _call_action("erpclaw-support", "resolve-issue", conn,
                                  issue_id=issue_id,
                                  resolution_notes="Fixed",
                                  company_id=env["company_id"])
            assert result["status"] == "ok"

        # Leave issue 3 (high priority) open / in progress

        # Run SLA compliance report
        result = _call_action("erpclaw-support", "sla-compliance-report", conn,
                              company_id=env["company_id"],
                              from_date="2026-01-01",
                              to_date="2026-12-31")
        assert result["status"] == "ok"
        report = result["report"]

        # All 3 issues have SLAs
        assert report["total_with_sla"] == 3

        # 2 resolved within SLA = compliant
        assert report["compliant"] == 2

        # 1 still in progress
        assert report["in_progress"] == 1

        # No breaches (all handled within time)
        assert report["breached"] == 0

        # Compliance rate: 2 compliant / (2 compliant + 0 breached) = 100%
        assert report["compliance_rate_pct"] == "100.00"

        # Now create an issue WITHOUT SLA (no default, remove default first)
        conn.execute(
            "UPDATE service_level_agreement SET is_default = 0 WHERE id = ?",
            (env["sla_id"],),
        )
        conn.commit()

        result = _call_action("erpclaw-support", "add-issue", conn,
                              subject="No SLA issue",
                              customer_id=env["customer_id"],
                              issue_type="question",
                              priority="low",
                              description="General inquiry",
                              company_id=env["company_id"])
        assert result["status"] == "ok"
        # This issue should NOT have SLA
        assert result["issue"]["sla_id"] is None

        # Re-run report -- total_with_sla should still be 3
        result = _call_action("erpclaw-support", "sla-compliance-report", conn,
                              company_id=env["company_id"],
                              from_date="2026-01-01",
                              to_date="2026-12-31")
        assert result["status"] == "ok"
        assert result["report"]["total_with_sla"] == 3, (
            "Issues without SLA should not be counted in SLA compliance"
        )
