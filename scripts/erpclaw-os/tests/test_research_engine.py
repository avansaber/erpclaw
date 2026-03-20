"""Tests for ERPClaw OS Research Engine (P1-8).

Covers:
- research_business_rule: knowledge base lookup, alias resolution, not-found handling
- KNOWLEDGE_BASE: completeness (all 22 entries), required fields
- get_implementation_guide: combines knowledge base + pattern library
- list_knowledge_base: returns all entries
- handle_research_rule: action handler wiring
"""
import os
import sys

import pytest

# Make the erpclaw-os package importable
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
OS_DIR = os.path.dirname(TESTS_DIR)
if OS_DIR not in sys.path:
    sys.path.insert(0, OS_DIR)

from research_engine import (
    KNOWLEDGE_BASE,
    _ALIASES,
    research_business_rule,
    get_implementation_guide,
    list_knowledge_base,
    handle_research_rule,
    handle_get_implementation_guide,
    _resolve_topic,
)


# ---------------------------------------------------------------------------
# Test: Knowledge Base Completeness
# ---------------------------------------------------------------------------

class TestKnowledgeBase:
    """Verify the KNOWLEDGE_BASE has all 22 expected entries with correct structure."""

    EXPECTED_KEYS = [
        "fifo_valuation",
        "three_way_match",
        "overtime_flsa",
        "nacha_ach",
        "blanket_orders",
        "document_close",
        "document_amendment",
        "recurring_billing",
        "multi_uom",
        "item_variants",
        "stock_projected_qty",
        "material_substitution",
        "co_products",
        "make_vs_buy",
        "grn_tolerance",
        "shift_management",
        "multi_state_payroll",
        "supplemental_wages",
        "retro_pay",
        "leave_carry_forward",
        "depreciation_straight_line",
        "bank_reconciliation",
    ]

    def test_knowledge_base_has_core_features(self):
        """All 22 core business rules exist in the knowledge base (verticals add more)."""
        for key in self.EXPECTED_KEYS:
            assert key in KNOWLEDGE_BASE, f"Missing knowledge base entry: {key}"
        assert len(KNOWLEDGE_BASE) >= 22, (
            f"Expected at least 22 entries, got {len(KNOWLEDGE_BASE)}"
        )

    def test_all_entries_have_required_fields(self):
        """Every entry has summary, source, implementation_hints, related_patterns."""
        for key, entry in KNOWLEDGE_BASE.items():
            assert "summary" in entry, f"{key} missing 'summary'"
            assert "source" in entry, f"{key} missing 'source'"
            assert "implementation_hints" in entry, f"{key} missing 'implementation_hints'"
            assert "related_patterns" in entry, f"{key} missing 'related_patterns'"

    def test_summaries_are_non_empty(self):
        """Every summary is a non-empty string."""
        for key, entry in KNOWLEDGE_BASE.items():
            assert isinstance(entry["summary"], str), f"{key} summary is not a string"
            assert len(entry["summary"]) > 10, f"{key} summary is too short"

    def test_sources_are_non_empty(self):
        """Every source is a non-empty string."""
        for key, entry in KNOWLEDGE_BASE.items():
            assert isinstance(entry["source"], str), f"{key} source is not a string"
            assert len(entry["source"]) > 0, f"{key} source is empty"

    def test_related_patterns_is_list(self):
        """Every related_patterns is a list."""
        for key, entry in KNOWLEDGE_BASE.items():
            assert isinstance(entry["related_patterns"], list), (
                f"{key} related_patterns is not a list"
            )


# ---------------------------------------------------------------------------
# Test: research_business_rule
# ---------------------------------------------------------------------------

