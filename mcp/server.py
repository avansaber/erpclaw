#!/usr/bin/env python3
"""ERPClaw MCP server — stdio, spawn-on-demand (ADR-0024, Nik D1).

A thin transport that exposes the foundation action surface to any MCP-speaking
runtime through three meta-tools over the ``db_query.py`` router:

  - ``erpclaw_list_actions(module?)``      — discovery: the foundation catalog.
  - ``erpclaw_describe_action(action_name)`` — grounding: one action's metadata.
  - ``erpclaw_action(action_name, args?, user_confirmed?)`` — the single
    execution tool; shells the router and returns its JSON verbatim.

The required-tool-use contract (ADR-0024): a result the client sees exists only
because the MCP framework dispatched one of these tools — so the FINDING-002
zero-tool-call confabulation is structurally impossible here.

No new write path. The server resolves lib + DB under ``ERPCLAW_HOME`` exactly
like the routers (paths.py, byte-identical when unset) and shells the unchanged
router; Decimal/UUID/12-step-GL/immutable-GL stay enforced by the router.

``erpclaw_read`` is deferred to v2 (Nik O3). The credential carve-out (ADR-0017
S0c) keeps backup/restore/credential/master-key actions off this surface.
"""
import json

# This package directory is named ``mcp`` (ADR-0024 / the plan's file layout),
# which collides with the official ``mcp`` SDK. We therefore NEVER put
# ``source/erpclaw`` on sys.path. Sibling modules are loaded by absolute path
# under a unique name when this file is run as a script; the relative import is
# used when the package is imported under a synthetic non-colliding package name
# (the test harness does this). Either way the SDK's top-level ``mcp`` stays
# resolvable for build_server().
if __package__:
    from . import confirm, skill_reader, tool_router
else:  # pragma: no cover - direct-script (.mcp.json spawn) path
    # Run as ``python3 source/erpclaw/mcp/server.py``: load this directory as a
    # package under a synthetic, collision-free name (``erpclaw_mcp``) so the
    # siblings' relative imports resolve AND the official top-level ``mcp`` SDK
    # stays reachable for build_server().
    import importlib
    import importlib.util
    import os
    import sys

    _HERE = os.path.dirname(os.path.abspath(__file__))
    _PKG = "erpclaw_mcp"
    if _PKG not in sys.modules:
        _spec = importlib.util.spec_from_file_location(
            _PKG, os.path.join(_HERE, "__init__.py"),
            submodule_search_locations=[_HERE])
        _pkg = importlib.util.module_from_spec(_spec)
        sys.modules[_PKG] = _pkg
        _spec.loader.exec_module(_pkg)
    confirm = importlib.import_module(f"{_PKG}.confirm")
    skill_reader = importlib.import_module(f"{_PKG}.skill_reader")
    tool_router = importlib.import_module(f"{_PKG}.tool_router")


# ── Tool definitions (names + schemas + annotations) ────────────────────────

LIST_ACTIONS = "erpclaw_list_actions"
DESCRIBE_ACTION = "erpclaw_describe_action"
ACTION = "erpclaw_action"


def _read_only_specs() -> bool:
    """True when the execution tool must advertise (and enforce) read-only.

    Fail closed: an invalid ``ERPCLAW_MCP_READONLY`` value advertises
    read-only too; never raises here.
    """
    try:
        return bool(confirm.session_readonly())
    except confirm.ReadonlyModeInvalid:
        return True


