"""Backups in the engine's own format, restore, and the book diff.

A backup is a logical export: every table the engine knows, one JSON object
per row, values in one canonical text form, so two sets of books compare
byte for byte. It is taken in one read transaction (one snapshot).
Restore additionally invalidates live sign-in codes and sessions; comparisons
accept only those field changes.

Restore never writes into books that exist and never uses a
connection a running engine holds. It stages a new database privately under a
random name, checks its safety preconditions and refuses when they fail,
restores there in one transaction on its own connection, re-proves the
ledger's integrity (a backup is untrusted input), proves what was committed,
and only then publishes the database under the final name, in one step that
never replaces anything. Switching the engine to them is a separate step.
It cannot prove that a balanced, consistent history is the one the business
really recorded.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import subprocess
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, Inexact, Rounded, localcontext
from importlib.metadata import version
from pathlib import Path

from sqlalchemy import Boolean, Date, DateTime, Integer, create_engine, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool
from sqlalchemy.engine import Connection, Engine

from . import files as files_mod  # the module; `files` names a backup's table lines below
from .db import Money, MoneyText, metadata
from .migrate import core_head, core_revisions

FORMAT = 1
VERSION_TABLE = "alembic_version"
SEEDED = (VERSION_TABLE, "currency")  # rows the core migration writes itself
NOTE = "Not encrypted: this backup holds the whole business. Keep it where the books are kept."


class BackupRefused(RuntimeError):
    """A backup or restore that would not be exact, or not safe."""


# --- canonical values --------------------------------------------------------

def _encode(value, coltype):
    if value is None:
        return None
    if isinstance(coltype, (Money, MoneyText)):
        return format(Decimal(value), "f")
    if isinstance(coltype, DateTime):
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if isinstance(coltype, Date):
        return value.isoformat()
    if isinstance(coltype, Boolean):
        return bool(value)
    if isinstance(coltype, Integer):
        return int(value)
    if isinstance(value, str):
        return value
    raise BackupRefused(f"a value of type {type(value).__name__} has no canonical form")


def _decode(value, coltype):
    if value is None:
        return None
    if isinstance(coltype, (Money, MoneyText)):
        return Decimal(value)
    if isinstance(coltype, DateTime):
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    if isinstance(coltype, Date):
        return date.fromisoformat(value)
    return value


def engine_tables() -> list:
    """Every table the engine knows, core's and every loaded plugin's, in foreign-key order: for readers that involve
    no books. A plugin loaded here need not be installed in a given set of books; their readers use books_tables."""
    from . import models  # noqa: F401  (core tables)
    from .plugins import load_plugins

    load_plugins()  # plugin tables
    return list(metadata.sorted_tables)


def _tables(versions: set[str]) -> list:
    """The tables of a set of books, in foreign-key order: core's, and each plugin's whose branch is applied
    there (a version row of its branch in `versions`, the books' or the backup's). An uninstalled plugin keeps
    its tables and its version row, so it is included; one never installed, or loaded here and never installed
    in these books, has neither."""
    from .plugins import REGISTRY, _branch_ids

    tables = engine_tables()
    absent = {name for p in REGISTRY.plugins.values() if p.migrations is not None
              and not versions & _branch_ids(p.migrations) for name in p.manifest.owned_tables}
    return [t for t in tables if t.name not in absent]


def books_tables(conn: Connection) -> list:
    """The tables of the books `conn` reads, from their own version rows."""
    return _tables(set(conn.execute(text(f"SELECT version_num FROM {VERSION_TABLE}")).scalars()))


def _versions(lines: list[str]) -> set[str]:
    return {json.loads(line)["version_num"] for line in lines}


def _restore_order(table, rows: list[dict]) -> list[dict]:
    """Primary-key order, then parents before children within a
    self-referencing table (account parents; a reversal after its original)."""
    pk = [c.name for c in table.primary_key.columns]
    rows = sorted(rows, key=lambda r: tuple("" if r[c] is None else str(r[c]) for c in pk))
    if table.name == "posting":
        rows.sort(key=lambda r: (r["company_id"], int(r["seq"])))
    selfrefs = [fk for fk in table.foreign_keys if fk.column.table is table and fk.parent.name != "company_id"]
    if not selfrefs:
        return rows
    cols = [fk.parent.name for fk in selfrefs]
    by_id = {r["id"]: r for r in rows}
    done: set[str] = set()
    out: list[dict] = []

    def visit(r, path: tuple):
        if r["id"] in done:
            return
        if r["id"] in path:
            raise BackupRefused(f"{table.name} rows refer to each other in a cycle")
        for c in cols:
            parent = r.get(c)
            if table.name == "stock_layer" and c == "group_id" and parent == r["id"]:
                continue  # A FIFO root names itself as its group; it has no parent to visit.
            if parent is not None and parent in by_id:
                visit(by_id[parent], path + (r["id"],))
        done.add(r["id"])
        out.append(r)

    for r in rows:
        visit(r, ())
    return out


def _dump(conn: Connection, table) -> list[str]:
    """The canonical lines of one table, in restore order."""
    if table == VERSION_TABLE:
        return [json.dumps({"version_num": v}) for v in
                sorted(conn.execute(text(f"SELECT version_num FROM {VERSION_TABLE}")).scalars())]
    rows = [{c.name: _encode(r[c.name], c.type) for c in table.columns}
            for r in conn.execute(select(table)).mappings()]
    return [json.dumps(r, separators=(",", ":"), ensure_ascii=False) for r in _restore_order(table, rows)]


def _rows(lines: list[str]) -> list[dict]:
    return [json.loads(line) for line in lines]


def _matches_backup(name: str, lines: list[str], restored: list[str]) -> bool:
    """Exact rows, except restore must invalidate previously live authentication rows.

    Already-used codes and already-ended sessions stay byte-for-byte equivalent.
    A previously unused code must be used; an open session must end as restored.
    All keys, other values, and the number of rows must still match.
    """
    if name not in ("principal_sign_in", "principal_session"):
        return lines == restored
    if len(lines) != len(restored):
        return False
    for before, after in zip(_rows(lines), _rows(restored)):
        changed = None
        if name == "principal_sign_in" and before["used_at"] is None:
            changed = "used_at"
        elif name == "principal_session" and before["ended_at"] is None:
            if before["ended_why"] is not None or after.get("ended_why") != "restored":
                return False
            changed = "ended_at"
            after["ended_why"] = None
        if changed:
            value = after.get(changed)
            try:
                if value is None or _encode(_decode(value, DateTime(timezone=True)), DateTime(timezone=True)) != value:
                    return False
            except (TypeError, ValueError):
                return False
            after[changed] = None
        if before != after:
            return False
    return True


def _sha(lines: list[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def _known_names(versions: set[str] | None = None) -> set[str]:
    return {t.name for t in _tables(versions)} | {VERSION_TABLE}


# --- backup ------------------------------------------------------------------

def _snapshot(engine: Engine) -> Connection:
    """One REPEATABLE READ, READ ONLY transaction for every table (else a torn snapshot)."""
    conn = engine.connect().execution_options(isolation_level="REPEATABLE READ")
    conn.begin()
    conn.execute(text("SET TRANSACTION READ ONLY"))
    return conn


def backup(engine: Engine, path: Path) -> dict:
    """Write a backup directory at `path` (which must not exist); returns its manifest."""
    path = Path(path)
    if path.exists():
        raise BackupRefused(f"{path} already exists; a backup never overwrites")
    conn = _snapshot(engine)
    try:
        versions = _dump(conn, VERSION_TABLE)  # which plugin branches these books hold, in the same snapshot
        tables = _tables(_versions(versions))
        unknown = set(inspect(conn).get_table_names()) - _known_names(_versions(versions))
        if unknown:
            raise BackupRefused(f"the database holds tables this engine does not know: {', '.join(sorted(unknown))}")
        dumps = {VERSION_TABLE: versions} | {t.name: _dump(conn, t) for t in tables}
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
    by_name = {t.name: t for t in tables}
    core = set(core_revisions())  # one version row per head: core's and one per plugin branch
    revision = next((v for v in (json.loads(line)["version_num"] for line in dumps[VERSION_TABLE]) if v in core), None)
    manifest = {
        "format": FORMAT, "engine_version": version("erpclaw-core"), "core_revision": revision,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "encrypted": False, "note": NOTE,
        "plugins": [json.loads(line) for line in dumps["plugin_install"]],
        "ownership": [json.loads(line)["mark"] for line in dumps["erpclaw_books"]],
        "books_id": next((json.loads(line)["books_id"] for line in dumps["books_identity"]), None),
        "files": stored,
        "tables": {name: {"owner": by_name[name].info.get("owner") if name in by_name else "core",
                          "rows": len(lines), "sha256": _sha(lines)} for name, lines in dumps.items()},
    }
    for name, lines in dumps.items():
        f = path / "tables" / f"{name}.jsonl"
        f.write_text("\n".join(lines) + ("\n" if lines else ""))
        os.chmod(f, 0o600)
    m = path / "MANIFEST.json"
    m.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    os.chmod(m, 0o600)
    return manifest


# --- restore -----------------------------------------------------------------

def _read(path: Path) -> tuple[dict, dict[str, list[str]]]:
    try:
        manifest = json.loads((path / "MANIFEST.json").read_text())
    except (OSError, ValueError) as e:
        raise BackupRefused(f"the backup has no readable manifest ({e})") from None
    if manifest.get("format") != FORMAT:
        raise BackupRefused(f"backup format {manifest.get('format')} is not {FORMAT}")
    files: dict[str, list[str]] = {}
    for name, meta in manifest["tables"].items():
        f = path / "tables" / f"{name}.jsonl"
        lines = f.read_text().splitlines() if f.exists() else None
        if lines is None or len(lines) != meta["rows"] or _sha(lines) != meta["sha256"]:
            raise BackupRefused(f"the backup is damaged: tables/{name}.jsonl does not match its manifest")
        files[name] = lines
    if VERSION_TABLE not in files or set(files) != _known_names(_versions(files[VERSION_TABLE])):
        raise BackupRefused("the backup's tables are not this engine's tables")
    return manifest, files


def _check_engine(manifest: dict) -> None:
    from .plugins import REGISTRY

    head = core_head()
    if manifest["core_revision"] != head:
        raise BackupRefused(f"the backup is at core revision {manifest['core_revision']}, this engine at {head}; "
                            "restoring into another engine version is an upgrade, not available yet")
    for rec in manifest["plugins"]:
        p = REGISTRY.plugins.get(rec["name"])
        if p is None:
            raise BackupRefused(f"the backup uses plugin {rec['name']}, which this engine does not have")
        for what, mine in (("version", p.version), ("manifest_digest", p.manifest.digest()),
                           ("artifact_digest", p.artifact_digest)):
            if rec[what] != mine:
                raise BackupRefused(f"the backup's plugin {rec['name']} has a different {what.replace('_', ' ')}")


def _exact():
    """A decimal context in which any rounding is an error, never a silent result."""
    return localcontext(prec=200, traps=[Inexact, Rounded])


def _integrity(conn: Connection) -> None:
    """Re-prove every posting in the target (a backup is untrusted input)."""
    from . import ledger, models as m
    from .money import MoneyError, to_amount

    currency = dict(conn.execute(select(m.company.c.id, m.company.c.functional_currency)).all())
    accounts = {r["id"]: dict(r) for r in conn.execute(select(m.account)).mappings()}
    parties = {r["id"]: dict(r) for r in conn.execute(select(m.party)).mappings()}
    lines: dict[str, list[dict]] = {}
    for r in conn.execute(select(m.ledger_entry).order_by(m.ledger_entry.c.posting_id, m.ledger_entry.c.line_no)).mappings():
        lines.setdefault(r["posting_id"], []).append(dict(r))
    postings = {r["id"]: dict(r) for r in conn.execute(select(m.posting)).mappings()}
    for pid, p in postings.items():
        ls = lines.get(pid, [])
        where = f"posting {p['seq']} of company {p['company_id']}"
        if len(ls) < 2:
            raise BackupRefused(f"{where} has fewer than two lines")
        for ln in ls:  # every amount within the decimal precision bounds, so the sums below are exact
            for side in ("debit", "credit"):
                try:
                    to_amount(ln[side], currency[p["company_id"]])
                except MoneyError as e:
                    raise BackupRefused(f"{where} has an amount outside the money rules: {e}") from None
        with _exact():
            balanced = sum((ln["debit"] for ln in ls), Decimal(0)) == sum((ln["credit"] for ln in ls), Decimal(0))
        if not balanced:
            raise BackupRefused(f"{where} does not balance")
        for ln in ls:
            if ln["currency"] != currency[p["company_id"]]:
                raise BackupRefused(f"{where} has a line in {ln['currency']}, not the company's currency")
            if accounts[ln["account_id"]]["company_id"] != p["company_id"] or ln["company_id"] != p["company_id"] or \
                    (ln["party_id"] and parties[ln["party_id"]]["company_id"] != p["company_id"]):
                raise BackupRefused(f"{where} uses another company's account, party or line")
            need = {"receivable": "customer", "payable": "supplier"}.get(accounts[ln["account_id"]]["account_type"])
            party = parties.get(ln["party_id"]) if ln["party_id"] else None
            if need and (party is None or party["kind"] != need):
                raise BackupRefused(f"{where} has a {accounts[ln['account_id']]['account_type']} line without a {need}")
            if not need and party is not None:
                raise BackupRefused(f"{where} names a party on an account that takes none")
        if p["reverses_posting_id"]:
            orig = postings.get(p["reverses_posting_id"])
            mirror = [(o["account_id"], o["party_id"], o["credit"], o["debit"]) for o in lines.get(orig["id"], [])] \
                if orig else None
            if orig is None or p["posting_date"] < orig["posting_date"] or \
                    [(ln["account_id"], ln["party_id"], ln["debit"], ln["credit"]) for ln in ls] != mirror:
                raise BackupRefused(f"{where} is a reversal that does not mirror its original")
    for cid in currency:
        if ledger.verify_chain(conn, cid) is not None:
            raise BackupRefused(f"company {cid}'s chain does not verify")
        with _exact():
            diff = sum((ln["debit"] - ln["credit"] for ls in lines.values() for ln in ls
                        if postings[ls[0]["posting_id"]]["company_id"] == cid), Decimal(0))
        if diff != 0:
            raise BackupRefused(f"company {cid}'s trial balance does not balance")


# --- restore: the target -----------------------------------------------------

@dataclass(frozen=True)
class NewPostgresDatabase:
    """A new PostgreSQL database `name` on the server of `server_url` (the owner role)."""
    server_url: str = field(repr=False)
    name: str
    runtime_role: str | None = None

    def __repr__(self) -> str:  # the owner role's URL carries its password: shown as server, no login or query
        from .db import safe_url

        try:
            server = safe_url(self.server_url)
        except Exception:  # noqa: BLE001 - a repr never fails, and never shows what it could not parse
            server = "<unreadable URL>"
        return f"NewPostgresDatabase(server={server!r}, name={self.name!r}, runtime_role={self.runtime_role!r})"


@dataclass(frozen=True)
class RestoreResult:
    location: str  # the database name
    digest: str  # the backup's manifest digest


PG_STAGE_PREFIX = "erpclaw_restore_"
PG_MARKER = "erpclaw-restore"
_HOOK = None  # tests only: called with a step name, to swap, connect or crash between the steps


def _hook(step: str, **kw) -> None:
    if _HOOK is not None:
        _HOOK(step, **kw)


def _process_identity() -> dict:
    """This process as cleanup will recognise it: host, pid and the process's start."""
    return {"host": socket.gethostname(), "pid": os.getpid(), "started": _process_started(os.getpid())}