class TestResearchBusinessRule:
    def test_research_fifo(self):
        """Look up FIFO inventory valuation by canonical key — returns correct rule."""
        result = research_business_rule("fifo_valuation")
        assert result["found"] is True
        assert result["canonical_key"] == "fifo_valuation"
        assert "First In First Out" in result["rule_summary"]
        assert "ASC 330" in result["source"]
        assert len(result["implementation_hints"]) > 0

    def test_research_fifo_natural_language(self):
        """Look up FIFO using natural language alias."""
        result = research_business_rule("FIFO inventory valuation")
        assert result["found"] is True
        assert result["canonical_key"] == "fifo_valuation"

    def test_research_flsa_overtime(self):
        """Look up FLSA overtime rules."""
        result = research_business_rule("FLSA overtime rules")
        assert result["found"] is True
        assert result["canonical_key"] == "overtime_flsa"
        assert "1.5x" in result["rule_summary"]

    def test_research_nacha(self):
        """Look up NACHA ACH file format."""
        result = research_business_rule("NACHA ACH file format")
        assert result["found"] is True
        assert result["canonical_key"] == "nacha_ach"

    def test_research_three_way_match(self):
        """Look up three-way match."""
        result = research_business_rule("three way match")
        assert result["found"] is True
        assert result["canonical_key"] == "three_way_match"
        assert "invoice_qty" in result["rule_summary"]

    def test_research_unknown_topic(self):
        """Unknown topic returns found=False gracefully — no exception, no crash."""
        result = research_business_rule("quantum entanglement valuation")
        assert result["found"] is False
        assert "topic" in result
        assert "message" in result

    def test_research_empty_topic(self):
        """Empty topic returns found=False."""
        result = research_business_rule("")
        assert result["found"] is False

    def test_research_none_topic(self):
        """None topic returns found=False."""
        result = research_business_rule(None)
        assert result["found"] is False

    def test_research_by_alias_ach(self):
        """Look up by short alias 'ach'."""
        result = research_business_rule("ach")
        assert result["found"] is True
        assert result["canonical_key"] == "nacha_ach"

    def test_research_by_alias_blanket(self):
        """Look up 'blanket order' via alias."""
        result = research_business_rule("blanket order")
        assert result["found"] is True
        assert result["canonical_key"] == "blanket_orders"

    def test_research_by_alias_depreciation(self):
        """Look up depreciation via alias."""
        result = research_business_rule("depreciation")
        assert result["found"] is True
        assert result["canonical_key"] == "depreciation_straight_line"

    def test_research_by_alias_bank_recon(self):
        """Look up bank reconciliation via alias."""
        result = research_business_rule("bank recon")
        assert result["found"] is True
        assert result["canonical_key"] == "bank_reconciliation"

    def test_research_returns_related_patterns(self):
        """FIFO result includes related_patterns."""
        result = research_business_rule("fifo_valuation")
        assert result["found"] is True
        assert isinstance(result["related_patterns"], list)
        assert "fifo_layer" in result["related_patterns"]

    def test_research_case_insensitive(self):
        """Lookup is case-insensitive."""
        result = research_business_rule("FIFO Valuation")
        assert result["found"] is True

    def test_research_with_domain_hint(self):
        """Domain hint is accepted (used for future web search, not KB filtering)."""
        result = research_business_rule("fifo_valuation", domain="inventory")
        assert result["found"] is True
        assert result["canonical_key"] == "fifo_valuation"


# ---------------------------------------------------------------------------
# Test: get_implementation_guide
# ---------------------------------------------------------------------------

