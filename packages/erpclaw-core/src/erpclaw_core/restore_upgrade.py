"""Restore at the backup's recorded revision, prove, then migrate.

The engine's restore (`backup.restore`) takes only a backup of this program's own core revision and plugin
artifacts. This route takes a backup of an older schema as well, without changing that path:

1. read the backup and check its files against the manifest; accept it only when its format is one this program
   reads, its core revision and every plugin revision it records are in this installation's chains, and every plugin
   it records is installed here (a newer schema, a plugin this installation lacks, or a revision outside the chain is
   refused with a plain sentence, before anything is created). Populated books from before ledger format 2 are
   refused as well; only an uninitialised, empty schema can cross that boundary;
2. stage a new database privately (`backup._PostgresStage`: a random name, admission closed and checked),
   create its schema at the backup's recorded core and plugin revisions (Alembic upgrade to those revisions),
   load the rows by the backup's own column lists, against the schema the catalog then shows (never the running
   code's tables), and commit;
3. prove, in a new transaction, that every table dumps exactly as the backup holds it at that revision (the book
   diff, each row's columns in the backup's own order);
4. in one transaction, run the migrations to this installation's head (core, then each plugin branch the books hold),
   initialise the ownership mark and identity for an empty pre-format-2 schema, end every open session and void
   every unused sign-in code, refresh each plugin's install record to the installed
   version and digests (as an update's first start does), run
   the integrity checks (`backup._integrity`, the install check), apply the runtime role's grants, and commit;
5. only then publish the database under its final name, without replacing anything, and return it. Switching to it
   (the books pointer) is the service's step, not this module's.

A failure at any step before publication removes what this call staged and publishes nothing. Called by the service
with the owner's database authority, never by the engine; it takes no engine and reads no books URL.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from packaging.version import InvalidVersion, Version
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from . import files as files_mod
from .backup import (FORMAT, VERSION_TABLE, BackupRefused, NewPostgresDatabase, _decode, _digest, _integrity,
                     _PostgresStage, _require_identity, _require_mark, _rows, _sha, _versions)
from .backup_reflect import Reflected, Revisions, chains, classify, dump, key_order, reflect, restore_order
from .migrate import applied_revisions, upgrade

# _PostgresStage requires CREATEDB and opens the new database with target.server_url's owner credential.
# Its schema creation, proof and migrations all use that connection; the runtime role only receives grants.
OWNER_ONLY = True  # existing runtime-policy scanner convention for owner/maintenance commands

NEWER = "This backup was made by a newer ERPClaw; update ERPClaw first."
BEFORE_FORMAT_2 = ("This backup uses an older bookkeeping format and cannot be upgraded by this ERPClaw. "
                   "Your current books have not been changed.")
_HOOK = None  # tests only: called with a step name, to fail between the steps


def _hook(step: str, **kw) -> None:
    if _HOOK is not None:
        _HOOK(step, **kw)


@dataclass(frozen=True)
class UpgradedRestore:
    location: str  # the new database's name, published; the service switches to it
    digest: str  # the backup's manifest digest
    recorded: Revisions  # the revisions the backup recorded, at which it was restored and proven
    migrated: Revisions  # the revisions the published books are at


# --- reading and accepting the backup ------------------------------------------------------------------------------

def _format(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def read(path: Path) -> tuple[dict, dict[str, list[str]]]:
    """The manifest and every table file, each checked against the manifest's rows and sha256."""
    try:
        manifest = json.loads((path / "MANIFEST.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise BackupRefused("The backup has no readable manifest.") from None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("tables"), dict):
        raise BackupRefused("the backup's manifest is not one ERPClaw writes")
    for key in ("format", "format_version"):
        if key == "format_version" and key not in manifest:
            continue  # the engine's own export writes `format` alone
        found = _format(manifest.get(key))
        if found is not None and found > FORMAT:
            raise BackupRefused(NEWER)
        if found != FORMAT:
            raise BackupRefused(f"the backup's format {manifest.get(key)!r} is not one this ERPClaw reads")
    files: dict[str, list[str]] = {}
    for name, meta in manifest["tables"].items():
        if not (isinstance(name, str) and name.replace("_", "").isalnum() and name.isascii()):
            raise BackupRefused("the backup's manifest names a table ERPClaw would not make")
        if (not isinstance(meta, dict) or type(meta.get("rows")) is not int or meta["rows"] < 0
                or not isinstance(meta.get("sha256"), str)):
            raise BackupRefused(f"the backup's {name} table metadata is not one ERPClaw writes")
        f = path / "tables" / f"{name}.jsonl"
        try:
            lines = f.read_text(encoding="utf-8").split("\n") if f.is_file() else None
            if lines and lines[-1] == "":
                lines.pop()  # exactly one final LF; Unicode separators within a JSON string are data
        except (OSError, UnicodeError):
            raise BackupRefused(f"the backup's {name} table could not be read") from None
        if lines is None or len(lines) != meta.get("rows") or _sha(lines) != meta.get("sha256"):
            raise BackupRefused(f"the backup is damaged: tables/{name}.jsonl does not match its manifest")
        try:
            rows = _rows(lines)
        except ValueError:
            raise BackupRefused(f"the backup's {name} table does not hold readable rows") from None
        if any(not isinstance(row, dict) for row in rows):
            raise BackupRefused(f"the backup's {name} table does not hold object rows")
        files[name] = lines
    if VERSION_TABLE not in files:
        raise BackupRefused("the backup has no migration history; it is not a backup of ERPClaw's books")
    if any(set(row) != {"version_num"} or not isinstance(row["version_num"], str)
           for row in _rows(files[VERSION_TABLE])):
        raise BackupRefused("the backup's migration history is not one ERPClaw writes")
    return manifest, files


def accept(manifest: dict, files: dict[str, list[str]]) -> Revisions:
    """The revisions to restore at, when this installation can take them; else a plain refusal."""
    from .plugins import REGISTRY

    core_chain, plugin_chains = chains()
    rows = _versions(files[VERSION_TABLE])
    if manifest.get("core_revision") not in core_chain:
        raise BackupRefused(NEWER)
    sv = manifest.get("schema_version")
    if sv is not None:  # acceptance reads the books' schema version, never the program that wrote the backup
        if not isinstance(sv, dict) or not isinstance(sv.get("plugins"), dict):
            raise BackupRefused("the backup's schema version is not one ERPClaw writes")
        if sv.get("core_revision") not in core_chain:
            raise BackupRefused(NEWER)
        for name, rev in sorted(sv["plugins"].items()):
            if name not in plugin_chains:
                raise BackupRefused(f"The backup uses the {name} plugin, which this ERPClaw does not have.")
            if rev not in plugin_chains[name]:
                raise BackupRefused(f"The backup's {name} plugin is at a revision this ERPClaw's {name} plugin does "
                                    f"not have ({rev}); it may be from a newer ERPClaw.")
    try:
        revisions = classify(rows)
    except BackupRefused as e:
        raise BackupRefused(f"The backup's books are at a revision this ERPClaw does not have: {e}") from None
    if revisions.core != manifest["core_revision"] or (
            sv is not None and (sv.get("core_revision"), sv["plugins"]) != (revisions.core, revisions.plugins)):
        raise BackupRefused("the backup's manifest does not match its own migration history")
    records = _rows(files.get("plugin_install", []))
    if records != manifest.get("plugins"):
        raise BackupRefused("the backup's plugin install records differ from its manifest")
    for rec in records:
        if not isinstance(rec.get("name"), str) or not isinstance(rec.get("version"), str):
            raise BackupRefused("the backup's plugin install record is not one ERPClaw writes")
        p = REGISTRY.plugins.get(rec.get("name"))
        if p is None:
            raise BackupRefused(f"The backup uses the {rec.get('name')} plugin, which this ERPClaw does not have.")
        try:
            newer = Version(str(rec.get("version"))) > Version(p.version)
        except InvalidVersion:
            raise BackupRefused(f"the backup's {p.name} plugin record has no version ERPClaw writes") from None
        if newer:
            raise BackupRefused(NEWER)
    if "erpclaw_books" not in files:
        raise BackupRefused("the backup does not carry this engine's ownership mark")
    if core_chain.index(revisions.core) > core_chain.index("0011"):
        # 0011 deliberately cannot convert existing books to ledger format 2. Even an ownership mark or an
        # identity without a company makes them existing books. Only migration history and seeded currencies
        # may be present in an empty schema; _load still checks its exact tables, columns and seeded rows.
        if any(lines for name, lines in files.items() if name not in (VERSION_TABLE, "currency")):
            raise BackupRefused(BEFORE_FORMAT_2)
        if manifest.get("ownership") != [] or manifest.get("books_id") is not None:
            raise BackupRefused("the empty backup's ownership or identity differs from its manifest")
    else:
        _require_mark(files)
        if "books_identity" in files:
            _require_identity(manifest, files)
    return revisions


# --- loading and proving at the recorded revision ------------------------------------------------------------------

def _columns(lines: list[str]) -> list[str] | None:
    """A table's columns in the backup's own order, from its first row; None when it has none."""
    return list(json.loads(lines[0])) if lines else None


def _plugin_triggers(conn: Connection, books: Reflected) -> list[tuple]:
    """The row triggers on plugin-owned tables: name, table, definition and whether enabled (as `backup.restore`
    reads them)."""
    names = {name for name, owner in books.owners.items() if owner != "core"}
    if not names:
        return []
    rows = conn.execute(text(
        "SELECT t.tgname, c.relname, pg_get_triggerdef(t.oid), t.tgenabled <> 'D' FROM pg_trigger t "
        "JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE NOT t.tgisinternal AND n.nspname = current_schema() ORDER BY t.tgname")).all()
    return [tuple(r) for r in rows if r[1] in names]


def _set_triggers(conn: Connection, triggers: list[tuple], state: str) -> None:
    """In the restore's own transaction on a staging database no other session can reach: the plugin tables'
    transition rules do not apply to a copy of a state. Keys, CHECKs and core's triggers stay on."""
    for table in sorted({t for _, t, _, _ in triggers}):
        conn.exec_driver_sql(f'ALTER TABLE "{table}" {state} TRIGGER USER')


def _load(conn: Connection, books: Reflected, files: dict[str, list[str]]) -> None:
    """Every row of the backup into the schema at its recorded revision, by the backup's own column lists. Rows the
    migrations wrote themselves (the seeded currencies, the version rows) are compared, never inserted."""
    if set(books.tables) != set(files):
        raise BackupRefused("the backup's tables are not the tables of the revision it records "
                            f"(only in the backup: {sorted(set(files) - set(books.tables))}; "
                            f"only at the revision: {sorted(set(books.tables) - set(files))})")
    # Check seeded tables as well, before using the backup's column order to select any values.
    for name, table in books.tables.items():
        if any(set(row) != set(table.c.keys()) for row in _rows(files[name])):
            raise BackupRefused(f"the backup's {name} rows do not have the columns of the revision it records")
    seeded = {name for name, t in books.tables.items()
              if conn.execute(select(func.count()).select_from(t)).scalar()}
    for name in sorted(seeded):
        if dump(conn, books.tables[name], _columns(files[name]) or key_order(books.tables[name])) != files[name]:
            raise BackupRefused(f"the backup's {name} rows differ from the ones its recorded revision makes")
    triggers = _plugin_triggers(conn, books)
    _set_triggers(conn, triggers, "DISABLE")
    order = next(iter(books.tables.values())).metadata.sorted_tables  # foreign-key order, from the catalog
    for table in order:
        if table.name in seeded:
            continue
        columns = {c.name: c for c in table.columns}
        rows = _rows(files[table.name])
        try:
            values = [{name: _decode(r[name], columns[name].type) for name in columns}
                      for r in restore_order(table, rows)]
        except (TypeError, ValueError):
            raise BackupRefused(f"the backup's {table.name} rows have invalid values") from None
        if not values:
            continue
        try:
            conn.execute(table.insert(), values)
        except DBAPIError as e:  # a key, CHECK or trigger refused a row: the backup breaks a rule
            raise BackupRefused(f"the backup's {table.name} rows break the database's rules "
                                f"({type(e.orig).__name__}: {str(e.orig).splitlines()[0]})") from None
    _set_triggers(conn, triggers, "ENABLE")
    if _plugin_triggers(conn, books) != triggers:
        raise BackupRefused("restore could not put back the plugin tables' triggers exactly")


def _prove(conn: Connection, files: dict[str, list[str]]) -> None:
    """The book diff at the recorded revision: every table, read back from the catalog's schema, dumps exactly as the
    backup holds it, byte for byte, each row's columns in the backup's own order."""
    books = reflect(conn)
    if set(books.tables) != set(files):
        raise BackupRefused("the restored books do not have the backup's tables")
    for name, table in sorted(books.tables.items()):
        if dump(conn, table, _columns(files[name]) or key_order(table)) != files[name]:
            raise BackupRefused(f"the restored {name} differs from the backup at its revision")


# --- migrating to this installation's head --------------------------------------------------------------------------

def _refresh_install_records(conn: Connection) -> None:
    """Each plugin's install record to the installed version and digests, its lifecycle status kept, as an
    update's first start refreshes them: the engine's opening check compares them."""
    from . import models as m
    from .plugins import REGISTRY

    for name, ver, mdig, adig in conn.execute(select(
            m.plugin_install.c.name, m.plugin_install.c.version, m.plugin_install.c.manifest_digest,
            m.plugin_install.c.artifact_digest)).all():
        p = REGISTRY.plugins[name]  # accept() refused a plugin this installation lacks
        now = (p.version, p.manifest.digest(), p.artifact_digest)
        if (ver, mdig, adig) != now:
            conn.execute(update(m.plugin_install).where(m.plugin_install.c.name == name).values(
                version=now[0], manifest_digest=now[1], artifact_digest=now[2]))


def _migrate(conn: Connection, recorded: Revisions) -> Revisions:
    try:
        upgrade(conn)  # core to this installation's head
        for name in sorted(recorded.plugins):
            upgrade(conn, f"{name}@head")  # each plugin branch the books hold
    except DBAPIError:
        # In particular, core's ledger-format boundary deliberately refuses populated pre-format-2 books.
        # Never bypass a migration's preconditions or publish a partially migrated copy.
        raise BackupRefused("This backup's books cannot be upgraded by this ERPClaw.") from None
    migrated = classify(applied_revisions(conn))
    core_chain, plugin_chains = chains()
    if migrated.core != core_chain[0] or any(rev != plugin_chains[n][0] for n, rev in migrated.plugins.items()) \
            or set(migrated.plugins) != set(recorded.plugins):
        raise BackupRefused("the migrations did not bring the restored books to this ERPClaw's revisions")
    return migrated


def _void_authentication(conn: Connection) -> None:
    """Only after the exact proof: copied credentials must be dead before the new books can serve."""
    from .restore_auth import invalidate_authentication

    invalidate_authentication(conn)


# --- the route -----------------------------------------------------------------------------------------------------

def restore(path: Path, target: NewPostgresDatabase) -> UpgradedRestore:
    """Restore the backup at `path` into new books at its recorded revisions, prove them, migrate them to this
    installation's head, check them, and publish them under `target`'s name. Takes no engine and reads no books URL:
    it never opens the books in use, and it never switches to the new ones."""
    from .grants import apply_grants
    from .plugins import BooksRefused, check_books, load_plugins

    load_plugins()
    path = Path(path)
    manifest, files = read(path)
    recorded = accept(manifest, files)
    digest = _digest(manifest)
    stored = None
    if files.get("file_object"):  # from 0010; the books' identity (0006) is then in the backup too
        stored = files_mod.RestoreFiles(path / "files", _rows(files["file_object"]), target.name,
                                        _require_identity(manifest, files))
        try:
            stored.check_backup()  # before anything is staged
        except files_mod.FileCheckFailed as e:
            raise BackupRefused(f"the backup's files do not match its rows: {e}") from None
    stage = _PostgresStage(target, digest)
    engine = None
    publishing = False
    try:
        engine = stage.engine()
        with engine.connect() as conn:
            stage.begin(conn)  # 1. the schema at the recorded revisions and the rows, in one transaction
            upgrade(conn, recorded.core)
            for rev in sorted(recorded.plugins.values()):
                upgrade(conn, rev)
            books = reflect(conn)
            if books.revisions != recorded:
                raise BackupRefused("the new database is not at the backup's recorded revisions")
            _load(conn, books, files)
            stage.check(conn)
            conn.commit()
            _hook("restored", stage=stage)

            conn.begin()  # 2. the proof, at the recorded revision, of what was committed
            stage.check(conn)
            _prove(conn, files)
            _hook("proved", conn=conn, stage=stage)
            conn.rollback()

            conn.begin()  # 3. the migrations to head and the checks, in one transaction
            stage.check(conn)
            migrated = _migrate(conn, recorded)
            if not files["erpclaw_books"]:  # accept permits this only for an empty pre-format-2 schema
                from .db import OWNER_MARK, books_identity, owner

                # As at first initialisation, but only after the empty schema crossed the format boundary.
                conn.execute(owner.insert().values(mark=OWNER_MARK))
                conn.execute(books_identity.insert().values(id=1, books_id=str(uuid.uuid4())))
            _void_authentication(conn)
            _hook("migrating", conn=conn, stage=stage)
            _refresh_install_records(conn)
            _integrity(conn)
            try:
                check_books(conn)  # the install records open with this engine
            except BooksRefused as e:
                raise BackupRefused(f"the restored plugin install records do not open with this engine: {e}") from None
            if target.runtime_role:
                apply_grants(conn, target.runtime_role)
            stage.check(conn)
            conn.commit()
        _hook("migrated", stage=stage)
        engine.dispose()
        engine = None
        if stored is not None and stored.refs:  # the files first, under the new name; then the database
            with stage.admin.connect() as c:
                server = files_mod.server_identity(c)
            try:
                stored.stage(server, digest)
                stored.publish()
            except files_mod.FileCheckFailed as e:
                raise BackupRefused(f"the files could not be restored: {e}") from None
        publishing = True  # from here the database may be published, even when publish raises
        location = stage.publish()
        if stored is not None:
            stored.finish()
    except BaseException as e:
        if engine is not None:
            engine.dispose()
        if publishing:  # the books may exist: their files stay; the cleanup clears the marker, or the root if not
            if stored is not None:
                stored.release()
            stage.cleanup()  # drops the staging database only if it was never renamed into the books
            raise
        if not isinstance(e, BackupRefused) or "check it with verify" not in str(e):
            if stored is not None:
                stored.abandon()  # the database was not published: the files it staged or published go too
            left = stage.cleanup()
            if left and isinstance(e, BackupRefused):
                raise BackupRefused(f"{e} (staging left for an administrator: {left})") from None
        raise
    finally:
        stage.close()
    return UpgradedRestore(location, digest, recorded, migrated)