def _process_started(pid: int) -> str | None:
    """The process's start time as `ps` reports it; None when it cannot be told."""
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _alive(identity: dict) -> bool:
    """Whether the restore that wrote a marker may still be running. Dead only
    when that is certain: no such process, or a process with that id that
    started at another time (the id was reused). Anything uncertain (another
    host, a process that cannot be inspected) counts as alive, so cleanup
    never removes a live restore's staging."""
    if identity.get("host") != socket.gethostname():
        return True
    try:
        pid = int(identity.get("pid", 0))
    except (TypeError, ValueError):
        return True
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False  # certainly gone
    except OSError:
        return True  # exists, not ours to signal: alive
    recorded = identity.get("started") or None  # the PostgreSQL marker writes an unknown start as an empty field
    started = _process_started(pid)
    if started is None or recorded is None:
        return True  # cannot tell, now or when the marker was written: keep it
    return started == recorded


def _digest(manifest: dict) -> str:
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


# --- restore: PostgreSQL staging ---------------------------------------------------

SAFE_QUERY = {"sslmode", "sslrootcert", "sslcert", "sslkey", "connect_timeout", "application_name"}


SESSION_WAIT_S = 5.0  # as long as PostgreSQL itself waits for other sessions before a rename or a drop


def _client_sessions(c, oid) -> int:
    """Client sessions on the database with this oid, other than the caller's own. The server's own workers
    (autovacuum) visit any database and are part of the trusted server, so they are not counted. An operator
    without statistics access sees another role's session with its type hidden; one with a user id is counted as a
    client (autovacuum has none), so the count does not depend on that privilege."""
    return c.exec_driver_sql(
        "SELECT count(*) FROM pg_stat_activity WHERE datid = %(o)s "
        "AND (backend_type = 'client backend' OR (backend_type IS NULL AND usesysid IS NOT NULL)) "
        "AND pid <> pg_backend_pid()", {"o": oid}).scalar()