class TestGetImplementationGuide:
    def test_get_implementation_guide_combines_sources(self):
        """Guide for three_way_match combines knowledge base rule + pattern."""
        guide = get_implementation_guide("three_way_match")
        assert guide["found"] is True
        assert guide["business_rule"] is not None
        assert "summary" in guide["business_rule"]
        assert guide["pattern"] is not None
        assert guide["pattern"]["key"] == "three_way_match"
        assert "run-match" in guide["pattern"]["actions"]

    def test_guide_has_code_template(self):
        """Guide includes a code template skeleton."""
        guide = get_implementation_guide("three_way_match")
        assert guide["code_template"] is not None
        assert "def handle_" in guide["code_template"]

    def test_guide_has_test_template(self):
        """Guide includes a test template skeleton."""
        guide = get_implementation_guide("three_way_match")
        assert guide["test_template"] is not None
        assert "class Test" in guide["test_template"]
        assert "happy_path" in guide["test_template"]
        assert "missing_required" in guide["test_template"]

    def test_guide_fifo_no_direct_pattern(self):
        """FIFO has a knowledge base entry but no direct pattern in pattern_library."""
        guide = get_implementation_guide("fifo_valuation")
        assert guide["found"] is True
        assert guide["business_rule"] is not None
        # fifo_layer is a related_pattern but may not exist in PATTERNS
        # So pattern may or may not be populated

    def test_guide_unknown_feature(self):
        """Unknown feature returns found=False."""
        guide = get_implementation_guide("time_travel_billing")
        assert guide["found"] is False

    def test_guide_empty_feature(self):
        """Empty feature returns found=False."""
        guide = get_implementation_guide("")
        assert guide["found"] is False

    def test_guide_document_close(self):
        """Document close has both knowledge base and pattern match."""
        guide = get_implementation_guide("document_close")
        assert guide["found"] is True
        assert guide["business_rule"] is not None
        assert guide["pattern"] is not None
        assert guide["pattern"]["key"] == "document_close"

    def test_guide_blanket_agreement(self):
        """Blanket agreement: knowledge base references blanket_agreement pattern."""
        guide = get_implementation_guide("blanket_orders")
        assert guide["found"] is True
        assert guide["business_rule"] is not None
        # blanket_orders KB entry has related_patterns: ["blanket_agreement"]
        if guide["pattern"]:
            assert guide["pattern"]["key"] == "blanket_agreement"


# ---------------------------------------------------------------------------
# Test: list_knowledge_base
# ---------------------------------------------------------------------------

class TestListKnowledgeBase:
    def test_returns_all_entries(self):
        """List returns all entries (22 core + 20 vertical = 42+)."""
        entries = list_knowledge_base()
        assert len(entries) >= 42

    def test_entries_have_required_fields(self):
        """Each entry has key, summary, source."""
        for entry in list_knowledge_base():
            assert "key" in entry
            assert "summary" in entry
            assert "source" in entry


# ---------------------------------------------------------------------------
# Test: _resolve_topic (internal)
# ---------------------------------------------------------------------------

class TestResolveTopic:
    def test_exact_key(self):
        assert _resolve_topic("fifo_valuation") == "fifo_valuation"

    def test_alias_resolution(self):
        assert _resolve_topic("fifo") == "fifo_valuation"
        assert _resolve_topic("ach") == "nacha_ach"

    def test_normalized_key(self):
        assert _resolve_topic("FIFO_VALUATION") == "fifo_valuation"

    def test_fuzzy_match(self):
        """Fuzzy matching finds the best overlap."""
        result = _resolve_topic("valuation fifo")
        assert result == "fifo_valuation"

    def test_none_returns_none(self):
        assert _resolve_topic(None) is None

    def test_empty_returns_none(self):
        assert _resolve_topic("") is None


# ---------------------------------------------------------------------------
# Test: Action handlers
# ---------------------------------------------------------------------------

class TestActionHandlers:
    def _make_args(self, **kwargs):
        """Build a minimal namespace for action handler testing."""
        import argparse
        return argparse.Namespace(**kwargs)

    def test_handle_research_rule_missing_topic(self):
        """Missing --topic returns error."""
        args = self._make_args(topic=None, domain=None)
        result = handle_research_rule(args)
        assert "error" in result

    def test_handle_research_rule_valid(self):
        """Valid topic returns knowledge base entry."""
        args = self._make_args(topic="fifo_valuation", domain=None)
        result = handle_research_rule(args)
        assert result["found"] is True

    def test_handle_get_implementation_guide_missing_feature(self):
        """Missing --feature-name returns error."""
        args = self._make_args(feature_name=None)
        result = handle_get_implementation_guide(args)
        assert "error" in result

    def test_handle_get_implementation_guide_valid(self):
        """Valid feature-name returns guide."""
        args = self._make_args(feature_name="three_way_match")
        result = handle_get_implementation_guide(args)
        assert result["found"] is True
