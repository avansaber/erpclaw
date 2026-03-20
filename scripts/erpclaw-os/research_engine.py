"""ERPClaw OS — Research Engine (P1-8)

Two-tier research system for business rules and ERP implementation patterns:

1. **KNOWLEDGE_BASE (runtime):** Built-in dictionary of common ERP business rules,
   regulatory standards, and implementation hints. Always available, zero latency.

2. **Web Search (dev-time only):** WebSearch/WebFetch deferred tools in the Claude
   Code environment. NOT available on the production OpenClaw server. Results are
   cached so the server can use them later.

Safety invariants:
- Research is DEV-TIME ONLY for web lookups. Server uses cached results.
- All returned data is advisory — never auto-applied to production code.
- Cache misses on the server return graceful not_found, never errors.
"""
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

# Make erpclaw-os package importable
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

try:
    from pattern_library import PATTERNS
except ImportError:
    PATTERNS = {}


# ---------------------------------------------------------------------------
# Knowledge Base — 22 common ERP business rules
# ---------------------------------------------------------------------------

KNOWLEDGE_BASE = {
    "fifo_valuation": {
        "summary": "First In First Out: consume oldest stock layers first on outgoing transactions.",
        "source": "ASC 330 / IAS 2",
        "implementation_hints": (
            "Track stock layers with (posting_date, qty, rate). On outgoing, "
            "iterate oldest-first, deducting qty. Each consumed layer becomes a "
            "stock_ledger_entry with the layer's rate."
        ),
        "related_patterns": ["fifo_layer"],
    },
    "three_way_match": {
        "summary": "Validate: invoice_qty <= received_qty <= ordered_qty within tolerance.",
        "source": "Standard procurement best practice",
        "implementation_hints": (
            "On PI submit, query PO items and PR items, compare quantities. "
            "If abs(invoice_qty - received_qty) / ordered_qty > tolerance_pct, block submission."
        ),
        "related_patterns": ["three_way_match"],
    },
    "overtime_flsa": {
        "summary": (
            "Federal: 1.5x regular rate after 40 hours/week. "
            "California: also 1.5x after 8 hours/day, 2x after 12 hours/day."
        ),
        "source": "Fair Labor Standards Act (FLSA), 29 USC \u00a7207",
        "implementation_hints": (
            "Track hours per week. Calculate: regular_hours = min(hours, 40), "
            "ot_hours = max(0, hours - 40). OT rate = regular_rate * 1.5."
        ),
        "related_patterns": [],
    },
    "nacha_ach": {
        "summary": (
            "NACHA file format for ACH direct deposit. Fixed-width records: "
            "File Header (1), Batch Header (5), Entry Detail (6), "
            "Batch Control (8), File Control (9)."
        ),
        "source": "NACHA Operating Rules",
        "implementation_hints": (
            "Generate fixed-width text file. Record type 1 (file header), "
            "5 (batch header), 6 (entry detail per employee), "
            "8 (batch control), 9 (file control). 94-character lines."
        ),
        "related_patterns": [],
    },
    "blanket_orders": {
        "summary": (
            "Long-term purchase agreement with a total qty/amount. Individual POs "
            "draw down from the blanket. When fully consumed, blanket is closed."
        ),
        "source": "Standard procurement practice, SAP ME31K/ME21N",
        "implementation_hints": (
            "Track total_qty and fulfilled_qty. On each PO referencing the blanket, "
            "increment fulfilled_qty. When fulfilled_qty >= total_qty, set status='closed'."
        ),
        "related_patterns": ["blanket_agreement"],
    },
    "document_close": {
        "summary": (
            "Close a document (SO, PO, etc.) to prevent further transactions against it. "
            "Closing is reversible (reopen). Differs from cancel which is permanent."
        ),
        "source": "Standard ERP workflow pattern",
        "implementation_hints": (
            "Add close_date, close_reason, closed_by columns. On close, set status='closed'. "
            "On reopen, clear close_date and set status back to previous state."
        ),
        "related_patterns": ["document_close"],
    },
    "document_amendment": {
        "summary": (
            "Create a new version of a submitted document. Original is frozen, "
            "amendment carries forward all data with amendment_number incremented."
        ),
        "source": "ERPNext document amendment model",
        "implementation_hints": (
            "Copy original row, set original_id = parent.id, increment amendment_number. "
            "Mark original status='amended'. New row starts as draft."
        ),
        "related_patterns": ["document_amendment"],
    },
    "recurring_billing": {
        "summary": (
            "Auto-generate invoices on a schedule (monthly, quarterly, annually). "
            "Template defines items, frequency, start/end dates."
        ),
        "source": "Standard SaaS/subscription billing practice",
        "implementation_hints": (
            "Store template with frequency and next_date. Cron job queries templates "
            "where next_date <= today, generates invoice via cross_skill, advances next_date."
        ),
        "related_patterns": ["recurring_template"],
    },
    "multi_uom": {
        "summary": (
            "Items can be stocked and sold in different units of measure. "
            "Conversion factors between UOMs must be maintained."
        ),
        "source": "Standard inventory management",
        "implementation_hints": (
            "Create uom_conversion table: item_id, from_uom, to_uom, conversion_factor. "
            "On stock/sales transactions, convert to base UOM for ledger entries."
        ),
        "related_patterns": [],
    },
    "item_variants": {
        "summary": (
            "Items can have variants (size, color, material). Variants share base "
            "properties but override specific attributes."
        ),
        "source": "Standard product management",
        "implementation_hints": (
            "item_variant table: id, template_item_id, attribute_values (JSON). "
            "Inherit price, description from template. Override per-variant as needed."
        ),
        "related_patterns": [],
    },
    "stock_projected_qty": {
        "summary": (
            "Projected qty = actual_qty + ordered_qty - reserved_qty - planned_qty. "
            "Shows future availability considering open POs and pending SOs."
        ),
        "source": "MRP standard calculation",
        "implementation_hints": (
            "Query: sum stock_ledger actual_qty, sum open PO items ordered_qty, "
            "sum open SO items reserved_qty. Projected = actual + ordered - reserved."
        ),
        "related_patterns": [],
    },
    "material_substitution": {
        "summary": (
            "Allow substituting one raw material for another in manufacturing. "
            "Substitute must meet quality specs and have sufficient stock."
        ),
        "source": "Manufacturing BOM management",
        "implementation_hints": (
            "Create material_substitute table: original_item_id, substitute_item_id, "
            "priority, conversion_factor. On stock shortage, suggest substitutes ordered by priority."
        ),
        "related_patterns": [],
    },
    "co_products": {
        "summary": (
            "Manufacturing process produces multiple outputs from one BOM. "
            "Each co-product has a cost allocation percentage."
        ),
        "source": "Process manufacturing (chemical, food, petroleum)",
        "implementation_hints": (
            "Extend BOM with co_product table: bom_id, item_id, qty, cost_pct. "
            "Sum of cost_pct must equal 100. On manufacture, create stock entries for all co-products."
        ),
        "related_patterns": [],
    },
    "make_vs_buy": {
        "summary": (
            "Decision framework: manufacture in-house or purchase from supplier. "
            "Based on cost comparison, capacity, lead time."
        ),
        "source": "Manufacturing planning practice",
        "implementation_hints": (
            "For each item, store make_cost and buy_cost. Planning engine compares "
            "and suggests make/buy. Override possible per planning run."
        ),
        "related_patterns": [],
    },
    "grn_tolerance": {
        "summary": (
            "Goods Receipt Note: accept over/under delivery within tolerance percentage. "
            "Outside tolerance requires approval."
        ),
        "source": "Standard procurement QA",
        "implementation_hints": (
            "On GRN submit, compare received_qty to PO qty. If abs(received - ordered) / ordered "
            "> tolerance_pct (default 5%), require approval or reject."
        ),
        "related_patterns": ["three_way_match"],
    },
    "shift_management": {
        "summary": (
            "Define work shifts (morning, afternoon, night). Assign employees to shifts. "
            "Track shift changes, shift differentials (pay premium)."
        ),
        "source": "Standard HR/workforce management",
        "implementation_hints": (
            "Create shift_type table (id, name, start_time, end_time, differential_rate). "
            "shift_assignment table (employee_id, shift_type_id, date). "
            "Payroll multiplies base rate by differential_rate for non-standard shifts."
        ),
        "related_patterns": [],
    },
    "multi_state_payroll": {
        "summary": (
            "Employees may work in multiple US states. Each state has its own income tax "
            "withholding rules, unemployment tax, and disability insurance."
        ),
        "source": "US state tax codes (varies by state)",
        "implementation_hints": (
            "Track work_state per pay period. Apply state-specific tax tables. "
            "Handle reciprocity agreements between states. "
            "SUI rates vary by employer experience rating."
        ),
        "related_patterns": [],
    },
    "supplemental_wages": {
        "summary": (
            "Bonuses, commissions, overtime premiums taxed differently than regular wages. "
            "Federal flat rate: 22% (or 37% above $1M)."
        ),
        "source": "IRS Publication 15, Section 7",
        "implementation_hints": (
            "Categorize each earning type as regular or supplemental. "
            "Apply flat 22% federal withholding to supplemental earnings, "
            "or aggregate method if employer chooses."
        ),
        "related_patterns": [],
    },
    "retro_pay": {
        "summary": (
            "Retroactive pay adjustment when a pay raise is effective before "
            "current pay period. Calculate difference for past periods."
        ),
        "source": "Standard payroll practice",
        "implementation_hints": (
            "Query past pay periods from effective_date. For each period, "
            "compute difference = (new_rate - old_rate) * hours. "
            "Add as supplemental earning in current payroll."
        ),
        "related_patterns": [],
    },
    "leave_carry_forward": {
        "summary": (
            "Unused leave balance carries forward to next year, subject to caps. "
            "Some leave types expire (use-it-or-lose-it), others accumulate."
        ),
        "source": "Standard HR leave policy",
        "implementation_hints": (
            "On fiscal year rollover, query leave_balance per employee and leave_type. "
            "Apply carry_forward_max from leave_type config. "
            "Excess is forfeited. Create new leave_allocation for carried amount."
        ),
        "related_patterns": [],
    },
    "depreciation_straight_line": {
        "summary": (
            "Asset cost spread evenly over useful life. "
            "Monthly depreciation = (cost - salvage_value) / (useful_life_months)."
        ),
        "source": "ASC 360 / IAS 16",
        "implementation_hints": (
            "On asset submit, calculate monthly_depreciation. "
            "Monthly cron posts GL: Dr Depreciation Expense, Cr Accumulated Depreciation. "
            "Track accumulated_depreciation on the asset record."
        ),
        "related_patterns": [],
    },
    "bank_reconciliation": {
        "summary": (
            "Match bank statement lines to GL journal entries. "
            "Unmatched items are either missing entries or bank errors."
        ),
        "source": "Standard accounting practice",
        "implementation_hints": (
            "Import bank statement lines (date, description, amount, reference). "
            "Auto-match by reference number and amount. Manual match for unmatched. "
            "Reconciled entries update clearance_date on payment/journal entry."
        ),
        "related_patterns": [],
    },
}

