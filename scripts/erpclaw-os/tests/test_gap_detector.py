#!/usr/bin/env python3
"""Tests for ERPClaw OS Gap Detection + Module Suggestions (Phase 3, Deliverable 3e).

Tests the detect-gaps and suggest-modules actions including error pattern
detection, workflow gap analysis, industry gap analysis, module ranking,
dependency constraints, and improvement_log integration.
"""
import json
import os
import sqlite3
import sys
import textwrap
import uuid
from datetime import datetime, timedelta

import pytest

# Add erpclaw-os directory to path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OS_DIR = os.path.dirname(SCRIPT_DIR)
if OS_DIR not in sys.path:
    sys.path.insert(0, OS_DIR)

# Add shared lib to path
sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
from erpclaw_lib.db import setup_pragmas

from gap_detector import (
    handle_detect_gaps,
    handle_suggest_modules,
    handle_detect_schema_divergence,
    handle_detect_stubs,
    detect_schema_code_divergence,
    detect_stubs,
    _load_registry,
    _get_installed_modules,
    _get_company_industry,
    _detect_error_patterns,
    _detect_workflow_gaps,
    _detect_industry_gaps,
    ERROR_THRESHOLD,
    WORKFLOW_GAP_SECONDS,
)

# Path to the real module_registry.json
REGISTRY_PATH = os.path.join(
    os.path.dirname(OS_DIR), "module_registry.json"
)


# ---------------------------------------------------------------------------
# Table DDL for test databases
# ---------------------------------------------------------------------------

