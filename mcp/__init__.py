"""ERPClaw MCP server — runtime-agnostic transport over the db_query.py routers.

This package is a *thin transport* (ADR-0024): it exposes the existing,
already-validated ERPClaw action surface to any MCP-speaking runtime (Claude
Code, Cursor, Cline, ...) through three meta-tools over the foundation
``db_query.py`` router. It opens **no new write path** — Decimal-as-TEXT, UUID4
IDs, the 12-step GL validation, and immutable GL all stay enforced by the
routers the server shells out to, never by the transport.

The meta-tool surface (sub-decision 1 of ADR-0024) is module-agnostic by
construction. v1 scope is foundation-only (Nik D3); all-modules discovery is
later config, not a redesign.

Entry point: ``server.py`` (stdio, spawn-on-demand).

Read-only sessions: when the server process environment sets
``ERPCLAW_MCP_READONLY=1``, the session can discover, describe and run every
foundation read (any ``get-*``/``list-*`` action plus a fixed report set) and
nothing else — writes are refused before any router process starts,
``user_confirmed`` is refused outright, and each router child runs with
``ERPCLAW_DB_READONLY=1``. Unset or empty means today's behaviour exactly;
any other value refuses every tool call.
"""
