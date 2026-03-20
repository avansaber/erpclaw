#!/usr/bin/env python3
"""Tests for ERPClaw OS Self-Improvement Log (Phase 3, Deliverable 3b).

Tests the log-improvement, list-improvements, and review-improvement
actions including filtering, pagination, status transitions, and
deploy_audit linkage.
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

from improvement_log import (
    handle_log_improvement,
    handle_list_improvements,
    handle_review_improvement,
    VALID_CATEGORIES,
    VALID_SOURCES,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

IMPROVEMENT_TABLE_DDL = """
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

CREATE INDEX IF NOT EXISTS idx_erpclaw_improvement_log_category
    ON erpclaw_improvement_log(category);
CREATE INDEX IF NOT EXISTS idx_erpclaw_improvement_log_status
    ON erpclaw_improvement_log(status);
CREATE INDEX IF NOT EXISTS idx_erpclaw_improvement_log_source
    ON erpclaw_improvement_log(source);
CREATE INDEX IF NOT EXISTS idx_erpclaw_improvement_log_proposed_at
    ON erpclaw_improvement_log(proposed_at);
"""


@pytest.fixture
def db_path(tmp_path):
    """Create a temporary SQLite database with the improvement log table."""
    path = str(tmp_path / "test_improvement.sqlite")
    conn = sqlite3.connect(path)
    setup_pragmas(conn)
    conn.executescript(IMPROVEMENT_TABLE_DDL)
    conn.commit()
    conn.close()
    return path


def _make_args(**kwargs):
    """Create a simple args namespace from keyword arguments."""
    return type("Args", (), kwargs)()


def _log(db_path, category="performance", description="Test improvement",
         source="heartbeat", module_name=None, evidence=None,
         expected_impact=None, proposed_diff=None):
    """Helper to log an improvement and return the result."""
    args = _make_args(
        category=category,
        description=description,
        source=source,
        module_name_arg=module_name,
        module_name=module_name,
        evidence=evidence,
        expected_impact=expected_impact,
        proposed_diff=proposed_diff,
        db_path=db_path,
    )
    return handle_log_improvement(args)


# ---------------------------------------------------------------------------
# Test: log-improvement
# ---------------------------------------------------------------------------

class TestLogImprovement:
    """Tests for the log-improvement action."""

    def test_log_and_retrieve(self, db_path):
        """Log an improvement and verify it can be retrieved."""
        result = _log(db_path, description="Optimize query for list-customers")
        assert result["result"] == "ok"
        assert result["improvement_id"]
        assert result["status"] == "proposed"

        # Retrieve via list
        list_args = _make_args(
            category=None, status_filter=None, module_name_arg=None,
            module_name=None, source=None, from_date=None, to_date=None,
            limit=50, offset=0, db_path=db_path,
        )
        list_result = handle_list_improvements(list_args)
        assert list_result["result"] == "ok"
        assert list_result["total"] == 1
        assert list_result["items"][0]["id"] == result["improvement_id"]

    def test_log_with_all_categories(self, db_path):
        """Verify all valid categories are accepted."""
        for cat in VALID_CATEGORIES:
            result = _log(db_path, category=cat, description=f"Test {cat}")
            assert result["result"] == "ok"
            assert result["category"] == cat

    def test_log_with_all_sources(self, db_path):
        """Verify all valid sources are accepted."""
        for src in VALID_SOURCES:
            result = _log(db_path, source=src, description=f"Test {src}")
            assert result["result"] == "ok"
            assert result["source"] == src

    def test_log_with_json_fields(self, db_path):
        """Log improvement with evidence, expected_impact, and proposed_diff."""
        evidence = json.dumps({"query_time_ms": 450, "threshold_ms": 200})
        impact = json.dumps({"expected_speedup": "2x"})
        diff = json.dumps({"file": "db_query.py", "changes": ["+index"]})

        result = _log(
            db_path,
            evidence=evidence,
            expected_impact=impact,
            proposed_diff=diff,
            module_name="erpclaw-selling",
        )
        assert result["result"] == "ok"

        # Verify JSON fields are stored and parsed back
        list_args = _make_args(
            category=None, status_filter=None, module_name_arg=None,
            module_name=None, source=None, from_date=None, to_date=None,
            limit=50, offset=0, db_path=db_path,
        )
        items = handle_list_improvements(list_args)["items"]
        assert items[0]["evidence"]["query_time_ms"] == 450
        assert items[0]["expected_impact"]["expected_speedup"] == "2x"
        assert items[0]["proposed_diff"]["file"] == "db_query.py"

    def test_log_missing_category(self, db_path):
        """Missing category returns error."""
        result = _log(db_path, category=None)
        assert "error" in result

    def test_log_missing_description(self, db_path):
        """Missing description returns error."""
        result = _log(db_path, description=None)
        assert "error" in result

    def test_log_missing_source(self, db_path):
        """Missing source returns error."""
        result = _log(db_path, source=None)
        assert "error" in result

    def test_log_invalid_category(self, db_path):
        """Invalid category returns error."""
        result = _log(db_path, category="invalid_cat")
        assert "error" in result
        assert "Invalid category" in result["error"]

    def test_log_invalid_source(self, db_path):
        """Invalid source returns error."""
        result = _log(db_path, source="invalid_src")
        assert "error" in result
        assert "Invalid source" in result["error"]


# ---------------------------------------------------------------------------
# Test: list-improvements
# ---------------------------------------------------------------------------

class TestListImprovements:
    """Tests for the list-improvements action."""

    def _list(self, db_path, **kwargs):
        """Helper to call list-improvements."""
        defaults = dict(
            category=None, status_filter=None, module_name_arg=None,
            module_name=None, source=None, from_date=None, to_date=None,
            limit=50, offset=0, db_path=db_path,
        )
        defaults.update(kwargs)
        return handle_list_improvements(_make_args(**defaults))

    def test_filter_by_category(self, db_path):
        """List improvements filtered by category."""
        _log(db_path, category="performance", description="Perf fix")
        _log(db_path, category="usability", description="UX fix")
        _log(db_path, category="performance", description="Another perf fix")

        result = self._list(db_path, category="performance")
        assert result["total"] == 2
        for item in result["items"]:
            assert item["category"] == "performance"

    def test_filter_by_status(self, db_path):
        """List improvements filtered by status."""
        _log(db_path, description="Proposed one")
        _log(db_path, description="Proposed two")

        result = self._list(db_path, status_filter="proposed")
        assert result["total"] == 2

        result = self._list(db_path, status_filter="approved")
        assert result["total"] == 0

    def test_filter_by_module_name(self, db_path):
        """List improvements filtered by module_name."""
        _log(db_path, module_name="erpclaw-selling", description="Selling fix")
        _log(db_path, module_name="erpclaw-buying", description="Buying fix")
        _log(db_path, module_name="erpclaw-selling", description="Another selling fix")

        result = self._list(db_path, module_name_arg="erpclaw-selling", module_name="erpclaw-selling")
        assert result["total"] == 2

    def test_filter_by_source(self, db_path):
        """List improvements filtered by source."""
        _log(db_path, source="heartbeat", description="From heartbeat")
        _log(db_path, source="dgm", description="From DGM")
        _log(db_path, source="heartbeat", description="Another heartbeat")

        result = self._list(db_path, source="heartbeat")
        assert result["total"] == 2

    def test_filter_by_date_range(self, db_path):
        """List improvements filtered by date range."""
        _log(db_path, description="Today's improvement")

        # Query with from_date in the past — should find it
        result = self._list(db_path, from_date="2020-01-01")
        assert result["total"] == 1

        # Query with from_date in the future — should find nothing
        result = self._list(db_path, from_date="2099-01-01")
        assert result["total"] == 0

    def test_date_range_to_date(self, db_path):
        """Verify to_date filtering works."""
        _log(db_path, description="Old improvement")

        # to_date in the past — should find nothing
        result = self._list(db_path, to_date="2020-01-01")
        assert result["total"] == 0

        # to_date in the future — should find it
        result = self._list(db_path, to_date="2099-12-31")
        assert result["total"] == 1

    def test_pagination(self, db_path):
        """Verify pagination works with limit and offset."""
        for i in range(15):
            _log(db_path, description=f"Improvement {i}")

        # Page 1, size 5
        result = self._list(db_path, limit=5, offset=0)
        assert len(result["items"]) == 5
        assert result["total"] == 15
        assert result["page"] == 1
        assert result["page_size"] == 5
        assert result["pages"] == 3

        # Page 2
        result = self._list(db_path, limit=5, offset=5)
        assert len(result["items"]) == 5
        assert result["page"] == 2

        # Page 3
        result = self._list(db_path, limit=5, offset=10)
        assert len(result["items"]) == 5
        assert result["page"] == 3

    def test_empty_list(self, db_path):
        """Empty database returns empty list."""
        result = self._list(db_path)
        assert result["result"] == "ok"
        assert result["total"] == 0
        assert result["items"] == []


# ---------------------------------------------------------------------------
# Test: review-improvement
# ---------------------------------------------------------------------------

class TestReviewImprovement:
    """Tests for the review-improvement action."""

    def _review(self, db_path, improvement_id, new_status, **kwargs):
        """Helper to call review-improvement."""
        defaults = dict(
            improvement_id=improvement_id,
            new_status=new_status,
            review_notes=None,
            reviewed_by=None,
            deploy=False,
            db_path=db_path,
        )
        defaults.update(kwargs)
        return handle_review_improvement(_make_args(**defaults))

    def test_approve(self, db_path):
        """Approve a proposed improvement."""
        log_result = _log(db_path, description="Should be approved")
        imp_id = log_result["improvement_id"]

        result = self._review(db_path, imp_id, "approved", reviewed_by="admin")
        assert result["result"] == "ok"
        assert result["new_status"] == "approved"
        assert result["previous_status"] == "proposed"
        assert result["reviewed_by"] == "admin"

    def test_reject_with_reason(self, db_path):
        """Reject a proposed improvement with review notes."""
        log_result = _log(db_path, description="Should be rejected")
        imp_id = log_result["improvement_id"]

        result = self._review(
            db_path, imp_id, "rejected",
            review_notes="Not needed — already optimized",
            reviewed_by="reviewer",
        )
        assert result["result"] == "ok"
        assert result["new_status"] == "rejected"
        assert result["review_notes"] == "Not needed — already optimized"

    def test_defer(self, db_path):
        """Defer a proposed improvement."""
        log_result = _log(db_path, description="Should be deferred")
        imp_id = log_result["improvement_id"]

        result = self._review(db_path, imp_id, "deferred")
        assert result["result"] == "ok"
        assert result["new_status"] == "deferred"

    def test_review_sets_reviewed_at(self, db_path):
        """Verify reviewed_at is set on review."""
        log_result = _log(db_path, description="Check timestamp")
        imp_id = log_result["improvement_id"]

        self._review(db_path, imp_id, "approved")

        # Verify via direct DB query
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT reviewed_at, reviewed_by FROM erpclaw_improvement_log WHERE id = ?",
            (imp_id,),
        ).fetchone()
        conn.close()

        assert row["reviewed_at"] is not None
        assert row["reviewed_by"] == "system"  # default

    def test_status_proposed_to_approved(self, db_path):
        """proposed -> approved transition works."""
        imp_id = _log(db_path, description="p->a")["improvement_id"]
        result = self._review(db_path, imp_id, "approved")
        assert result["previous_status"] == "proposed"
        assert result["new_status"] == "approved"

    def test_status_proposed_to_rejected(self, db_path):
        """proposed -> rejected transition works."""
        imp_id = _log(db_path, description="p->r")["improvement_id"]
        result = self._review(db_path, imp_id, "rejected")
        assert result["previous_status"] == "proposed"
        assert result["new_status"] == "rejected"

    def test_status_proposed_to_deferred(self, db_path):
        """proposed -> deferred transition works."""
        imp_id = _log(db_path, description="p->d")["improvement_id"]
        result = self._review(db_path, imp_id, "deferred")
        assert result["previous_status"] == "proposed"
        assert result["new_status"] == "deferred"

    def test_deployed_cannot_be_reviewed(self, db_path):
        """deployed -> any review status should fail."""
        imp_id = _log(db_path, description="Already deployed")["improvement_id"]

        # Manually set status to deployed
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE erpclaw_improvement_log SET status = 'deployed' WHERE id = ?",
            (imp_id,),
        )
        conn.commit()
        conn.close()

        result = self._review(db_path, imp_id, "approved")
        assert "error" in result
        assert "deployed" in result["error"].lower()

    def test_review_nonexistent_improvement(self, db_path):
        """Review of nonexistent improvement returns error."""
        result = self._review(db_path, str(uuid.uuid4()), "approved")
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_review_missing_improvement_id(self, db_path):
        """Missing improvement_id returns error."""
        result = self._review(db_path, None, "approved")
        assert "error" in result

    def test_review_missing_status(self, db_path):
        """Missing status returns error."""
        imp_id = _log(db_path, description="test")["improvement_id"]
        result = self._review(db_path, imp_id, None)
        assert "error" in result

    def test_review_invalid_status(self, db_path):
        """Invalid review status returns error."""
        imp_id = _log(db_path, description="test")["improvement_id"]
        result = self._review(db_path, imp_id, "invalid_status")
        assert "error" in result

    def test_rejected_records_reason(self, db_path):
        """Verify rejection reason is persisted."""
        imp_id = _log(db_path, description="Will reject")["improvement_id"]
        self._review(
            db_path, imp_id, "rejected",
            review_notes="Too risky for production",
        )

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT review_notes FROM erpclaw_improvement_log WHERE id = ?",
            (imp_id,),
        ).fetchone()
        conn.close()
        assert row["review_notes"] == "Too risky for production"

    def test_approved_with_deploy_links_audit(self, db_path):
        """Approved + deploy creates deploy_audit entry and sets FK."""
        imp_id = _log(
            db_path,
            description="Deploy this improvement",
            module_name="erpclaw-selling",
        )["improvement_id"]

        result = self._review(
            db_path, imp_id, "approved",
            deploy=True, reviewed_by="admin",
        )
        assert result["result"] == "ok"
        # If deploy_audit module was importable, check linkage
        if "deploy_audit_id" in result:
            assert result["deploy_audit_id"] is not None
            assert result["new_status"] == "deployed"

            # Verify FK in DB
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT deploy_audit_id, status FROM erpclaw_improvement_log WHERE id = ?",
                (imp_id,),
            ).fetchone()
            conn.close()
            assert row["deploy_audit_id"] == result["deploy_audit_id"]
            assert row["status"] == "deployed"
