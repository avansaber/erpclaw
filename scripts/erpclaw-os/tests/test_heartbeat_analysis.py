#!/usr/bin/env python3
"""Tests for ERPClaw OS Heartbeat Analysis Engine (Phase 3, Deliverable 3d).

Tests usage pattern analysis, gap detection, workflow optimization proposals,
module suggestions, and the heartbeat-report / heartbeat-suggest actions.
"""
import json
import os
import sqlite3
import sys
import uuid

import pytest

# Add erpclaw-os directory to path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OS_DIR = os.path.dirname(SCRIPT_DIR)
if OS_DIR not in sys.path:
    sys.path.insert(0, OS_DIR)

# Add shared lib to path
sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
from erpclaw_lib.db import setup_pragmas

from heartbeat_analysis import (
    _analyze_usage_patterns,
    _detect_gaps,
    _detect_workflow_patterns,
    _log_proposal,
    _suggest_modules,
    handle_heartbeat_analyze,
    handle_heartbeat_report,
    handle_heartbeat_suggest,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TABLE_DDL = """
CREATE TABLE IF NOT EXISTS action_call_log (
    id              TEXT PRIMARY KEY,
    timestamp       TEXT DEFAULT (datetime('now')),
    action_name     TEXT NOT NULL,
    routed_to       TEXT NOT NULL DEFAULT '',
    route_tier      INTEGER NOT NULL DEFAULT 0,
    session_id      TEXT,
    status          TEXT DEFAULT 'success',
    error_message   TEXT
);

CREATE INDEX IF NOT EXISTS idx_acl_action ON action_call_log(action_name);
CREATE INDEX IF NOT EXISTS idx_acl_session ON action_call_log(session_id);

CREATE TABLE IF NOT EXISTS erpclaw_deploy_audit (
    id              TEXT PRIMARY KEY,
    module_name     TEXT NOT NULL,
    pipeline_result TEXT NOT NULL CHECK(pipeline_result IN ('deployed', 'queued', 'rejected', 'failed')),
    tier            INTEGER,
    steps           TEXT NOT NULL DEFAULT '[]',
    git_commit      TEXT,
    human_approved  INTEGER CHECK(human_approved IN (0, 1)),
    approved_by     TEXT,
    deployed_at     TEXT DEFAULT (datetime('now')),
    reasoning       TEXT
);

CREATE TABLE IF NOT EXISTS erpclaw_improvement_log (
    id              TEXT PRIMARY KEY,
    module_name     TEXT,
    category        TEXT NOT NULL CHECK(category IN ('performance', 'usability', 'coverage', 'semantic', 'structural')),
    description     TEXT NOT NULL,
    evidence        TEXT,
    proposed_diff   TEXT,
    expected_impact TEXT,
    source          TEXT NOT NULL CHECK(source IN ('heartbeat', 'dgm', 'semantic', 'manual', 'gap_detector')),
    status          TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN ('proposed', 'approved', 'rejected', 'deferred', 'deployed')),
    proposed_at     TEXT DEFAULT (datetime('now')),
    reviewed_at     TEXT,
    reviewed_by     TEXT,
    review_notes    TEXT,
    deploy_audit_id TEXT REFERENCES erpclaw_deploy_audit(id)
);

CREATE INDEX IF NOT EXISTS idx_improvement_category ON erpclaw_improvement_log(category);
CREATE INDEX IF NOT EXISTS idx_improvement_status ON erpclaw_improvement_log(status);
CREATE INDEX IF NOT EXISTS idx_improvement_source ON erpclaw_improvement_log(source);

CREATE TABLE IF NOT EXISTS erpclaw_module_action (
    id              TEXT PRIMARY KEY,
    module_name     TEXT NOT NULL,
    action_name     TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS erpclaw_module (
    name            TEXT PRIMARY KEY,
    install_status  TEXT NOT NULL DEFAULT 'installed',
    is_active       INTEGER NOT NULL DEFAULT 1
);
"""


@pytest.fixture
def db_path(tmp_path):
    """Create a temporary SQLite database with required tables."""
    path = str(tmp_path / "test_heartbeat.sqlite")
    conn = sqlite3.connect(path)
    setup_pragmas(conn)
    conn.executescript(TABLE_DDL)
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def registry_path(tmp_path):
    """Create a temporary module_registry.json."""
    registry = {
        "version": "3.0.0",
        "modules": {
            "erpclaw": {
                "display_name": "ERPClaw Core",
                "category": "core",
                "tags": ["erp", "accounting", "selling", "buying", "inventory"],
            },
            "erpclaw-growth": {
                "display_name": "ERPClaw Growth",
                "category": "expansion",
                "tags": ["crm", "leads", "campaigns", "analytics"],
            },
            "erpclaw-ops": {
                "display_name": "ERPClaw Operations",
                "category": "expansion",
                "tags": ["manufacturing", "projects", "assets", "quality"],
            },
            "healthclaw": {
                "display_name": "HealthClaw",
                "category": "vertical",
                "tags": ["health", "patients", "appointments", "medical"],
            },
            "retailclaw": {
                "display_name": "RetailClaw",
                "category": "vertical",
                "tags": ["retail", "pos", "inventory", "selling"],
            },
        },
    }
    path = str(tmp_path / "module_registry.json")
    with open(path, "w") as f:
        json.dump(registry, f)
    return path


def _make_args(**kwargs):
    """Create a simple args namespace from keyword arguments."""
    return type("Args", (), kwargs)()


def _insert_call(conn, action_name, routed_to="erpclaw-selling", route_tier=2,
                 session_id=None, status="success", error_message=None,
                 timestamp=None):
    """Insert a row into action_call_log."""
    ts = timestamp or "2026-03-15 10:00:00"
    conn.execute(
        "INSERT INTO action_call_log "
        "(id, timestamp, action_name, routed_to, route_tier, session_id, status, error_message) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), ts, action_name, routed_to, route_tier,
         session_id, status, error_message),
    )


