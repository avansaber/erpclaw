#!/usr/bin/env python3
"""ERPClaw OS — Gap Detection + Module Suggestions (Phase 3, Deliverable 3e)

Analyzes action_call_log and company configuration to detect missing
functionality and suggest relevant modules. Three detection methods:

  1. Error pattern analysis — actions that consistently fail with "Unknown action"
  2. Workflow gap analysis — action pairs with long gaps suggesting manual intervention
  3. Industry gap analysis — cross-reference company industry with module_registry.json

All detected gaps are logged to erpclaw_improvement_log via improvement_log.py.
"""
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone

# Add shared lib to path
sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))

from erpclaw_lib.db import get_connection
from erpclaw_lib.query import Q, P, Table, Field, fn

# Add erpclaw-os directory to path for sibling imports
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from improvement_log import handle_log_improvement
from industry_configs import INDUSTRY_CONFIGS

# Module registry lives next to the root db_query.py
REGISTRY_PATH = os.path.join(
    os.path.dirname(SCRIPT_DIR), "module_registry.json"
)

# Error threshold: how many errors before flagging a gap
ERROR_THRESHOLD = 3
# Workflow gap threshold in seconds (5 minutes)
WORKFLOW_GAP_SECONDS = 300


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------

def _load_registry(registry_path=None):
    """Load module_registry.json and return the modules dict."""
    path = registry_path or REGISTRY_PATH
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data.get("modules", {})
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _get_installed_modules(conn):
    """Return set of installed module names from erpclaw_module table."""
    try:
        rows = conn.execute(
            "SELECT name FROM erpclaw_module WHERE install_status = 'installed'"
        ).fetchall()
        return {r["name"] for r in rows}
    except sqlite3.OperationalError:
        # Table doesn't exist
        return set()


