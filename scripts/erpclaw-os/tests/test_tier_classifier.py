#!/usr/bin/env python3
"""Tests for ERPClaw OS Tier Classification System (Deliverable 2a)."""
import json
import os
import sqlite3
import sys
import tempfile
import uuid

import pytest

# Add erpclaw-os to path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OS_DIR = os.path.dirname(SCRIPT_DIR)
if OS_DIR not in sys.path:
    sys.path.insert(0, OS_DIR)

from tier_classifier import (
    TIER_0, TIER_1, TIER_2, TIER_3,
    TIER_NAMES, TIER_DESCRIPTIONS,
    classify_action, classify_all_actions,
    classify_with_override, classify_with_persistence,
    get_persisted_classification, ensure_tier_table,
    handle_classify_operation, _load_action_map,
)


# ---------------------------------------------------------------------------
# Tier 0: Read-only operations
# ---------------------------------------------------------------------------

class TestTier0ReadOnly:
    """Verify read-only operations are classified as Tier 0."""

    @pytest.mark.parametrize("action", [
        "list-customers",
        "list-sales-invoices",
        "list-payments",
        "list-accounts",
        "list-modules",
        "list-items",
        "list-suppliers",
        "list-employees",
        "list-leads",
    ])
    def test_list_actions_tier_0(self, action):
        result = classify_action(action)
        assert result["tier"] == TIER_0, f"{action} should be Tier 0, got {result['tier']}: {result['reasoning']}"

    @pytest.mark.parametrize("action", [
        "get-customer",
        "get-sales-invoice",
        "get-payment",
        "get-account",
        "get-item",
        "get-employee",
    ])
    def test_get_actions_tier_0(self, action):
        result = classify_action(action)
        assert result["tier"] == TIER_0

    @pytest.mark.parametrize("action", [
        "search-modules",
        "available-modules",
        "module-status",
        "list-profiles",
        "list-articles",
        "list-industries",
        "list-all-actions",
        "build-table-registry",
    ])
    def test_utility_read_actions_tier_0(self, action):
        result = classify_action(action)
        assert result["tier"] == TIER_0

    def test_classify_operation_is_tier_0(self):
        result = classify_action("classify-operation")
        assert result["tier"] == TIER_0

    def test_deploy_audit_log_is_tier_0(self):
        result = classify_action("deploy-audit-log")
        assert result["tier"] == TIER_0

    def test_schema_drift_is_tier_0(self):
        result = classify_action("schema-drift")
        assert result["tier"] == TIER_0

    def test_compliance_weather_status_is_tier_0(self):
        result = classify_action("compliance-weather-status")
        assert result["tier"] == TIER_0


# ---------------------------------------------------------------------------
# Tier 1: Guardrailed write operations
# ---------------------------------------------------------------------------

class TestTier1GuardrailedWrites:
    """Verify write operations with validation are Tier 1."""

    @pytest.mark.parametrize("action", [
        "submit-sales-invoice",
        "submit-purchase-invoice",
        "submit-payment",
        "submit-journal-entry",
        "submit-payroll-run",
    ])
    def test_submit_actions_tier_1(self, action):
        result = classify_action(action)
        assert result["tier"] == TIER_1

    @pytest.mark.parametrize("action", [
        "cancel-sales-invoice",
        "cancel-purchase-invoice",
        "cancel-payment",
    ])
    def test_cancel_actions_tier_1(self, action):
        result = classify_action(action)
        assert result["tier"] == TIER_1

    @pytest.mark.parametrize("action", [
        "add-customer",
        "add-supplier",
        "add-item",
        "add-employee",
        "add-account",
        "add-payment",
        "add-journal-entry",
    ])
    def test_add_actions_tier_1(self, action):
        result = classify_action(action)
        assert result["tier"] == TIER_1

    @pytest.mark.parametrize("action", [
        "update-customer",
        "update-item",
        "update-employee",
    ])
    def test_update_actions_tier_1(self, action):
        result = classify_action(action)
        assert result["tier"] == TIER_1

    @pytest.mark.parametrize("action", [
        "create-sales-invoice",
        "create-delivery-note",
        "create-credit-note",
        "create-purchase-receipt",
    ])
    def test_create_actions_tier_1(self, action):
        result = classify_action(action)
        assert result["tier"] == TIER_1

    @pytest.mark.parametrize("action", [
        "delete-customer",
        "delete-item",
    ])
    def test_delete_actions_tier_1(self, action):
        result = classify_action(action)
        assert result["tier"] == TIER_1

    def test_setup_company_tier_1(self):
        result = classify_action("setup-company")
        assert result["tier"] == TIER_1

    def test_onboard_tier_1(self):
        result = classify_action("onboard")
        assert result["tier"] == TIER_1

    def test_seed_demo_data_tier_1(self):
        result = classify_action("seed-demo-data")
        assert result["tier"] == TIER_1

    @pytest.mark.parametrize("action", [
        "approve-leave",
        "approve-expense",
        "reject-leave",
    ])
    def test_approval_actions_tier_1(self, action):
        result = classify_action(action)
        assert result["tier"] == TIER_1