def _insert_module_action(conn, module_name, action_name):
    """Insert a row into erpclaw_module_action."""
    conn.execute(
        "INSERT INTO erpclaw_module_action (id, module_name, action_name) VALUES (?, ?, ?)",
        (str(uuid.uuid4()), module_name, action_name),
    )


def _insert_module(conn, name, install_status="installed"):
    """Insert a row into erpclaw_module."""
    conn.execute(
        "INSERT OR IGNORE INTO erpclaw_module (name, install_status, is_active) VALUES (?, ?, 1)",
        (name, install_status),
    )


# ---------------------------------------------------------------------------
# 1. Usage Pattern Analysis Tests
# ---------------------------------------------------------------------------

class TestUsagePatternAnalysis:
    """Tests for usage pattern analysis (most-used, never-used, highest-error)."""

    def test_most_used_actions_identified(self, db_path):
        """Seed action_call_log with known patterns, verify most-used actions."""
        conn = sqlite3.connect(db_path)
        for _ in range(20):
            _insert_call(conn, "add-customer", routed_to="erpclaw-selling")
        for _ in range(15):
            _insert_call(conn, "list-customers", routed_to="erpclaw-selling")
        for _ in range(5):
            _insert_call(conn, "add-item", routed_to="erpclaw-inventory")
        conn.commit()

        result = _analyze_usage_patterns(conn)
        conn.close()

        most_used = result["most_used"]
        assert len(most_used) >= 3
        assert most_used[0]["action"] == "add-customer"
        assert most_used[0]["call_count"] == 20
        assert most_used[1]["action"] == "list-customers"
        assert most_used[1]["call_count"] == 15

    def test_most_used_limited_to_top_10(self, db_path):
        """Verify most_used is capped at 10 entries."""
        conn = sqlite3.connect(db_path)
        for i in range(15):
            for _ in range(15 - i):
                _insert_call(conn, f"action-{i:02d}")
        conn.commit()

        result = _analyze_usage_patterns(conn)
        conn.close()

        assert len(result["most_used"]) == 10

    def test_never_used_actions_identified(self, db_path):
        """Seed with registered but uncalled actions, verify they're identified."""
        conn = sqlite3.connect(db_path)
        _insert_module_action(conn, "erpclaw", "add-customer")
        _insert_module_action(conn, "erpclaw", "list-customers")
        _insert_module_action(conn, "erpclaw", "delete-customer")
        _insert_module_action(conn, "erpclaw", "get-customer")

        # Only call two of the four
        _insert_call(conn, "add-customer")
        _insert_call(conn, "list-customers")
        conn.commit()

        result = _analyze_usage_patterns(conn)
        conn.close()

        never_used_names = [x["action"] for x in result["never_used"]]
        assert "delete-customer" in never_used_names
        assert "get-customer" in never_used_names
        assert "add-customer" not in never_used_names

    def test_highest_error_actions_identified(self, db_path):
        """Seed with high-error actions (error_rate > 10%), verify identified."""
        conn = sqlite3.connect(db_path)
        # Action with 50% error rate
        for _ in range(5):
            _insert_call(conn, "submit-invoice", status="success")
        for _ in range(5):
            _insert_call(conn, "submit-invoice", status="error")

        # Action with 5% error rate (below threshold)
        for _ in range(19):
            _insert_call(conn, "add-customer", status="success")
        _insert_call(conn, "add-customer", status="error")

        conn.commit()

        result = _analyze_usage_patterns(conn)
        conn.close()

        high_err = result["highest_error"]
        high_err_names = [x["action"] for x in high_err]
        assert "submit-invoice" in high_err_names
        assert "add-customer" not in high_err_names

        invoice_entry = next(x for x in high_err if x["action"] == "submit-invoice")
        assert invoice_entry["error_rate"] == 0.5
        assert invoice_entry["error_count"] == 5
        assert invoice_entry["total_count"] == 10

    def test_empty_action_call_log_no_errors(self, db_path):
        """Empty action_call_log produces no proposals (not errors)."""
        conn = sqlite3.connect(db_path)
        result = _analyze_usage_patterns(conn)
        conn.close()

        assert result["most_used"] == []
        assert result["never_used"] == []
        assert result["highest_error"] == []
        assert result["stats"]["total_call_count"] == 0

    def test_stats_total_counts(self, db_path):
        """Verify stats contain correct totals."""
        conn = sqlite3.connect(db_path)
        for _ in range(10):
            _insert_call(conn, "add-customer")
        for _ in range(5):
            _insert_call(conn, "list-items")
        conn.commit()

        result = _analyze_usage_patterns(conn)
        conn.close()

        assert result["stats"]["total_actions_called"] == 2
        assert result["stats"]["total_call_count"] == 15


