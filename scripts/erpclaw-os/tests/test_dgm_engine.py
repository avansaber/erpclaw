#!/usr/bin/env python3
"""Tests for ERPClaw OS DGM Variant Engine (Phase 3, Deliverable 3c).

Tests the dgm-run-variant, dgm-list-variants, and dgm-select-best
actions including safety exclusions, variant generation, scoring,
selection, cleanup, and improvement proposal creation.
"""
import json
import os
import sqlite3
import sys
import uuid
from decimal import Decimal

import pytest

# Add erpclaw-os directory to path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OS_DIR = os.path.dirname(SCRIPT_DIR)
if OS_DIR not in sys.path:
    sys.path.insert(0, OS_DIR)

# Add shared lib to path
sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
from erpclaw_lib.db import setup_pragmas

from dgm_engine import (
    handle_dgm_run_variant,
    handle_dgm_list_variants,
    handle_dgm_select_best,
    is_safety_excluded,
    SAFETY_EXCLUDED_FILES,
    VALID_MUTATION_TYPES,
    _compute_composite_score,
    _generate_variant_code,
    _compute_diff,
)
from variant_manager import (
    store_variant,
    compare_variants,
    select_best,
    cleanup_old_variants,
    get_variant_diff,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

DGM_TABLE_DDL = """
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

CREATE TABLE IF NOT EXISTS erpclaw_dgm_run (
    id              TEXT PRIMARY KEY,
    module_name     TEXT NOT NULL,
    action_name     TEXT NOT NULL,
    variant_count   INTEGER NOT NULL,
    best_variant_id TEXT,
    current_exec_ms INTEGER,
    best_exec_ms    INTEGER,
    improvement_pct TEXT,
    status          TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed', 'no_improvement')),
    improvement_id  TEXT REFERENCES erpclaw_improvement_log(id),
    started_at      TEXT DEFAULT (datetime('now')),
    completed_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_erpclaw_dgm_run_module
    ON erpclaw_dgm_run(module_name);
CREATE INDEX IF NOT EXISTS idx_erpclaw_dgm_run_status
    ON erpclaw_dgm_run(status);

CREATE TABLE IF NOT EXISTS erpclaw_dgm_variant (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES erpclaw_dgm_run(id),
    module_name     TEXT NOT NULL,
    action_name     TEXT NOT NULL,
    variant_number  INTEGER NOT NULL,
    variant_code    TEXT NOT NULL,
    variant_diff    TEXT,
    mutation_type   TEXT NOT NULL CHECK(mutation_type IN ('query_optimization', 'algorithm_change', 'caching', 'parameter_reorder', 'data_structure', 'batch_processing')),
    exec_time_ms    INTEGER,
    memory_kb       INTEGER,
    test_pass_count INTEGER,
    test_total      INTEGER,
    composite_score TEXT,
    is_selected     INTEGER NOT NULL DEFAULT 0 CHECK(is_selected IN (0, 1)),
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_erpclaw_dgm_variant_run
    ON erpclaw_dgm_variant(run_id);
CREATE INDEX IF NOT EXISTS idx_erpclaw_dgm_variant_module
    ON erpclaw_dgm_variant(module_name);
CREATE INDEX IF NOT EXISTS idx_erpclaw_dgm_variant_selected
    ON erpclaw_dgm_variant(is_selected);
"""


@pytest.fixture
def db_path(tmp_path):
    """Create a temporary SQLite database with DGM tables."""
    path = str(tmp_path / "test_dgm.sqlite")
    conn = sqlite3.connect(path)
    setup_pragmas(conn)
    conn.executescript(DGM_TABLE_DDL)
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def db_conn(db_path):
    """Return a connection to the test database with row_factory."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    setup_pragmas(conn)
    yield conn
    conn.close()


def _make_args(**kwargs):
    """Create a simple args namespace from keyword arguments."""
    return type("Args", (), kwargs)()


def _create_run(conn, module_name="retailclaw", action_name="list-products",
                variant_count=3, status="running"):
    """Helper to create a DGM run record."""
    run_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO erpclaw_dgm_run (id, module_name, action_name, variant_count, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (run_id, module_name, action_name, variant_count, status),
    )
    conn.commit()
    return run_id


def _create_variant(conn, run_id, variant_number=1, mutation_type="caching",
                    exec_time_ms=100, memory_kb=2048, test_pass_count=10,
                    test_total=10, composite_score=None, module_name="retailclaw",
                    action_name="list-products"):
    """Helper to create a DGM variant record directly."""
    if composite_score is None:
        composite_score = _compute_composite_score(
            exec_time_ms, memory_kb, test_pass_count, test_total,
        )
    variant_data = {
        "module_name": module_name,
        "action_name": action_name,
        "variant_number": variant_number,
        "variant_code": f"# variant {variant_number}\ndef handler(args): pass",
        "variant_diff": f"--- original\n+++ variant {variant_number}",
        "mutation_type": mutation_type,
        "exec_time_ms": exec_time_ms,
        "memory_kb": memory_kb,
        "test_pass_count": test_pass_count,
        "test_total": test_total,
        "composite_score": composite_score,
    }
    return store_variant(conn, run_id, variant_data)


# ===========================================================================
# Test: Safety Exclusion List
# ===========================================================================

class TestSafetyExclusion:
    """Tests that the DGM engine refuses to touch safety-critical files."""

    def test_blocks_gl_posting(self):
        """DGM must reject actions touching gl_posting.py."""
        excluded, reason = is_safety_excluded("gl_posting", "submit-journal", source_file="gl_posting.py")
        assert excluded is True
        assert "gl_posting.py" in reason

    def test_blocks_stock_posting(self):
        """DGM must reject actions touching stock_posting.py."""
        excluded, reason = is_safety_excluded("stock_posting", "submit-stock", source_file="stock_posting.py")
        assert excluded is True
        assert "stock_posting.py" in reason

    def test_blocks_tax_calculation(self):
        """DGM must reject actions touching tax_calculation.py."""
        excluded, reason = is_safety_excluded("tax_calculation", "calc-tax", source_file="tax_calculation.py")
        assert excluded is True
        assert "tax_calculation.py" in reason

    def test_blocks_cross_skill(self):
        """DGM must reject actions touching cross_skill.py."""
        excluded, reason = is_safety_excluded("cross_skill", "cross-invoke", source_file="cross_skill.py")
        assert excluded is True
        assert "cross_skill.py" in reason

    def test_blocks_constitution(self):
        """DGM must reject actions touching constitution.py."""
        excluded, reason = is_safety_excluded("erpclaw-os", "list-articles", source_file="constitution.py")
        assert excluded is True
        assert "constitution.py" in reason

    def test_blocks_validate_module(self):
        """DGM must reject actions touching validate_module.py."""
        excluded, reason = is_safety_excluded("erpclaw-os", "validate-module", source_file="validate_module.py")
        assert excluded is True
        assert "validate_module.py" in reason

    def test_blocks_sandbox(self):
        """DGM must reject actions touching sandbox.py."""
        excluded, reason = is_safety_excluded("erpclaw-os", "run-sandbox", source_file="sandbox.py")
        assert excluded is True
        assert "sandbox.py" in reason

    def test_blocks_gl_invariant_checker(self):
        """DGM must reject actions touching gl_invariant_checker.py."""
        excluded, reason = is_safety_excluded("erpclaw-os", "check-gl", source_file="gl_invariant_checker.py")
        assert excluded is True
        assert "gl_invariant_checker.py" in reason

    def test_blocks_dgm_engine_itself(self):
        """DGM engine must NOT be able to modify itself."""
        excluded, reason = is_safety_excluded("erpclaw-os", "dgm-run-variant", source_file="dgm_engine.py")
        assert excluded is True
        assert "dgm_engine.py" in reason

    def test_blocks_tier_3_actions(self):
        """DGM must reject Tier 3 classified actions."""
        # "schema-apply" with code containing DROP TABLE → Tier 3 escalation
        # We test via module_name in SAFETY_EXCLUDED_MODULES
        excluded, reason = is_safety_excluded("gl_posting", "submit-gl")
        assert excluded is True
        assert "financial core" in reason.lower()

    def test_allows_safe_action(self):
        """DGM allows non-excluded actions."""
        excluded, reason = is_safety_excluded("retailclaw", "list-products")
        assert excluded is False
        assert reason is None

    def test_allows_list_action(self):
        """DGM allows read-only list actions."""
        excluded, reason = is_safety_excluded("hospitalityclaw", "hotel-list-rooms")
        assert excluded is False

    def test_blocks_schema_migrator(self):
        """DGM must reject actions touching schema_migrator.py (can DROP tables)."""
        excluded, reason = is_safety_excluded("erpclaw-os", "schema-apply", source_file="schema_migrator.py")
        assert excluded is True
        assert "schema_migrator.py" in reason

    def test_blocks_tier_classifier(self):
        """DGM must reject actions touching tier_classifier.py (changing tier = bypassing safety)."""
        excluded, reason = is_safety_excluded("erpclaw-os", "classify-action", source_file="tier_classifier.py")
        assert excluded is True
        assert "tier_classifier.py" in reason

    def test_blocks_improvement_log(self):
        """DGM must reject actions touching improvement_log.py (manipulating approval status)."""
        excluded, reason = is_safety_excluded("erpclaw-os", "approve-improvement", source_file="improvement_log.py")
        assert excluded is True
        assert "improvement_log.py" in reason

    def test_blocks_deploy_pipeline(self):
        """DGM must reject actions touching deploy_pipeline.py (auto-deploy logic)."""
        excluded, reason = is_safety_excluded("erpclaw-os", "deploy-module", source_file="deploy_pipeline.py")
        assert excluded is True
        assert "deploy_pipeline.py" in reason

    def test_blocks_in_module_generator(self):
        """DGM must reject actions touching in_module_generator.py (must not self-modify)."""
        excluded, reason = is_safety_excluded("erpclaw-os", "generate-feature", source_file="in_module_generator.py")
        assert excluded is True
        assert "in_module_generator.py" in reason

    def test_exclusion_list_count(self):
        """SAFETY_EXCLUDED_FILES must contain exactly 16 entries."""
        assert len(SAFETY_EXCLUDED_FILES) == 16

    def test_exclusion_list_is_immutable(self):
        """The safety exclusion list is a frozenset (immutable)."""
        assert isinstance(SAFETY_EXCLUDED_FILES, frozenset)
        with pytest.raises(AttributeError):
            SAFETY_EXCLUDED_FILES.add("new_file.py")

    @pytest.mark.parametrize("filename", [
        "schema_migrator.py",
        "tier_classifier.py",
        "improvement_log.py",
        "deploy_pipeline.py",
        "in_module_generator.py",
    ])
    def test_new_safety_files_in_frozenset(self, filename):
        """Each newly added safety file must be present in SAFETY_EXCLUDED_FILES."""
        assert filename in SAFETY_EXCLUDED_FILES

    def test_dgm_run_variant_rejects_excluded_module(self, db_path):
        """dgm-run-variant action rejects excluded modules."""
        args = _make_args(
            module_name_arg="gl_posting",
            module_name="gl_posting",
            action_name="submit-journal",
            variant_count=3,
            db_path=db_path,
        )
        result = handle_dgm_run_variant(args)
        assert "error" in result
        assert "safety exclusion" in result["error"].lower()
        assert result.get("safety_excluded") is True


# ===========================================================================
# Test: Composite Score Calculation
# ===========================================================================

class TestCompositeScore:
    """Tests for the composite scoring formula."""

    def test_score_formula(self):
        """Verify composite score formula: (1/exec_ms)*0.6 + (1/mem_kb)*0.2 + pass_rate*0.2."""
        score = _compute_composite_score(100, 2048, 10, 10)
        # (1/100)*0.6 + (1/2048)*0.2 + (10/10)*0.2
        expected = Decimal("1") / Decimal("100") * Decimal("0.6") \
                 + Decimal("1") / Decimal("2048") * Decimal("0.2") \
                 + Decimal("1.0") * Decimal("0.2")
        expected_str = str(expected.quantize(Decimal("0.000001")))
        assert score == expected_str

    def test_failed_tests_disqualify(self):
        """Variant with test_pass_rate < 1.0 gets score 0."""
        score = _compute_composite_score(100, 2048, 9, 10)
        assert score == "0"

    def test_zero_tests_disqualify(self):
        """Variant with no tests gets score 0."""
        score = _compute_composite_score(100, 2048, 0, 0)
        assert score == "0"

    def test_faster_variant_scores_higher(self):
        """Faster execution time produces higher composite score."""
        fast_score = _compute_composite_score(50, 2048, 10, 10)
        slow_score = _compute_composite_score(200, 2048, 10, 10)
        assert Decimal(fast_score) > Decimal(slow_score)

    def test_less_memory_scores_higher(self):
        """Lower memory usage produces higher composite score."""
        lean_score = _compute_composite_score(100, 1024, 10, 10)
        heavy_score = _compute_composite_score(100, 8192, 10, 10)
        assert Decimal(lean_score) > Decimal(heavy_score)

    def test_all_pass_required(self):
        """Only variants with 100% test pass rate qualify."""
        full_pass = _compute_composite_score(100, 2048, 10, 10)
        one_fail = _compute_composite_score(100, 2048, 9, 10)
        assert Decimal(full_pass) > Decimal("0")
        assert one_fail == "0"


# ===========================================================================
# Test: Variant Generation
# ===========================================================================

class TestVariantGeneration:
    """Tests for variant code generation and diffing."""

    def test_generates_distinct_variants(self):
        """Each mutation type produces a different variant."""
        original = "def handler(args):\n    return list(args)\n"
        variants = set()
        for mt in VALID_MUTATION_TYPES:
            code = _generate_variant_code(original, mt, 1)
            variants.add(code)
        # All mutation types should produce unique variants
        assert len(variants) == len(VALID_MUTATION_TYPES)

    def test_variant_contains_mutation_header(self):
        """Generated variant includes mutation type annotation."""
        original = "def handler(args): pass\n"
        for mt in VALID_MUTATION_TYPES:
            code = _generate_variant_code(original, mt, 1)
            assert f"mutation: {mt}" in code

    def test_diff_captures_changes(self):
        """_compute_diff produces a non-empty unified diff."""
        original = "line1\nline2\nline3\n"
        variant = "line1\nmodified_line2\nline3\n"
        diff = _compute_diff(original, variant)
        assert len(diff) > 0
        assert "---" in diff
        assert "+++" in diff

    def test_diff_empty_for_identical(self):
        """_compute_diff produces empty string for identical code."""
        code = "def handler(): pass\n"
        diff = _compute_diff(code, code)
        assert diff == ""

    def test_variant_number_in_code(self):
        """Variant number appears in generated code."""
        code = _generate_variant_code("pass", "caching", 42)
        assert "Variant 42" in code


# ===========================================================================
# Test: Variant Manager
# ===========================================================================

class TestVariantManager:
    """Tests for variant storage, comparison, selection, and cleanup."""

    def test_store_and_retrieve_variant(self, db_conn):
        """Store a variant and retrieve it."""
        run_id = _create_run(db_conn)
        vid = _create_variant(db_conn, run_id)
        db_conn.commit()

        row = db_conn.execute(
            "SELECT * FROM erpclaw_dgm_variant WHERE id = ?", (vid,)
        ).fetchone()
        assert row is not None
        assert row["run_id"] == run_id
        assert row["is_selected"] == 0

    def test_compare_variants_sorted_by_score(self, db_conn):
        """compare_variants returns variants sorted by composite_score descending."""
        run_id = _create_run(db_conn)
        # Create variants with different exec times (lower = better score)
        _create_variant(db_conn, run_id, variant_number=1, exec_time_ms=200)
        _create_variant(db_conn, run_id, variant_number=2, exec_time_ms=50)
        _create_variant(db_conn, run_id, variant_number=3, exec_time_ms=100)
        db_conn.commit()

        variants = compare_variants(db_conn, run_id)
        assert len(variants) == 3
        # Best (fastest) should be first
        scores = [Decimal(v["composite_score"]) for v in variants]
        assert scores == sorted(scores, reverse=True)

    def test_select_best_marks_selected(self, db_conn):
        """select_best marks the best variant with is_selected=1."""
        run_id = _create_run(db_conn)
        vid1 = _create_variant(db_conn, run_id, variant_number=1, exec_time_ms=200)
        vid2 = _create_variant(db_conn, run_id, variant_number=2, exec_time_ms=50)
        db_conn.commit()

        result = select_best(db_conn, run_id)
        db_conn.commit()

        assert result is not None
        assert result["variant_id"] == vid2  # faster variant

        # Verify is_selected in DB
        row = db_conn.execute(
            "SELECT is_selected FROM erpclaw_dgm_variant WHERE id = ?",
            (vid2,),
        ).fetchone()
        assert row["is_selected"] == 1

        # Other variant should remain unselected
        row = db_conn.execute(
            "SELECT is_selected FROM erpclaw_dgm_variant WHERE id = ?",
            (vid1,),
        ).fetchone()
        assert row["is_selected"] == 0

    def test_select_best_creates_improvement_proposal(self, db_conn):
        """select_best creates an improvement_log entry with source='dgm'."""
        run_id = _create_run(db_conn)
        _create_variant(db_conn, run_id, variant_number=1, exec_time_ms=50)
        db_conn.commit()

        result = select_best(db_conn, run_id)
        db_conn.commit()

        assert result is not None
        improvement_id = result["improvement_id"]
        assert improvement_id is not None

        # Verify in DB
        row = db_conn.execute(
            "SELECT * FROM erpclaw_improvement_log WHERE id = ?",
            (improvement_id,),
        ).fetchone()
        assert row is not None
        assert row["source"] == "dgm"
        assert row["category"] == "performance"
        assert row["status"] == "proposed"
        assert "retailclaw" in row["module_name"]

    def test_select_best_no_qualifying_variant(self, db_conn):
        """select_best returns None when no variant has 100% test pass rate."""
        run_id = _create_run(db_conn)
        # All variants have failed tests
        _create_variant(db_conn, run_id, variant_number=1, test_pass_count=8, test_total=10)
        _create_variant(db_conn, run_id, variant_number=2, test_pass_count=9, test_total=10)
        db_conn.commit()

        result = select_best(db_conn, run_id)
        assert result is None

    def test_cleanup_old_variants(self, db_conn):
        """cleanup_old_variants removes unselected variants older than threshold."""
        run_id = _create_run(db_conn)
        vid1 = _create_variant(db_conn, run_id, variant_number=1)
        # Manually set created_at to 60 days ago
        db_conn.execute(
            "UPDATE erpclaw_dgm_variant SET created_at = datetime('now', '-60 days') WHERE id = ?",
            (vid1,),
        )
        vid2 = _create_variant(db_conn, run_id, variant_number=2)
        db_conn.commit()

        deleted = cleanup_old_variants(db_conn, days=30)
        db_conn.commit()

        assert deleted == 1  # Only the old one

        # vid1 should be gone, vid2 should remain
        assert db_conn.execute("SELECT id FROM erpclaw_dgm_variant WHERE id = ?", (vid1,)).fetchone() is None
        assert db_conn.execute("SELECT id FROM erpclaw_dgm_variant WHERE id = ?", (vid2,)).fetchone() is not None

    def test_cleanup_preserves_selected_variants(self, db_conn):
        """cleanup_old_variants does not delete selected variants."""
        run_id = _create_run(db_conn)
        vid = _create_variant(db_conn, run_id, variant_number=1)
        # Mark as selected and set old date
        db_conn.execute(
            "UPDATE erpclaw_dgm_variant SET is_selected = 1, created_at = datetime('now', '-60 days') WHERE id = ?",
            (vid,),
        )
        db_conn.commit()

        deleted = cleanup_old_variants(db_conn, days=30)
        db_conn.commit()

        assert deleted == 0
        assert db_conn.execute("SELECT id FROM erpclaw_dgm_variant WHERE id = ?", (vid,)).fetchone() is not None

    def test_get_variant_diff(self, db_conn):
        """get_variant_diff returns the variant details including diff."""
        run_id = _create_run(db_conn)
        vid = _create_variant(db_conn, run_id, variant_number=1)
        db_conn.commit()

        result = get_variant_diff(db_conn, vid)
        assert result is not None
        assert result["id"] == vid
        assert result["mutation_type"] == "caching"
        assert result["variant_diff"] is not None

    def test_get_variant_diff_not_found(self, db_conn):
        """get_variant_diff returns None for nonexistent variant."""
        result = get_variant_diff(db_conn, str(uuid.uuid4()))
        assert result is None


# ===========================================================================
# Test: dgm-run-variant Action
# ===========================================================================

class TestDgmRunVariant:
    """Tests for the dgm-run-variant action handler."""

    def test_successful_run(self, db_path):
        """Run DGM variant generation for a safe action."""
        args = _make_args(
            module_name_arg="retailclaw",
            module_name="retailclaw",
            action_name="list-products",
            variant_count=3,
            db_path=db_path,
        )
        result = handle_dgm_run_variant(args)
        assert result["result"] == "ok"
        assert result["run_id"]
        assert result["variant_count"] == 3
        assert len(result["variants"]) == 3
        assert result["status"] in ("completed", "no_improvement")

    def test_run_creates_db_records(self, db_path):
        """Run creates records in erpclaw_dgm_run and erpclaw_dgm_variant."""
        args = _make_args(
            module_name_arg="retailclaw",
            module_name="retailclaw",
            action_name="list-products",
            variant_count=2,
            db_path=db_path,
        )
        result = handle_dgm_run_variant(args)
        run_id = result["run_id"]

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        run_row = conn.execute("SELECT * FROM erpclaw_dgm_run WHERE id = ?", (run_id,)).fetchone()
        assert run_row is not None
        assert run_row["module_name"] == "retailclaw"
        assert run_row["variant_count"] == 2

        variant_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM erpclaw_dgm_variant WHERE run_id = ?", (run_id,)
        ).fetchone()["cnt"]
        assert variant_count == 2
        conn.close()

    def test_run_with_safety_exclusion(self, db_path):
        """Run rejects safety-excluded modules."""
        args = _make_args(
            module_name_arg="gl_posting",
            module_name="gl_posting",
            action_name="submit-journal",
            variant_count=3,
            db_path=db_path,
        )
        result = handle_dgm_run_variant(args)
        assert "error" in result
        assert result.get("safety_excluded") is True

    def test_run_missing_module_name(self, db_path):
        """Run requires --module-name."""
        args = _make_args(
            module_name_arg=None,
            module_name=None,
            action_name="list-products",
            variant_count=3,
            db_path=db_path,
        )
        result = handle_dgm_run_variant(args)
        assert "error" in result
        assert "module-name" in result["error"].lower()

    def test_run_missing_action_name(self, db_path):
        """Run requires --action-name."""
        args = _make_args(
            module_name_arg="retailclaw",
            module_name="retailclaw",
            action_name=None,
            variant_count=3,
            db_path=db_path,
        )
        result = handle_dgm_run_variant(args)
        assert "error" in result
        assert "action-name" in result["error"].lower()

    def test_run_metrics_populated(self, db_path):
        """Variants have exec_time_ms and memory_kb populated."""
        args = _make_args(
            module_name_arg="retailclaw",
            module_name="retailclaw",
            action_name="list-products",
            variant_count=2,
            db_path=db_path,
        )
        result = handle_dgm_run_variant(args)
        for v in result["variants"]:
            assert v["exec_time_ms"] is not None
            assert v["exec_time_ms"] > 0
            assert v["memory_kb"] is not None
            assert v["memory_kb"] > 0

    def test_run_status_lifecycle(self, db_path):
        """Run transitions from running to completed or no_improvement."""
        args = _make_args(
            module_name_arg="retailclaw",
            module_name="retailclaw",
            action_name="list-products",
            variant_count=3,
            db_path=db_path,
        )
        result = handle_dgm_run_variant(args)
        assert result["status"] in ("completed", "no_improvement")

        # Verify in DB
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        run_row = conn.execute(
            "SELECT status, completed_at FROM erpclaw_dgm_run WHERE id = ?",
            (result["run_id"],),
        ).fetchone()
        assert run_row["status"] in ("completed", "no_improvement")
        assert run_row["completed_at"] is not None
        conn.close()


# ===========================================================================
# Test: dgm-list-variants Action
# ===========================================================================

class TestDgmListVariants:
    """Tests for the dgm-list-variants action handler."""

    def test_list_by_run_id(self, db_path):
        """List variants for a specific run."""
        # First create a run
        run_args = _make_args(
            module_name_arg="retailclaw",
            module_name="retailclaw",
            action_name="list-products",
            variant_count=3,
            db_path=db_path,
        )
        run_result = handle_dgm_run_variant(run_args)
        run_id = run_result["run_id"]

        # List variants
        list_args = _make_args(
            run_id=run_id,
            module_name_arg=None,
            module_name=None,
            db_path=db_path,
        )
        result = handle_dgm_list_variants(list_args)
        assert result["result"] == "ok"
        assert result["count"] == 3
        assert len(result["variants"]) == 3
        assert result.get("run") is not None

    def test_list_by_module_name(self, db_path):
        """List variants filtered by module name."""
        run_args = _make_args(
            module_name_arg="retailclaw",
            module_name="retailclaw",
            action_name="list-products",
            variant_count=2,
            db_path=db_path,
        )
        handle_dgm_run_variant(run_args)

        list_args = _make_args(
            run_id=None,
            module_name_arg="retailclaw",
            module_name="retailclaw",
            db_path=db_path,
        )
        result = handle_dgm_list_variants(list_args)
        assert result["result"] == "ok"
        assert result["count"] == 2

    def test_list_requires_filter(self, db_path):
        """List requires at least --run-id or --module-name."""
        args = _make_args(
            run_id=None,
            module_name_arg=None,
            module_name=None,
            db_path=db_path,
        )
        result = handle_dgm_list_variants(args)
        assert "error" in result

    def test_list_empty_run(self, db_path):
        """List for nonexistent run returns empty."""
        args = _make_args(
            run_id=str(uuid.uuid4()),
            module_name_arg=None,
            module_name=None,
            db_path=db_path,
        )
        result = handle_dgm_list_variants(args)
        assert result["result"] == "ok"
        assert result["count"] == 0


# ===========================================================================
# Test: dgm-select-best Action
# ===========================================================================

class TestDgmSelectBest:
    """Tests for the dgm-select-best action handler."""

    def test_select_best_marks_variant(self, db_path):
        """dgm-select-best marks the correct variant."""
        # Create a run with variants (not auto-selected)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        setup_pragmas(conn)
        run_id = _create_run(conn)
        _create_variant(conn, run_id, variant_number=1, exec_time_ms=200)
        vid_best = _create_variant(conn, run_id, variant_number=2, exec_time_ms=50)
        conn.commit()
        conn.close()

        args = _make_args(run_id=run_id, db_path=db_path)
        result = handle_dgm_select_best(args)
        assert result["result"] == "ok"
        assert result["best_variant_id"] == vid_best
        assert result["status"] == "completed"
        assert result["improvement_id"] is not None

    def test_select_best_no_improvement(self, db_path):
        """dgm-select-best with all-failing variants returns no_improvement."""
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        setup_pragmas(conn)
        run_id = _create_run(conn)
        _create_variant(conn, run_id, variant_number=1, test_pass_count=8, test_total=10)
        conn.commit()
        conn.close()

        args = _make_args(run_id=run_id, db_path=db_path)
        result = handle_dgm_select_best(args)
        assert result["result"] == "ok"
        assert result["status"] == "no_improvement"

    def test_select_best_missing_run_id(self, db_path):
        """dgm-select-best requires --run-id."""
        args = _make_args(run_id=None, db_path=db_path)
        result = handle_dgm_select_best(args)
        assert "error" in result

    def test_select_best_nonexistent_run(self, db_path):
        """dgm-select-best with nonexistent run returns error."""
        args = _make_args(run_id=str(uuid.uuid4()), db_path=db_path)
        result = handle_dgm_select_best(args)
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_select_best_already_selected(self, db_path):
        """dgm-select-best on a run with existing selection returns already_selected."""
        # First run and auto-select
        run_args = _make_args(
            module_name_arg="retailclaw",
            module_name="retailclaw",
            action_name="list-products",
            variant_count=2,
            db_path=db_path,
        )
        run_result = handle_dgm_run_variant(run_args)

        if run_result.get("best_variant_id"):
            # Re-select
            args = _make_args(run_id=run_result["run_id"], db_path=db_path)
            result = handle_dgm_select_best(args)
            assert result["result"] == "ok"
            assert result.get("already_selected") is True