# ---------------------------------------------------------------------------
# Tier 2: Human-approved operations
# ---------------------------------------------------------------------------

class TestTier2HumanApproved:
    """Verify schema/module operations are Tier 2."""

    @pytest.mark.parametrize("action", [
        "generate-module",
        "configure-module",
        "install-module",
        "remove-module",
        "update-modules",
        "install-suite",
        "deploy-module",
        "validate-module",
    ])
    def test_module_lifecycle_tier_2(self, action):
        result = classify_action(action)
        assert result["tier"] == TIER_2, f"{action} should be Tier 2, got {result['tier']}"

    @pytest.mark.parametrize("action", [
        "schema-plan",
        "schema-apply",
        "schema-rollback",
    ])
    def test_schema_operations_tier_2(self, action):
        result = classify_action(action)
        assert result["tier"] == TIER_2

    def test_run_audit_tier_2(self):
        result = classify_action("run-audit")
        assert result["tier"] == TIER_2

    def test_regenerate_skill_md_tier_2(self):
        result = classify_action("regenerate-skill-md")
        assert result["tier"] == TIER_2


# ---------------------------------------------------------------------------
# Tier 3: Human-only (content-based escalation)
# ---------------------------------------------------------------------------

class TestTier3HumanOnly:
    """Verify core-modifying operations are Tier 3."""

    def test_drop_table_escalation(self):
        """Code containing DROP TABLE should escalate to Tier 3."""
        result = classify_action(
            "custom-action",
            action_code="def handle(): conn.execute('DROP TABLE customers')"
        )
        assert result["tier"] == TIER_3
        assert "DROP TABLE" in result["reasoning"]

    def test_gl_posting_modification_escalation(self):
        """Code modifying gl_posting.py should escalate to Tier 3."""
        result = classify_action(
            "custom-action",
            action_code="import gl_posting.py as gl; gl.modify_pipeline()"
        )
        assert result["tier"] == TIER_3

    def test_cross_skill_modification_escalation(self):
        """Code modifying cross_skill.py should escalate to Tier 3."""
        result = classify_action(
            "custom-action",
            action_code="# patching cross_skill.py to bypass validation"
        )
        assert result["tier"] == TIER_3

    def test_no_escalation_without_patterns(self):
        """Normal code should not escalate."""
        result = classify_action(
            "add-widget",
            action_code="def handle(): conn.execute('INSERT INTO widgets VALUES (?)', (name,))"
        )
        assert result["tier"] == TIER_1  # add-* pattern, no escalation


# ---------------------------------------------------------------------------
# Namespaced actions (module prefixes)
# ---------------------------------------------------------------------------

class TestNamespacedActions:
    """Verify namespaced actions inherit base tier."""

    @pytest.mark.parametrize("action,expected_tier", [
        ("dental-list-patients", TIER_0),
        ("dental-add-patient", TIER_1),
        ("dental-get-patient", TIER_0),
        ("vet-add-pet", TIER_1),
        ("vet-list-pets", TIER_0),
        ("legal-add-matter", TIER_1),
        ("legal-list-matters", TIER_0),
        ("retail-add-item", TIER_1),
        ("retail-list-items", TIER_0),
        ("auto-submit-repair-order", TIER_1),
        ("food-list-recipes", TIER_0),
        ("groom-add-appointment", TIER_1),
    ])
    def test_namespaced_actions(self, action, expected_tier):
        result = classify_action(action, module_name="non-core")
        assert result["tier"] == expected_tier, (
            f"{action} should be Tier {expected_tier}, got {result['tier']}: {result['reasoning']}"
        )


