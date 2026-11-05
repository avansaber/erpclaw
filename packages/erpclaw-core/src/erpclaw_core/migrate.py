"""Run the migrations (Alembic, plan section 10) on a connection.

Core's revisions carry the branch label `core`. Each plugin with tables ships
its own branch in its package (`migrations/versions/`), labelled with its
name, with its own base and no `depends_on`:
`alembic_version` holds one row per head, core's and one per installed
plugin. The required order (a plugin after its core revision) is the
engine's check against the books, not Alembic's.
"""
from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

CORE_VERSIONS = Path(__file__).with_name("migrations") / "versions"

# Tables the core revisions own. Every revision declares its data class
# (schema, backfill or destructive); revisions that hold books never downgrade.
CORE_TABLES = ("currency", "company", "fiscal_year", "fiscal_period", "account", "party",
               "posting_chain_head", "posting", "ledger_entry", "dimension_set", "fiscal_year_close",
               "system_of_record", "external_ref", "account_map", "sync_payload", "sync_journal",
               # revision 0004: the tables create_all still made until then
               "erpclaw_books", "plugin_install", "number_series", "audit_log", "approval",
               "outbox", "item",  # (0004 also made operation; 0005 replaces it with operation_outcome)
               # revision 0005: principals, the rebuilt card, outcomes (operation is dropped there)
               "principal", "principal_grant", "operation_outcome",
               # revision 0006: the books' identity
               "books_identity",
               # revision 0007: the conversation fence
               "conversation_fence",
               # revision 0009: a message's attempts and attachments
               "outbox_attempt", "outbox_attachment",
               # revision 0010: the file store
               "file_object",
               # revision 0015: supplier payables
               "supplier_payable", "supplier_payment", "supplier_payment_allocation")


def config(connection: Connection | None = None, *, plugins: bool = True,
           locations: list[Path] | None = None) -> Config:
    """Core's revisions, plus every loaded plugin's branch unless `plugins` is false
    (or exactly `locations`)."""
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).with_name("migrations")))
    if locations is None:
        locations = [CORE_VERSIONS]
        if plugins:
            from .plugins import REGISTRY

            locations += [p.migrations for p in REGISTRY.plugins.values() if p.migrations]
    cfg.set_main_option("path_separator", "os")
    cfg.set_main_option("version_locations", os.pathsep.join(str(x) for x in locations))
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def upgrade(connection: Connection, revision: str = "core@head") -> None:
    command.upgrade(config(connection), revision)


def upgrade_plugin(connection: Connection, name: str) -> None:
    """The plugin's branch to its head, on the caller's transaction (install)."""
    command.upgrade(config(connection), f"{name}@head")


def core_revisions() -> list[str]:
    """Core's revisions, newest first."""
    return [r.revision for r in ScriptDirectory.from_config(config(plugins=False)).walk_revisions()]


def core_head() -> str:
    return core_revisions()[0]


def branch_scripts(versions: Path) -> list:
    """The revisions in one plugin's versions folder, as Alembic reads them."""
    return list(ScriptDirectory.from_config(config(locations=[versions])).walk_revisions())


def applied_revisions(conn: Connection) -> set[str]:
    """The rows of these books' alembic_version: one per head."""
    if not inspect(conn).has_table("alembic_version"):
        return set()
    return set(conn.execute(text("SELECT version_num FROM alembic_version")).scalars())


def applied_core_revision(conn: Connection) -> str | None:
    core = set(core_revisions())
    rows = applied_revisions(conn) & core
    return next(iter(rows)) if len(rows) == 1 else None


def core_at_or_after(applied: str, wanted: str) -> bool:
    """True when `wanted` is `applied` or one of its ancestors in core's graph."""
    order = core_revisions()  # newest first; core's graph is linear
    return wanted in order and applied in order and order.index(applied) <= order.index(wanted)