# Canonical name aliases — maps search terms to KNOWLEDGE_BASE keys
_ALIASES = {
    "fifo": "fifo_valuation",
    "first in first out": "fifo_valuation",
    "fifo inventory": "fifo_valuation",
    "fifo inventory valuation": "fifo_valuation",
    "three way match": "three_way_match",
    "3 way match": "three_way_match",
    "3-way match": "three_way_match",
    "three-way match": "three_way_match",
    "overtime": "overtime_flsa",
    "flsa": "overtime_flsa",
    "flsa overtime": "overtime_flsa",
    "flsa overtime rules": "overtime_flsa",
    "ach": "nacha_ach",
    "nacha": "nacha_ach",
    "nacha ach file format": "nacha_ach",
    "ach file": "nacha_ach",
    "blanket": "blanket_orders",
    "blanket order": "blanket_orders",
    "blanket purchase order": "blanket_orders",
    "framework agreement": "blanket_orders",
    "close": "document_close",
    "document close": "document_close",
    "close document": "document_close",
    "amendment": "document_amendment",
    "document amendment": "document_amendment",
    "amend": "document_amendment",
    "recurring": "recurring_billing",
    "recurring billing": "recurring_billing",
    "recurring invoice": "recurring_billing",
    "subscription billing": "recurring_billing",
    "multi uom": "multi_uom",
    "unit of measure": "multi_uom",
    "uom conversion": "multi_uom",
    "item variant": "item_variants",
    "item variants": "item_variants",
    "product variant": "item_variants",
    "projected qty": "stock_projected_qty",
    "projected quantity": "stock_projected_qty",
    "available qty": "stock_projected_qty",
    "material substitute": "material_substitution",
    "material substitution": "material_substitution",
    "alternate material": "material_substitution",
    "co product": "co_products",
    "co-product": "co_products",
    "by-product": "co_products",
    "make or buy": "make_vs_buy",
    "make vs buy": "make_vs_buy",
    "grn tolerance": "grn_tolerance",
    "goods receipt tolerance": "grn_tolerance",
    "over delivery": "grn_tolerance",
    "shift": "shift_management",
    "shift management": "shift_management",
    "work shift": "shift_management",
    "multi state payroll": "multi_state_payroll",
    "state tax": "multi_state_payroll",
    "state withholding": "multi_state_payroll",
    "supplemental wage": "supplemental_wages",
    "supplemental wages": "supplemental_wages",
    "bonus tax": "supplemental_wages",
    "retro pay": "retro_pay",
    "retroactive pay": "retro_pay",
    "back pay": "retro_pay",
    "leave carry forward": "leave_carry_forward",
    "leave rollover": "leave_carry_forward",
    "pto rollover": "leave_carry_forward",
    "depreciation": "depreciation_straight_line",
    "straight line depreciation": "depreciation_straight_line",
    "asset depreciation": "depreciation_straight_line",
    "bank reconciliation": "bank_reconciliation",
    "bank recon": "bank_reconciliation",
    "reconciliation": "bank_reconciliation",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def research_business_rule(topic: str, domain: str = None) -> dict:
    """Look up a business rule from the knowledge base.

    Args:
        topic: Natural language search term, e.g., "FIFO inventory valuation"
               or "FLSA overtime rules" or a canonical key like "fifo_valuation".
        domain: Optional domain hint (e.g., "inventory", "payroll", "procurement").

    Returns:
        dict with keys: topic, found, rule_summary, source, implementation_hints,
                        related_patterns, canonical_key
        If not found: {topic, found: False, message: "..."}
    """
    if not topic or not topic.strip():
        return {"topic": topic, "found": False, "message": "Empty topic"}

    canonical_key = _resolve_topic(topic)

    if canonical_key and canonical_key in KNOWLEDGE_BASE:
        entry = KNOWLEDGE_BASE[canonical_key]
        return {
            "topic": topic,
            "found": True,
            "canonical_key": canonical_key,
            "rule_summary": entry["summary"],
            "source": entry["source"],
            "implementation_hints": entry["implementation_hints"],
            "related_patterns": entry.get("related_patterns", []),
        }

    # Not found in knowledge base — return graceful not_found
    return {
        "topic": topic,
        "found": False,
        "message": f"No knowledge base entry for '{topic}'. "
                   "Try a more specific term or check available topics with list_knowledge_base().",
    }


def get_implementation_guide(feature_name: str) -> dict:
    """Combine knowledge base lookup + pattern library match + code template hints.

    Provides a full implementation guide for a business feature by combining:
    1. Business rule from KNOWLEDGE_BASE
    2. Code pattern from pattern_library.PATTERNS
    3. Test template skeleton

    Args:
        feature_name: Feature name, e.g., "three_way_match", "fifo_valuation"

    Returns:
        dict with keys: feature_name, business_rule, pattern, code_template, test_template
    """
    if not feature_name or not feature_name.strip():
        return {"feature_name": feature_name, "found": False, "message": "Empty feature name"}

    result = {
        "feature_name": feature_name,
        "found": False,
        "business_rule": None,
        "pattern": None,
        "code_template": None,
        "test_template": None,
    }

    # 1. Knowledge base lookup
    rule = research_business_rule(feature_name)
    if rule.get("found"):
        result["business_rule"] = {
            "summary": rule["rule_summary"],
            "source": rule["source"],
            "hints": rule["implementation_hints"],
        }
        result["found"] = True

        # 2. Pattern library match via related_patterns
        matched_pattern = None
        for rp in rule.get("related_patterns", []):
            if rp in PATTERNS:
                matched_pattern = PATTERNS[rp]
                result["pattern"] = {
                    "key": rp,
                    "name": matched_pattern["name"],
                    "actions": matched_pattern["actions"],
                    "requires_gl": matched_pattern["requires_gl"],
                    "schema_fields": matched_pattern.get("schema_fields", []),
                }
                break

        # Also check if feature_name itself is a pattern key
        if not matched_pattern and feature_name in PATTERNS:
            matched_pattern = PATTERNS[feature_name]
            result["pattern"] = {
                "key": feature_name,
                "name": matched_pattern["name"],
                "actions": matched_pattern["actions"],
                "requires_gl": matched_pattern["requires_gl"],
                "schema_fields": matched_pattern.get("schema_fields", []),
            }

        # 3. Generate code template skeleton
        if matched_pattern:
            actions = matched_pattern["actions"]
            result["code_template"] = _build_code_template(feature_name, actions)

        # 4. Generate test template skeleton
        result["test_template"] = _build_test_template(feature_name, rule)

    else:
        # Check if it matches a pattern directly even without a knowledge base entry
        if feature_name in PATTERNS:
            pat = PATTERNS[feature_name]
            result["found"] = True
            result["pattern"] = {
                "key": feature_name,
                "name": pat["name"],
                "actions": pat["actions"],
                "requires_gl": pat["requires_gl"],
                "schema_fields": pat.get("schema_fields", []),
            }
            result["code_template"] = _build_code_template(feature_name, pat["actions"])
            result["test_template"] = _build_test_template(feature_name, None)

    return result


def list_knowledge_base() -> list[dict]:
    """List all topics in the knowledge base.

    Returns:
        list of {key, summary, source} dicts.
    """
    return [
        {"key": k, "summary": v["summary"][:100], "source": v["source"]}
        for k, v in sorted(KNOWLEDGE_BASE.items())
    ]


# ---------------------------------------------------------------------------
# Action handler (wired into erpclaw-os/db_query.py)
# ---------------------------------------------------------------------------

def handle_research_rule(args) -> dict:
    """Action handler for research-business-rule.

    Required:
        --topic: The business rule topic to research
    Optional:
        --domain: Domain hint (inventory, payroll, procurement, etc.)
    """
    topic = getattr(args, "topic", None)
    domain = getattr(args, "domain", None)

    if not topic:
        return {"error": "--topic is required"}

    return research_business_rule(topic, domain)


def handle_get_implementation_guide(args) -> dict:
    """Action handler for get-implementation-guide.

    Required:
        --feature-name: The feature to get an implementation guide for
    """
    feature_name = getattr(args, "feature_name", None)

    if not feature_name:
        return {"error": "--feature-name is required"}

    return get_implementation_guide(feature_name)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _resolve_topic(topic: str) -> str | None:
    """Resolve a natural language topic to a canonical KNOWLEDGE_BASE key.

    Matching priority:
    1. Exact match on KNOWLEDGE_BASE key
    2. Exact match on _ALIASES key
    3. Normalized match (lowercase, stripped)
    4. Fuzzy substring match
    """
    if not topic:
        return None

    # 1. Exact key match
    if topic in KNOWLEDGE_BASE:
        return topic

    # 2. Normalized key match
    normalized = topic.lower().strip().replace("-", "_").replace("  ", " ")
    if normalized in KNOWLEDGE_BASE:
        return normalized

    # 3. Alias match
    alias_key = normalized.replace("_", " ")
    if alias_key in _ALIASES:
        return _ALIASES[alias_key]
    if normalized in _ALIASES:
        return _ALIASES[normalized]

    # 4. Fuzzy substring match — find the best matching key
    # Require at least 2 overlapping words, or that overlap covers >50% of the key
    best_key = None
    best_score = 0
    best_ratio = 0.0
    search_words = set(normalized.split())

    for key in KNOWLEDGE_BASE:
        key_words = set(key.split("_"))
        overlap = len(search_words & key_words)
        ratio = overlap / max(len(key_words), 1)
        if overlap > best_score or (overlap == best_score and ratio > best_ratio):
            best_score = overlap
            best_ratio = ratio
            best_key = key

    # Also check aliases
    for alias, kb_key in _ALIASES.items():
        alias_words = set(alias.split())
        overlap = len(search_words & alias_words)
        ratio = overlap / max(len(alias_words), 1)
        if overlap > best_score or (overlap == best_score and ratio > best_ratio):
            best_score = overlap
            best_ratio = ratio
            best_key = kb_key

    # Require at least 2 overlapping words, OR overlap covers >50% of the target key
    if best_score >= 2 or (best_score >= 1 and best_ratio > 0.5):
        return best_key

    return None


def _build_code_template(feature_name: str, actions: list[str]) -> str:
    """Build a skeleton code template for a feature.

    Returns a string with pseudo-code showing the function structure.
    """
    func_name = feature_name.replace("-", "_")
    lines = [f"# Code template for {feature_name}", ""]

    for action in actions:
        action_func = action.replace("-", "_")
        lines.append(f"def handle_{func_name}_{action_func}(conn, args):")
        lines.append(f'    """Handle {action} for {feature_name}."""')
        lines.append(f"    # Validate required parameters")
        lines.append(f"    # Query/update database")
        lines.append(f"    # Return result via ok()")
        lines.append(f"    pass")
        lines.append("")

    return "\n".join(lines)


def _build_test_template(feature_name: str, rule: dict | None) -> str:
    """Build a skeleton test template for a feature.

    Returns a string with pytest test structure.
    """
    class_name = "".join(w.capitalize() for w in feature_name.replace("-", "_").split("_"))
    lines = [
        f"# Test template for {feature_name}",
        "",
        f"class Test{class_name}:",
    ]

    # Happy path
    lines.append(f"    def test_{feature_name.replace('-', '_')}_happy_path(self, conn, env):")
    lines.append(f'        """Verify {feature_name} works with valid inputs."""')
    lines.append(f"        # Setup: create prerequisite data")
    lines.append(f"        # Act: call the action")
    lines.append(f"        # Assert: verify expected result")
    lines.append(f"        pass")
    lines.append("")

    # Error path
    lines.append(f"    def test_{feature_name.replace('-', '_')}_missing_required(self, conn, env):")
    lines.append(f'        """Verify {feature_name} rejects missing required params."""')
    lines.append(f"        # Act: call with missing required param")
    lines.append(f"        # Assert: error response")
    lines.append(f"        pass")
    lines.append("")

    # Source citation if available
    if rule and rule.get("found"):
        source = rule.get("source", "unknown")
        lines.append(f"    # Business rule source: {source}")

    return "\n".join(lines)
