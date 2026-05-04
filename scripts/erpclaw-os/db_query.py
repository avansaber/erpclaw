#!/usr/bin/env python3
"""ERPClaw OS — db_query.py (foundation runtime only)

After the 2026-05-04 split, the foundation `erpclaw-os/` keeps only
the runtime-essential actions: module validation against the
Constitution, schema migration, and the table-ownership registry
builder. The dev-time module-generation, deploy pipeline, and
DGM evolution actions moved to the optional addon
`erpclaw-os-engine` (avansaber/erpclaw-addons subdir
erpclaw-os-engine).

Runtime actions handled here (7 total):
- validate-module
- list-articles
- build-table-registry
- schema-plan
- schema-apply
- schema-rollback
- schema-drift

If a caller invokes one of the moved actions on the foundation
router, they get a missing-addon error JSON pointing them at
`module_manager.py install-module --module-name erpclaw-os-engine`.
The mapping from old bare names to new os-prefixed names is
emitted with the error.

Usage: python3 db_query.py --action <action-name> [--flags ...]
Output: JSON to stdout, exit 0 on success, exit 1 on error.
"""
import json
import os
import sys
import time

# Add shared lib to path
try:
    sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
    from erpclaw_lib.response import ok, err
    from erpclaw_lib.args import SafeArgumentParser, check_unknown_args
except ImportError:
    # Fallback: define minimal ok/err if shared lib not installed
    def ok(data):
        data["status"] = "ok"
        print(json.dumps(data, indent=2, default=str))
        sys.exit(0)

    def err(message, suggestion=None):
        data = {"status": "error", "message": message}
        if suggestion:
            data["suggestion"] = suggestion
        print(json.dumps(data, indent=2))
        sys.exit(1)

    from argparse import ArgumentParser as SafeArgumentParser

    def check_unknown_args(parser, unknown):
        if unknown:
            print(json.dumps({"status": "error", "message": f"Unknown flags: {', '.join(unknown)}"}))
            sys.exit(1)

# Same-package imports (runtime files only)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from validate_module import validate_module_static, validate_module_runtime, build_table_ownership_registry
from constitution import ARTICLES, get_static_articles, get_runtime_articles
from schema_migrator import (
    handle_schema_plan, handle_schema_apply,
    handle_schema_rollback, handle_schema_drift,
)


# ---------------------------------------------------------------------------
# Action: validate-module
# ---------------------------------------------------------------------------

def handle_validate_module(args):
    """Validate a module against the ERPClaw Constitution."""
    module_path = args.module_path
    validation_type = args.validation_type
    db_path = getattr(args, "db_path", None)

    if not os.path.isdir(module_path):
        err(f"Module path does not exist or is not a directory: {module_path}")

    start_time = time.time()

    result = {}

    if validation_type in ("static", "full"):
        static_result = validate_module_static(module_path)
        result.update(static_result)

    if validation_type in ("runtime", "full"):
        runtime_result = validate_module_runtime(module_path, db_path)
        if validation_type == "full":
            result["runtime"] = runtime_result
            if runtime_result["result"] == "fail":
                result["result"] = "fail"
                result["articles"][9] = "fail"
            else:
                result["articles"][9] = "pass"
        else:
            result = runtime_result

    duration_ms = int((time.time() - start_time) * 1000)
    result["duration_ms"] = duration_ms
    result["validation_type"] = validation_type

    ok(result)


# ---------------------------------------------------------------------------
# Action: list-articles
# ---------------------------------------------------------------------------

def handle_list_articles(args):
    """List all Constitution articles."""
    article_type = getattr(args, "article_type", "all")

    if article_type == "static":
        articles = get_static_articles()
    elif article_type == "runtime":
        articles = get_runtime_articles()
    else:
        articles = ARTICLES

    ok({
        "articles": articles,
        "count": len(articles),
    })


# ---------------------------------------------------------------------------
# Action: build-table-registry
# ---------------------------------------------------------------------------

def handle_build_table_registry(args):
    """Build and display the table ownership registry."""
    src_root = args.src_root

    if not os.path.isdir(src_root):
        err(f"Source root does not exist or is not a directory: {src_root}")

    registry = build_table_ownership_registry(src_root)

    # Group by module for readability
    by_module = {}
    for table, module in sorted(registry.items()):
        by_module.setdefault(module, []).append(table)

    ok({
        "total_tables": len(registry),
        "total_modules": len(by_module),
        "registry": registry,
        "by_module": by_module,
    })