def _tool_specs():
    """Return the static tool spec list (name, description, schema, annotations).

    Kept as plain dicts so the test harness can introspect them without the SDK.
    """
    specs = [
        {
            "name": LIST_ACTIONS,
            "description": (
                "List the ERPClaw foundation actions available through this MCP "
                "server. Returns each action's name, whether it is destructive "
                "(requires confirmation), and a short description. Call this "
                "first to ground any task in real, dispatchable actions."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "module": {
                        "type": "string",
                        "description": "Module scope (v1: foundation only).",
                        "default": "foundation",
                    },
                },
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        },
        {
            "name": DESCRIBE_ACTION,
            "description": (
                "Describe one ERPClaw action: its description, whether it is "
                "destructive, how to pass arguments, and whether a genuine "
                "user confirmation is required before erpclaw_action will run it."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action_name": {
                        "type": "string",
                        "description": "The action to describe (e.g. add-customer).",
                    },
                },
                "required": ["action_name"],
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        },
        {
            "name": ACTION,
            "description": (
                "Execute an ERPClaw foundation action. Dispatches the action "
                "through the validated router and returns its JSON result "
                "verbatim. Destructive actions are refused unless called with "
                "user_confirmed=true reflecting a genuine user confirmation. "
                "Pass action arguments as a JSON object whose keys map to the "
                "router flags (e.g. {\"name\": \"Acme\", \"company_id\": \"...\"})."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action_name": {
                        "type": "string",
                        "description": "The action to execute (e.g. add-customer).",
                    },
                    "args": {
                        "type": "object",
                        "description": "Action arguments as a JSON object.",
                        "default": {},
                    },
                    "user_confirmed": {
                        "type": "boolean",
                        "description": (
                            "Set true ONLY to reflect a genuine user "
                            "confirmation of a destructive action."
                        ),
                        "default": False,
                    },
                    "authorization_id": {
                        "type": "string",
                        "description": (
                            "Id of a single-use authorization an operator "
                            "issued for this exact call. A model cannot "
                            "create one."
                        ),
                    },
                },
                "required": ["action_name"],
                "additionalProperties": False,
            },
            # destructiveHint:true — the server holds gated actions; a confirmed
            # erpclaw_action can mutate immutable-grade records (GL, fiscal year).
            "annotations": {"readOnlyHint": False, "destructiveHint": True},
        },
    ]
    if _read_only_specs():
        for spec in specs:
            if spec.get("name") == ACTION:
                spec["annotations"] = {
                    "readOnlyHint": True, "destructiveHint": False}
                spec["description"] = (
                    "This session is read-only; only read actions can run. "
                    + spec["description"]
                )
    return specs


# ── Tool implementations (SDK-independent; the harness calls these directly) ──

def _outer_container_error() -> dict:
    return {"status": "error", "error": "invalid_arguments",
            "detail": "tool arguments must be a JSON object."}


def _read_only_catalog_error() -> dict:
    """Fail-closed list error when the read gate cannot be computed."""
    return {
        "status": "error",
        "error": "read_only_session",
        "detail": "this session is read-only; only read actions can run.",
    }


def _read_gate_sets():
    """Live (dangerous, module_actions, onboarding_actions) sets at call time.

    The dangerous set is read through the ``skill_reader`` module object and
    the install-state sets through ``tool_router._router_maps()``. Raises on
    any failure; callers refuse fail-closed and never pass.
    """
    dangerous = skill_reader.dangerous_actions()
    module_actions, onboarding_actions, _aliases, _action_map = (
        tool_router._router_maps())
    return dangerous, module_actions, onboarding_actions


def call_list_actions(arguments: dict) -> dict:
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return _outer_container_error()
    try:
        readonly = confirm.session_readonly()
    except confirm.ReadonlyModeInvalid:
        return confirm.invalid_mode()
    module = arguments.get("module", "foundation")
    result = skill_reader.list_actions(module)
    if isinstance(result, dict) and result.get("status") == "error":
        return result
    if readonly:
        try:
            dangerous, module_actions, onboarding_actions = _read_gate_sets()
        except Exception:
            return _read_only_catalog_error()
        result = [
            entry for entry in result
            if confirm.is_session_read(
                entry.get("name"), dangerous=dangerous,
                module_actions=module_actions,
                onboarding_actions=onboarding_actions)
        ]
    envelope = {"status": "ok", "module": "foundation", "actions": result}
    problems = skill_reader.discovery_problems()
    if problems:
        warnings = []
        for item in problems:
            if isinstance(item, (tuple, list)) and len(item) == 2:
                item_path, item_reason = item
                warnings.append({"path": item_path, "detail": item_reason})
            elif isinstance(item, dict):
                warnings.append(item)
            else:
                warnings.append({"detail": str(item)})
        envelope["warnings"] = warnings
    return envelope