# ---------------------------------------------------------------------------
# 2. Gap Detection Tests
# ---------------------------------------------------------------------------

class TestGapDetection:
    """Tests for gap detection (unknown action errors)."""

    def test_unknown_action_errors_detected(self, db_path):
        """Seed with 'unknown action' errors, verify gap detected."""
        conn = sqlite3.connect(db_path)
        for _ in range(3):
            _insert_call(
                conn, "auto-add-repair",
                status="error",
                error_message="Unknown action: auto-add-repair",
            )
        conn.commit()

        gaps = _detect_gaps(conn)
        conn.close()

        assert len(gaps) >= 1
        gap_actions = [g["action"] for g in gaps]
        assert "auto-add-repair" in gap_actions
        repair_gap = next(g for g in gaps if g["action"] == "auto-add-repair")
        assert repair_gap["occurrence_count"] == 3

    def test_unregistered_action_errors_detected(self, db_path):
        """Actions with errors that aren't in module_action table are detected."""
        conn = sqlite3.connect(db_path)
        _insert_module_action(conn, "erpclaw", "add-customer")

        for _ in range(2):
            _insert_call(conn, "foo-bar-action", status="error")
        conn.commit()

        gaps = _detect_gaps(conn)
        conn.close()

        gap_actions = [g["action"] for g in gaps]
        assert "foo-bar-action" in gap_actions

    def test_no_gaps_when_no_errors(self, db_path):
        """No gaps detected when all actions succeed."""
        conn = sqlite3.connect(db_path)
        for _ in range(5):
            _insert_call(conn, "add-customer", status="success")
        conn.commit()

        gaps = _detect_gaps(conn)
        conn.close()

        assert len(gaps) == 0


# ---------------------------------------------------------------------------
# 3. Workflow Optimization Tests
# ---------------------------------------------------------------------------

