"""The service's backup: the engine's backup format, written from the books' own schema.

The `before-update` backup is taken by a new version's service on books its migrations have not touched yet, so this
exporter never uses the running code's table definitions. It reads the books' schema from the database's catalog
(`pg_catalog`, through SQLAlchemy's reflection: tables, columns in their ordinal order, types, primary and foreign
keys) and the books' revisions from their `alembic_version` rows, and dumps every table with the engine's canonical
encoding (`backup._encode`) by the reflected types, in the engine's restore order (`backup._restore_order`) by the
reflected keys.

Its manifest is `backup.backup`'s, with three additions: `schema_version` (the ERPClaw
release whose schema the books are at, when the caller knows it from `installed.json`, and the core and plugin
revisions found in the books), `format_version` (the backup format) and `written_by` (this program, for the record
only). On books at this program's revision it writes the same bytes as `backup.backup`, file by file (its tests).

Called by the service with the owner's database authority, never by the engine.
"""
from __future__ import annotations

import json
import os
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

from sqlalchemy import MetaData, Table, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SAWarning

from . import files as files_mod
from .backup import (FORMAT, NOTE, VERSION_TABLE, BackupRefused, _dump, _encode, _restore_order, _rows, _sha,
                     _snapshot)
from .migrate import CORE_TABLES, branch_scripts, core_revisions

# Core tables an older revision has and a later one drops: `operation`, made by 0004 and replaced in 0005 (migrate.py).
RETIRED_CORE_TABLES = ("operation",)

# Format 1 writes a row's columns in the order of the model of the version whose schema the books are at. The catalog
# gives the columns' ordinal order, which is that order except where a migration added a column somewhere else than
# the model has it. Those exceptions are pinned here, each for the exact column set it applies to, so the order is
# never read from the running code; a test asserts that ordinal order with these exceptions is the model order for
# every table at this revision, so a migration that adds a new exception fails that test first.
FORMAT_KEY_ORDER: dict[str, tuple[str, ...]] = {
    # 0011 appended hash_format and the functional amounts to the physical tables.
    "posting": ("id", "company_id", "seq", "posting_date", "voucher_type", "voucher_id", "memo",
                "reverses_posting_id", "operation_key", "created_at", "created_by", "prev_hash", "hash",
                "hash_format", "ledger"),
    "ledger_entry": ("id", "company_id", "posting_id", "line_no", "account_id", "party_id", "debit", "credit",
                     "currency", "rate", "functional_currency", "functional_debit", "functional_credit", "kind",
                     "dimension_set_id"),
    # 0009 added `tries` after `accepted_at`; the model has it after `next_try_at`
    "outbox": ("id", "company_id", "operation_key", "slot", "recipient", "subject", "body", "status", "created_at",
               "delivered_at", "detail", "message_id", "from_address", "from_name", "reply_to", "current_attempt_id",
               "next_try_at", "tries", "first_failed_at", "accepted_at"),
}


@dataclass(frozen=True)
class Revisions:
    """The books' revisions as their `alembic_version` rows record them, by branch."""
    core: str
    plugins: dict[str, str]


def chains() -> tuple[list[str], dict[str, list[str]]]:
    """This installation's chains, newest first: core's, and each loaded plugin's that has migrations."""
    from .plugins import REGISTRY, load_plugins

    load_plugins()
    return core_revisions(), {name: [sc.revision for sc in branch_scripts(p.migrations)]
                              for name, p in sorted(REGISTRY.plugins.items()) if p.migrations is not None}


def classify(rows: set[str]) -> Revisions:
    """Each version row to its branch: core's, or the plugin whose chain holds it. A row in no chain this program has
    is refused, naming it: the books are at a revision this program does not know (newer, or another program's)."""
    core_chain, plugin_chains = chains()
    core = [r for r in rows if r in core_chain]
    plugins: dict[str, str] = {}
    unknown = []
    for r in sorted(set(rows) - set(core)):
        owner = next((n for n, chain in plugin_chains.items() if r in chain), None)
        if owner is None:
            unknown.append(r)
        elif owner in plugins:
            raise BackupRefused(f"the books record two revisions of the {owner} plugin ({plugins[owner]}, {r})")
        else:
            plugins[owner] = r
    if unknown:
        raise BackupRefused(f"the books are at a revision this ERPClaw does not know: {', '.join(unknown)}")
    if len(core) != 1:
        raise BackupRefused(f"the books record {len(core)} core revisions, not one")
    return Revisions(core[0], plugins)


def owner_of(name: str, revisions: Revisions) -> str | None:
    """The table's owner: the plugin (applied in these books) whose manifest declares it, or core; None if nobody."""
    from .plugins import REGISTRY

    for plugin in sorted(revisions.plugins):
        if name in REGISTRY.plugins[plugin].manifest.owned_tables:
            return plugin
    if name == VERSION_TABLE or name in CORE_TABLES or name in RETIRED_CORE_TABLES:
        return "core"
    return None