TABLE_DDL = """
CREATE TABLE IF NOT EXISTS action_call_log (
    id              TEXT PRIMARY KEY,
    timestamp       TEXT DEFAULT (datetime('now')),
    action_name     TEXT NOT NULL,
    routed_to       TEXT NOT NULL,
    route_tier      INTEGER NOT NULL,
    session_id      TEXT
);

CREATE INDEX IF NOT EXISTS idx_action_call_log_ts ON action_call_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_action_call_log_action ON action_call_log(action_name);

CREATE TABLE IF NOT EXISTS erpclaw_module (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    display_name    TEXT NOT NULL,
    version         TEXT NOT NULL DEFAULT '0.0.0',
    category        TEXT NOT NULL DEFAULT 'expansion',
    github_repo     TEXT NOT NULL DEFAULT '',
    install_path    TEXT NOT NULL DEFAULT '',
    installed_at    TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now')),
    install_status  TEXT NOT NULL DEFAULT 'pending',
    git_commit      TEXT,
    tables_created  INTEGER NOT NULL DEFAULT 0,
    action_count    INTEGER NOT NULL DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS erpclaw_module_config (
    id              TEXT PRIMARY KEY,
    module_name     TEXT NOT NULL,
    config_type     TEXT NOT NULL,
    config_data     TEXT,
    industry        TEXT,
    size_tier       TEXT
);

CREATE TABLE IF NOT EXISTS erpclaw_deploy_audit (
    id              TEXT PRIMARY KEY,
    module_name     TEXT NOT NULL,
    pipeline_result TEXT NOT NULL,
    tier            INTEGER,
    steps           TEXT NOT NULL DEFAULT '[]',
    git_commit      TEXT,
    human_approved  INTEGER,
    approved_by     TEXT,
    deployed_at     TEXT DEFAULT (datetime('now')),
    reasoning       TEXT
);

CREATE TABLE IF NOT EXISTS erpclaw_improvement_log (
    id              TEXT PRIMARY KEY,
    module_name     TEXT,
    category        TEXT NOT NULL,
    description     TEXT NOT NULL,
    evidence        TEXT,
    proposed_diff   TEXT,
    expected_impact TEXT,
    source          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'proposed',
    proposed_at     TEXT DEFAULT (datetime('now')),
    reviewed_at     TEXT,
    reviewed_by     TEXT,
    review_notes    TEXT,
    deploy_audit_id TEXT REFERENCES erpclaw_deploy_audit(id)
);

CREATE INDEX IF NOT EXISTS idx_erpclaw_improvement_log_source
    ON erpclaw_improvement_log(source);
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path):
    """Create a temporary SQLite database with all required tables."""
    path = str(tmp_path / "test_gap_detector.sqlite")
    conn = sqlite3.connect(path)
    setup_pragmas(conn)
    conn.executescript(TABLE_DDL)
    conn.commit()
    conn.close()
    return path


def _make_args(**kwargs):
    """Create a simple args namespace from keyword arguments."""
    return type("Args", (), kwargs)()


def _insert_action_call(conn, action_name, routed_to, route_tier=2,
                         session_id=None, timestamp=None):
    """Helper to insert a row into action_call_log."""
    call_id = str(uuid.uuid4())
    if timestamp:
        conn.execute(
            "INSERT INTO action_call_log (id, timestamp, action_name, routed_to, route_tier, session_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (call_id, timestamp, action_name, routed_to, route_tier, session_id),
        )
    else:
        conn.execute(
            "INSERT INTO action_call_log (id, action_name, routed_to, route_tier, session_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (call_id, action_name, routed_to, route_tier, session_id),
        )
    return call_id


def _install_module(conn, module_name, display_name=None):
    """Helper to register a module as installed."""
    conn.execute(
        "INSERT INTO erpclaw_module (id, name, display_name, install_status, is_active) "
        "VALUES (?, ?, ?, 'installed', 1)",
        (str(uuid.uuid4()), module_name, display_name or module_name),
    )


def _set_industry(conn, industry, size_tier="small"):
    """Helper to set company industry config."""
    conn.execute(
        "INSERT INTO erpclaw_module_config (id, module_name, config_type, config_data, industry, size_tier) "
        "VALUES (?, 'erpclaw', 'industry_config', '{}', ?, ?)",
        (str(uuid.uuid4()), industry, size_tier),
    )


# ---------------------------------------------------------------------------
# Test: Error pattern analysis
# ---------------------------------------------------------------------------

class TestErrorPatternDetection:
    """Tests for error pattern gap detection (method 1)."""

    def test_detects_unknown_action_errors(self, db_path):
        """Seed action_call_log with error-routed actions and verify gap detected."""
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        # Insert more than ERROR_THRESHOLD error-routed calls for same action
        for i in range(ERROR_THRESHOLD + 2):
            _insert_action_call(conn, "missing-action", "error")
        conn.commit()

        gaps = _detect_error_patterns(conn)
        conn.close()

        assert len(gaps) == 1
        assert gaps[0]["gap_type"] == "error_pattern"
        assert "missing-action" in gaps[0]["description"]
        assert gaps[0]["error_count"] == ERROR_THRESHOLD + 2
        assert gaps[0]["severity"] == "high"

    def test_below_threshold_not_flagged(self, db_path):
        """Actions with fewer errors than threshold should not be flagged."""
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        for i in range(ERROR_THRESHOLD - 1):
            _insert_action_call(conn, "rare-error", "error")
        conn.commit()

        gaps = _detect_error_patterns(conn)
        conn.close()

        assert len(gaps) == 0

    def test_multiple_error_actions_detected(self, db_path):
        """Multiple different failing actions each produce a gap."""
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        for i in range(ERROR_THRESHOLD + 1):
            _insert_action_call(conn, "missing-a", "error")
            _insert_action_call(conn, "missing-b", "error")
        conn.commit()

        gaps = _detect_error_patterns(conn)
        conn.close()

        assert len(gaps) == 2
        action_names = {g["action_name"] for g in gaps}
        assert "missing-a" in action_names
        assert "missing-b" in action_names


# ---------------------------------------------------------------------------
# Test: Workflow gap analysis
# ---------------------------------------------------------------------------

class TestWorkflowGapDetection:
    """Tests for workflow gap detection (method 2)."""

    def test_detects_long_gap_between_action_pairs(self, db_path):
        """Seed action pairs with long gaps and verify workflow gap detected."""
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        session = "test-session-1"
        base = datetime(2026, 3, 15, 10, 0, 0)

        # Create two occurrences of A->B with >5 min gap
        for i in range(2):
            offset_base = base + timedelta(hours=i)
            t1 = offset_base.strftime("%Y-%m-%d %H:%M:%S")
            t2 = (offset_base + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
            _insert_action_call(conn, "list-items", "erpclaw-inventory", session_id=session, timestamp=t1)
            _insert_action_call(conn, "add-stock-entry", "erpclaw-inventory", session_id=session, timestamp=t2)

        conn.commit()

        gaps = _detect_workflow_gaps(conn)
        conn.close()

        assert len(gaps) >= 1
        wf_gap = gaps[0]
        assert wf_gap["gap_type"] == "workflow_gap"
        assert wf_gap["severity"] == "medium"
        assert wf_gap["avg_gap_seconds"] > WORKFLOW_GAP_SECONDS

    def test_short_gaps_not_flagged(self, db_path):
        """Action pairs with short gaps should not be flagged."""
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        session = "test-session-2"
        base = datetime(2026, 3, 15, 10, 0, 0)

        for i in range(3):
            offset_base = base + timedelta(hours=i)
            t1 = offset_base.strftime("%Y-%m-%d %H:%M:%S")
            t2 = (offset_base + timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S")
            _insert_action_call(conn, "list-customers", "erpclaw-selling", session_id=session, timestamp=t1)
            _insert_action_call(conn, "get-customer", "erpclaw-selling", session_id=session, timestamp=t2)

        conn.commit()

        gaps = _detect_workflow_gaps(conn)
        conn.close()

        # No workflow gaps should be detected for 10-second gaps
        workflow_gaps_for_pair = [
            g for g in gaps
            if g.get("action_a") == "list-customers" and g.get("action_b") == "get-customer"
        ]
        assert len(workflow_gaps_for_pair) == 0


# ---------------------------------------------------------------------------
# Test: Industry gap analysis
# ---------------------------------------------------------------------------

class TestIndustryGapDetection:
    """Tests for industry gap detection (method 3)."""

    def test_healthcare_industry_suggests_healthclaw(self, db_path):
        """Set company industry to dental_practice, install only erpclaw core -> suggests healthclaw."""
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _set_industry(conn, "dental_practice", "small")
        _install_module(conn, "erpclaw", "ERPClaw Core")
        conn.commit()

        registry = _load_registry(REGISTRY_PATH)
        gaps = _detect_industry_gaps(conn, registry)
        conn.close()

        assert len(gaps) >= 1
        gap_modules = {g["module_name"] for g in gaps}
        assert "healthclaw" in gap_modules
        assert "healthclaw-dental" in gap_modules
        for g in gaps:
            assert g["gap_type"] == "industry_gap"
            assert g["industry"] == "dental_practice"

    def test_restaurant_industry_suggests_foodclaw(self, db_path):
        """Set company industry to restaurant -> suggests foodclaw."""
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _set_industry(conn, "restaurant", "small")
        _install_module(conn, "erpclaw", "ERPClaw Core")
        conn.commit()

        registry = _load_registry(REGISTRY_PATH)
        gaps = _detect_industry_gaps(conn, registry)
        conn.close()

        gap_modules = {g["module_name"] for g in gaps}
        assert "foodclaw" in gap_modules

    def test_construction_industry_suggests_constructclaw(self, db_path):
        """Set company industry to general_contractor -> suggests constructclaw."""
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _set_industry(conn, "general_contractor", "small")
        _install_module(conn, "erpclaw", "ERPClaw Core")
        conn.commit()

        registry = _load_registry(REGISTRY_PATH)
        gaps = _detect_industry_gaps(conn, registry)
        conn.close()

        gap_modules = {g["module_name"] for g in gaps}
        assert "constructclaw" in gap_modules

    def test_no_gaps_when_modules_installed(self, db_path):
        """No industry gaps when all standard modules are installed."""
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _set_industry(conn, "restaurant", "small")
        _install_module(conn, "erpclaw", "ERPClaw Core")
        _install_module(conn, "foodclaw", "FoodClaw")
        conn.commit()

        registry = _load_registry(REGISTRY_PATH)
        gaps = _detect_industry_gaps(conn, registry)
        conn.close()

        assert len(gaps) == 0

    def test_unknown_industry_no_gaps(self, db_path):
        """Unknown/unset industry produces no gaps, not errors."""
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _set_industry(conn, "unknown_industry_xyz", "small")
        conn.commit()

        registry = _load_registry(REGISTRY_PATH)
        gaps = _detect_industry_gaps(conn, registry)
        conn.close()

        assert len(gaps) == 0

    def test_no_industry_set_no_gaps(self, db_path):
        """No industry configured produces empty gap list."""
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        registry = _load_registry(REGISTRY_PATH)
        gaps = _detect_industry_gaps(conn, registry)
        conn.close()

        assert len(gaps) == 0


# ---------------------------------------------------------------------------
# Test: detect-gaps action (integration)
# ---------------------------------------------------------------------------

class TestDetectGaps:
    """Integration tests for the detect-gaps action."""

    def test_empty_action_call_log_no_errors(self, db_path):
        """Empty action_call_log produces no gaps, not errors."""
        args = _make_args(db_path=db_path, registry_path=REGISTRY_PATH)
        result = handle_detect_gaps(args)

        assert result["result"] == "ok"
        assert result["total_gaps"] == 0
        assert result["gaps"] == []
        assert result["gaps_by_type"]["error_pattern"] == 0
        assert result["gaps_by_type"]["workflow_gap"] == 0
        assert result["gaps_by_type"]["industry_gap"] == 0

    def test_returns_structured_gap_list(self, db_path):
        """Verify detect-gaps returns structured gap list."""
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _set_industry(conn, "dental_practice", "small")
        _install_module(conn, "erpclaw", "ERPClaw Core")
        conn.commit()
        conn.close()

        args = _make_args(db_path=db_path, registry_path=REGISTRY_PATH)
        result = handle_detect_gaps(args)

        assert result["result"] == "ok"
        assert result["total_gaps"] >= 1
        assert isinstance(result["gaps"], list)

        for gap in result["gaps"]:
            assert "gap_type" in gap
            assert "description" in gap
            assert "severity" in gap
            assert "suggested_action" in gap
            assert gap["gap_type"] in ("error_pattern", "workflow_gap", "industry_gap")

    def test_gaps_logged_to_improvement_log(self, db_path):
        """Verify all detected gaps are logged to erpclaw_improvement_log."""
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _set_industry(conn, "dental_practice", "small")
        _install_module(conn, "erpclaw", "ERPClaw Core")
        conn.commit()
        conn.close()

        args = _make_args(db_path=db_path, registry_path=REGISTRY_PATH)
        result = handle_detect_gaps(args)

        total_gaps = result["total_gaps"]
        assert total_gaps >= 1

        # Check improvement_log
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM erpclaw_improvement_log WHERE source = 'gap_detector'"
        ).fetchall()
        conn.close()

        assert len(rows) == total_gaps
        for row in rows:
            assert row["category"] == "coverage"
            assert row["source"] == "gap_detector"
            assert row["status"] == "proposed"
            assert row["description"] is not None


# ---------------------------------------------------------------------------
# Test: suggest-modules action
# ---------------------------------------------------------------------------

class TestSuggestModules:
    """Tests for the suggest-modules action."""

    def test_returns_ranked_list_with_reasons(self, db_path):
        """Verify suggest-modules returns ranked list with reasons."""
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _set_industry(conn, "dental_practice", "small")
        _install_module(conn, "erpclaw", "ERPClaw Core")
        conn.commit()
        conn.close()

        args = _make_args(db_path=db_path, registry_path=REGISTRY_PATH)
        result = handle_suggest_modules(args)

        assert result["result"] == "ok"
        assert result["suggestion_count"] >= 1
        assert isinstance(result["suggestions"], list)

        for s in result["suggestions"]:
            assert "module_name" in s
            assert "relevance_score" in s
            assert "reason" in s
            assert "dependencies" in s
            assert isinstance(s["relevance_score"], float)

    def test_suggestions_sorted_by_relevance(self, db_path):
        """Verify suggestions are ranked by relevance_score descending."""
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _set_industry(conn, "dental_practice", "small")
        _install_module(conn, "erpclaw", "ERPClaw Core")
        conn.commit()
        conn.close()

        args = _make_args(db_path=db_path, registry_path=REGISTRY_PATH)
        result = handle_suggest_modules(args)

        scores = [s["relevance_score"] for s in result["suggestions"]]
        assert scores == sorted(scores, reverse=True)

    def test_excludes_already_installed_modules(self, db_path):
        """Verify suggestions exclude already-installed modules."""
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _set_industry(conn, "dental_practice", "small")
        _install_module(conn, "erpclaw", "ERPClaw Core")
        _install_module(conn, "healthclaw", "HealthClaw")
        conn.commit()
        conn.close()

        args = _make_args(db_path=db_path, registry_path=REGISTRY_PATH)
        result = handle_suggest_modules(args)

        suggested_names = {s["module_name"] for s in result["suggestions"]}
        assert "healthclaw" not in suggested_names
        assert "erpclaw" not in suggested_names

    def test_only_references_registry_modules(self, db_path):
        """Verify suggestions only reference modules that exist in module_registry.json."""
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _set_industry(conn, "dental_practice", "small")
        _install_module(conn, "erpclaw", "ERPClaw Core")
        conn.commit()
        conn.close()

        registry = _load_registry(REGISTRY_PATH)
        args = _make_args(db_path=db_path, registry_path=REGISTRY_PATH)
        result = handle_suggest_modules(args)

        for s in result["suggestions"]:
            assert s["module_name"] in registry, (
                f"Suggested module '{s['module_name']}' not in module_registry.json"
            )

    def test_dependency_constraints_respected(self, db_path):
        """Sub-verticals without parent installed should be penalized or excluded.

        healthclaw-dental requires healthclaw. If healthclaw is not installed,
        healthclaw-dental should have lower score than healthclaw.
        """
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _set_industry(conn, "dental_practice", "small")
        _install_module(conn, "erpclaw", "ERPClaw Core")
        conn.commit()
        conn.close()

        args = _make_args(db_path=db_path, registry_path=REGISTRY_PATH)
        result = handle_suggest_modules(args)

        suggestions_map = {s["module_name"]: s for s in result["suggestions"]}

        # healthclaw should be suggested (required by dental_practice)
        assert "healthclaw" in suggestions_map

        # If healthclaw-dental is suggested, it should have lower score than healthclaw
        # because healthclaw dependency is not yet installed
        if "healthclaw-dental" in suggestions_map:
            assert suggestions_map["healthclaw-dental"]["relevance_score"] <= \
                   suggestions_map["healthclaw"]["relevance_score"]

    def test_handles_unknown_industry_gracefully(self, db_path):
        """Unknown industry still returns suggestions (no industry boost)."""
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _set_industry(conn, "unknown_industry_xyz", "small")
        _install_module(conn, "erpclaw", "ERPClaw Core")
        conn.commit()
        conn.close()

        args = _make_args(db_path=db_path, registry_path=REGISTRY_PATH)
        result = handle_suggest_modules(args)

        assert result["result"] == "ok"
        assert result["industry"] == "unknown_industry_xyz"
        # Should still return some suggestions (from dependency/category scoring)

    def test_no_industry_set(self, db_path):
        """No industry configured still returns valid response."""
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _install_module(conn, "erpclaw", "ERPClaw Core")
        conn.commit()
        conn.close()

        args = _make_args(db_path=db_path, registry_path=REGISTRY_PATH)
        result = handle_suggest_modules(args)

        assert result["result"] == "ok"
        assert result["industry"] is None


# ---------------------------------------------------------------------------
# Test: Registry and helper functions
# ---------------------------------------------------------------------------

class TestRegistryAndHelpers:
    """Tests for registry loading and helper functions."""

    def test_module_registry_readable_and_parseable(self):
        """Verify module_registry.json is readable and parseable."""
        registry = _load_registry(REGISTRY_PATH)
        assert isinstance(registry, dict)
        assert len(registry) > 0
        # Check a known module exists
        assert "erpclaw" in registry
        assert "healthclaw" in registry
        assert "foodclaw" in registry

    def test_registry_missing_file_returns_empty(self, tmp_path):
        """Missing registry file returns empty dict, not error."""
        registry = _load_registry(str(tmp_path / "nonexistent.json"))
        assert registry == {}

    def test_get_installed_modules_empty(self, db_path):
        """No installed modules returns empty set."""
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        installed = _get_installed_modules(conn)
        conn.close()
        assert installed == set()

    def test_get_installed_modules_with_data(self, db_path):
        """Installed modules are returned correctly."""
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _install_module(conn, "erpclaw", "ERPClaw Core")
        _install_module(conn, "healthclaw", "HealthClaw")
        conn.commit()

        installed = _get_installed_modules(conn)
        conn.close()

        assert "erpclaw" in installed
        assert "healthclaw" in installed

    def test_get_company_industry_none_when_unset(self, db_path):
        """Returns (None, None) when no industry config exists."""
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        industry, size_tier = _get_company_industry(conn)
        conn.close()
        assert industry is None
        assert size_tier is None

    def test_get_company_industry_reads_config(self, db_path):
        """Returns industry and size_tier from erpclaw_module_config."""
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _set_industry(conn, "restaurant", "medium")
        conn.commit()

        industry, size_tier = _get_company_industry(conn)
        conn.close()

        assert industry == "restaurant"
        assert size_tier == "medium"


# ---------------------------------------------------------------------------
# Fixtures for Method 4 (Schema-Code Divergence) and Method 5 (Stubs)
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_src(tmp_path):
    """Create a synthetic src/ tree with init_schema.py, init_db.py, and db_query.py files.

    Tables defined:
      - company (heavily used in db_query.py — many references)
      - customer (heavily used in db_query.py — many references)
      - blanket_order (zero references in db_query.py)
      - item_attribute (zero references in db_query.py)
      - pricing_rule (2 references — minimal_code)
    """
    # Module 1: core setup with init_schema.py
    setup_dir = tmp_path / "erpclaw" / "scripts" / "erpclaw-setup"
    setup_dir.mkdir(parents=True)
    (setup_dir / "init_schema.py").write_text(textwrap.dedent("""\
        DDL = \"\"\"
        CREATE TABLE IF NOT EXISTS company (
            id   TEXT PRIMARY KEY,
            name TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS customer (
            id   TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            company_id TEXT REFERENCES company(id)
        );

        CREATE TABLE IF NOT EXISTS blanket_order (
            id   TEXT PRIMARY KEY,
            blanket_order_type TEXT NOT NULL,
            company_id TEXT REFERENCES company(id)
        );

        CREATE TABLE IF NOT EXISTS item_attribute (
            id      TEXT PRIMARY KEY,
            item_id TEXT NOT NULL,
            attr    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pricing_rule (
            id      TEXT PRIMARY KEY,
            name    TEXT NOT NULL
        );
        \"\"\"
    """))

    # db_query.py with references to company, customer, pricing_rule (2x)
    (setup_dir / "db_query.py").write_text(textwrap.dedent("""\
        # This file references company and customer tables
        def list_companies(conn):
            conn.execute("SELECT * FROM company")
            conn.execute("SELECT * FROM company WHERE name = ?", ("x",))
            conn.execute("SELECT * FROM company WHERE id = ?", ("y",))

        def list_customers(conn):
            conn.execute("SELECT * FROM customer")
            conn.execute("SELECT * FROM customer WHERE name = ?", ("x",))
            conn.execute("SELECT * FROM customer WHERE id = ?", ("y",))
            conn.execute("SELECT * FROM customer WHERE company_id = ?", ("z",))

        def get_pricing_rule(conn):
            conn.execute("SELECT * FROM pricing_rule WHERE id = ?", ("x",))

        def apply_pricing_rule(conn):
            conn.execute("UPDATE pricing_rule SET name = ? WHERE id = ?", ("new", "x"))
    """))

    # Module 2: addon with init_db.py
    addon_dir = tmp_path / "addon"
    addon_dir.mkdir()
    (addon_dir / "init_db.py").write_text(textwrap.dedent("""\
        DDL = \"\"\"
        CREATE TABLE IF NOT EXISTS addon_widget (
            id   TEXT PRIMARY KEY,
            name TEXT NOT NULL
        );
        \"\"\"
    """))

    addon_scripts = addon_dir / "scripts"
    addon_scripts.mkdir()
    (addon_scripts / "db_query.py").write_text(textwrap.dedent("""\
        def list_widgets(conn):
            conn.execute("SELECT * FROM addon_widget")
            conn.execute("SELECT * FROM addon_widget WHERE id = ?", ("x",))
            conn.execute("SELECT * FROM addon_widget WHERE name = ?", ("y",))
    """))

    return str(tmp_path)


@pytest.fixture
def fake_src_with_stubs(tmp_path):
    """Create a synthetic src/ tree with various stub patterns.

    Files:
      - stock_posting.py (SAFETY_EXCLUDED) with "Not yet implemented" + "Phase 2+"
      - gl_posting.py (SAFETY_EXCLUDED) with "TODO: add FIFO"
      - helper.py (non-excluded) with "placeholder" and "TODO"
      - tests/test_stuff.py (should be skipped — in tests/ dir)
    """
    lib_dir = tmp_path / "erpclaw" / "scripts" / "erpclaw-setup" / "lib" / "erpclaw_lib"
    lib_dir.mkdir(parents=True)

    # Safety-excluded file with stubs
    (lib_dir / "stock_posting.py").write_text(textwrap.dedent("""\
        \"\"\"Stock posting engine.\"\"\"
        # Valuation methods:
        # - moving_average: implemented
        # - fifo: Not yet implemented (Phase 2+); falls back to moving_average
        def get_valuation_rate():
            pass
    """))

    # Another safety-excluded file
    (lib_dir / "gl_posting.py").write_text(textwrap.dedent("""\
        \"\"\"GL posting engine.\"\"\"
        # TODO: add multi-currency support
        def post_gl():
            pass
    """))

    # Non-excluded file with stubs
    helper_dir = tmp_path / "erpclaw" / "scripts" / "erpclaw-selling"
    helper_dir.mkdir(parents=True)
    (helper_dir / "helper.py").write_text(textwrap.dedent("""\
        \"\"\"Helper functions.\"\"\"
        def calculate_discount():
            # placeholder for discount logic
            return 0

        def apply_coupon():
            # TODO: implement coupon logic
            return 0

        def check_loyalty():
            # Extensible in future for loyalty programs
            pass
    """))

    # Test file (should be excluded from scan)
    test_dir = tmp_path / "erpclaw" / "scripts" / "erpclaw-selling" / "tests"
    test_dir.mkdir(parents=True)
    (test_dir / "test_stuff.py").write_text(textwrap.dedent("""\
        def test_placeholder():
            # TODO: write real test
            # placeholder assertion
            assert True
    """))

    return str(tmp_path)


# ---------------------------------------------------------------------------
# Test: Schema-code divergence detection (Method 4)
# ---------------------------------------------------------------------------

class TestSchemaCodeDivergence:
    """Tests for schema-code divergence detection (method 4)."""

    def test_detect_schema_code_divergence_finds_blanket_order(self, fake_src):
        """blanket_order has zero references in db_query.py — should be flagged as no_code."""
        gaps = detect_schema_code_divergence(fake_src)
        no_code = {g["table_name"] for g in gaps if g["status"] == "no_code"}
        assert "blanket_order" in no_code

        bo_gap = [g for g in gaps if g["table_name"] == "blanket_order"][0]
        assert bo_gap["action_references"] == 0
        assert bo_gap["severity"] == "high"
        assert "init_schema.py" in bo_gap["defined_in"]
        assert "Generate CRUD" in bo_gap["recommendation"]

    def test_detect_schema_code_divergence_finds_item_attribute(self, fake_src):
        """item_attribute has zero references — should be flagged as no_code."""
        gaps = detect_schema_code_divergence(fake_src)
        no_code = {g["table_name"] for g in gaps if g["status"] == "no_code"}
        assert "item_attribute" in no_code

    def test_detect_schema_code_divergence_ignores_used_tables(self, fake_src):
        """company and customer have many references — should NOT appear in gaps."""
        gaps = detect_schema_code_divergence(fake_src)
        gap_tables = {g["table_name"] for g in gaps}
        assert "company" not in gap_tables
        assert "customer" not in gap_tables

    def test_detect_schema_code_divergence_flags_minimal_code(self, fake_src):
        """pricing_rule has exactly 2 references — should be flagged as minimal_code."""
        gaps = detect_schema_code_divergence(fake_src)
        minimal = [g for g in gaps if g["table_name"] == "pricing_rule"]
        assert len(minimal) == 1
        assert minimal[0]["status"] == "minimal_code"
        assert minimal[0]["action_references"] == 2
        assert minimal[0]["severity"] == "medium"

    def test_detect_schema_code_divergence_well_used_addon(self, fake_src):
        """addon_widget has 3+ references — should NOT be flagged."""
        gaps = detect_schema_code_divergence(fake_src)
        gap_tables = {g["table_name"] for g in gaps}
        assert "addon_widget" not in gap_tables

    def test_detect_schema_code_divergence_sorted_by_severity(self, fake_src):
        """Gaps should be sorted: no_code (high) first, then minimal_code (medium)."""
        gaps = detect_schema_code_divergence(fake_src)
        severities = [g["severity"] for g in gaps]
        # All 'high' should come before any 'medium'
        high_indices = [i for i, s in enumerate(severities) if s == "high"]
        medium_indices = [i for i, s in enumerate(severities) if s == "medium"]
        if high_indices and medium_indices:
            assert max(high_indices) < min(medium_indices)

    def test_detect_schema_code_divergence_invalid_path(self):
        """Invalid src_root returns empty list, not an error."""
        gaps = detect_schema_code_divergence("/nonexistent/path")
        assert gaps == []

    def test_detect_schema_divergence_action_handler(self, fake_src, db_path):
        """Test the handle_detect_schema_divergence action handler."""
        args = _make_args(src_root=fake_src, db_path=db_path)
        result = handle_detect_schema_divergence(args)
        assert result["result"] == "ok"
        assert result["no_code_count"] >= 2  # blanket_order, item_attribute
        assert result["total"] == result["no_code_count"] + result["minimal_code_count"]

    def test_detect_schema_divergence_action_handler_no_src(self, db_path):
        """Missing --src-root returns error."""
        args = _make_args(src_root=None, db_path=db_path)
        result = handle_detect_schema_divergence(args)
        assert "error" in result


# ---------------------------------------------------------------------------
# Test: Stub detection (Method 5)
# ---------------------------------------------------------------------------

class TestStubDetection:
    """Tests for stub detection (method 5)."""

    def test_detect_stubs_finds_fifo_stub(self, fake_src_with_stubs):
        """The FIFO 'Not yet implemented' stub in stock_posting.py should be found."""
        stubs = detect_stubs(fake_src_with_stubs)
        fifo_stubs = [
            s for s in stubs
            if s["file"] == "stock_posting.py" and "Not yet implemented" in s["text"]
        ]
        assert len(fifo_stubs) >= 1
        assert fifo_stubs[0]["line"] == 4  # line 4 in our fixture

    def test_detect_stubs_classifies_safety_excluded_as_human_required(self, fake_src_with_stubs):
        """stock_posting.py is in SAFETY_EXCLUDED_FILES — stubs should be human_required."""
        stubs = detect_stubs(fake_src_with_stubs)
        sp_stubs = [s for s in stubs if s["file"] == "stock_posting.py"]
        assert len(sp_stubs) >= 1
        for stub in sp_stubs:
            assert stub["is_safety_excluded"] is True
            assert stub["classification"] == "human_required"

    def test_detect_stubs_classifies_gl_posting_as_human_required(self, fake_src_with_stubs):
        """gl_posting.py is in SAFETY_EXCLUDED_FILES — stubs should be human_required."""
        stubs = detect_stubs(fake_src_with_stubs)
        gl_stubs = [s for s in stubs if s["file"] == "gl_posting.py"]
        assert len(gl_stubs) >= 1
        for stub in gl_stubs:
            assert stub["is_safety_excluded"] is True
            assert stub["classification"] == "human_required"

    def test_detect_stubs_classifies_non_excluded_as_os_addressable(self, fake_src_with_stubs):
        """helper.py is NOT in SAFETY_EXCLUDED_FILES — stubs should be os_addressable."""
        stubs = detect_stubs(fake_src_with_stubs)
        helper_stubs = [s for s in stubs if s["file"] == "helper.py"]
        assert len(helper_stubs) >= 1
        for stub in helper_stubs:
            assert stub["is_safety_excluded"] is False
            assert stub["classification"] == "os_addressable"

    def test_detect_stubs_finds_placeholder_pattern(self, fake_src_with_stubs):
        """'placeholder' keyword in helper.py should be detected."""
        stubs = detect_stubs(fake_src_with_stubs)
        ph_stubs = [s for s in stubs if "placeholder" in s["text"].lower() and s["file"] == "helper.py"]
        assert len(ph_stubs) >= 1

    def test_detect_stubs_finds_todo_pattern(self, fake_src_with_stubs):
        """'TODO:' keyword in helper.py should be detected."""
        stubs = detect_stubs(fake_src_with_stubs)
        todo_stubs = [s for s in stubs if "TODO" in s["text"] and s["file"] == "helper.py"]
        assert len(todo_stubs) >= 1

    def test_detect_stubs_finds_extensible_pattern(self, fake_src_with_stubs):
        """'Extensible in future' in helper.py should be detected."""
        stubs = detect_stubs(fake_src_with_stubs)
        ext_stubs = [s for s in stubs if "Extensible in future" in s["text"]]
        assert len(ext_stubs) >= 1

    def test_detect_stubs_ignores_test_files(self, fake_src_with_stubs):
        """Files in tests/ directories should NOT be scanned."""
        stubs = detect_stubs(fake_src_with_stubs)
        test_stubs = [s for s in stubs if s["file"] == "test_stuff.py"]
        assert len(test_stubs) == 0

    def test_detect_stubs_sorted_human_first(self, fake_src_with_stubs):
        """human_required stubs should be sorted before os_addressable."""
        stubs = detect_stubs(fake_src_with_stubs)
        classifications = [s["classification"] for s in stubs]
        human_indices = [i for i, c in enumerate(classifications) if c == "human_required"]
        os_indices = [i for i, c in enumerate(classifications) if c == "os_addressable"]
        if human_indices and os_indices:
            assert max(human_indices) < min(os_indices)

    def test_detect_stubs_invalid_path(self):
        """Invalid src_root returns empty list, not an error."""
        stubs = detect_stubs("/nonexistent/path")
        assert stubs == []

    def test_detect_stubs_action_handler(self, fake_src_with_stubs, db_path):
        """Test the handle_detect_stubs action handler."""
        args = _make_args(src_root=fake_src_with_stubs, db_path=db_path)
        result = handle_detect_stubs(args)
        assert result["result"] == "ok"
        assert result["total"] >= 1
        assert result["human_required_count"] >= 1
        assert result["os_addressable_count"] >= 1
        assert result["total"] == result["human_required_count"] + result["os_addressable_count"]

    def test_detect_stubs_action_handler_no_src(self, db_path):
        """Missing --src-root returns error."""
        args = _make_args(src_root=None, db_path=db_path)
        result = handle_detect_stubs(args)
        assert "error" in result


# ---------------------------------------------------------------------------
# Test: detect-gaps integration with methods 4 and 5
# ---------------------------------------------------------------------------

class TestDetectGapsWithNewMethods:
    """Integration tests for detect-gaps including methods 4 and 5."""

    def test_detect_gaps_includes_schema_divergence(self, fake_src, db_path):
        """detect-gaps with --src-root should include schema_code_divergence gaps."""
        args = _make_args(db_path=db_path, registry_path=REGISTRY_PATH, src_root=fake_src)
        result = handle_detect_gaps(args)
        assert result["result"] == "ok"
        assert result["gaps_by_type"]["schema_code_divergence"] >= 1
        schema_gaps = [g for g in result["gaps"] if g["gap_type"] == "schema_code_divergence"]
        assert len(schema_gaps) >= 1

    def test_detect_gaps_includes_stubs(self, fake_src_with_stubs, db_path):
        """detect-gaps with --src-root should include stub gaps."""
        args = _make_args(db_path=db_path, registry_path=REGISTRY_PATH, src_root=fake_src_with_stubs)
        result = handle_detect_gaps(args)
        assert result["result"] == "ok"
        assert result["gaps_by_type"]["stub"] >= 1
        stub_gaps = [g for g in result["gaps"] if g["gap_type"] == "stub"]
        assert len(stub_gaps) >= 1

    def test_detect_gaps_without_src_root_skips_new_methods(self, db_path):
        """detect-gaps without --src-root should still work (methods 4+5 skipped)."""
        args = _make_args(db_path=db_path, registry_path=REGISTRY_PATH, src_root=None)
        result = handle_detect_gaps(args)
        assert result["result"] == "ok"
        assert result["gaps_by_type"]["schema_code_divergence"] == 0
        assert result["gaps_by_type"]["stub"] == 0