class TestWorkflowPatterns:
    """Tests for workflow optimization detection."""

    def test_sequential_pattern_detected(self, db_path):
        """Seed A→B within 60s repeated 5+ times, verify workflow detected."""
        conn = sqlite3.connect(db_path)
        session = str(uuid.uuid4())

        for i in range(6):
            base_time = f"2026-03-15 10:{i:02d}:00"
            follow_time = f"2026-03-15 10:{i:02d}:30"
            _insert_call(
                conn, "add-quotation",
                session_id=session, timestamp=base_time,
                routed_to="erpclaw-selling",
            )
            _insert_call(
                conn, "convert-quotation-to-so",
                session_id=session, timestamp=follow_time,
                routed_to="erpclaw-selling",
            )
        conn.commit()

        workflows = _detect_workflow_patterns(conn)
        conn.close()

        assert len(workflows) >= 1
        wf = workflows[0]
        assert wf["action_a"] == "add-quotation"
        assert wf["action_b"] == "convert-quotation-to-so"
        assert wf["occurrence_count"] >= 5

    def test_no_pattern_below_threshold(self, db_path):
        """Patterns with < 5 occurrences are not detected."""
        conn = sqlite3.connect(db_path)
        session = str(uuid.uuid4())

        for i in range(3):
            base_time = f"2026-03-15 10:{i:02d}:00"
            follow_time = f"2026-03-15 10:{i:02d}:30"
            _insert_call(conn, "add-quotation", session_id=session, timestamp=base_time)
            _insert_call(conn, "convert-quotation-to-so", session_id=session, timestamp=follow_time)
        conn.commit()

        workflows = _detect_workflow_patterns(conn)
        conn.close()

        # Should not detect with only 3 occurrences
        matching = [w for w in workflows
                    if w["action_a"] == "add-quotation"
                    and w["action_b"] == "convert-quotation-to-so"]
        assert len(matching) == 0

    def test_workflow_has_proposed_combined_name(self, db_path):
        """Verify proposals include a proposed combined action name."""
        conn = sqlite3.connect(db_path)
        session = str(uuid.uuid4())

        for i in range(6):
            base_time = f"2026-03-15 10:{i:02d}:00"
            follow_time = f"2026-03-15 10:{i:02d}:30"
            _insert_call(conn, "add-quotation", session_id=session, timestamp=base_time)
            _insert_call(conn, "submit-quotation", session_id=session, timestamp=follow_time)
        conn.commit()

        workflows = _detect_workflow_patterns(conn)
        conn.close()

        assert any(
            w["proposed_combined"] == "add-quotation-and-submit-quotation"
            for w in workflows
        )


# ---------------------------------------------------------------------------
# 4. Module Suggestions Tests
# ---------------------------------------------------------------------------

class TestModuleSuggestions:
    """Tests for module suggestion based on usage patterns."""

    def test_suggests_module_based_on_usage(self, db_path, registry_path):
        """Heavy selling usage suggests retailclaw (which has selling tag)."""
        conn = sqlite3.connect(db_path)
        _insert_module(conn, "erpclaw")

        for _ in range(50):
            _insert_call(conn, "add-customer", routed_to="erpclaw-selling")
        for _ in range(30):
            _insert_call(conn, "create-sales-invoice", routed_to="erpclaw-selling")
        conn.commit()

        suggestions = _suggest_modules(conn, registry_path)
        conn.close()

        suggested_names = [s["module"] for s in suggestions]
        assert "retailclaw" in suggested_names

    def test_excludes_already_installed_modules(self, db_path, registry_path):
        """Already-installed modules should not be suggested."""
        conn = sqlite3.connect(db_path)
        _insert_module(conn, "erpclaw")
        _insert_module(conn, "retailclaw")

        for _ in range(50):
            _insert_call(conn, "add-customer", routed_to="erpclaw-selling")
        conn.commit()

        suggestions = _suggest_modules(conn, registry_path)
        conn.close()

        suggested_names = [s["module"] for s in suggestions]
        assert "retailclaw" not in suggested_names
        assert "erpclaw" not in suggested_names

    def test_no_suggestions_with_empty_log(self, db_path, registry_path):
        """No suggestions when action_call_log is empty."""
        conn = sqlite3.connect(db_path)
        suggestions = _suggest_modules(conn, registry_path)
        conn.close()

        assert suggestions == []

    def test_suggestions_have_display_name(self, db_path, registry_path):
        """Module suggestions include display_name from registry."""
        conn = sqlite3.connect(db_path)
        _insert_module(conn, "erpclaw")

        for _ in range(20):
            _insert_call(conn, "add-lead", routed_to="erpclaw-crm")
        conn.commit()

        suggestions = _suggest_modules(conn, registry_path)
        conn.close()

        for s in suggestions:
            assert "display_name" in s
            assert s["display_name"] != ""


