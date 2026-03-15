#!/usr/bin/env python3
"""Tests for ERPClaw OS install-suite (Deliverable 2d).

Tests dependency resolution, topological sort, circular dependency detection,
prefix collision detection, and suite definitions.
"""
import json
import os
import sys

import pytest

# Add erpclaw-os directory to path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OS_DIR = os.path.dirname(SCRIPT_DIR)
if OS_DIR not in sys.path:
    sys.path.insert(0, OS_DIR)

from dependency_resolver import (
    detect_circular_deps,
    detect_prefix_collisions,
    load_registry,
    resolve_install_order,
)
from install_suite import (
    SUITE_DEFINITIONS,
    handle_install_suite,
    install_suite,
    list_suites,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def registry():
    """Load the real module registry."""
    return load_registry()


@pytest.fixture
def mock_registry():
    """Create a mock registry for testing edge cases."""
    return {
        "core": {"display_name": "Core", "requires": [], "has_init_db": False},
        "mod-a": {"display_name": "Mod A", "requires": ["core"], "has_init_db": True},
        "mod-b": {"display_name": "Mod B", "requires": ["core"], "has_init_db": True},
        "mod-c": {"display_name": "Mod C", "requires": ["core", "mod-a"], "has_init_db": True},
        "mod-d": {"display_name": "Mod D", "requires": ["mod-c"], "has_init_db": True},
    }


@pytest.fixture
def circular_registry():
    """Registry with circular dependencies."""
    return {
        "mod-x": {"display_name": "Mod X", "requires": ["mod-y"]},
        "mod-y": {"display_name": "Mod Y", "requires": ["mod-z"]},
        "mod-z": {"display_name": "Mod Z", "requires": ["mod-x"]},
    }


# ---------------------------------------------------------------------------
# Dependency Resolution Tests
# ---------------------------------------------------------------------------

class TestResolveInstallOrder:
    """Test topological sort for install order."""

    def test_single_module_no_deps(self, mock_registry):
        result = resolve_install_order(["core"], registry=mock_registry)
        assert result["errors"] == []
        assert result["order"] == ["core"]

    def test_module_with_dep(self, mock_registry):
        result = resolve_install_order(["mod-a"], registry=mock_registry)
        assert result["errors"] == []
        # core must come before mod-a
        order = result["order"]
        assert order.index("core") < order.index("mod-a")
        assert "core" in result["added_dependencies"]

    def test_deep_dependency_chain(self, mock_registry):
        result = resolve_install_order(["mod-d"], registry=mock_registry)
        assert result["errors"] == []
        order = result["order"]
        # core → mod-a → mod-c → mod-d
        assert order.index("core") < order.index("mod-a")
        assert order.index("mod-a") < order.index("mod-c")
        assert order.index("mod-c") < order.index("mod-d")

    def test_multiple_modules_shared_dep(self, mock_registry):
        result = resolve_install_order(["mod-a", "mod-b"], registry=mock_registry)
        assert result["errors"] == []
        order = result["order"]
        # core comes first (shared dep), then a and b in any order
        assert order[0] == "core"
        assert set(order[1:]) == {"mod-a", "mod-b"}

    def test_already_includes_deps(self, mock_registry):
        result = resolve_install_order(["core", "mod-a"], registry=mock_registry)
        assert result["errors"] == []
        assert result["added_dependencies"] == []  # core was explicitly requested

    def test_unknown_module(self, mock_registry):
        result = resolve_install_order(["nonexistent"], registry=mock_registry)
        assert len(result["errors"]) > 0
        assert "not found" in result["errors"][0]

    def test_educlaw_full_stack(self, registry):
        """Resolve educlaw with all sub-verticals in correct order."""
        modules = [
            "educlaw", "educlaw-highered", "educlaw-finaid",
            "educlaw-scheduling", "educlaw-lms", "educlaw-statereport",
        ]
        result = resolve_install_order(modules, registry=registry)
        assert result["errors"] == []
        order = result["order"]

        # erpclaw must come first (dependency of educlaw)
        assert order[0] == "erpclaw"
        # educlaw must come before all sub-verticals
        educlaw_idx = order.index("educlaw")
        for sub in ["educlaw-highered", "educlaw-finaid", "educlaw-scheduling",
                     "educlaw-lms", "educlaw-statereport"]:
            assert order.index(sub) > educlaw_idx, f"{sub} must come after educlaw"

        # educlaw-statereport requires educlaw-k12, which should be auto-added
        if "educlaw-k12" in order:
            assert order.index("educlaw-k12") < order.index("educlaw-statereport")

    def test_healthclaw_full_stack(self, registry):
        """Resolve healthclaw with all sub-verticals."""
        modules = ["healthclaw", "healthclaw-dental", "healthclaw-vet",
                    "healthclaw-mental", "healthclaw-homehealth"]
        result = resolve_install_order(modules, registry=registry)
        assert result["errors"] == []
        order = result["order"]

        # erpclaw → healthclaw → sub-verticals
        assert order.index("erpclaw") < order.index("healthclaw")
        for sub in ["healthclaw-dental", "healthclaw-vet",
                     "healthclaw-mental", "healthclaw-homehealth"]:
            assert order.index(sub) > order.index("healthclaw")


class TestCircularDeps:
    """Test circular dependency detection."""

    def test_no_cycles_in_normal_registry(self, mock_registry):
        modules = list(mock_registry.keys())
        cycles = detect_circular_deps(modules, registry=mock_registry)
        assert cycles == []

    def test_detects_cycle(self, circular_registry):
        modules = list(circular_registry.keys())
        cycles = detect_circular_deps(modules, registry=circular_registry)
        assert len(cycles) > 0

    def test_no_cycles_in_real_registry(self, registry):
        """The real module registry must have no circular dependencies."""
        modules = list(registry.keys())
        cycles = detect_circular_deps(modules, registry=registry)
        assert cycles == [], f"Real registry has cycles: {cycles}"


class TestPrefixCollisions:
    """Test prefix collision detection."""

    def test_no_collisions_normal(self):
        registry = {
            "healthclaw": {},
            "educlaw": {},
            "retailclaw": {},
        }
        collisions = detect_prefix_collisions(
            ["healthclaw", "educlaw", "retailclaw"], registry=registry
        )
        assert collisions == []

    def test_detects_collision(self):
        """Two modules with same prefix base should collide."""
        registry = {
            "testclaw": {},
            "testclaw-addon": {},
        }
        collisions = detect_prefix_collisions(
            ["testclaw", "testclaw-addon"], registry=registry
        )
        # Both would derive prefix "test"
        assert len(collisions) > 0


# ---------------------------------------------------------------------------
# Suite Installation Tests
# ---------------------------------------------------------------------------

class TestInstallSuite:
    """Test suite installation orchestration."""

    def test_healthcare_full_suite(self):
        result = install_suite(suite_name="healthcare-full", dry_run=True)
        assert result["result"] == "planned"
        assert result["total_modules"] >= 5
        # healthclaw and all sub-verticals present
        order = result["install_order"]
        assert "healthclaw" in order
        assert "healthclaw-dental" in order

    def test_university_suite(self):
        result = install_suite(suite_name="university", dry_run=True)
        assert result["result"] == "planned"
        order = result["install_order"]
        assert "educlaw" in order
        assert "educlaw-highered" in order
        assert "educlaw-finaid" in order

    def test_enterprise_suite(self):
        result = install_suite(suite_name="enterprise", dry_run=True)
        assert result["result"] == "planned"
        assert "erpclaw-growth" in result["install_order"]

    def test_unknown_suite(self):
        result = install_suite(suite_name="nonexistent")
        assert result["result"] == "error"
        assert "available_suites" in result

    def test_custom_modules(self):
        result = install_suite(modules="healthclaw,retailclaw", dry_run=True)
        assert result["result"] == "planned"
        assert "healthclaw" in result["install_order"]
        assert "retailclaw" in result["install_order"]
        # erpclaw should be auto-added as dependency
        assert "erpclaw" in result["install_order"]

    def test_no_suite_no_modules(self):
        result = install_suite()
        assert result["result"] == "error"
        assert "available_suites" in result

    def test_install_plan_structure(self):
        result = install_suite(suite_name="retail", dry_run=True)
        for item in result["install_plan"]:
            assert "module" in item
            assert "display_name" in item
            assert "has_init_db" in item
            assert "is_dependency" in item

    def test_added_dependencies_flagged(self):
        result = install_suite(modules="healthclaw-dental", dry_run=True)
        assert "erpclaw" in result["added_dependencies"]
        assert "healthclaw" in result["added_dependencies"]
        # In plan, auto-added modules should be flagged
        for item in result["install_plan"]:
            if item["module"] in ("erpclaw", "healthclaw"):
                assert item["is_dependency"] is True


class TestListSuites:
    """Test suite listing."""

    def test_list_suites(self):
        suites = list_suites()
        assert len(suites) >= 5
        names = [s["name"] for s in suites]
        assert "healthcare-full" in names
        assert "university" in names
        assert "enterprise" in names

    def test_suite_structure(self):
        suites = list_suites()
        for suite in suites:
            assert "name" in suite
            assert "display_name" in suite
            assert "description" in suite
            assert "module_count" in suite
            assert "modules" in suite
            assert suite["module_count"] > 0


class TestSuiteDefinitions:
    """Validate all predefined suite definitions."""

    def test_all_suites_resolve(self):
        """Every predefined suite must resolve without errors."""
        registry = load_registry()
        for suite_name, suite_info in SUITE_DEFINITIONS.items():
            result = resolve_install_order(suite_info["modules"], registry=registry)
            assert result["errors"] == [], \
                f"Suite '{suite_name}' has resolution errors: {result['errors']}"

    def test_all_suite_modules_exist(self):
        """Every module in every suite must exist in the registry."""
        registry = load_registry()
        for suite_name, suite_info in SUITE_DEFINITIONS.items():
            for mod in suite_info["modules"]:
                assert mod in registry, \
                    f"Suite '{suite_name}' references unknown module '{mod}'"


# ---------------------------------------------------------------------------
# CLI Handler Tests
# ---------------------------------------------------------------------------

class TestHandleInstallSuite:
    """Test CLI handler."""

    def test_no_args_lists_suites(self):
        class Args:
            suite = None
            modules = None
            dry_run = False
        result = handle_install_suite(Args())
        assert result["result"] == "ok"
        assert "suites" in result

    def test_with_suite(self):
        class Args:
            suite = "healthcare-dental"
            modules = None
            dry_run = True
        result = handle_install_suite(Args())
        assert result["result"] in ("planned", "ready")

    def test_with_modules(self):
        class Args:
            suite = None
            modules = "retailclaw,foodclaw"
            dry_run = True
        result = handle_install_suite(Args())
        assert result["result"] in ("planned", "ready")
        assert "retailclaw" in result["install_order"]