# ---------------------------------------------------------------------------
# Classify all actions from ACTION_MAP
# ---------------------------------------------------------------------------

class TestClassifyAll:
    """Verify bulk classification of all actions."""

    def test_classify_all_no_unclassified(self):
        """Every action must have a classification."""
        action_map = {
            "list-customers": "erpclaw-selling",
            "add-customer": "erpclaw-selling",
            "submit-sales-invoice": "erpclaw-billing",
            "generate-module": "erpclaw-os",
            "get-item": "erpclaw-inventory",
        }
        result = classify_all_actions(action_map)
        assert result["total"] == 5
        assert len(result["unclassified"]) == 0
        assert result["summary"]["tier_0_read_only"] == 2  # list + get
        assert result["summary"]["tier_1_guardrailed"] == 2  # add + submit
        assert result["summary"]["tier_2_human_approved"] == 1  # generate

    def test_classify_real_action_map(self):
        """If core ACTION_MAP is loadable, classify all real actions."""
        action_map = _load_action_map()
        if action_map is None:
            pytest.skip("Could not load ACTION_MAP from core db_query.py")

        result = classify_all_actions(action_map)

        # All actions must be classified
        assert len(result["unclassified"]) == 0
        assert result["total"] == len(action_map)

        # Sanity: should have a mix of tiers
        assert result["summary"]["tier_0_read_only"] > 0, "Should have Tier 0 actions"
        assert result["summary"]["tier_1_guardrailed"] > 0, "Should have Tier 1 actions"
        assert result["summary"]["tier_2_human_approved"] > 0, "Should have Tier 2 actions"

        # Verify specific well-known actions
        by_name = {c["action_name"]: c for c in result["classifications"]}
        if "list-customers" in by_name:
            assert by_name["list-customers"]["tier"] == TIER_0
        if "submit-sales-invoice" in by_name:
            assert by_name["submit-sales-invoice"]["tier"] == TIER_1
        if "generate-module" in by_name:
            assert by_name["generate-module"]["tier"] == TIER_2


# ---------------------------------------------------------------------------
# Human overrides + persistence
# ---------------------------------------------------------------------------

class TestHumanOverrides:
    """Verify human override system."""

    @pytest.fixture
    def db_path(self, tmp_path):
        """Create a temp DB with the tier table."""
        path = str(tmp_path / "test_tier.sqlite")
        ensure_tier_table(path)
        return path

    def test_override_changes_tier(self, db_path):
        """Human override should change the tier."""
        result = classify_with_override(
            action_name="list-customers",
            module_name="erpclaw-selling",
            tier_override=TIER_2,
            override_by="admin",
            override_reason="Contains sensitive PII queries",
            db_path=db_path,
        )
        assert result["tier"] == TIER_2
        assert result["auto_tier"] == TIER_0  # Original auto-classification
        assert result["override_by"] == "admin"

    def test_override_persists_to_db(self, db_path):
        """Override should be persisted and retrieved."""
        classify_with_override(
            action_name="list-customers",
            module_name="erpclaw-selling",
            tier_override=TIER_2,
            override_by="admin",
            override_reason="Contains sensitive PII queries",
            db_path=db_path,
        )

        persisted = get_persisted_classification("list-customers", db_path)
        assert persisted is not None
        assert persisted["tier"] == TIER_2
        assert persisted["override_by"] == "admin"

    def test_persisted_override_takes_precedence(self, db_path):
        """Classify with persistence should use DB override first."""
        # Set override
        classify_with_override(
            action_name="list-customers",
            module_name="erpclaw-selling",
            tier_override=TIER_2,
            override_by="admin",
            override_reason="PII concern",
            db_path=db_path,
        )

        # Now classify with persistence — should use override
        result = classify_with_persistence("list-customers", "erpclaw-selling", db_path)
        assert result["tier"] == TIER_2
        assert result["source"] == "human_override"

    def test_auto_classification_persists(self, db_path):
        """Auto-classification should also persist."""
        result = classify_with_persistence("add-item", "erpclaw-inventory", db_path)
        assert result["tier"] == TIER_1
        assert result["source"] == "auto"

        persisted = get_persisted_classification("add-item", db_path)
        assert persisted is not None
        assert persisted["tier"] == TIER_1

    def test_invalid_override_tier(self):
        """Invalid tier should return error."""
        result = classify_with_override(
            "list-customers", "erpclaw", 5, "admin", "test"
        )
        assert "error" in result

    def test_override_without_by_returns_error(self):
        """Override without override_by should fail."""
        from types import SimpleNamespace
        args = SimpleNamespace(
            action_name="list-customers",
            module_name_arg="erpclaw",
            classify_all=False,
            override_tier=2,
            override_by=None,
            override_reason="test",
            db_path=None,
        )
        result = handle_classify_operation(args)
        assert "error" in result