# ---------------------------------------------------------------------------
# 5. Improvement Log Integration Tests
# ---------------------------------------------------------------------------

class TestProposalLogging:
    """Tests for logging proposals to improvement_log."""

    def test_proposals_logged_to_improvement_log(self, db_path):
        """Verify proposals are logged with source='heartbeat'."""
        result = _log_proposal(
            db_path,
            category="performance",
            description="Test proposal from heartbeat",
            evidence={"metric": "response_time", "value_ms": 500},
        )
        assert result["result"] == "ok"
        assert result["source"] == "heartbeat"
        assert result["status"] == "proposed"

        # Verify in DB
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM erpclaw_improvement_log WHERE id = ?",
            (result["improvement_id"],),
        ).fetchone()
        conn.close()

        assert row["source"] == "heartbeat"
        assert row["category"] == "performance"
        assert "Test proposal" in row["description"]

    def test_proposals_have_correct_categories(self, db_path):
        """Verify proposals use appropriate categories."""
        # Performance category for error rates
        r1 = _log_proposal(db_path, "performance", "High error rate", {"rate": 0.5})
        assert r1["category"] == "performance"

        # Coverage for gaps
        r2 = _log_proposal(db_path, "coverage", "Missing action", {"action": "foo"})
        assert r2["category"] == "coverage"

        # Usability for workflows
        r3 = _log_proposal(db_path, "usability", "Combine actions", {"pair": "a+b"})
        assert r3["category"] == "usability"


# ---------------------------------------------------------------------------
# 6. heartbeat-analyze Action Tests
# ---------------------------------------------------------------------------

class TestHeartbeatAnalyze:
    """Tests for the heartbeat-analyze action."""

    def test_analyze_returns_summary_counts(self, db_path, registry_path):
        """Verify heartbeat-analyze returns summary counts."""
        conn = sqlite3.connect(db_path)
        for _ in range(10):
            _insert_call(conn, "add-customer")
        conn.commit()
        conn.close()

        args = _make_args(db_path=db_path, registry_path=registry_path)
        result = handle_heartbeat_analyze(args)

        assert result["result"] == "ok"
        assert "analysis_summary" in result
        assert "proposals_logged" in result
        assert "duration_ms" in result
        summary = result["analysis_summary"]
        assert "most_used_actions" in summary
        assert "high_error_actions" in summary
        assert "gaps_detected" in summary
        assert "workflow_patterns" in summary
        assert "module_suggestions" in summary

    def test_analyze_with_empty_log(self, db_path, registry_path):
        """Empty action_call_log produces zero proposals, no errors."""
        args = _make_args(db_path=db_path, registry_path=registry_path)
        result = handle_heartbeat_analyze(args)

        assert result["result"] == "ok"
        assert result["proposals_logged"] == 0
        assert result["analysis_summary"]["most_used_actions"] == 0
        assert result["analysis_summary"]["gaps_detected"] == 0

    def test_analyze_logs_high_error_proposals(self, db_path, registry_path):
        """Verify high-error actions are logged as proposals."""
        conn = sqlite3.connect(db_path)
        for _ in range(5):
            _insert_call(conn, "submit-invoice", status="success")
        for _ in range(6):
            _insert_call(conn, "submit-invoice", status="error")
        conn.commit()
        conn.close()

        args = _make_args(db_path=db_path, registry_path=registry_path)
        result = handle_heartbeat_analyze(args)

        assert result["analysis_summary"]["high_error_actions"] >= 1
        assert result["proposals_logged"] >= 1

        # Verify in improvement_log
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM erpclaw_improvement_log WHERE source = 'heartbeat'"
        ).fetchall()
        conn.close()

        assert len(rows) >= 1
        descriptions = [r["description"] for r in rows]
        assert any("submit-invoice" in d for d in descriptions)


# ---------------------------------------------------------------------------
# 7. heartbeat-report Action Tests
# ---------------------------------------------------------------------------