def call_describe_action(arguments: dict) -> dict:
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return _outer_container_error()
    action_name = arguments.get("action_name")
    if action_name is None or (isinstance(action_name, str) and action_name == ""):
        return {"status": "error", "error": "action_name is required."}
    if not isinstance(action_name, str):
        return {"status": "error", "error": "invalid_action",
                "detail": "action_name must be a string."}
    if not action_name:
        return {"status": "error", "error": "action_name is required."}
    try:
        readonly = confirm.session_readonly()
    except confirm.ReadonlyModeInvalid:
        return confirm.invalid_mode()
    if readonly:
        try:
            dangerous, module_actions, onboarding_actions = _read_gate_sets()
        except Exception:
            return confirm.refusal(action_name)
        if not confirm.is_session_read(
                action_name, dangerous=dangerous,
                module_actions=module_actions,
                onboarding_actions=onboarding_actions):
            return confirm.refusal(action_name)
    return skill_reader.describe_action(action_name)


def call_action(arguments: dict) -> dict:
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return _outer_container_error()
    action_name = arguments.get("action_name")
    if not action_name:
        return {"status": "error", "error": "action_name is required."}
    if "args" in arguments and arguments.get("args") is None:
        raw_args = {}
    elif "args" in arguments:
        raw_args = arguments.get("args")
    else:
        raw_args = {}
    if "user_confirmed" in arguments:
        raw_confirmed = arguments.get("user_confirmed")
    else:
        raw_confirmed = False
    return tool_router.dispatch(
        action_name, raw_args, raw_confirmed,
        authorization_id=arguments.get("authorization_id"))


_DISPATCH = {
    LIST_ACTIONS: call_list_actions,
    DESCRIBE_ACTION: call_describe_action,
    ACTION: call_action,
}


def handle_tool_call(name: str, arguments: dict) -> dict:
    """Route an MCP tool call to its implementation. Shared by the SDK wiring
    and the in-process test harness (MCPDriver)."""
    if not isinstance(name, str) or name not in _DISPATCH:
        return {"status": "error", "error": "unknown_tool",
                "detail": "unknown tool."}
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return _outer_container_error()
    try:
        confirm.session_readonly()
    except confirm.ReadonlyModeInvalid:
        return confirm.invalid_mode()
    impl = _DISPATCH[name]
    return impl(arguments)


# ── stdio MCP server wiring (the spawn-on-demand entry point) ────────────────

def build_server():
    """Build the low-level MCP ``Server`` with the three meta-tools registered.

    Imported lazily so the tool implementations + the test harness do not
    require the ``mcp`` SDK to be installed.
    """
    from mcp.server import Server
    import mcp.types as types

    server = Server("erpclaw")

    @server.list_tools()
    async def list_tools():
        tools = []
        for spec in _tool_specs():
            ann = spec.get("annotations") or {}
            tools.append(types.Tool(
                name=spec["name"],
                description=spec["description"],
                inputSchema=spec["inputSchema"],
                annotations=types.ToolAnnotations(
                    readOnlyHint=ann.get("readOnlyHint"),
                    destructiveHint=ann.get("destructiveHint"),
                ),
            ))
        return tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        result = handle_tool_call(name, arguments or {})
        return [types.TextContent(type="text", text=json.dumps(result, default=str))]

    return server


async def _run_stdio():  # pragma: no cover - exercised by the live stdio path
    from mcp.server.stdio import stdio_server
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream,
            server.create_initialization_options(),
        )


def main():  # pragma: no cover - process entry point
    import asyncio
    asyncio.run(_run_stdio())


if __name__ == "__main__":  # pragma: no cover
    main()