# ---------------------------------------------------------------------------
# Result structure
# ---------------------------------------------------------------------------

class TestResultStructure:
    """Verify classification result format."""

    def test_result_has_required_fields(self):
        result = classify_action("list-customers")
        assert "action_name" in result
        assert "module_name" in result
        assert "tier" in result
        assert "tier_name" in result
        assert "reasoning" in result

    def test_tier_names_are_valid(self):
        for tier in (TIER_0, TIER_1, TIER_2, TIER_3):
            assert tier in TIER_NAMES
            assert tier in TIER_DESCRIPTIONS

    def test_unknown_action_defaults_to_tier_1(self):
        """Unknown action patterns default to Tier 1 (guardrailed)."""
        result = classify_action("xyzzy-frobnicate")
        assert result["tier"] == TIER_1
        assert "default" in result["reasoning"].lower() or "unknown" in result["reasoning"].lower()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_empty_action_name(self):
        result = classify_action("")
        assert result["tier"] == TIER_1  # Default

    def test_none_module_name(self):
        result = classify_action("list-stuff", module_name=None)
        assert result["module_name"] == "unknown"

    def test_generate_not_generate_module(self):
        """generate-salary-slips is Tier 1, generate-module is Tier 2."""
        r1 = classify_action("generate-salary-slips")
        r2 = classify_action("generate-module")
        assert r1["tier"] == TIER_1
        assert r2["tier"] == TIER_2

    def test_escalation_only_increases_tier(self):
        """Content escalation should only increase tier, never decrease."""
        # A Tier 2 action with DROP TABLE should go to Tier 3
        result = classify_action(
            "schema-apply",
            action_code="conn.execute('DROP TABLE old_table')"
        )
        assert result["tier"] == TIER_3

    def test_case_sensitivity(self):
        """Action names should be treated as-is (kebab-case, lowercase)."""
        result = classify_action("list-customers")
        assert result["tier"] == TIER_0


# ---------------------------------------------------------------------------
# Action handler integration
# ---------------------------------------------------------------------------

class TestHandleClassifyOperation:
    """Test the action handler that bridges CLI → classifier."""

    def test_classify_single_action(self):
        from types import SimpleNamespace
        args = SimpleNamespace(
            action_name="list-customers",
            module_name_arg=None,
            classify_all=False,
            override_tier=None,
            override_by=None,
            override_reason=None,
            db_path=None,
        )
        result = handle_classify_operation(args)
        assert result["tier"] == TIER_0

    def test_classify_all_flag(self):
        from types import SimpleNamespace
        args = SimpleNamespace(
            action_name=None,
            module_name_arg=None,
            classify_all=True,
            override_tier=None,
            override_by=None,
            override_reason=None,
            db_path=None,
        )
        result = handle_classify_operation(args)
        # If ACTION_MAP loaded, should have classifications
        if "error" not in result:
            assert "classifications" in result
            assert result["total"] > 0

    def test_no_action_no_all_returns_error(self):
        from types import SimpleNamespace
        args = SimpleNamespace(
            action_name=None,
            module_name_arg=None,
            classify_all=False,
            override_tier=None,
            override_by=None,
            override_reason=None,
            db_path=None,
        )
        result = handle_classify_operation(args)
        assert "error" in result