class TestHeartbeatReport:
    """Tests for the heartbeat-report action."""

    def test_report_returns_structured_results(self, db_path):
        """Verify heartbeat-report returns structured data."""
        # Log some proposals first
        _log_proposal(db_path, "performance", "Improvement A", {"a": 1})
        _log_proposal(db_path, "coverage", "Improvement B", {"b": 2})

        args = _make_args(db_path=db_path, limit=50)
        result = handle_heartbeat_report(args)

        assert result["result"] == "ok"
        assert result["total"] == 2
        assert len(result["items"]) == 2
        assert "limit" in result

    def test_report_respects_limit(self, db_path):
        """Verify --limit parameter works."""
        for i in range(10):
            _log_proposal(db_path, "performance", f"Improvement {i}", {"i": i})

        args = _make_args(db_path=db_path, limit=3)
        result = handle_heartbeat_report(args)

        assert len(result["items"]) == 3
        assert result["total"] == 10

    def test_report_only_heartbeat_source(self, db_path):
        """Report only includes heartbeat-sourced items."""
        from improvement_log import handle_log_improvement

        # Log from heartbeat
        _log_proposal(db_path, "performance", "From heartbeat", {"x": 1})

        # Log from another source
        manual_args = _make_args(
            category="usability", description="From manual",
            source="manual", evidence=None, expected_impact=None,
            proposed_diff=None, module_name_arg=None,
            module_name=None, db_path=db_path,
        )
        handle_log_improvement(manual_args)

        args = _make_args(db_path=db_path, limit=50)
        result = handle_heartbeat_report(args)

        assert result["total"] == 1
        assert result["items"][0]["source"] == "heartbeat"

    def test_report_parses_json_evidence(self, db_path):
        """Verify JSON evidence fields are parsed in report items."""
        _log_proposal(db_path, "performance", "Test evidence", {"metric": "latency", "ms": 250})

        args = _make_args(db_path=db_path, limit=50)
        result = handle_heartbeat_report(args)

        item = result["items"][0]
        assert isinstance(item["evidence"], dict)
        assert item["evidence"]["metric"] == "latency"


# ---------------------------------------------------------------------------
# 8. heartbeat-suggest Action Tests
# ---------------------------------------------------------------------------

class TestHeartbeatSuggest:
    """Tests for the heartbeat-suggest action."""

    def test_suggest_returns_only_proposed(self, db_path):
        """heartbeat-suggest returns only status='proposed' items."""
        _log_proposal(db_path, "performance", "Proposed item", {"a": 1})
        _log_proposal(db_path, "coverage", "Another proposed", {"b": 2})

        # Approve one
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        first = conn.execute(
            "SELECT id FROM erpclaw_improvement_log ORDER BY proposed_at LIMIT 1"
        ).fetchone()
        conn.execute(
            "UPDATE erpclaw_improvement_log SET status = 'approved' WHERE id = ?",
            (first["id"],),
        )
        conn.commit()
        conn.close()

        args = _make_args(db_path=db_path)
        result = handle_heartbeat_suggest(args)

        assert result["result"] == "ok"
        assert result["total"] == 1
        for item in result["suggestions"]:
            assert item["status"] == "proposed"

    def test_suggest_grouped_by_category(self, db_path):
        """Verify suggestions are grouped by category."""
        _log_proposal(db_path, "performance", "Perf 1", {"x": 1})
        _log_proposal(db_path, "performance", "Perf 2", {"x": 2})
        _log_proposal(db_path, "coverage", "Coverage 1", {"y": 1})

        args = _make_args(db_path=db_path)
        result = handle_heartbeat_suggest(args)

        assert result["by_category"]["performance"] == 2
        assert result["by_category"]["coverage"] == 1

    def test_suggest_empty_when_no_proposals(self, db_path):
        """Empty improvement_log returns empty suggestions."""
        args = _make_args(db_path=db_path)
        result = handle_heartbeat_suggest(args)

        assert result["result"] == "ok"
        assert result["total"] == 0
        assert result["suggestions"] == []

    def test_suggest_excludes_non_heartbeat(self, db_path):
        """Suggestions from non-heartbeat sources are excluded."""
        from improvement_log import handle_log_improvement

        _log_proposal(db_path, "performance", "From heartbeat", {"x": 1})

        manual_args = _make_args(
            category="usability", description="From manual",
            source="manual", evidence=None, expected_impact=None,
            proposed_diff=None, module_name_arg=None,
            module_name=None, db_path=db_path,
        )
        handle_log_improvement(manual_args)

        args = _make_args(db_path=db_path)
        result = handle_heartbeat_suggest(args)

        assert result["total"] == 1
        assert all(s["source"] == "heartbeat" for s in result["suggestions"])