@dataclass
class Reflected:
    """The books' own schema: every table as the catalog has it, the revisions, and each table's owner."""
    tables: dict[str, Table]
    revisions: Revisions
    owners: dict[str, str]


def reflect(conn: Connection) -> Reflected:
    """The schema of the books `conn` reads, from the catalog. A table no branch in these books owns is refused, as
    `backup.backup` refuses one (an unknown table means unknown books)."""
    present = MetaData()
    with warnings.catch_warnings():  # partial and expression indexes are not needed to dump rows
        warnings.simplefilter("ignore", SAWarning)
        present.reflect(bind=conn)
    if VERSION_TABLE not in present.tables:
        raise BackupRefused("the database has no migration history; these are not ERPClaw's books")
    revisions = classify(set(conn.execute(select(present.tables[VERSION_TABLE].c.version_num)).scalars()))
    owners = {name: owner_of(name, revisions) for name in present.tables}
    unknown = sorted(name for name, owner in owners.items() if owner is None)
    if unknown:
        raise BackupRefused(f"the database holds tables this engine does not know: {', '.join(unknown)}")
    return Reflected(dict(present.tables), revisions, owners)


def key_order(table: Table) -> list[str]:
    """A row's columns in format 1's order: the catalog's ordinal order, or the pinned order for this column set."""
    ordinal = [c.name for c in table.columns]
    pinned = FORMAT_KEY_ORDER.get(table.name)
    if pinned is not None and set(pinned) == set(ordinal) and len(pinned) == len(ordinal):
        return list(pinned)
    return ordinal


def restore_order(table: Table, rows: list[dict]) -> list[dict]:
    """Canonical dependency order, allowing a FIFO root layer to name itself as its group."""
    if table.name != "stock_layer":
        return _restore_order(table, rows)
    original = {row["id"]: row for row in rows}
    dependencies = [dict(row, group_id=None) if row["group_id"] == row["id"] else row for row in rows]
    return [original[row["id"]] for row in _restore_order(table, dependencies)]


def dump(conn: Connection, table: Table | str, order: list[str] | None = None) -> list[str]:
    """One table's canonical lines in restore order, each row's columns in `order` (by default
    `key_order`): the engine's `_dump`, by the reflected table rather than the code's."""
    if isinstance(table, str) or table.name == VERSION_TABLE:
        return _dump(conn, VERSION_TABLE)
    order = key_order(table) if order is None else order
    cols = {c.name: c for c in table.columns}
    rows = [{name: _encode(r[name], cols[name].type) for name in order} for r in conn.execute(select(table)).mappings()]
    return [json.dumps(r, separators=(",", ":"), ensure_ascii=False) for r in restore_order(table, rows)]


def _first(lines: list[str], key: str):
    return next((json.loads(line)[key] for line in lines), None)


def backup(engine: Engine, path: Path, *, schema_release: str | None = None) -> dict:
    """Write a backup directory at `path` (which must not exist), from the books' own schema; returns its manifest.
    `schema_release` is the ERPClaw release whose schema the books are at, when the caller knows it."""
    path = Path(path)
    if path.exists():
        raise BackupRefused("The backup destination already exists; a backup never overwrites.")
    conn = _snapshot(engine)
    try:
        books = reflect(conn)  # the catalog, read in the same snapshot as the rows
        dumps = {name: dump(conn, table) for name, table in books.tables.items()}
        source_root = files_mod.root_dir(conn)
    finally:
        conn.rollback()
        conn.close()
    (path / "tables").mkdir(parents=True)
    os.chmod(path, 0o700)
    os.chmod(path / "tables", 0o700)
    try:  # every file a row names, each copy checked against its row
        stored = files_mod.back_up(source_root, _rows(dumps.get("file_object", [])), path / "files")
    except files_mod.FileCheckFailed as e:
        raise BackupRefused(f"the backup failed: {e}") from None
    manifest = {
        "format": FORMAT, "engine_version": version("erpclaw-core"), "core_revision": books.revisions.core,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "encrypted": False, "note": NOTE,
        "plugins": _rows(dumps.get("plugin_install", [])),
        "ownership": [json.loads(line)["mark"] for line in dumps.get("erpclaw_books", [])],
        "books_id": _first(dumps.get("books_identity", []), "books_id"),
        "files": stored,
        "tables": {name: {"owner": books.owners[name], "rows": len(lines), "sha256": _sha(lines)}
                   for name, lines in dumps.items()},
        # acceptance of a backup reads these two, never `written_by`
        "schema_version": {"release": schema_release, "core_revision": books.revisions.core,
                           "plugins": dict(sorted(books.revisions.plugins.items()))},
        "format_version": FORMAT,
        "written_by": {"program": "erpclaw-core", "version": version("erpclaw-core")},
    }
    for name, lines in dumps.items():
        f = path / "tables" / f"{name}.jsonl"
        f.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        os.chmod(f, 0o600)
    m = path / "MANIFEST.json"
    m.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(m, 0o600)
    return manifest