def _q(name: str) -> str:
    if not (name.isascii() and name.replace("_", "").isalnum() and name[0].isalpha() and name == name.lower()):
        raise BackupRefused("Invalid database name; restore accepts [a-z][a-z0-9_]*")
    return f'"{name}"'


class _PostgresStage:
    """A staging database with a random name, CONNECT taken from PUBLIC at
    creation, a rename barrier, and admission checked before it is used."""

    def __init__(self, target: NewPostgresDatabase, digest: str):
        if len(target.name) > 63:
            raise BackupRefused("a PostgreSQL database name has at most 63 characters")
        _q(target.name)
        self.target = target
        url = make_url(target.server_url)
        if not url.drivername.startswith("postgresql"):
            raise BackupRefused("the server URL is not a PostgreSQL URL")
        unsafe = set(url.query) - SAFE_QUERY
        if unsafe:  # a dbname, service or options parameter would choose the database itself
            raise BackupRefused(f"the server URL carries connection parameters restore does not accept: "
                                f"{', '.join(sorted(unsafe))}")
        self.url = url
        self.admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool)
        self.tx = create_engine(url.set(database="postgres"), poolclass=NullPool)  # publication's transaction
        self.owner = url.username
        with self.admin.connect() as c:
            if c.exec_driver_sql("SELECT 1 FROM pg_database WHERE datname = %(n)s", {"n": target.name}).first():
                raise BackupRefused(f"database {target.name} already exists; restore never writes over anything")
            if not c.exec_driver_sql("SELECT rolcreatedb OR rolsuper FROM pg_roles WHERE rolname = current_user").scalar():
                raise BackupRefused("the owner role needs CREATEDB to restore into a new database")
            self.name = PG_STAGE_PREFIX + secrets.token_hex(8)
            c.exec_driver_sql(f"CREATE DATABASE {_q(self.name)} TEMPLATE template0")
            c.exec_driver_sql(f"REVOKE CONNECT ON DATABASE {_q(self.name)} FROM PUBLIC")
            ident = _process_identity()
            mark = " ".join([PG_MARKER, digest, ident["host"], str(ident["pid"]), (ident["started"] or "").replace(" ", "_")])
            if "'" in mark:
                raise BackupRefused("the restore marker cannot be written safely")
            c.exec_driver_sql(f"COMMENT ON DATABASE {_q(self.name)} IS '{mark}'")
            self.oid = c.exec_driver_sql("SELECT oid FROM pg_database WHERE datname = %(n)s", {"n": self.name}).scalar()
        self.trusted = True
        self.closed_admission = False
        _hook("staged", stage=self)
        self._barrier()

    def _barrier(self) -> None:
        """Nothing else is, or is on its way to being, connected."""
        with self.admin.connect() as c:
            try:  # PostgreSQL refuses a rename while another session is in the database, and waits for a login
                c.exec_driver_sql(f"ALTER DATABASE {_q(self.name)} RENAME TO {_q(self.name + '_b')}")
            except Exception:
                self.trusted = False
                raise BackupRefused(f"another session is connected to the staging database {self.name}; "
                                    "nothing was restored and nothing was removed") from None
            self.name += "_b"
            self._admitted(c, before_connect=True)
        self.closed_admission = True

    def _admitted(self, c, before_connect: bool) -> None:
        # A session that has just closed (restore's own, a moment ago) is still listed until its backend exits, so
        # the count waits a bounded time for sessions to leave; it decides on the last count, never admits early.
        # `c` is in autocommit, so each poll reads a fresh activity snapshot.
        deadline = time.monotonic() + SESSION_WAIT_S
        while (others := _client_sessions(c, self.oid)) and time.monotonic() < deadline:
            time.sleep(0.1)
        oid = c.exec_driver_sql("SELECT oid FROM pg_database WHERE datname = %(n)s", {"n": self.name}).scalar()
        roles = [r for (r,) in c.exec_driver_sql(
            "SELECT DISTINCT r.rolname FROM pg_shdepend d JOIN pg_roles r ON r.oid = d.refobjid "
            "WHERE d.dbid = %(o)s AND d.refclassid = 'pg_authid'::regclass", {"o": self.oid})]
        allowed = set() if before_connect else {self.owner, self.target.runtime_role}
        if oid != self.oid or others or set(roles) - allowed:
            self.trusted = False
            raise BackupRefused(f"the staging database {self.name} is not restore's alone (another session, or "
                                "objects made by another role); nothing was published and nothing was removed")

    def engine(self) -> Engine:
        return create_engine(self.url.set(database=self.name), poolclass=NullPool)

    def _on_staging(self, conn: Connection) -> None:
        """The connection really is on the staging database (its oid), not wherever a URL led."""
        oid = conn.exec_driver_sql("SELECT oid FROM pg_database WHERE datname = current_database()").scalar()
        if oid != self.oid:
            self.trusted = False
            raise BackupRefused("restore's connection is not on its staging database; nothing was published")

    def begin(self, conn: Connection) -> None:
        conn.begin()
        self._on_staging(conn)
        found = conn.exec_driver_sql(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%%'").scalar()
        if found:
            self.trusted = False
            raise BackupRefused(f"the staging database {self.name} holds relations nobody but restore should have made")

    def check(self, conn: Connection | None = None) -> None:
        if conn is not None:
            self._on_staging(conn)

    def publish(self) -> str:
        with self.admin.connect() as c:
            self._admitted(c, before_connect=False)
        _hook("before_publish", stage=self)
        final = _q(self.target.name)
        with self.tx.begin() as c:  # one transaction: the publication, the single commit point
            try:
                c.exec_driver_sql(f"ALTER DATABASE {_q(self.name)} RENAME TO {final}")
            except DBAPIError as e:
                if getattr(e.orig, "sqlstate", None) == "55006":  # object in use: a session arrived after the count
                    raise BackupRefused(f"the staging database {self.name} is not restore's alone (another session, "
                                        "or objects made by another role); nothing was published") from None
                raise
            if self.target.runtime_role:
                c.exec_driver_sql(f"GRANT CONNECT ON DATABASE {final} TO {_q(self.target.runtime_role)}")
            c.exec_driver_sql(f"COMMENT ON DATABASE {final} IS NULL")
            _hook("publishing", stage=self)
        _hook("published", stage=self)
        with self.admin.connect() as c:
            if c.exec_driver_sql("SELECT oid FROM pg_database WHERE datname = %(n)s",
                                 {"n": self.target.name}).scalar() != self.oid:
                raise BackupRefused(f"database {self.target.name} is not the restored one; check it with verify")
        return self.target.name

    def cleanup(self) -> str | None:
        if not (self.trusted and self.closed_admission):
            return self.name
        try:
            with self.admin.connect() as c:
                self._admitted(c, before_connect=False)
                c.exec_driver_sql(f"DROP DATABASE {_q(self.name)}")
        except Exception:
            return self.name
        return None

    def close(self) -> None:
        self.admin.dispose()
        self.tx.dispose()


# --- restore -----------------------------------------------------------------

def _plugin_triggers(conn: Connection) -> list[tuple]:
    """The row triggers on plugin-owned tables: name, table, definition and whether enabled."""
    names = sorted(t.name for t in books_tables(conn) if t.info.get("owner") not in (None, "core"))
    if not names:
        return []
    rows = conn.execute(text(
        "SELECT t.tgname, c.relname, pg_get_triggerdef(t.oid), t.tgenabled <> 'D' FROM pg_trigger t "
        "JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE NOT t.tgisinternal AND n.nspname = current_schema() ORDER BY t.tgname")).all()
    return [tuple(r) for r in rows if r[1] in names]


def _suspend(conn: Connection, triggers: list[tuple]) -> None:
    """In restore's own transaction, on a staging database no other session can reach: the plugin tables'
    transition rules (lines only into a draft, status only forward) do not apply to a copy of a state.
    Keys, CHECKs and core's triggers stay on."""
    for table in sorted({t for _, t, _, _ in triggers}):
        conn.exec_driver_sql(f'ALTER TABLE "{table}" DISABLE TRIGGER USER')


def _resume(conn: Connection, triggers: list[tuple]) -> None:
    for table in sorted({t for _, t, _, _ in triggers}):
        conn.exec_driver_sql(f'ALTER TABLE "{table}" ENABLE TRIGGER USER')


def _require_mark(files: dict[str, list[str]]) -> None:
    """The engine's own ownership mark, not merely some row."""
    from .db import OWNER_MARK

    if OWNER_MARK not in {json.loads(line).get("mark") for line in files["erpclaw_books"]}:
        raise BackupRefused("the backup does not carry this engine's ownership mark")


def _require_identity(manifest: dict, files: dict[str, list[str]]) -> str:
    """The books' identity: exactly one row, the one the manifest names. Restore copies it as it is, so restored
    books keep the identity of the books they came from."""
    rows = [json.loads(line).get("books_id") for line in files["books_identity"]]
    if len(rows) != 1 or rows[0] != manifest.get("books_id"):
        raise BackupRefused("the backup does not carry exactly one books identity, the one its manifest names")
    return rows[0]


def restore(path: Path, target: NewPostgresDatabase) -> RestoreResult:
    """Restore the backup at `path` into new books, then publish them.
    Takes no engine and reads no books URL: it never opens books in use."""
    from . import models as m
    from .grants import apply_grants
    from .migrate import upgrade
    from .plugins import BooksRefused, check_books, load_plugins

    load_plugins()
    manifest, files = _read(Path(path))
    _check_engine(manifest)
    if [json.loads(line) for line in files["plugin_install"]] != manifest["plugins"]:
        raise BackupRefused("the backup's plugin install records differ from its manifest")
    _require_mark(files)
    _require_identity(manifest, files)
    tables = _tables(_versions(files[VERSION_TABLE]))
    digest = _digest(manifest)
    stored = files_mod.RestoreFiles(Path(path) / "files", _rows(files.get("file_object", [])), target.name,
                                    _require_identity(manifest, files))
    try:
        stored.check_backup()  # before anything is staged
    except files_mod.FileCheckFailed as e:
        raise BackupRefused(f"the backup's files do not match its rows: {e}") from None
    stage = _PostgresStage(target, digest)
    engine = None
    publishing = False
    try:
        _hook("staged_ready", stage=stage)
        engine = stage.engine()
        with engine.connect() as conn:
            stage.begin(conn)
            upgrade(conn, manifest["core_revision"])
            for rev in sorted(json.loads(line)["version_num"] for line in files[VERSION_TABLE]):
                if rev != manifest["core_revision"]:
                    upgrade(conn, rev)  # each plugin's branch to the revision the backup records
            triggers = _plugin_triggers(conn)
            _suspend(conn, triggers)  # a copy of a state, not business steps
            for name in SEEDED:
                if not _matches_backup(name, files[name], _dump(
                        conn, name if name == VERSION_TABLE else metadata.tables[name])):
                    raise BackupRefused(f"the backup's {name} rows differ from this engine's")
            for t in tables:
                if t.name in SEEDED:
                    continue
                for r in _restore_order(t, [json.loads(line) for line in files[t.name]]):
                    try:
                        conn.execute(t.insert().values({c.name: _decode(r[c.name], c.type) for c in t.columns}))
                    except DBAPIError as e:  # a key, CHECK or trigger refused the row: the backup breaks a rule
                        raise BackupRefused(f"the backup's {t.name} rows break the database's rules "
                                            f"({type(e.orig).__name__}: {str(e.orig).splitlines()[0]})") from None
            _resume(conn, triggers)
            if _plugin_triggers(conn) != triggers:
                raise BackupRefused("restore could not put back the plugin tables' triggers exactly")
            # Invalidate the copied credentials in this staging transaction, before any commit or publication.
            conn.execute(m.principal_session.update().where(m.principal_session.c.ended_at.is_(None)).values(
                ended_at=func.now(), ended_why="restored"))
            conn.execute(m.principal_sign_in.update().where(m.principal_sign_in.c.used_at.is_(None)).values(
                used_at=func.now()))
            _hook("inserted", stage=stage)
            _integrity(conn)
            try:
                check_books(conn)  # the restored install records open with this engine
            except BooksRefused as e:
                raise BackupRefused(f"the restored plugin install records do not open with this engine: {e}") from None
            if target.runtime_role:
                apply_grants(conn, target.runtime_role)
            stage.check(conn)
            conn.commit()
        _hook("committed", stage=stage)
        with engine.connect() as conn:  # prove what was committed, on a new connection
            stage.check(conn)
            for t in [VERSION_TABLE, *tables]:
                name = t if isinstance(t, str) else t.name
                if not _matches_backup(name, files[name], _dump(conn, t)):
                    raise BackupRefused(f"the committed {name} differs from the backup")
        _hook("proved", stage=stage)
        engine.dispose()
        engine = None
        if stored.refs:  # the files first, under the new name; then the database
            with stage.admin.connect() as c:
                server = files_mod.server_identity(c)
            try:
                stored.stage(server, digest)
                _hook("files_staged", stage=stage)
                stored.publish()
            except files_mod.FileCheckFailed as e:
                raise BackupRefused(f"the files could not be restored: {e}") from None
            _hook("files_published", stage=stage)
        publishing = True  # from here the database may be published, even when publish raises
        location = stage.publish()
        stored.finish()
    except BaseException as e:
        if engine is not None:
            engine.dispose()
        if publishing:  # the books may exist: their files stay; the cleanup clears the marker, or the root if not
            stored.release()
            stage.cleanup()  # drops the staging database only if it was never renamed into the books
            raise
        if not isinstance(e, BackupRefused) or "check it with verify" not in str(e):
            stored.abandon()  # the database was not published: the files it staged or published go too
            left = stage.cleanup()
            if left and isinstance(e, BackupRefused):
                raise BackupRefused(f"{e} (staging left for an administrator: {left})") from None
        raise
    finally:
        stage.close()
    return RestoreResult(location, digest)


def verify(path: Path, target: NewPostgresDatabase) -> list[str]:
    """Whether the books at `target` equal the backup and pass the integrity
    checks, read-only; empty when they do (for an uncertain outcome, section 4a)."""
    from .plugins import load_plugins

    load_plugins()
    manifest, files = _read(Path(path))
    out: list[str] = []
    for check in (lambda: _require_mark(files), lambda: _require_identity(manifest, files)):
        try:
            check()
        except BackupRefused as e:
            out.append(str(e))
    url = make_url(target.server_url)
    if set(url.query) - SAFE_QUERY:
        return out + ["the server URL carries connection parameters verify does not accept"]
    engine = create_engine(url.set(database=target.name), poolclass=NullPool)
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            if conn.exec_driver_sql("SELECT current_database()").scalar() != target.name:
                return out + ["the connection is not on the named database"]
            marks = set(conn.execute(text("SELECT mark FROM erpclaw_books")).scalars()) \
                if "erpclaw_books" in inspect(conn).get_table_names() else set()
            from .db import OWNER_MARK
            if OWNER_MARK not in marks:
                out.append("the books do not carry this engine's ownership mark")
            for t in [VERSION_TABLE, *_tables(_versions(files[VERSION_TABLE]))]:
                name = t if isinstance(t, str) else t.name
                if not _matches_backup(name, files[name], _dump(conn, t)):
                    out.append(f"{name} differs from the backup")
            if not out:
                try:
                    _integrity(conn)
                except BackupRefused as e:
                    out.append(str(e))
            conn.rollback()
    finally:
        engine.dispose()
    out += files_mod.check_restored(files_mod.files_dir() / files_mod.root_name(target.name),
                                    _rows(files.get("file_object", [])))
    return out


def clean_restored_files(server_url: str) -> dict:
    """Remove what this server's own crashed restores marked in the files folder, never a root they did not mark."""
    admin = create_engine(make_url(server_url).set(database="postgres"), poolclass=NullPool,
                          isolation_level="AUTOCOMMIT")  # each question sees the server as it is at that moment
    try:
        with admin.connect() as c:
            server = files_mod.server_identity(c)
            return files_mod.clean_restore_marks(
                server, lambda name: name is not None and c.exec_driver_sql(
                    "SELECT 1 FROM pg_database WHERE datname = %(n)s", {"n": name}).first() is not None)
    finally:
        admin.dispose()


def stale_stagings(server_url: str) -> list[str]:
    """Marked staging databases whose restore is no longer running (section 4a):
    only these may be cleaned. Unmarked databases are never listed."""
    out: list[str] = []
    admin = create_engine(make_url(server_url).set(database="postgres"), poolclass=NullPool)
    try:
        with admin.connect() as c:
            for name, comment in c.exec_driver_sql(
                    "SELECT d.datname, shobj_description(d.oid, 'pg_database') FROM pg_database d "
                    "WHERE d.datname LIKE 'erpclaw\\_restore\\_%%'"):
                parts = (comment or "").split(" ")
                if len(parts) == 5 and parts[0] == PG_MARKER and not _alive(
                        {"host": parts[2], "pid": parts[3], "started": parts[4].replace("_", " ")}):
                    out.append(name)
    finally:
        admin.dispose()
    return out


def clean_staging(item: str, server_url: str) -> None:
    """Remove one stale staging database listed by `stale_stagings`, and only it, while no client is connected to it.
    The server's own workers (autovacuum) are not counted (`_client_sessions`): they visit any database, and the
    drop makes them leave, as at publication. A client that connects before the drop makes the drop fail, and that
    is the same refusal. No wait here: an operator's command on a stale database, where no session of restore's is
    closing."""
    if not item.startswith(PG_STAGE_PREFIX):
        raise BackupRefused(f"{item} is not a restore staging database")
    in_use = f"{item} has a session connected; it is not removed"
    admin = create_engine(make_url(server_url).set(database="postgres"), isolation_level="AUTOCOMMIT",
                          poolclass=NullPool)
    try:
        with admin.connect() as c:
            oid = c.exec_driver_sql("SELECT oid FROM pg_database WHERE datname = %(n)s", {"n": item}).scalar()
            if oid is not None and _client_sessions(c, oid):
                raise BackupRefused(in_use)
            try:
                c.exec_driver_sql(f"DROP DATABASE {_q(item)}")
            except DBAPIError as e:
                if getattr(e.orig, "sqlstate", None) == "55006":  # object in use: a client arrived after the count
                    raise BackupRefused(in_use) from None
                raise
    finally:
        admin.dispose()


# --- the book diff -----------------------------------------------------------

def book_diff(a: Engine, b: Engine) -> list[str]:
    """Differences between books, allowing restore's authentication invalidation on either side."""
    out: list[str] = []
    with a.connect() as ca, b.connect() as cb:
        tables = books_tables(ca)  # each side's own tables; one that only one side has is reported just below
        tables += [t for t in books_tables(cb) if t.name not in {u.name for u in tables}]
        names_a, names_b = set(inspect(ca).get_table_names()), set(inspect(cb).get_table_names())
        if names_a != names_b:
            out.append(f"tables differ: {sorted(names_a ^ names_b)}")
        for t in [VERSION_TABLE, *tables]:
            name = t if isinstance(t, str) else t.name
            if name not in names_a or name not in names_b:
                continue
            la, lb = _dump(ca, t), _dump(cb, t)
            if la != lb and not (_matches_backup(name, la, lb) or _matches_backup(name, lb, la)):
                first = next((i for i, (x, y) in enumerate(zip(la, lb)) if x != y), min(len(la), len(lb)))
                out.append(f"{name}: {len(la)} and {len(lb)} rows; first difference at row {first + 1}")
    return out