def _get_company_industry(conn):
    """Read industry from erpclaw_module_config (set by configure-module).

    Returns (industry, size_tier) or (None, None) if not configured.
    """
    try:
        row = conn.execute(
            "SELECT industry, size_tier FROM erpclaw_module_config "
            "WHERE config_type = 'industry_config' "
            "ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if row:
            return row["industry"], row["size_tier"]
    except sqlite3.OperationalError:
        pass
    return None, None


# ---------------------------------------------------------------------------
# Detection method 1: Error pattern analysis
# ---------------------------------------------------------------------------

def _detect_error_patterns(conn):
    """Find actions that consistently fail with 'Unknown action' errors.

    Queries action_call_log for action names that appear with errors
    (routed_to containing 'error' or route_tier = -1) more than ERROR_THRESHOLD times.

    Since action_call_log only logs successful routes (from the router in db_query.py),
    we look for action names that appear frequently but were routed to a module
    that doesn't exist, or for patterns indicating unknown action handling.

    In practice, the main router logs calls before forwarding. If the action
    was unknown, it wouldn't be logged. So we check for actions where the
    routed_to is suspicious or look for repeated calls to the same non-standard
    action that may indicate user demand.
    """
    gaps = []

    # Check for repeated action_names that route to non-core domains
    # (could indicate users hitting missing features)
    try:
        t = Table("action_call_log")
        q = (
            Q.from_(t)
            .select(t.action_name, fn.Count("*").as_("call_count"))
            .where(t.routed_to == P())
            .groupby(t.action_name)
        )
        sql = q.get_sql() + ' HAVING COUNT(*) > ?'

        rows = conn.execute(sql, ["error", ERROR_THRESHOLD]).fetchall()
        for row in rows:
            gaps.append({
                "gap_type": "error_pattern",
                "description": (
                    f"Action '{row['action_name']}' failed {row['call_count']} times "
                    f"with routing errors, suggesting missing module or action."
                ),
                "severity": "high",
                "suggested_action": f"Investigate adding action '{row['action_name']}' "
                                    f"or installing the module that provides it.",
                "action_name": row["action_name"],
                "error_count": row["call_count"],
            })
    except sqlite3.OperationalError:
        pass

    return gaps


# ---------------------------------------------------------------------------
# Detection method 2: Workflow gap analysis
# ---------------------------------------------------------------------------

def _detect_workflow_gaps(conn):
    """Find action pairs with consistently long gaps suggesting manual intervention.

    Looks for consecutive action pairs (by session_id or timestamp order) where
    the time between them exceeds WORKFLOW_GAP_SECONDS, suggesting the user
    had to do something manually between the two actions.
    """
    gaps = []

    try:
        # Find action pairs with large time gaps within the same session
        sql = """
            SELECT
                a.action_name AS action_a,
                b.action_name AS action_b,
                COUNT(*) AS pair_count,
                AVG(
                    CAST(
                        (julianday(b.timestamp) - julianday(a.timestamp)) * 86400
                    AS REAL)
                ) AS avg_gap_seconds
            FROM action_call_log a
            JOIN action_call_log b
                ON a.session_id = b.session_id
                AND b.rowid = (
                    SELECT MIN(c.rowid)
                    FROM action_call_log c
                    WHERE c.session_id = a.session_id
                      AND c.rowid > a.rowid
                )
            WHERE a.session_id IS NOT NULL
            GROUP BY a.action_name, b.action_name
            HAVING COUNT(*) >= 2
               AND AVG(
                   CAST(
                       (julianday(b.timestamp) - julianday(a.timestamp)) * 86400
                   AS REAL)
               ) > ?
        """
        rows = conn.execute(sql, [WORKFLOW_GAP_SECONDS]).fetchall()
        for row in rows:
            avg_gap = round(row["avg_gap_seconds"], 1)
            gaps.append({
                "gap_type": "workflow_gap",
                "description": (
                    f"Action pair '{row['action_a']}' -> '{row['action_b']}' "
                    f"has an average gap of {avg_gap}s across {row['pair_count']} occurrences, "
                    f"suggesting manual intervention between steps."
                ),
                "severity": "medium",
                "suggested_action": (
                    f"Consider adding an automated bridge action between "
                    f"'{row['action_a']}' and '{row['action_b']}'."
                ),
                "action_a": row["action_a"],
                "action_b": row["action_b"],
                "avg_gap_seconds": avg_gap,
                "pair_count": row["pair_count"],
            })
    except sqlite3.OperationalError:
        pass

    return gaps


# ---------------------------------------------------------------------------
# Detection method 3: Industry gap analysis
# ---------------------------------------------------------------------------

def _detect_industry_gaps(conn, registry):
    """Cross-reference company industry with standard modules for that industry.

    Reads industry from erpclaw_module_config (set by configure-module),
    looks up standard modules from INDUSTRY_CONFIGS, and flags any that
    are not installed.
    """
    gaps = []

    industry, size_tier = _get_company_industry(conn)
    if not industry:
        return gaps

    config = INDUSTRY_CONFIGS.get(industry)
    if not config:
        return gaps

    size_tier = size_tier or "small"
    standard_modules = config["modules"].get(size_tier, config["modules"].get("small", []))
    installed = _get_installed_modules(conn)

    for mod_name in standard_modules:
        if mod_name not in installed and mod_name in registry:
            mod_info = registry[mod_name]
            gaps.append({
                "gap_type": "industry_gap",
                "description": (
                    f"Module '{mod_name}' ({mod_info.get('display_name', mod_name)}) "
                    f"is standard for {config['display_name']} ({size_tier} tier) "
                    f"but is not installed."
                ),
                "severity": "medium",
                "suggested_action": (
                    f"Install module '{mod_name}' with: "
                    f"--action install-module --module-name {mod_name}"
                ),
                "module_name": mod_name,
                "industry": industry,
                "size_tier": size_tier,
            })

    return gaps


# ---------------------------------------------------------------------------
# Log gaps to improvement_log
# ---------------------------------------------------------------------------

def _log_gaps_to_improvement_log(gaps, db_path=None):
    """Log each gap as a 'coverage' improvement with source='gap_detector'."""
    for gap in gaps:
        args = type("Args", (), {
            "category": "coverage",
            "description": gap["description"],
            "source": "gap_detector",
            "evidence": json.dumps({k: v for k, v in gap.items() if k != "description"}),
            "expected_impact": None,
            "proposed_diff": None,
            "module_name_arg": gap.get("module_name"),
            "module_name": gap.get("module_name"),
            "db_path": db_path,
        })()
        handle_log_improvement(args)


# ---------------------------------------------------------------------------
# Action: detect-gaps
# ---------------------------------------------------------------------------

def handle_detect_gaps(args):
    """Analyze action_call_log for patterns indicating missing functionality.

    Uses three detection methods:
      1. Error pattern analysis (actions failing consistently)
      2. Workflow gap analysis (long gaps between action pairs)
      3. Industry gap analysis (standard modules not installed)

    Returns list of gaps with gap_type, description, severity, suggested_action.
    All gaps are logged to erpclaw_improvement_log.
    """
    db_path = getattr(args, "db_path", None)
    registry_path = getattr(args, "registry_path", None)

    registry = _load_registry(registry_path)

    conn = get_connection(db_path)
    try:
        all_gaps = []

        # Method 1: Error pattern analysis
        error_gaps = _detect_error_patterns(conn)
        all_gaps.extend(error_gaps)

        # Method 2: Workflow gap analysis
        workflow_gaps = _detect_workflow_gaps(conn)
        all_gaps.extend(workflow_gaps)

        # Method 3: Industry gap analysis
        industry_gaps = _detect_industry_gaps(conn, registry)
        all_gaps.extend(industry_gaps)
    finally:
        conn.close()

    # Log all gaps to improvement_log
    if all_gaps:
        _log_gaps_to_improvement_log(all_gaps, db_path=db_path)

    return {
        "result": "ok",
        "total_gaps": len(all_gaps),
        "gaps_by_type": {
            "error_pattern": len(error_gaps),
            "workflow_gap": len(workflow_gaps),
            "industry_gap": len(industry_gaps),
        },
        "gaps": all_gaps,
    }


# ---------------------------------------------------------------------------
# Action: suggest-modules
# ---------------------------------------------------------------------------

def handle_suggest_modules(args):
    """Generate prioritized module suggestions based on multiple signals.

    Ranking factors:
      1. Industry match (from company's industry/onboarding profile)
      2. Usage intensity in related domains (from action_call_log)
      3. Dependency compatibility (from module_registry.json "requires" field)
      4. Already-installed modules (excluded)

    Returns ranked suggestions with module_name, relevance_score, reason, dependencies.
    """
    db_path = getattr(args, "db_path", None)
    registry_path = getattr(args, "registry_path", None)

    registry = _load_registry(registry_path)
    if not registry:
        return {"error": "Could not load module_registry.json"}

    conn = get_connection(db_path)
    try:
        installed = _get_installed_modules(conn)
        industry, size_tier = _get_company_industry(conn)

        # Get action usage counts by routed_to domain for usage intensity scoring
        usage_by_domain = {}
        try:
            rows = conn.execute(
                "SELECT routed_to, COUNT(*) as cnt FROM action_call_log "
                "GROUP BY routed_to"
            ).fetchall()
            for row in rows:
                usage_by_domain[row["routed_to"]] = row["cnt"]
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()

    suggestions = []

    for mod_name, mod_info in registry.items():
        # Skip already-installed modules and core
        if mod_name in installed or mod_name == "erpclaw":
            continue

        score = 0.0
        reasons = []

        # Factor 1: Industry match
        if industry:
            config = INDUSTRY_CONFIGS.get(industry)
            if config:
                tier = size_tier or "small"
                standard_modules = config["modules"].get(tier, config["modules"].get("small", []))
                if mod_name in standard_modules:
                    score += 50.0
                    reasons.append(
                        f"Standard module for {config['display_name']} ({tier} tier)"
                    )

        # Factor 2: Usage intensity in related domains
        # Check if any of the module's tags overlap with heavily-used domains
        tags = set(mod_info.get("tags", []))
        for domain, count in usage_by_domain.items():
            # Simple heuristic: if the domain name overlaps with module tags
            domain_lower = domain.lower().replace("erpclaw-", "").replace("-", " ")
            for tag in tags:
                if tag.replace("-", " ") in domain_lower or domain_lower in tag.replace("-", " "):
                    intensity_score = min(count * 0.5, 30.0)
                    score += intensity_score
                    reasons.append(
                        f"Related domain '{domain}' used {count} times"
                    )
                    break

        # Factor 3: Dependency compatibility
        requires = mod_info.get("requires", [])
        deps_met = all(dep in installed or dep == "erpclaw" for dep in requires)

        if not deps_met:
            # Penalize if dependencies not met
            score -= 20.0
            missing_deps = [d for d in requires if d not in installed and d != "erpclaw"]
            reasons.append(
                f"Missing dependencies: {', '.join(missing_deps)}"
            )
        elif requires:
            # Bonus for having all dependencies already installed
            score += 10.0
            reasons.append("All dependencies already installed")

        # Factor 4: Category bonus — verticals score higher for empty setups
        category = mod_info.get("category", "")
        if category == "vertical":
            score += 5.0
        elif category == "sub-vertical":
            # Sub-verticals need their parent — only suggest if parent installed
            if not deps_met:
                score -= 10.0

        # Only suggest modules with positive score or industry match
        if score > 0:
            suggestions.append({
                "module_name": mod_name,
                "display_name": mod_info.get("display_name", mod_name),
                "relevance_score": round(score, 1),
                "reason": "; ".join(reasons) if reasons else "General expansion module",
                "dependencies": requires,
                "category": category,
                "action_count": mod_info.get("action_count", 0),
            })

    # Sort by relevance_score descending
    suggestions.sort(key=lambda s: s["relevance_score"], reverse=True)

    return {
        "result": "ok",
        "industry": industry,
        "size_tier": size_tier,
        "installed_count": len(installed),
        "suggestion_count": len(suggestions),
        "suggestions": suggestions,
    }