# ---------------------------------------------------------------------------
# Missing-addon stubs for moved actions
# ---------------------------------------------------------------------------

MOVED_ACTIONS_TO_ADDON = {
    "generate-module": "os-generate-module",
    "configure-module": "os-configure-module",
    "list-industries": "os-list-industries",
    "classify-operation": "os-classify-operation",
    "deploy-module": "os-deploy-module",
    "deploy-audit-log": "os-deploy-audit-log",
    "install-suite": "os-install-suite",
    "run-audit": "os-run-audit",
    "compliance-weather-status": "os-compliance-weather-status",
    "log-improvement": "os-log-improvement",
    "list-improvements": "os-list-improvements",
    "review-improvement": "os-review-improvement",
    "semantic-check": "os-semantic-check",
    "semantic-rules-list": "os-semantic-rules-list",
    "dgm-run-variant": "os-dgm-run-variant",
    "dgm-list-variants": "os-dgm-list-variants",
    "dgm-select-best": "os-dgm-select-best",
    "detect-gaps": "os-detect-gaps",
    "detect-schema-divergence": "os-detect-schema-divergence",
    "detect-stubs": "os-detect-stubs",
    "suggest-modules": "os-suggest-modules",
    "heartbeat-analyze": "os-heartbeat-analyze",
    "heartbeat-report": "os-heartbeat-report",
    "heartbeat-suggest": "os-heartbeat-suggest",
    "add-feature-to-module": "os-add-feature-to-module",
    "check-feature-completeness": "os-check-feature-completeness",
    "list-feature-matrix": "os-list-feature-matrix",
    "research-business-rule": "os-research-business-rule",
    "get-implementation-guide": "os-get-implementation-guide",
}


def handle_moved_to_addon(action):
    """Return a structured error JSON for an action that moved to erpclaw-os-engine."""
    new_action = MOVED_ACTIONS_TO_ADDON[action]
    print(json.dumps({
        "status": "error",
        "error": f"action '{action}' moved to erpclaw-os-engine addon (renamed to '{new_action}')",
        "missing_addon": "erpclaw-os-engine",
        "old_action": action,
        "new_action": new_action,
        "install_command": (
            "python3 ~/.openclaw/workspace/skills/erpclaw/scripts/module_manager.py "
            "--action install-module --module-name erpclaw-os-engine"
        ),
        "github": "https://github.com/avansaber/erpclaw-addons (subdir erpclaw-os-engine)",
        "since_version": "4.0.0",
    }, indent=2))
    sys.exit(1)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

ACTIONS = {
    "validate-module": None,
    "list-articles": None,
    "build-table-registry": None,
    "schema-plan": None,
    "schema-apply": None,
    "schema-rollback": None,
    "schema-drift": None,
}


def main():
    parser = SafeArgumentParser(description="ERPClaw OS — runtime actions (foundation)")
    all_known = sorted(list(ACTIONS.keys()) + list(MOVED_ACTIONS_TO_ADDON.keys()))
    parser.add_argument("--action", required=True, choices=all_known)
    parser.add_argument("--module-path", help="Path to the module directory (validate-module)")
    parser.add_argument("--validation-type", default="full", choices=["static", "runtime", "full"])
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--article-type", default="all", choices=["all", "static", "runtime"])
    parser.add_argument("--src-root", help="Source root (build-table-registry, schema-*)")
    parser.add_argument("--module-name", help="Module name (schema-*)")
    parser.add_argument("--target", help="Target environment (schema-*)")
    parser.add_argument("--dry-run", action="store_true")

    args, unknown = parser.parse_known_args()
    check_unknown_args(parser, unknown)

    action = args.action

    if action in MOVED_ACTIONS_TO_ADDON:
        handle_moved_to_addon(action)
        return  # unreachable; handle_moved_to_addon calls sys.exit

    if action == "validate-module":
        if not args.module_path:
            err("--module-path is required for validate-module")
        handle_validate_module(args)
    elif action == "list-articles":
        handle_list_articles(args)
    elif action == "build-table-registry":
        if not args.src_root:
            err("--src-root is required for build-table-registry")
        handle_build_table_registry(args)
    elif action == "schema-plan":
        handle_schema_plan(args)
    elif action == "schema-apply":
        handle_schema_apply(args)
    elif action == "schema-rollback":
        handle_schema_rollback(args)
    elif action == "schema-drift":
        handle_schema_drift(args)
    else:
        err(f"Unknown action in router: {action}")


if __name__ == "__main__":
    main()
